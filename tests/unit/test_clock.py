"""One calendar, not two.

Every timestamp the store writes is UTC. Lifecycle decisions compare a "today"
against those timestamps, so taking today from the *local* clock does arithmetic
across two calendars — and gets a different answer for as many hours a day as the
machine is offset from UTC.

This was a live bug, and it presented as an intermittently failing test rather than
as anything resembling a clock problem: west of UTC, `brain sync` in the evening
would decline to tombstone memories that were past their grace period, then work
again in the morning. Nothing errored. The suite went red for four hours a night.

The tests below run the real scenario under two deliberately extreme zones. UTC-12
and UTC+14 are 26 hours apart, so **at any instant at least one of them disagrees
with the UTC date** — which is what makes this deterministic rather than a test that
only catches the bug during the window that caused it.
"""

from __future__ import annotations

import os
import re
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from brain import model
from brain.config import Paths
from brain.frontmatter import serialize
from brain.model import Evidence, Memory, MemoryType, ProvenanceClass, Status, Volatility
from brain.store import deletion, lifecycle
from brain.store import memory as mem

#: UTC-12 and UTC+14. Note the sign inversion: POSIX `Etc/GMT+12` is *west* of
#: Greenwich, the opposite of the ISO-8601 convention. Getting this backwards would
#: leave the test passing while exercising nothing.
WEST_OF_UTC = "Etc/GMT+12"
EAST_OF_UTC = "Etc/GMT-14"


@contextmanager
def local_zone(zone: str) -> Iterator[None]:
    """Run in ``zone``, then put the process back exactly as it was.

    Deliberately not built on ``monkeypatch``. ``tzset()`` caches the zone inside
    libc, so restoring ``TZ`` is only half the job — and a fixture that depends on
    ``monkeypatch`` is torn down *before* it, so the second ``tzset()`` would run
    while the fake zone was still set and restore nothing. Process-global state has
    to be unwound in one place, in the right order.
    """
    previous = os.environ.get("TZ")
    os.environ["TZ"] = zone
    time.tzset()
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = previous
        time.tzset()


@pytest.fixture
def tz(request: pytest.FixtureRequest) -> Iterator[str]:
    zone: str = request.param
    with local_zone(zone):
        yield zone


def seed(paths: Paths, body: str, **kw: object) -> Memory:
    m = Memory(
        id="01K1Z8V4Q00000000000000000",
        type=MemoryType.SEMANTIC,
        provenance_class=ProvenanceClass.DIRECT_USER_STATEMENT,
        volatility=Volatility.EPHEMERAL,
        valid_from=kw.get("valid_from", date(2026, 1, 1)),  # type: ignore[arg-type]
        evidence=[Evidence("event:x")],
        body=body,
    )
    mem.write(paths, m.id, serialize(m).encode(), None)
    return m


def test_the_two_zones_actually_straddle_the_utc_date_boundary() -> None:
    """Guard the guard.

    If both zones happened to agree with UTC, every test below would pass while
    testing nothing. 12 + 14 > 24 makes that impossible, and this asserts it rather
    than trusting the arithmetic.
    """
    utc_date = datetime.now(UTC).date()
    local_dates = []
    for zone in (WEST_OF_UTC, EAST_OF_UTC):
        with local_zone(zone):
            local_dates.append(date.today())

    assert any(d != utc_date for d in local_dates), (
        "neither test zone disagreed with the UTC date, so these tests would be "
        "vacuous — check the Etc/GMT sign convention"
    )


@pytest.mark.parametrize("tz", [WEST_OF_UTC, EAST_OF_UTC], indirect=True)
def test_today_is_utc_regardless_of_the_local_zone(tz: str) -> None:
    assert model.today() == datetime.now(UTC).date()


def test_no_source_file_reads_the_local_clock() -> None:
    """Enforced by test, not convention.

    The behavioural tests above cover the lifecycle sweep, which is where this bug
    actually bit. They cannot cover a `date.today()` someone adds tomorrow in a new
    comparison — and the failure mode is a four-hour nightly window, which is exactly
    the kind of thing that survives review. So the rule is checked structurally.

    `model.today()` and `model.utcnow()` are the only sanctioned clocks.
    """
    src = Path(__file__).resolve().parents[2] / "src" / "brain"
    banned = re.compile(r"\bdate\.today\(\)|\bdatetime\.now\(\s*\)")
    offenders = []
    for path in sorted(src.rglob("*.py")):
        for lineno, line in enumerate(path.read_text().splitlines(), 1):
            if line.lstrip().startswith("#") or '"""' in line:
                continue  # prose about the rule is not a violation of it
            if banned.search(line):
                offenders.append(f"{path.relative_to(src)}:{lineno}: {line.strip()}")

    assert not offenders, (
        "local-clock call in library code — use model.today() / model.utcnow():\n"
        + "\n".join(offenders)
    )


@pytest.mark.parametrize("tz", [WEST_OF_UTC, EAST_OF_UTC], indirect=True)
def test_expired_past_grace_is_tombstoned_in_any_timezone(paths: Paths, tz: str) -> None:
    """The bug as it actually presented: a sweep that quietly did nothing.

    `recorded_at` is UTC. Comparing it against a local `date.today()` west of UTC
    makes the memory look like it was recorded *tomorrow*, so the grace period never
    elapses and the tombstone never happens — with no error anywhere.
    """
    m = seed(paths, "salamander stale", valid_from=date.today() - timedelta(days=400))
    lifecycle.sweep(paths)
    lifecycle.sweep(paths, grace_days=0)

    assert deletion.is_tombstoned(paths, m.id), f"not tombstoned under TZ={tz}"
    assert not mem.present_path(paths, "default", "semantic", m.id).exists()


@pytest.mark.parametrize("tz", [WEST_OF_UTC, EAST_OF_UTC], indirect=True)
def test_a_fresh_memory_does_not_lapse_early_in_any_timezone(paths: Paths, tz: str) -> None:
    """The mirror-image failure. East of UTC the local date runs *ahead*, so a
    memory written moments ago can look a day old and lapse before its time."""
    m = seed(paths, "brand new", valid_from=model.today())
    lifecycle.sweep(paths)

    assert not deletion.is_tombstoned(paths, m.id)
    from brain.frontmatter import parse

    path = mem.present_path(paths, "default", "semantic", m.id)
    assert parse(path.read_text()).status is not Status.EXPIRED, f"lapsed early under TZ={tz}"

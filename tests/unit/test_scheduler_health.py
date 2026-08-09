"""The sweeps are correct and useless unscheduled, so `doctor` has to say which.

Every check here exists because of a failure that actually happened on the operator's
own machine: `brain install-timers` had never been run, so `sync`, `expire`, and
`backup` had never fired; the only backup was five days old and a quarter the size of
the live store; and `doctor` reported `status: ok` through all of it, because
`_backup_health` checked whether a backup *existed* and never how old it was.

The units also pointed at a development virtualenv, which is the failure that comes
next: a unit that ran yesterday and silently stops when the checkout moves.

The first attempt at this check was subprocess-free, on the reasoning that
`systemctl` is missing in containers and CI. Review killed it: unit files,
wants-links and a live interpreter can all be correct while nothing runs — a masked
timer, or a user manager never started because the account has no lingering. Calling
that "runnable" was the same overclaim the check exists to catch. So the runtime is
asked, the query boundary is mocked here, and "cannot be asked" is reported as
unverifiable rather than healthy.
"""

from __future__ import annotations

import os
import time
from datetime import timedelta
from pathlib import Path

import pytest

from brain import doctor, ops
from brain.model import iso, utcnow

# -- age arithmetic, which is where the timezone bugs live ---------------------------


def test_age_is_measured_against_utc_not_the_local_clock() -> None:
    """Pinned under two zones 26 hours apart, so one always disagrees with UTC.

    A local-clock comparison passes in one of these and fails in the other. This is
    the cheap deterministic shape for any UTC/local bug in this codebase — the
    lifecycle sweep already paid for the lesson once.
    """
    stamp = iso(utcnow() - timedelta(days=5))
    ages = []
    original = os.environ.get("TZ")
    try:
        for zone in ("Etc/GMT+12", "Etc/GMT-14"):
            os.environ["TZ"] = zone
            time.tzset()
            ages.append(doctor._age_in_days(stamp))
    finally:
        if original is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = original
        time.tzset()
    assert ages == [5, 5]


def test_unparseable_timestamp_reports_unknown_rather_than_zero() -> None:
    assert doctor._age_in_days("not a timestamp") is None


def test_naive_timestamp_is_unknown_rather_than_silently_offset() -> None:
    """Subtracting naive from aware raises; guessing a zone would be worse."""
    assert doctor._age_in_days("2026-08-01T00:00:00") is None


# -- backup recency -----------------------------------------------------------------


def _backup(monkeypatch: pytest.MonkeyPatch, *, days_old: int) -> list[doctor.Check]:
    monkeypatch.setattr(
        doctor.backup_mod,
        "list_backups",
        lambda _p: [
            {
                "generation": "20260101T000000.000Z",
                "created_at": iso(utcnow() - timedelta(days=days_old)),
                "mechanism": "validated-double-collection",
                "files": 170,
                "tombstone_seq": 2,
            }
        ],
    )
    return doctor._backup_health(None)  # type: ignore[arg-type]


def test_a_fresh_backup_is_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    assert _backup(monkeypatch, days_old=0)[0].level is doctor.Level.OK


def test_a_stale_backup_warns_rather_than_reporting_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    """The regression under test: existence is not health."""
    check = _backup(monkeypatch, days_old=doctor.BACKUP_STALE_DAYS + 2)[0]
    assert check.level is doctor.Level.WARN
    assert "days old" in check.detail
    assert check.fix


# -- scheduler state ----------------------------------------------------------------


def _write_units(
    unit_dir: Path,
    *,
    interpreter: str,
    enabled: bool,
    runtime_enabled: bool = False,
    exec_start: bool = True,
) -> None:
    unit_dir.mkdir(parents=True, exist_ok=True)
    for name, *_ in ops.UNITS:
        body = "[Service]\n"
        if exec_start:
            body += f"ExecStart={interpreter} -m brain.cli sync\n"
        (unit_dir / f"{name}.service").write_text(body)
        (unit_dir / f"{name}.timer").write_text("[Timer]\nOnCalendar=hourly\n")
    if enabled or runtime_enabled:
        wants = (unit_dir / ".runtime" if runtime_enabled else unit_dir) / "timers.target.wants"
        wants.mkdir(parents=True, exist_ok=True)
        for name, *_ in ops.UNITS:
            # A real symlink, because `systemctl enable` makes symlinks and the
            # implementation claims the link is ground truth. Writing plain files
            # here would have tested a weaker thing than the docstring promises.
            (wants / f"{name}.timer").symlink_to(unit_dir / f"{name}.timer")


@pytest.fixture
def unit_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    d = tmp_path / "systemd"
    monkeypatch.setattr(ops, "UNIT_DIR", d)
    # `ops` imports the predicate by name, so patch it there — patching
    # `config.running_on_darwin` would leave this module's binding untouched.
    monkeypatch.setattr(ops, "running_on_darwin", lambda: False)
    monkeypatch.setattr(ops, "_runtime_wants_dir", lambda: d / ".runtime")
    # Default: the runtime says "live". Individual tests override.
    monkeypatch.setattr(ops, "_systemd_active", lambda _timers: True)
    return d


def test_missing_units_warn_and_name_the_fix(unit_dir: Path) -> None:
    check = doctor._scheduler_health(None)[0]  # type: ignore[arg-type]
    assert check.level is doctor.Level.WARN
    assert check.fix == "brain install-timers"


def test_units_written_but_not_enabled_warn(unit_dir: Path) -> None:
    """Writing a unit file schedules nothing — the installer says so, so must doctor."""
    _write_units(unit_dir, interpreter="/usr/bin/python3", enabled=False)
    check = doctor._scheduler_health(None)[0]  # type: ignore[arg-type]
    assert check.level is doctor.Level.WARN
    assert "not enabled" in check.detail


def test_runtime_enablement_counts_as_enabled(unit_dir: Path) -> None:
    """`systemctl --user enable --runtime` links under /run, not ~/.config."""
    _write_units(unit_dir, interpreter="/usr/bin/python3", enabled=False, runtime_enabled=True)
    assert doctor._scheduler_health(None)[0].level is doctor.Level.OK  # type: ignore[arg-type]


def test_a_vanished_interpreter_warns_and_names_it(unit_dir: Path) -> None:
    """The failure that bit: units bound to a checkout venv that can be deleted.

    WARN rather than FAIL — protection is absent, but nothing is corrupt right now,
    which is the line the module's rubric draws.
    """
    _write_units(unit_dir, interpreter="/nonexistent/venv/bin/python", enabled=True)
    check = doctor._scheduler_health(None)[0]  # type: ignore[arg-type]
    assert check.level is doctor.Level.WARN
    assert "/nonexistent/venv/bin/python" in check.detail


def test_a_service_with_no_execstart_is_broken_not_ignored(unit_dir: Path) -> None:
    _write_units(unit_dir, interpreter="/usr/bin/python3", enabled=True, exec_start=False)
    check = doctor._scheduler_health(None)[0]  # type: ignore[arg-type]
    assert check.level is doctor.Level.WARN
    assert "cannot be understood" in check.detail


def test_enabled_but_inactive_warns(unit_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Masked, or a user manager that is not running them."""
    monkeypatch.setattr(ops, "_systemd_active", lambda _timers: False)
    _write_units(unit_dir, interpreter="/usr/bin/python3", enabled=True)
    check = doctor._scheduler_health(None)[0]  # type: ignore[arg-type]
    assert check.level is doctor.Level.WARN
    assert "inactive" in check.detail


def test_unqueryable_runtime_warns_rather_than_reporting_ok(
    unit_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The overclaim this check exists to prevent.

    Unit files, wants-links and a live interpreter can all be correct while nothing
    runs — no lingering, headless SSH, a dead bus. Filesystem state cannot tell
    'scheduled' from 'looks scheduled', so unverifiable must not read as healthy.
    """
    monkeypatch.setattr(ops, "_systemd_active", lambda _timers: None)
    _write_units(unit_dir, interpreter="/usr/bin/python3", enabled=True)
    check = doctor._scheduler_health(None)[0]  # type: ignore[arg-type]
    assert check.level is doctor.Level.WARN
    assert "could not be asked" in check.detail


def test_installed_enabled_and_active_is_ok(unit_dir: Path) -> None:
    import sys

    _write_units(unit_dir, interpreter=sys.executable, enabled=True)
    check = doctor._scheduler_health(None)[0]  # type: ignore[arg-type]
    assert check.level is doctor.Level.OK
    assert "active" in check.detail


def test_a_dead_bus_is_unknown_rather_than_inactive(monkeypatch: pytest.MonkeyPatch) -> None:
    """"Failed to connect to bus" is not the same answer as "inactive"."""
    monkeypatch.setattr(ops, "_ask", lambda _argv: (1, "Failed to connect to bus: No such file"))
    assert ops._systemd_active(["brain-sync.timer"]) is None
    monkeypatch.setattr(ops, "_ask", lambda _argv: (3, "inactive"))
    assert ops._systemd_active(["brain-sync.timer"]) is False
    monkeypatch.setattr(ops, "_ask", lambda _argv: None)
    assert ops._systemd_active(["brain-sync.timer"]) is None


def test_a_corrupt_plist_is_broken_not_ignored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Previously swallowed, which let the launchd path degrade to a mere INFO."""
    agents = tmp_path / "LaunchAgents"
    agents.mkdir()
    monkeypatch.setattr(ops, "launch_agents_dir", lambda: agents)
    monkeypatch.setattr(ops, "running_on_darwin", lambda: True)
    monkeypatch.setattr(ops, "_launchd_active", lambda _labels: True)
    for name, *_ in ops.UNITS:
        (agents / f"dev.brain.{name.removeprefix('brain-')}.plist").write_bytes(b"not a plist")
    check = doctor._scheduler_health(None)[0]  # type: ignore[arg-type]
    assert check.level is doctor.Level.WARN
    assert "cannot be understood" in check.detail


# -- the Documentation= path ---------------------------------------------------------


def test_documentation_is_omitted_when_no_runbook_is_installed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A wheel has no docs/. The old code emitted a file:// URL to nothing.

    It was correct from a checkout and wrong from an installed tool — and since a
    proper install is the recommended path, the wrong case was the common one.
    """
    monkeypatch.setattr(ops, "runbook_path", lambda: None)
    assert ops._documentation_line() == ""
    body = ops.SERVICE.format(
        desc="d", documentation=ops._documentation_line(), state="/s", python="/p", command="sync"
    )
    assert "Documentation=" not in body
    assert "\n\n[Service]" in body  # no blank hole where the field would have been


def test_documentation_is_emitted_when_a_runbook_really_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runbook = tmp_path / "RUNBOOK.md"
    runbook.write_text("# runbook")
    monkeypatch.setattr(ops, "runbook_path", lambda: runbook)
    assert f"Documentation=file://{runbook}" in ops._documentation_line()

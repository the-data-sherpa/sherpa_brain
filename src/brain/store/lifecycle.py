"""Expiry and decay (BLUEPRINT.md §5.7, §8.1, §8.3).

Invariant 7: **unconfirmed memory decays.** This is what inverts the accretion
dynamic that degrades memory systems over months — a store where nothing ever lapses
fills with claims nobody has confirmed and nobody has cited, and its precision falls
even as its recall rises.

Two independent clocks:

- **Volatility expiry.** How fast a claim goes stale is a property of the claim, not
  a global setting. "I was born in March" never expires; "I'm blocked on the auth
  bug" should be gone in a fortnight. A single decay curve is wrong for most facts
  about a person.
- **Proposal decay.** Anything written without human confirmation carries 30 days.
  Confirm it, cite it, or lose it.

**Expiry is not deletion.** An expired memory stops being served but stays on disk
for a grace period, so a lapse can be undone. Only after that does it become a
tombstone — at which point it is a real deletion with all the machinery that implies.

Every transition goes through the write protocol, so it lands as an auditable
revision rather than a silent mutation. You can always see *when* something lapsed
and what it said.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from ..config import Paths
from ..frontmatter import InvalidFrontmatter, parse, serialize
from ..model import DEFAULT_EXPIRY_DAYS, PROPOSAL_DECAY_DAYS, Memory, Status
from ..model import today as utc_today
from . import deletion
from . import memory as mem

#: How long an expired memory stays recoverable before it is tombstoned (§8.3).
EXPIRED_GRACE_DAYS = 90


@dataclass
class Lapsed:
    memory_id: str
    reason: str
    was_status: str


@dataclass
class Sweep:
    expired: list[Lapsed] = field(default_factory=list)
    purged: list[str] = field(default_factory=list)
    reviewable: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "expired": [{"id": x.memory_id, "reason": x.reason} for x in self.expired],
            "purged": self.purged,
            "due_for_review": self.reviewable,
        }


def expiry_date(m: Memory) -> date | None:
    """When this memory lapses, or None if it never does.

    A proposal's clock runs from when it was recorded; a confirmed memory's runs from
    when its claim became true. Those are different questions and deserve different
    anchors — an old fact confirmed today is not stale.
    """
    if m.status is Status.PROPOSED:
        return m.recorded_at.date() + timedelta(days=PROPOSAL_DECAY_DAYS)
    days = DEFAULT_EXPIRY_DAYS[m.volatility]
    return None if days is None else m.valid_from + timedelta(days=days)


def is_lapsed(m: Memory, today: date) -> str | None:
    """Why this memory has lapsed, or None."""
    if m.status in (Status.EXPIRED, Status.TOMBSTONED):
        return None
    due = expiry_date(m)
    if due is None or today < due:
        return None
    if m.status is Status.PROPOSED:
        return (
            f"proposed on {m.recorded_at.date()} and never confirmed; "
            f"{PROPOSAL_DECAY_DAYS}-day decay elapsed on {due}"
        )
    return f"{m.volatility.value} claim valid from {m.valid_from}; review due {due}"


def _rewrite_status(paths: Paths, m: Memory, path: Path, status: Status) -> bool:
    """Transition status through the write protocol, so it lands as a revision."""
    m.status = status
    try:
        mem.write(
            paths,
            m.id,
            serialize(m).encode(),
            mem.present_hash(path),
            workspace=m.workspace,
            memory_type=m.type.value,
            reason=f"lifecycle: -> {status.value}",
        )
    except mem.Divergence:
        # A contested memory is already failing closed; leave it for a human rather
        # than compounding the conflict with an automated status change.
        return False
    return True


def sweep(
    paths: Paths,
    *,
    today: date | None = None,
    grace_days: int = EXPIRED_GRACE_DAYS,
    purge: bool = True,
) -> Sweep:
    """Expire what has lapsed, tombstone what has been expired past its grace.

    Idempotent, and safe to run on every startup or from cron.
    """
    today = today or utc_today()
    report = Sweep()
    if not paths.memories.is_dir():
        return report

    for path in sorted(paths.memories.rglob("*.md")):
        if ".revisions" in path.parts or ".staging" in path.parts:
            continue
        try:
            m = parse(path.read_text(), path)
        except (InvalidFrontmatter, OSError, UnicodeDecodeError):
            continue  # quarantined; `brain validate` reports it
        if mem.is_contested(paths, m.id) or deletion.is_tombstoned(paths, m.id):
            continue

        if m.status is Status.EXPIRED:
            # Past the grace period an expired memory becomes a real deletion, with
            # the tombstone, the purge, and the replication that implies.
            lapsed_on = m.recorded_at.date()
            if purge and today >= lapsed_on + timedelta(days=grace_days):
                deletion.forget(
                    paths,
                    m.id,
                    workspace=m.workspace,
                    mtype=m.type.value,
                    replicate=deletion.NullReplicator(),
                    reason="expired past grace period",
                )
                report.purged.append(m.id)
            continue

        if reason := is_lapsed(m, today):
            if _rewrite_status(paths, m, path, Status.EXPIRED):
                report.expired.append(Lapsed(m.id, reason, m.status.value))
        elif m.review_by and today >= m.review_by:
            report.reviewable.append(m.id)

    return report


def unexpire(paths: Paths, memory_id: str) -> bool:
    """Undo a lapse. Only possible while the memory is still within its grace period."""
    for path in sorted(paths.memories.rglob("*.md")):
        if ".revisions" in path.parts or ".staging" in path.parts:
            continue
        try:
            m = parse(path.read_text(), path)
        except (InvalidFrontmatter, OSError, UnicodeDecodeError):
            continue
        if m.id != memory_id:
            continue
        if m.status is not Status.EXPIRED:
            return False
        return _rewrite_status(paths, m, path, Status.CONFIRMED)
    return False


def upcoming(
    paths: Paths, within_days: int = 14, today: date | None = None
) -> list[dict[str, Any]]:
    """What is about to lapse. Surfacing this is what makes decay a policy, not a trap."""
    today = today or utc_today()
    out: list[dict[str, Any]] = []
    if not paths.memories.is_dir():
        return out
    for path in sorted(paths.memories.rglob("*.md")):
        if ".revisions" in path.parts or ".staging" in path.parts:
            continue
        try:
            m = parse(path.read_text(), path)
        except (InvalidFrontmatter, OSError, UnicodeDecodeError):
            continue
        if m.status in (Status.EXPIRED, Status.TOMBSTONED):
            continue
        due = expiry_date(m)
        if due and today <= due <= today + timedelta(days=within_days):
            out.append(
                {
                    "id": m.id,
                    "due": due.isoformat(),
                    "days": (due - today).days,
                    "volatility": m.volatility.value,
                    "status": m.status.value,
                }
            )
    return sorted(out, key=lambda x: x["due"])

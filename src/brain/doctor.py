"""``brain doctor`` — one command that answers "is this store healthy?".

Written because the failure modes this design worries about are quiet ones. A broken
ledger, an unreachable replica, an unpurged residue, a stale backup: none of them
announce themselves during normal use, and several of them mean a *safety property*
has silently stopped holding while every ordinary command still works.

Checks are graded by what a failure actually costs:

    FAIL  a safety property is not holding right now
    WARN  a property will stop holding, or cannot be verified
    INFO  worth knowing, nothing to do
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from . import backup as backup_mod
from . import config
from .config import Paths
from .index import build
from .store import deletion, ledger, lifecycle
from .store.ops import pending_ops, stuck_ops


class Level(StrEnum):
    OK = "ok"
    INFO = "info"
    WARN = "warn"
    FAIL = "fail"


@dataclass
class Check:
    name: str
    level: Level
    detail: str
    fix: str = ""

    def as_dict(self) -> dict[str, str]:
        d = {"check": self.name, "level": self.level.value, "detail": self.detail}
        if self.fix:
            d["fix"] = self.fix
        return d


def _ledger_health(p: Paths) -> list[Check]:
    try:
        deletion.verify_all_ledgers(p)
    except ledger.LedgerError as exc:
        return [
            Check(
                "ledger-integrity",
                Level.FAIL,
                f"a hash chain is broken: {exc}",
                "The store is refusing to serve. Restore the ledger from a replica.",
            )
        ]
    seq, _ = ledger.head(p.tombstones)
    return [
        Check("ledger-integrity", Level.OK, f"all three chains verify (tombstones at seq {seq})")
    ]


def _replica_health(p: Paths) -> list[Check]:
    if not p.ledger_git.exists():
        return [
            Check(
                "replica",
                Level.WARN,
                "no off-device replica configured, so deletion quorum is unreachable "
                "and every `forget` will report pending forever",
                "brain ledger init --remote git@github.com:you/brain-ledger.git",
            )
        ]
    from .store.replicate import GitLedgerReplicator

    r = GitLedgerReplicator(p)
    if not r.remote:
        return [Check("replica", Level.WARN, "ledger repo exists but has no remote configured")]
    if not r.verify_protection():
        return [
            Check(
                "replica",
                Level.WARN,
                f"{r.remote} is configured but its protection against force-push could "
                "not be verified; acks from an unprotected remote are rejected at "
                "quorum time",
                "Enable a branch ruleset blocking non-fast-forward and deletion.",
            )
        ]
    return [Check("replica", Level.OK, f"{r.remote} configured and protected")]


def _deletion_health(p: Paths) -> list[Check]:
    out = []
    if pending := deletion.pending_deletions(p):
        out.append(
            Check(
                "deletions-pending",
                Level.WARN,
                f"{len(pending)} deletion(s) suppressed locally but not replicated",
                "brain sync",
            )
        )
    residue = [
        sid
        for sid in deletion.tombstoned_ids(p)
        if any(path.exists() for path in deletion._purge_paths(p, sid, "default", "semantic"))
    ]
    if residue:
        out.append(
            Check(
                "deletion-residue",
                Level.FAIL,
                f"{len(residue)} tombstoned subject(s) still have bytes on disk — "
                "retrieval is suppressed but the content was not removed",
                "brain sync",
            )
        )
    if not out:
        out.append(Check("deletions", Level.OK, "no pending deletions, no residue"))
    return out


def _integrity_health(p: Paths) -> list[Check]:
    out = []
    if stuck := stuck_ops(p):
        out.append(
            Check(
                "stuck-operations",
                Level.FAIL,
                f"{len(stuck)} operation(s) cannot complete: {stuck[0][1]}",
                "brain status, then repair by hand — these are never guessed away",
            )
        )
    elif ops := pending_ops(p):
        out.append(
            Check("pending-operations", Level.WARN, f"{len(ops)} in flight", "brain recover")
        )

    conflicts = list(p.conflicts.glob("*.json")) if p.conflicts.is_dir() else []
    if conflicts:
        out.append(
            Check(
                "conflicts",
                Level.WARN,
                f"{len(conflicts)} contested memory/ies — reads fail closed until resolved",
                "brain conflicts list",
            )
        )
    if dangling := deletion.dangling_evidence(p):
        out.append(
            Check(
                "dangling-evidence",
                Level.WARN,
                f"{len(dangling)} claim(s) cite evidence that no longer resolves",
                "brain validate",
            )
        )
    return out or [Check("integrity", Level.OK, "no stuck ops, conflicts, or dangling evidence")]


def _index_health(p: Paths) -> list[Check]:
    if not p.db.exists():
        return [Check("index", Level.INFO, "no index yet", "brain reindex")]
    try:
        conn = build.connect(p)
        n = conn.execute("SELECT COUNT(*) FROM memory_index").fetchone()[0]
        conn.close()
    except Exception as exc:
        return [
            Check(
                "index",
                Level.WARN,
                f"index unreadable ({exc}) — this is derived, so nothing is lost",
                "brain reindex",
            )
        ]
    return [Check("index", Level.OK, f"{n} memories indexed")]


def _backup_health(p: Paths) -> list[Check]:
    backups = backup_mod.list_backups(p)
    if not backups:
        return [
            Check(
                "backup",
                Level.WARN,
                "no backup has ever been taken",
                "brain backup create",
            )
        ]
    latest = backups[-1]
    return [
        Check(
            "backup",
            Level.OK,
            f"latest {latest['generation']} via {latest['mechanism']}, "
            f"{latest['files']} files, tombstone seq {latest['tombstone_seq']}",
        )
    ]


def _lifecycle_health(p: Paths) -> list[Check]:
    soon = lifecycle.upcoming(p, within_days=14)
    if not soon:
        return [Check("lifecycle", Level.OK, "nothing lapses in the next 14 days")]
    return [
        Check(
            "lifecycle",
            Level.INFO,
            f"{len(soon)} memory/ies lapse within 14 days",
            "brain expire --dry-run",
        )
    ]


def _environment_health(p: Paths) -> list[Check]:
    out = []
    if reason := config.denylist_reason(p.root):
        out.append(Check("filesystem", Level.FAIL, reason, "Move the store to a local disk."))
    else:
        out.append(
            Check(
                "filesystem",
                Level.OK,
                "no known-unsafe filesystem or sync folder — note this establishes "
                "capability, not crash durability (ADR 0005)",
            )
        )
    if not shutil.which("rg"):
        out.append(Check("ripgrep", Level.WARN, "not installed; rung-0 search unavailable"))
    if not shutil.which("git"):
        out.append(Check("git", Level.WARN, "not installed; ledger replication unavailable"))
    return out


def _capture_health(p: Paths) -> list[Check]:
    """The failure mode that actually kills personal knowledge systems.

    Not corruption — emptiness. A store nobody writes to is a notes app you stopped
    opening, and it fails silently by definition.
    """
    n = (
        sum(
            1
            for f in p.memories.rglob("*.md")
            if ".revisions" not in f.parts and ".staging" not in f.parts
        )
        if p.memories.is_dir()
        else 0
    )
    if n == 0:
        return [
            Check(
                "capture",
                Level.WARN,
                "the store is empty. Every personal knowledge system that failed, "
                "failed of disuse rather than corruption",
                "brain remember '...' — or wire the MCP server into your agent",
            )
        ]
    if n < 20:
        return [
            Check(
                "capture",
                Level.INFO,
                f"{n} memories. Below ~150 the eval instruments cannot report a trustworthy slope",
            )
        ]
    return [Check("capture", Level.OK, f"{n} memories")]


def run(p: Paths) -> tuple[list[Check], Level]:
    checks: list[Check] = []
    for fn in (
        _environment_health,
        _ledger_health,
        _replica_health,
        _deletion_health,
        _integrity_health,
        _index_health,
        _backup_health,
        _lifecycle_health,
        _capture_health,
    ):
        try:
            checks.extend(fn(p))
        except Exception as exc:
            checks.append(Check(fn.__name__.strip("_"), Level.WARN, f"check itself failed: {exc}"))

    worst = Level.OK
    for c in checks:
        if c.level is Level.FAIL:
            worst = Level.FAIL
            break
        if c.level is Level.WARN:
            worst = Level.WARN
    return checks, worst


def report(p: Paths) -> dict[str, Any]:
    checks, worst = run(p)
    return {
        "root": str(p.root),
        "status": worst.value,
        "checks": [c.as_dict() for c in checks],
    }


__all__ = ["Check", "Level", "report", "run"]

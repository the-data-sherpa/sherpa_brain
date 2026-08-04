"""Resource budgets and idempotency (BLUEPRINT.md §12.1, §14).

Two unrelated problems that share a home because both are about a store that runs
unattended for months.

**Budgets.** Append-only is the right shape for correctness and the wrong shape for
disk. The query log grows on every search forever; the purge ledger grows on every
deletion. Every limit here is a hard cap with defined behaviour on breach — never a
silent truncation, because a log that quietly drops its oldest entries reads as
"nothing happened then" rather than "we stopped recording".

**Idempotency.** A retried tool call must not create a second memory. The MCP surface
accepted an ``idempotency_key`` and ignored it, which meant any client retry — a
timeout, a dropped connection — silently duplicated the write.

Note what is *not* budgeted: tombstones. That ledger is the anti-resurrection
authority and compacting it would mean forgetting what was forgotten.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from ..atomic import write_atomic
from ..config import Paths
from ..model import iso, utcnow

#: Retrieval log. Big enough to be useful for the failure taxonomy, bounded enough
#: that an unattended store does not fill a disk with its own telemetry.
QUERY_LOG_MAX_LINES = 50_000
QUERY_LOG_RETAIN_DAYS = 180

#: Proposals per background batch. Prevents one runaway session from flooding review.
MAX_PROPOSALS_PER_BATCH = 50

#: Idempotency records are only useful for as long as a client might retry.
IDEMPOTENCY_TTL_HOURS = 24


@dataclass(frozen=True)
class Trimmed:
    path: str
    removed: int
    reason: str


def trim_query_log(
    paths: Paths,
    *,
    max_lines: int = QUERY_LOG_MAX_LINES,
    retain_days: int = QUERY_LOG_RETAIN_DAYS,
) -> Trimmed | None:
    """Bound the retrieval log by age first, then by count.

    Age before count, deliberately: dropping by count alone would discard the oldest
    entries even when they are recent and the store is merely busy, and the slope
    analysis in §10.1 needs history more than it needs the last thousand queries.

    The trim is *logged*, never silent. A log that quietly loses its tail is
    indistinguishable from a log of a quiet period.
    """
    log = paths.logs / "queries.jsonl"
    if not log.exists():
        return None

    lines = [ln for ln in log.read_text().splitlines() if ln.strip()]
    if not lines:
        return None

    cutoff = iso(utcnow() - timedelta(days=retain_days))
    kept: list[str] = []
    for ln in lines:
        try:
            if str(json.loads(ln).get("at", "")) >= cutoff:
                kept.append(ln)
        except json.JSONDecodeError:
            continue

    reason = f"older than {retain_days} days"
    if len(kept) > max_lines:
        kept = kept[-max_lines:]
        reason = f"exceeded {max_lines} lines"

    removed = len(lines) - len(kept)
    if removed <= 0:
        return None

    write_atomic(log, ("\n".join(kept) + "\n" if kept else "").encode())
    marker = paths.logs / "trims.jsonl"
    with marker.open("a") as fh:
        fh.write(json.dumps({"at": iso(utcnow()), "removed": removed, "reason": reason}) + "\n")
    return Trimmed(str(log), removed, reason)


def compact_purge_ledger(paths: Paths) -> Trimmed | None:
    """Collapse duplicate purge observations for the same subject.

    Purge receipts are point-in-time observations (§11.5), so a subject re-scanned a
    hundred times accumulates a hundred entries that say the same thing. Only the
    most recent observation per subject carries information.

    **Tombstones and acks are never compacted.** They are the anti-resurrection
    authority; discarding entries there would mean forgetting what was forgotten.
    """
    from . import ledger

    if not paths.purges.exists():
        return None
    entries = ledger.read_chain(paths.purges)
    latest: dict[str, Any] = {}
    for e in entries:
        latest[e.subject_id] = e.payload
    if len(latest) == len(entries):
        return None

    removed = len(entries) - len(latest)
    paths.purges.unlink()
    for payload in latest.values():
        ledger.append(paths.purges, payload)
    return Trimmed(str(paths.purges), removed, "duplicate purge observations")


# ── idempotency ──────────────────────────────────────────────────────────────────


def _keys_path(paths: Paths):  # type: ignore[no-untyped-def]
    return paths.root / "idempotency.json"


def _load(paths: Paths) -> dict[str, Any]:
    f = _keys_path(paths)
    if not f.exists():
        return {}
    try:
        data = json.loads(f.read_text())
    except json.JSONDecodeError:
        return {}
    cutoff = iso(utcnow() - timedelta(hours=IDEMPOTENCY_TTL_HOURS))
    return {k: v for k, v in data.items() if str(v.get("at", "")) >= cutoff}


def remember_key(paths: Paths, key: str, result: dict[str, Any]) -> None:
    data = _load(paths)
    data[key] = {"at": iso(utcnow()), "result": result}
    write_atomic(_keys_path(paths), json.dumps(data, indent=2, sort_keys=True).encode())


def replay(paths: Paths, key: str | None) -> dict[str, Any] | None:
    """Return the prior result for this key, if it is still within the retry window.

    A retried call must return what the first one returned — not merely avoid a
    duplicate. A client that retries after a timeout needs the original id back, or
    it will think the write failed and try again with a fresh key.
    """
    if not key:
        return None
    entry = _load(paths).get(key)
    if entry is None:
        return None
    result = dict(entry["result"])
    result["idempotent_replay"] = True
    return result


def sweep(paths: Paths) -> dict[str, Any]:
    """Run every budget. Safe to call on a timer."""
    out: dict[str, Any] = {}
    if t := trim_query_log(paths):
        out["query_log"] = {"removed": t.removed, "reason": t.reason}
    if t := compact_purge_ledger(paths):
        out["purge_ledger"] = {"removed": t.removed, "reason": t.reason}
    # Rewriting with the TTL filter applied is what actually expires old keys.
    write_atomic(_keys_path(paths), json.dumps(_load(paths), indent=2, sort_keys=True).encode())
    return out

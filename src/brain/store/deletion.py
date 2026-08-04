"""Deletion, purge, and replication (BLUEPRINT.md §11.5).

**Two independent gates, and conflating them fails open.** An earlier design ordered
this "tombstone (quorum) → stop retrieval", which leaves deleted content *retrievable*
whenever the push fails or the machine is offline — the exact opposite of fail-closed,
arrived at by hanging both properties off one step.

    durable local tombstone  ->  retrieval suppressed IMMEDIATELY, unconditionally
    replica quorum           ->  gates only the SUCCESS RECEIPT

So `brain forget` offline suppresses instantly and reports ``pending``, never
``deleted``. **There is no ``--force-local``**: a flag cannot both exit success and
honour "not a completed deletion without quorum", so it was removed rather than
weakened.

Purge and replication are both **resumable**, because a crash between tombstone and
purge leaves valid suppression with residual bytes on disk. Since delivery and purge
state are derived projections, the backlog is computable and resumption is idempotent.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

from ..atomic import fsync_dir, write_atomic
from ..config import Paths
from . import ledger


class DeliveryState(StrEnum):
    PENDING = "pending"
    CONFIRMED = "confirmed"


@dataclass
class ForgetResult:
    subject_id: str
    suppressed: bool  # always True once the local tombstone is durable
    delivery: DeliveryState
    replicas: int
    required: int
    removed: list[str] = field(default_factory=list)
    residue: list[str] = field(default_factory=list)

    @property
    def complete(self) -> bool:
        return self.delivery is DeliveryState.CONFIRMED


def is_tombstoned(paths: Paths, subject_id: str) -> bool:
    """The single source of suppression truth. Never gated on quorum or purge."""
    return any(e.subject_id == subject_id for e in ledger.read_chain(paths.tombstones))


def tombstoned_ids(paths: Paths) -> set[str]:
    return {e.subject_id for e in ledger.read_chain(paths.tombstones) if e.subject_id}


def delivery_state(paths: Paths, subject_id: str, required: int = 2) -> tuple[DeliveryState, int]:
    """Derived projection over (tombstones, acks). Never stored, never mutated.

    Quorum counts **distinct replica identities**, not ack entries: a replayed or
    duplicated ack must not inflate it. Local durability is replica one; each verified
    remote is another.
    """
    tombs = {e.subject_id: e for e in ledger.read_chain(paths.tombstones)}
    if subject_id not in tombs:
        return DeliveryState.PENDING, 0

    valid_head = tombs[subject_id].hash
    identities: set[str] = {"local"}  # the local ledger is durable by construction
    for a in ledger.read_chain(paths.acks):
        p = a.payload
        if p.get("subject_id") != subject_id:
            continue
        # An ack must resolve to an exact local tombstone entry. One that references
        # an unknown or non-matching chain head is invalid, not merely unhelpful.
        if p.get("chain_head") != valid_head:
            continue
        if not p.get("protection_verified"):
            continue
        identities.add(str(p.get("replica_identity", "")))
    identities.discard("")
    n = len(identities)
    return (DeliveryState.CONFIRMED if n >= required else DeliveryState.PENDING), n


def _purge_paths(paths: Paths, subject_id: str, workspace: str, mtype: str) -> list[Path]:
    """Every path that could hold bytes for this subject."""
    out: list[Path] = []
    present = paths.memory_dir(workspace, mtype) / f"{subject_id}.md"
    out.append(present)
    # Workspace/type may have changed over the subject's life; sweep broadly.
    if paths.memories.is_dir():
        out.extend(
            p
            for p in paths.memories.rglob(f"{subject_id}.md")
            if ".revisions" not in p.parts and p not in out
        )
    rev_dir = paths.revision_dir(subject_id)
    if rev_dir.is_dir():
        out.extend(sorted(rev_dir.iterdir()))
    out.append(paths.conflict_path(subject_id))
    if paths.quarantine.is_dir():
        out.extend(paths.quarantine.glob(f"{subject_id}*"))
    return out


def purge_query_log(paths: Paths, subject_id: str) -> int:
    """Remove query-log entries that reference a tombstoned subject.

    The retrieval log is a derived representation, and §11.5 requires deletion to
    reach every one of them. This is easy to miss because the log looks like
    telemetry rather than content — but an entry pairs a *query string* with the IDs
    it returned, and a query is very often a fragment of the memory itself. Leaving
    it behind means the words you asked to forget survive in a file nobody thinks of
    as storage.

    Returns the number of entries removed.
    """
    log = paths.logs / "queries.jsonl"
    if not log.exists():
        return 0
    kept: list[str] = []
    dropped = 0
    for line in log.read_text().splitlines():
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if subject_id in (entry.get("retrieved") or []) or subject_id == entry.get("cited"):
            dropped += 1
            continue
        kept.append(line)
    if dropped:
        write_atomic(log, ("\n".join(kept) + "\n" if kept else "").encode())
    return dropped


def purge(
    paths: Paths, subject_id: str, workspace: str = "default", mtype: str = "semantic"
) -> tuple[list[str], list[str]]:
    """Remove every byte for a subject, then *verify absence*, then record.

    Ordering matters and was wrong in an earlier draft: a receipt appended before the
    unlink is durable means the system believes bytes are gone while they survive.

        unlink -> fsync parent directories -> re-stat to VERIFY -> only then record

    Returns ``(removed, residue)``. Residue is anything that would not go away, which
    is reported rather than silently accepted.
    """
    removed: list[str] = []
    dirs: set[Path] = set()
    for p in _purge_paths(paths, subject_id, workspace, mtype):
        if p.exists() or p.is_symlink():
            try:
                p.unlink()
                removed.append(str(p))
                dirs.add(p.parent)
            except OSError:
                continue
    rev_dir = paths.revision_dir(subject_id)
    if rev_dir.is_dir() and not any(rev_dir.iterdir()):
        rev_dir.rmdir()
        dirs.add(rev_dir.parent)

    for d in dirs:
        if d.exists():
            fsync_dir(d)

    # The query log is a derived representation too, and deletion must reach it.
    purge_query_log(paths, subject_id)

    # Verify absence by re-stat. `unlink` returning 0 is not the same as the byte
    # being gone — a hard link elsewhere, or a concurrent recreate, changes the answer.
    residue = [
        str(p)
        for p in _purge_paths(paths, subject_id, workspace, mtype)
        if p.exists() or p.is_symlink()
    ]
    if not residue:
        ledger.append(paths.purges, ledger.purge_payload(subject_id, removed))
    return removed, residue


def purge_artifact(paths: Paths, digest: str) -> tuple[list[str], list[str]]:
    """Erase an ingested artifact.

    Without this, a secret arriving inside an ingested document cannot be removed —
    which would make "forgetting" true of memories and false of evidence, and
    evidence is where the sensitive material usually is.
    """
    from . import artifacts

    d = artifacts.blob_dir(paths, digest)
    if not d.exists():
        return [], []
    removed = [str(p) for p in d.rglob("*") if p.is_file()]
    artifacts.purge(paths, digest)
    residue = [str(p) for p in d.rglob("*")] if d.exists() else []
    if not residue:
        ledger.append(paths.purges, ledger.purge_payload(digest, removed))
    return removed, residue


def purge_event(paths: Paths, event_id: str) -> tuple[list[str], list[str]]:
    """Erase one event by redaction fork — never by editing a segment in place."""
    from . import events

    found = events.find(paths, event_id)
    if found is None:
        return [], []
    _, segment = found
    forked, dropped = events.redaction_fork(paths, segment, {event_id})
    if dropped == 0:
        return [], [str(segment)]
    ledger.append(paths.purges, ledger.purge_payload(event_id, [str(segment)]))
    return [f"{segment} -> {forked}"], []


def dangling_evidence(paths: Paths) -> list[dict[str, Any]]:
    """Memories whose evidence no longer resolves.

    Deleting evidence is legitimate, but it silently weakens every claim that cited
    it. A pointer that has stopped resolving should be visible, not merely fail-safe.
    """
    from ..frontmatter import InvalidFrontmatter, parse
    from . import artifacts

    out: list[dict[str, Any]] = []
    if not paths.memories.is_dir():
        return out
    tombstoned = tombstoned_ids(paths)
    for path in sorted(paths.memories.rglob("*.md")):
        if ".revisions" in path.parts or ".staging" in path.parts:
            continue
        try:
            m = parse(path.read_text(), path)
        except (InvalidFrontmatter, OSError, UnicodeDecodeError):
            continue
        if m.id in tombstoned:
            continue
        for e in m.evidence:
            if not e.ref.startswith(("event:", "artifact:")):
                continue
            resolved = artifacts.resolve_evidence(paths, e.ref, e.span_start, e.span_end)
            if not resolved.get("resolved"):
                out.append(
                    {
                        "memory_id": m.id,
                        "ref": e.ref,
                        "reason": resolved.get("reason", "unresolved"),
                    }
                )
    return out


def forget(
    paths: Paths,
    subject_id: str,
    *,
    kind: str = "memory",
    workspace: str = "default",
    mtype: str = "semantic",
    replicate: Replicator | None = None,
    required_replicas: int = 2,
    reason: str | None = None,
) -> ForgetResult:
    """Delete a subject. Suppression is immediate; the receipt waits on quorum.

    ``kind`` is ``memory``, ``artifact``, or ``event``. All three are erasable — a
    store that can forget what it concluded but not what it read has not really
    forgotten anything.
    """
    # 1. Durable local tombstone. From this instant the subject is unreachable,
    #    regardless of network state, purge progress, or anything else.
    if not is_tombstoned(paths, subject_id):
        ledger.append(
            paths.tombstones, ledger.tombstone_payload(subject_id, kind=kind, reason=reason)
        )

    # 2. Physical purge. Independent of replication.
    if kind == "artifact":
        removed, residue = purge_artifact(paths, subject_id)
    elif kind == "event":
        removed, residue = purge_event(paths, subject_id)
    else:
        removed, residue = purge(paths, subject_id, workspace, mtype)

    # 3. Replication. Gates only the receipt.
    if replicate is not None:
        replicate.push_and_ack(paths, subject_id)

    state, n = delivery_state(paths, subject_id, required_replicas)
    return ForgetResult(
        subject_id=subject_id,
        suppressed=True,
        delivery=state,
        replicas=n,
        required=required_replicas,
        removed=removed,
        residue=residue,
    )


def resume(
    paths: Paths, replicate: Replicator | None = None, required: int = 2
) -> dict[str, list[Any]]:
    """Idempotently finish every incomplete deletion. Run at startup and on `sync`.

    **Every tombstoned subject is re-scanned for physical residue, regardless of
    ``purge_state``.** An earlier draft resumed only tombstones "lacking a purge",
    which skips exactly the IDs whose bytes could have been recreated *after* a
    receipt was written — silently reintroducing the residue the receipt claims is
    gone. A receipt informs history; it never shortens a scan.
    """
    report: dict[str, list[Any]] = {"repurged": [], "replicated": [], "residue": []}
    for sid in sorted(tombstoned_ids(paths)):
        found = [
            str(p)
            for p in _purge_paths(paths, sid, "default", "semantic")
            if p.exists() or p.is_symlink()
        ]
        if found:
            removed, residue = purge(paths, sid)
            report["repurged"].append({"subject_id": sid, "removed": removed})
            if residue:
                report["residue"].append({"subject_id": sid, "paths": residue})
        state, _ = delivery_state(paths, sid, required)
        if (
            state is DeliveryState.PENDING
            and replicate is not None
            and replicate.push_and_ack(paths, sid)
        ):
            report["replicated"].append(sid)
    return report


def pending_deletions(paths: Paths, required: int = 2) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for sid in sorted(tombstoned_ids(paths)):
        state, n = delivery_state(paths, sid, required)
        if state is DeliveryState.PENDING:
            out.append({"subject_id": sid, "replicas": n, "required": required})
    return out


def verify_all_ledgers(paths: Paths) -> None:
    """Verify every chain. Raises ``LedgerError`` — callers must refuse to serve.

    All three, not just tombstones: an unverified ack ledger would let a forged ack
    fabricate quorum, which defeats the mechanism the tombstone chain protects.
    """
    for path in (paths.tombstones, paths.acks, paths.purges):
        ledger.read_chain(path, verify=True)


class Replicator:
    """Interface for pushing the tombstone ledger to an independent replica.

    Two rules that a naive implementation gets wrong:

    - **The push is not the acknowledgement.** After pushing, re-read the remote ref
      and confirm it contains the ``(seq, chain_head)`` just written. Only that read
      is the ack. A push whose outcome is uncertain — a timeout after send — is
      resolved by re-reading, never by assuming either way.
    - **Identity comes from the configured endpoint**, not from anything the ack says.
    """

    identity: str = "unconfigured"

    def push_and_ack(self, paths: Paths, subject_id: str) -> bool:  # pragma: no cover
        raise NotImplementedError


class NullReplicator(Replicator):
    """No second replica configured. Quorum is never met, and that is reported.

    Deliberately not a silent success: a deletion with one replica is a real,
    durable, retrieval-suppressing deletion that has not been replicated, and the
    caller should be told which of those two things it got.
    """

    identity = "none"

    def push_and_ack(self, paths: Paths, subject_id: str) -> bool:
        return False


__all__ = [
    "DeliveryState",
    "ForgetResult",
    "NullReplicator",
    "Replicator",
    "delivery_state",
    "forget",
    "is_tombstoned",
    "pending_deletions",
    "purge",
    "resume",
    "tombstoned_ids",
    "verify_all_ledgers",
]

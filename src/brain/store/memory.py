"""The write protocol (BLUEPRINT.md §6.5).

The ordering here is the whole safety argument, and every line of it was wrong in at
least one earlier draft:

1. **stage** the new body — complete and fsynced before it is named anywhere;
2. **create the op record** — intent durable *before every other durable effect*;
3. **publish** the revision by ``link()`` — never overwriting, never torn;
4. **exchange** present with the staged copy — ONE atomic operation, never undone;
5. **retire the op** — last, so a crash replays the same recovery;
6. update the index — derived, and repairable by ``reindex`` in any case.

Two things this deliberately does *not* do.

**It never rolls back.** An earlier version exchanged, inspected, and swapped back on
mismatch. That is three operations, and the undo is itself a non-atomic
read-modify-write that races the next editor write::

    stage C -> exchange: present=C, staged=B   (an editor had written B)
    detect B != A -> editor writes D over present
    exchange back: present=B, staged=D          <- D clobbered, C lost

The rollback *creates* the loss it was meant to prevent.

**It does not promise present state is unchanged on divergence.** The filesystem
offers no primitive for that against a writer who bypasses our locks, and asserting
it would be an unearned guarantee. What is guaranteed instead, and is enforceable:

    No committed state is ever lost, and a contested memory never serves one branch
    as though it were settled.

Present may hold either branch after a divergence — reads fail closed until a human
resolves it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from ..atomic import exchange, fsync_dir, rename_noreplace, write_staged
from ..config import Paths
from ..frontmatter import content_hash
from ..ids import new_opid
from ..model import Capture, Disposition, iso, utcnow
from . import revisions
from .ops import Op, OpPhase, advance_op, create_op, memory_lock, new_op, retire_op, store_lock


class Divergence(Exception):
    """Two branches from one predecessor. Both retained; a human must choose."""

    def __init__(self, memory_id: str, ours: int, theirs: int) -> None:
        self.memory_id, self.ours, self.theirs = memory_id, ours, theirs
        super().__init__(
            f"{memory_id}: divergent write. Both branches retained as revisions "
            f"{ours} and {theirs}; the memory is contested and reads will fail closed "
            f"until `brain conflicts resolve {memory_id}`."
        )


@dataclass(frozen=True)
class WriteResult:
    memory_id: str
    revision_no: int
    content_hash: str
    opid: str
    contested: bool = False


def present_path(paths: Paths, workspace: str, mtype: str, memory_id: str) -> Path:
    return paths.memory_dir(workspace, mtype) / f"{memory_id}.md"


def read_present(path: Path) -> bytes | None:
    return path.read_bytes() if path.exists() else None


def present_hash(path: Path) -> str | None:
    data = read_present(path)
    return content_hash(data) if data is not None else None


def write_conflict(paths: Paths, memory_id: str, ours: int, theirs: int, opid: str) -> None:
    """Record a divergence. Its presence is what makes reads fail closed."""
    paths.conflicts.mkdir(parents=True, exist_ok=True)
    payload = {
        "memory_id": memory_id,
        "detected_at": iso(utcnow()),
        "opid": opid,
        "branches": [
            {"revision": ours, "origin": "mediated"},
            {"revision": theirs, "origin": "unwitnessed"},
        ],
        "resolution": "required — `brain conflicts resolve <id> --take <revision>`",
    }
    from ..atomic import write_atomic

    write_atomic(paths.conflict_path(memory_id), json.dumps(payload, indent=2).encode() + b"\n")


def is_contested(paths: Paths, memory_id: str) -> bool:
    return paths.conflict_path(memory_id).exists()


def write(
    paths: Paths,
    memory_id: str,
    body: bytes,
    expected_predecessor: str | None,
    *,
    workspace: str = "default",
    memory_type: str = "semantic",
    actor: str | None = None,
    session: str | None = None,
    reason: str | None = None,
) -> WriteResult:
    """Commit a new state for ``memory_id``.

    ``expected_predecessor`` is the content hash the caller believed it was editing,
    or None for a create. A mismatch is a divergence, never an overwrite.
    """
    dest = present_path(paths, workspace, memory_type, memory_id)
    dest.parent.mkdir(parents=True, exist_ok=True)

    with memory_lock(paths, memory_id):  # held THROUGH op retirement
        opid = new_opid()
        staged = paths.staging_path(opid)
        # Present gets its OWN inode, deliberately. Publishing a revision hard-links
        # the staged inode into the log, so if present were also a link to it, an
        # editor's in-place write to present would retroactively rewrite published
        # history. Two staged files, two inodes, one body.
        present_tmp = paths.staging_path(f"{opid}.present")

        # 1. Stage a complete, fsynced body. Nothing references it yet.
        write_staged(staged, body)

        # 2. Intent durable BEFORE any other durable effect. Serialized against GC
        #    so no scan can observe the window between staging and its op record.
        op = new_op(
            opid,
            memory_id,
            staged,
            expected_predecessor,
            workspace=workspace,
            memory_type=memory_type,
            actor=actor,
            session=session,
            reason=reason,
        )
        with store_lock(paths):
            create_op(paths, op)

        theirs: int | None = None
        try:
            # 3. Publish into the append-only log. Never overwrites.
            n = revisions.publish(paths, memory_id, staged)
            advance_op(paths, op, OpPhase.PUBLISHED, revision_no=n)

            # 4. Materialize present. ONE atomic operation, never undone.
            write_staged(present_tmp, body)
            if rename_noreplace(present_tmp, dest):
                displaced_hash = None  # nothing was there to displace
            else:
                exchange(dest, present_tmp)
                displaced = present_tmp.read_bytes() if present_tmp.exists() else None
                displaced_hash = content_hash(displaced) if displaced is not None else None

            advance_op(paths, op, OpPhase.EXCHANGED)

            # UNCONDITIONAL: if the bytes we just displaced are not already in the
            # revision log, publish them. This holds "no committed state is ever
            # lost" *structurally*, rather than relying on the caller to supply an
            # honest predecessor.
            #
            # An earlier version only captured on mismatch, which meant a caller who
            # read present immediately before writing — exactly what the MCP server
            # did — always matched, and any unwitnessed edit sitting in present was
            # silently destroyed. The guarantee cannot depend on caller discipline.
            displaced_unrecorded = (
                displaced_hash is not None
                and revisions.find_by_hash(paths, memory_id, displaced_hash) is None
            )
            if displaced_unrecorded:
                _publish_displaced(paths, memory_id, present_tmp)

            # Contested is a *separate* question: did the caller write from a state
            # other than the one it displaced?
            contested = displaced_hash != expected_predecessor
            if contested:
                theirs = _publish_displaced(paths, memory_id, present_tmp)
                write_conflict(paths, memory_id, ours=n, theirs=theirs, opid=opid)

            fsync_dir(dest.parent)
        finally:
            # 5. Retire last, so a crash anywhere above replays the same recovery.
            retire_op(paths, opid)
            staged.unlink(missing_ok=True)
            present_tmp.unlink(missing_ok=True)

    result = WriteResult(memory_id, n, content_hash(body), opid, contested)
    if contested:
        raise Divergence(memory_id, n, theirs or n)
    return result


def _publish_displaced(paths: Paths, memory_id: str, holder: Path) -> int:
    """Publish displaced bytes as a reconciled revision, unless already in the log."""
    data = holder.read_bytes()
    digest = content_hash(data)
    if (existing := revisions.find_by_hash(paths, memory_id, digest)) is not None:
        return existing
    return revisions.publish(paths, memory_id, holder)


def serving_disposition(paths: Paths, memory_id: str, path: Path) -> Disposition:
    """What may be served for this memory (BLUEPRINT §6.6).

    **First match wins**, and the order is load-bearing. This runs only *after*
    ``recover_pending_ops`` — an earlier design folded recovery in as a priority
    level and deadlocked: with ``CONTESTED`` ahead of it, a crash mid-resolution
    left an op that was never reached and resolution stalled forever.
    """
    from ..frontmatter import InvalidFrontmatter, parse

    data = read_present(path)
    if data is None:
        return Disposition.QUARANTINED
    try:
        parse(data.decode("utf-8"), path)
    except (InvalidFrontmatter, UnicodeDecodeError):
        return Disposition.QUARANTINED

    if is_contested(paths, memory_id):
        return Disposition.CONTESTED

    ph = content_hash(data)
    newest = revisions.newest_revision(paths, memory_id)
    if newest is None:
        return Disposition.UNWITNESSED
    n, newest_bytes = newest
    nh = content_hash(newest_bytes)

    if ph == nh:
        return Disposition.SETTLED

    # An interrupted mediated write leaves present holding the *predecessor* of the
    # newest revision. That is distinguishable from a direct edit only because the
    # op recorded what it expected to displace.
    if _predecessor_of(paths, memory_id, n) == ph:
        return Disposition.INTERRUPTED

    if ph in revisions.revision_hashes(paths, memory_id):
        return Disposition.INTERRUPTED
    return Disposition.UNWITNESSED


def _predecessor_of(paths: Paths, memory_id: str, n: int) -> str | None:
    if n <= 1:
        return None
    for prev in reversed(revisions.revision_numbers(paths, memory_id)):
        if prev < n and (data := revisions.read_revision(paths, memory_id, prev)):
            return content_hash(data)
    return None


def recover_pending_ops(paths: Paths) -> list[str]:
    """PASS 1 — run unconditionally, before anything is served.

    Ignores conflict and quarantine state entirely: those are answers to a different
    question. Takes the *same* per-memory lock a live writer holds, because op
    records become visible before publication by design, so an unguarded pass would
    race the writer that owns the op.
    """
    from .ops import pending_ops

    recovered: list[str] = []
    for op in pending_ops(paths):
        with memory_lock(paths, op.memory_id):
            current = _reread_op(paths, op.opid)
            if current is None:
                continue  # the owning writer finished while we waited
            recovered.append(_recover_one(paths, current))
    return [r for r in recovered if r]


def _reread_op(paths: Paths, opid: str) -> Op | None:
    from .ops import read_op

    return read_op(paths, opid)


def _recover_one(paths: Paths, op: Op) -> str:
    staged = Path(op.staging_path)
    dest = present_path(paths, op.workspace, op.memory_type, op.memory_id)

    if staged.exists():
        data = staged.read_bytes()
        digest = content_hash(data)
        known = revisions.revision_hashes(paths, op.memory_id)
        if digest not in known:
            # Bytes nobody has recorded. This is the displaced branch the crash
            # stranded; publishing it is what stops it being lost.
            theirs = revisions.publish(paths, op.memory_id, staged)
            ours = op.revision_no or theirs
            if ours != theirs:
                write_conflict(paths, op.memory_id, ours=ours, theirs=theirs, opid=op.opid)
            retire_op(paths, op.opid)
            staged.unlink(missing_ok=True)
            return f"{op.memory_id}: recovered stranded branch as revision {theirs}"

    if op.revision_no is not None and dest.exists():
        ph = present_hash(dest)
        rev = revisions.read_revision(paths, op.memory_id, op.revision_no)
        if (
            rev is not None
            and ph != content_hash(rev)
            and ph is not None
            and ph not in revisions.revision_hashes(paths, op.memory_id)
        ):
            # A direct edit landed after publication. Not an unwitnessed edit —
            # there is a published competing revision, so it is a divergence.
            write_staged(staged, dest.read_bytes())
            theirs = revisions.publish(paths, op.memory_id, staged)
            write_conflict(paths, op.memory_id, ours=op.revision_no, theirs=theirs, opid=op.opid)
            retire_op(paths, op.opid)
            staged.unlink(missing_ok=True)
            return f"{op.memory_id}: divergence recovered ({op.revision_no} vs {theirs})"

    retire_op(paths, op.opid)
    staged.unlink(missing_ok=True)
    return f"{op.memory_id}: retired stale op {op.opid}"


__all__ = [
    "Capture",
    "Divergence",
    "WriteResult",
    "is_contested",
    "present_hash",
    "present_path",
    "read_present",
    "recover_pending_ops",
    "serving_disposition",
    "write",
]

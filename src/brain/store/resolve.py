"""Resolving a contested memory (BLUEPRINT.md §6.5).

Resolution is itself a multi-step mutation, so it needs the same discipline as any
other write — an earlier design specified no ordering for it at all.

    1. write op record (phase: resolving)
    2. publish the chosen branch as a NEW revision
       (never rewrite history — the losing branch stays in the log permanently)
    3. materialize present
    4. fsync
    5. ARCHIVE the conflict marker      <- LAST, after resolution is durable
    6. retire the op record

A crash anywhere leaves the memory contested with reads still failing closed — the
safe direction.

**The marker is archived, not deleted.** Deleting it destroys the audit trail of the
resolution and leaves recovery unable to tell "resolved" from "never contested".
Archiving gives recovery a decidable third state: *resolved-marker plus live op*
means finish the resolution; *resolved-marker plus settled revision* means retire the
op. Neither is inferable once the marker is simply gone.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from ..atomic import fsync_dir, rename_noreplace, write_staged
from ..config import Paths
from ..ids import new_opid
from . import revisions
from .memory import present_path
from .ops import OpPhase, create_op, memory_lock, new_op, retire_op, store_lock


class NotContested(ValueError):
    pass


class UnknownBranch(ValueError):
    pass


@dataclass(frozen=True)
class Resolution:
    memory_id: str
    taken: int
    new_revision: int
    archived_marker: str

    def __str__(self) -> str:
        return (
            f"{self.memory_id}: resolved by taking revision {self.taken}, "
            f"republished as {self.new_revision}. The losing branch remains in the log."
        )


def branches(paths: Paths, memory_id: str) -> list[int]:
    marker = paths.conflict_path(memory_id)
    if not marker.exists():
        raise NotContested(f"{memory_id} is not contested")
    record = json.loads(marker.read_text())
    return [int(b["revision"]) for b in record.get("branches", [])]


def resolve(
    paths: Paths,
    memory_id: str,
    take: int,
    *,
    workspace: str = "default",
    mtype: str = "semantic",
) -> Resolution:
    """Resolve a contested memory by adopting one branch.

    The losing branch is never removed. It stays in the append-only log because a
    resolution is a *decision*, not an erasure — and a decision you cannot review
    later is not auditable.
    """
    marker = paths.conflict_path(memory_id)
    if not marker.exists():
        raise NotContested(f"{memory_id} is not contested; nothing to resolve")

    available = branches(paths, memory_id)
    if take not in available:
        raise UnknownBranch(
            f"revision {take} is not a branch of this conflict; choose one of {available}"
        )
    chosen = revisions.read_revision(paths, memory_id, take)
    if chosen is None:
        raise UnknownBranch(f"revision {take} is recorded in the conflict but missing from disk")

    dest = present_path(paths, workspace, mtype, memory_id)

    with memory_lock(paths, memory_id):
        opid = new_opid()
        staged = paths.staging_path(opid)
        present_tmp = paths.staging_path(f"{opid}.present")

        write_staged(staged, chosen)
        op = new_op(
            opid,
            memory_id,
            staged,
            None,
            workspace=workspace,
            memory_type=mtype,
            phase=OpPhase.RESOLVING,
            reason=f"resolve: took revision {take}",
        )
        with store_lock(paths):
            create_op(paths, op)

        try:
            # 2. Republish as a NEW revision. History is appended to, never rewritten.
            n = revisions.publish(paths, memory_id, staged)

            # 3. Materialize present.
            write_staged(present_tmp, chosen)
            if not rename_noreplace(present_tmp, dest):
                from ..atomic import exchange

                exchange(dest, present_tmp)
            fsync_dir(dest.parent)

            # 5. Archive the marker LAST — a no-replace move, never a delete.
            paths.resolved_conflicts.mkdir(parents=True, exist_ok=True)
            archived = paths.resolved_conflicts / f"{memory_id}.{opid}.json"
            if not rename_noreplace(marker, archived):
                archived = paths.resolved_conflicts / f"{memory_id}.{opid}.dup.json"
                rename_noreplace(marker, archived)
            fsync_dir(paths.conflicts)
            fsync_dir(paths.resolved_conflicts)
        finally:
            # 6. Retire the op record.
            retire_op(paths, opid)
            staged.unlink(missing_ok=True)
            present_tmp.unlink(missing_ok=True)

    return Resolution(memory_id, take, n, str(archived))


__all__ = ["NotContested", "Resolution", "UnknownBranch", "branches", "resolve"]

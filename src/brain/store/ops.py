"""Durable intent records, and the locks that make them safe.

An earlier design computed every state from the file tree and treated the absence of
recovery metadata as elegance. **That was the bug.** The tree cannot distinguish
"an operation was in flight" from "nothing was happening", and no ordering of writes
creates that distinction, because the missing information is *intent* — which is not
a property of data at rest. Two concrete losses followed from its absence:

- crash after the exchange, before publishing the displaced branch: the tree reads
  ``SETTLED`` while an editor's bytes sit in staging awaiting GC;
- crash after publication, before materialization, then a direct edit: the tree reads
  ``UNWITNESSED``, the editor's branch is reconciled, and the already-published
  competing revision is buried with no conflict raised.

So intent is made durable **before every other durable effect**, and retired last.

Two distinct locks, with different jobs. Conflating them leaves a race open:

- ``memory_lock`` — held by a writer from staging through op retirement, and acquired
  by recovery before it takes over any op. Op records become visible *before*
  publication by design, so without this a recovery pass would race the writer that
  owns the op.
- ``store_lock`` — serializes staging-file GC against op creation. A grace period
  narrows that window but does not close it; it is a race, not a timing preference.
"""

from __future__ import annotations

import fcntl
import json
import os
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path

from ..atomic import fsync_dir, write_atomic
from ..config import Paths
from ..model import iso, utcnow


class OpPhase(StrEnum):
    STAGED = "staged"  # body staged, intent durable, nothing published yet
    PUBLISHED = "published"  # revision linked into the log
    EXCHANGED = "exchanged"  # present materialized
    RESOLVING = "resolving"  # conflict resolution in flight


@dataclass
class Op:
    opid: str
    memory_id: str
    phase: OpPhase
    staging_path: str
    expected_predecessor: str | None
    created_at: str
    revision_no: int | None = None
    workspace: str = "default"
    memory_type: str = "semantic"
    actor: str | None = None
    session: str | None = None
    reason: str | None = None

    def to_json(self) -> bytes:
        d = asdict(self)
        d["phase"] = self.phase.value
        return json.dumps(d, indent=2, sort_keys=True).encode() + b"\n"

    @classmethod
    def from_json(cls, raw: bytes) -> Op:
        d = json.loads(raw)
        d["phase"] = OpPhase(d["phase"])
        return cls(**d)


@contextmanager
def _flock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


@contextmanager
def memory_lock(paths: Paths, memory_id: str) -> Iterator[None]:
    """Held by a writer from staging through op retirement; taken by recovery too.

    Advisory only. A direct editor ignores it entirely — which is exactly why CAS,
    not this lock, is the safety mechanism for concurrent writes. This lock's job is
    narrower: keeping recovery and a live writer off the same op.
    """
    with _flock(paths.memory_lock(memory_id)):
        yield


@contextmanager
def store_lock(paths: Paths) -> Iterator[None]:
    """Serializes staging GC against op creation."""
    with _flock(paths.store_lock):
        yield


def create_op(paths: Paths, op: Op) -> None:
    """Make intent durable. Must be called before any other durable effect."""
    paths.ops.mkdir(parents=True, exist_ok=True)
    write_atomic(paths.op_path(op.opid), op.to_json())
    fsync_dir(paths.ops)


def advance_op(paths: Paths, op: Op, phase: OpPhase, **fields: object) -> Op:
    op.phase = phase
    for k, v in fields.items():
        setattr(op, k, v)
    write_atomic(paths.op_path(op.opid), op.to_json())
    return op


def retire_op(paths: Paths, opid: str) -> None:
    """Retire an op. Always the last step, so a crash replays the same recovery."""
    paths.op_path(opid).unlink(missing_ok=True)
    if paths.ops.exists():
        fsync_dir(paths.ops)


def read_op(paths: Paths, opid: str) -> Op | None:
    path = paths.op_path(opid)
    if not path.exists():
        return None
    try:
        return Op.from_json(path.read_bytes())
    except (json.JSONDecodeError, KeyError, ValueError):
        return None


def pending_ops(paths: Paths) -> list[Op]:
    if not paths.ops.exists():
        return []
    out = []
    for p in sorted(paths.ops.glob("*.json")):
        try:
            out.append(Op.from_json(p.read_bytes()))
        except (json.JSONDecodeError, KeyError, ValueError):
            continue
    return out


def new_op(
    opid: str,
    memory_id: str,
    staging_path: Path,
    expected_predecessor: str | None,
    *,
    workspace: str = "default",
    memory_type: str = "semantic",
    phase: OpPhase = OpPhase.STAGED,
    actor: str | None = None,
    session: str | None = None,
    reason: str | None = None,
    now: datetime | None = None,
) -> Op:
    return Op(
        opid=opid,
        memory_id=memory_id,
        phase=phase,
        staging_path=str(staging_path),
        expected_predecessor=expected_predecessor,
        created_at=iso(now or utcnow()),
        workspace=workspace,
        memory_type=memory_type,
        actor=actor,
        session=session,
        reason=reason,
    )


def gc_staging(paths: Paths) -> list[Path]:
    """Delete staging files no op references. Serialized against op creation.

    Returns what was removed. Anything an op still references is left alone — the GC
    deleting a staged file out from under a writer was the original mechanism of
    data loss here, and it is a race rather than a timing problem, so it needs the
    lock rather than a grace period.
    """
    removed: list[Path] = []
    with store_lock(paths):
        if not paths.staging.exists():
            return removed
        referenced = {Path(op.staging_path).name for op in pending_ops(paths)}
        for f in paths.staging.iterdir():
            if f.name not in referenced:
                f.unlink(missing_ok=True)
                removed.append(f)
        if removed:
            fsync_dir(paths.staging)
    return removed


def stuck_ops(paths: Paths) -> list[tuple[Op, str]]:
    """Ops that cannot be completed and need explicit repair.

    Surfaced in ``brain status``, never guessed away by GC. An op whose staging is
    gone and whose revision was never published is a real inconsistency; tidying it
    silently is how the one branch that mattered gets lost.
    """
    out = []
    for op in pending_ops(paths):
        if not Path(op.staging_path).exists() and op.revision_no is None:
            out.append((op, "staging file missing and no revision was published"))
    return out

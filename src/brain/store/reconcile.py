"""Capturing edits the write protocol never witnessed (BLUEPRINT.md §6.6).

Editing memory files directly — in an editor, or with an agent's ``Edit`` tool — is a
*supported path*, not a violation. It follows that some mutations will not pass
through the write protocol, and those have to be captured rather than discovered
later as corruption.

**Pull-based, never a watcher.** A filesystem watcher would race the write it is
meant to observe: it can start after the write, coalesce several saves, miss an
atomic rename, or crash between observing and recording.

**But pull-based still races the editor**, and an earlier draft claimed otherwise. A
hash taken mid-save reads a torn file. So the read must be verified *stable* before
anything is captured: read, wait, read again, and require the two to agree.

**Transaction time is an interval, not a point.** For a reconciled revision nobody
knows when the edit happened — only that it was somewhere between the last time the
file was seen intact and now. Recording a precise timestamp there would be a lie the
audit trail then rests on.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from ..config import Paths
from ..frontmatter import InvalidFrontmatter, content_hash, parse, serialize
from ..model import Capture, Disposition, iso, utcnow
from . import revisions
from .memory import serving_disposition
from .ops import memory_lock

#: Sidecars an editor leaves while a buffer is open. Their presence means "come back".
EDITOR_SIDECARS = (".swp", ".swx", ".swo", ".~lock", ".tmp")

STABLE_READ_DELAY_S = 0.05


@dataclass(frozen=True)
class Reconciled:
    memory_id: str
    revision_no: int
    recorded_from: str
    recorded_to: str

    def __str__(self) -> str:
        return (
            f"{self.memory_id}: captured unwitnessed edit as revision "
            f"{self.revision_no} (transaction time bounded to "
            f"[{self.recorded_from}, {self.recorded_to}])"
        )


@dataclass(frozen=True)
class Deferred:
    memory_id: str
    reason: str

    def __str__(self) -> str:
        return f"{self.memory_id}: deferred — {self.reason}"


def _has_editor_sidecar(path: Path) -> bool:
    parent, stem = path.parent, path.name
    for suffix in EDITOR_SIDECARS:
        if (parent / f".{stem}{suffix}").exists() or (parent / f"{stem}{suffix}").exists():
            return True
    return (parent / f"#{stem}#").exists()


def stable_read(path: Path, delay: float = STABLE_READ_DELAY_S) -> bytes | None:
    """Read a file only once it has stopped changing.

    Returns None when the file is in flux — deferred, never guessed at. Comparing
    ``(size, mtime_ns, hash)`` twice catches the mid-save window that a single read
    would silently sample.
    """
    if _has_editor_sidecar(path):
        return None
    try:
        st1 = path.stat()
        data1 = path.read_bytes()
    except (OSError, FileNotFoundError):
        return None
    time.sleep(delay)
    try:
        st2 = path.stat()
        data2 = path.read_bytes()
    except (OSError, FileNotFoundError):
        return None
    if (st1.st_size, st1.st_mtime_ns) != (st2.st_size, st2.st_mtime_ns) or data1 != data2:
        return None
    return data2


def _last_known_good(paths: Paths, memory_id: str) -> str:
    """The lower bound of the interval: when the newest revision was recorded."""
    newest = revisions.newest_revision(paths, memory_id)
    if newest is None:
        return iso(datetime.fromtimestamp(0).astimezone())
    try:
        m = parse(newest[1].decode("utf-8"))
        return iso(m.recorded_at)
    except (InvalidFrontmatter, UnicodeDecodeError):
        return iso(datetime.fromtimestamp(0).astimezone())


def reconcile_one(paths: Paths, memory_id: str, path: Path) -> Reconciled | Deferred | None:
    """Capture an unwitnessed edit to one memory, if there is one and it is stable."""
    with memory_lock(paths, memory_id):
        disposition = serving_disposition(paths, memory_id, path)
        if disposition is not Disposition.UNWITNESSED:
            return None

        data = stable_read(path)
        if data is None:
            return Deferred(memory_id, "file is still being written; will retry")
        try:
            parse(data.decode("utf-8"), path)
        except (InvalidFrontmatter, UnicodeDecodeError) as exc:
            return Deferred(memory_id, f"quarantined rather than captured: {exc}")

        # Re-check after the stable read: the disposition may have changed while we
        # waited, and capturing on a stale reading would publish a duplicate.
        if content_hash(data) == revisions.newest_hash(paths, memory_id):
            return None

        # Capture the lower bound BEFORE publishing. Reading it afterwards would
        # read the revision we just created and collapse the interval to a point.
        lower_bound = _last_known_good(paths, memory_id)
        upper_bound = iso(utcnow())

        # Stamp the capture class and the interval onto the bytes before publishing.
        # Without this the revision records `capture: mediated` by default, and the
        # audit trail loses the one distinction it exists to make: whether the write
        # protocol actually witnessed this transition or is inferring it after the
        # fact. Present is materialized from the same stamped bytes, so present and
        # newest revision stay byte-identical and the memory settles.
        stamped = serialize(
            parse(data.decode("utf-8"), path),
            capture=Capture.RECONCILED,
            recorded_from=lower_bound,
            recorded_to=upper_bound,
        ).encode()

        from ..atomic import write_atomic, write_staged

        staged = paths.staging_path(f"reconcile-{memory_id}")
        write_staged(staged, stamped)
        try:
            n = revisions.publish(paths, memory_id, staged)
            write_atomic(path, stamped)
        finally:
            staged.unlink(missing_ok=True)

        return Reconciled(
            memory_id=memory_id,
            revision_no=n,
            recorded_from=lower_bound,
            recorded_to=upper_bound,
        )


def reconcile_all(paths: Paths) -> list[Reconciled | Deferred]:
    """Sweep every memory for unwitnessed edits. Idempotent; safe to run any time."""
    out: list[Reconciled | Deferred] = []
    if not paths.memories.is_dir():
        return out
    for path in sorted(paths.memories.rglob("*.md")):
        if ".revisions" in path.parts or ".staging" in path.parts:
            continue
        try:
            m = parse(path.read_text(), path)
        except (InvalidFrontmatter, OSError, UnicodeDecodeError):
            continue  # quarantined; `brain validate` reports it
        if (result := reconcile_one(paths, m.id, path)) is not None:
            out.append(result)
    return out


__all__ = ["Capture", "Deferred", "Reconciled", "reconcile_all", "reconcile_one", "stable_read"]

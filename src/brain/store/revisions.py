"""The append-only revision log.

**Revisions hold every committed state, including the current one.** The present file
is a materialized view of the newest revision. This is not a detail: an earlier design
described revisions as holding *prior* states, which made the unwitnessed-edit
detector in ``reconcile`` compare two things that are unequal by construction for
every memory, always. The detector would have fired on everything, forever. It took
writing the protocol out to see it.

Publication is by ``link()`` from an already-complete, already-fsynced staging file.
Neither of the obvious alternatives works:

- ``rename()`` silently overwrites, so a crash-retry or a concurrent allocator could
  destroy immutable history using the mechanism meant to preserve it;
- ``O_CREAT|O_EXCL`` on the final path leaves a *torn* file permanently occupying a
  revision number after a crash mid-write — worse, because the number is unusable.
"""

from __future__ import annotations

import re
from pathlib import Path

from ..atomic import publish_link
from ..config import Paths
from ..frontmatter import content_hash

_REV = re.compile(r"^(\d{6})\.md$")
MAX_ALLOC_ATTEMPTS = 1000


def revision_numbers(paths: Paths, memory_id: str) -> list[int]:
    d = paths.revision_dir(memory_id)
    if not d.is_dir():
        return []
    out = []
    for f in d.iterdir():
        if m := _REV.match(f.name):
            out.append(int(m.group(1)))
    return sorted(out)


def newest_number(paths: Paths, memory_id: str) -> int | None:
    nums = revision_numbers(paths, memory_id)
    return nums[-1] if nums else None


def read_revision(paths: Paths, memory_id: str, n: int) -> bytes | None:
    p = paths.revision_path(memory_id, n)
    return p.read_bytes() if p.exists() else None


def newest_revision(paths: Paths, memory_id: str) -> tuple[int, bytes] | None:
    n = newest_number(paths, memory_id)
    if n is None:
        return None
    data = read_revision(paths, memory_id, n)
    return (n, data) if data is not None else None


def newest_hash(paths: Paths, memory_id: str) -> str | None:
    rev = newest_revision(paths, memory_id)
    return content_hash(rev[1]) if rev else None


def revision_hashes(paths: Paths, memory_id: str) -> dict[str, int]:
    """Map content hash -> revision number, for "does this content exist in history?"."""
    out: dict[str, int] = {}
    for n in revision_numbers(paths, memory_id):
        if data := read_revision(paths, memory_id, n):
            out.setdefault(content_hash(data), n)
    return out


def publish(paths: Paths, memory_id: str, staged: Path) -> int:
    """Link a staged file into the revision log. Returns the revision number.

    ``EEXIST`` means another writer claimed the number first: increment and retry.
    Gaps are legal and mean an abandoned allocation.
    """
    start = (newest_number(paths, memory_id) or 0) + 1
    for n in range(start, start + MAX_ALLOC_ATTEMPTS):
        dest = paths.revision_path(memory_id, n)
        if publish_link(staged, dest):
            return n
    raise RuntimeError(
        f"could not allocate a revision number for {memory_id} after "
        f"{MAX_ALLOC_ATTEMPTS} attempts starting at {start}"
    )


def find_by_hash(paths: Paths, memory_id: str, digest: str) -> int | None:
    return revision_hashes(paths, memory_id).get(digest)


def all_memory_ids(paths: Paths) -> list[str]:
    if not paths.revisions.is_dir():
        return []
    return sorted(d.name for d in paths.revisions.iterdir() if d.is_dir())

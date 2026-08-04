"""The append-only event log (BLUEPRINT.md §6.4).

Evidence pointers have to resolve against *something*. This is that something: an
append-only JSONL log, segmented by day, holding the messages, tool calls,
observations, and feedback that memories are extracted from. It is canonical, it is a
file class, and without it "every claim traces to a source span" is a slogan rather
than a property.

Per-line checksums, but **no hash chain** — deliberately. Events are evidence, not an
authority. A tombstone ledger has to prove nobody rewrote it; an event segment only
has to prove a line was not corrupted in transit. Chaining would also make erasure
worse, and events *are* erasable.

**Erasure is by redaction fork, never in-place rewrite** (§11.5.4). Editing a segment
would violate invariant §5.1, and it silently invalidates every span offset pointing
into it.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from ..atomic import fsync_dir, write_atomic
from ..config import Paths
from ..ids import new_ulid
from ..model import iso, utcnow


@dataclass(frozen=True)
class Event:
    id: str
    kind: str
    occurred_at: str
    payload: dict[str, Any]
    checksum: str
    session: str | None = None
    actor: str | None = None

    @property
    def text(self) -> str:
        """The body a span points into."""
        return str(self.payload.get("text", ""))


def _checksum(record: dict[str, Any]) -> str:
    material = json.dumps(record, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(material.encode()).hexdigest()[:32]


def segment_path(paths: Paths, when: date | None = None) -> Path:
    day = when or utcnow().date()
    return paths.events / f"{day.isoformat()}.jsonl"


def append(
    paths: Paths,
    kind: str,
    payload: dict[str, Any],
    *,
    session: str | None = None,
    actor: str | None = None,
    when: date | None = None,
) -> Event:
    """Append an event and return it. The id is what evidence pointers reference."""
    record: dict[str, Any] = {
        "id": new_ulid(),
        "kind": kind,
        "occurred_at": iso(utcnow()),
        "session": session,
        "actor": actor,
        "payload": payload,
    }
    line = dict(record, checksum=_checksum(record))
    path = segment_path(paths, when)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.write(fd, (json.dumps(line, sort_keys=True, separators=(",", ":")) + "\n").encode())
        os.fsync(fd)
    finally:
        os.close(fd)
    fsync_dir(path.parent)
    return Event(
        str(record["id"]),
        kind,
        str(record["occurred_at"]),
        payload,
        str(line["checksum"]),
        session,
        actor,
    )


def read_segment(path: Path) -> list[Event]:
    """Read one segment. A torn trailing line is discarded; a corrupt one is skipped."""
    if not path.exists():
        return []
    out: list[Event] = []
    lines = path.read_text().splitlines()
    for i, line in enumerate(lines):
        if not line.strip():
            continue
        try:
            raw = json.loads(line)
        except json.JSONDecodeError:
            if i == len(lines) - 1:
                break  # torn trailing write; the append never completed
            continue
        checksum = raw.pop("checksum", "")
        if _checksum(raw) != checksum:
            continue  # corrupted line; evidence that cannot be verified is not evidence
        out.append(
            Event(
                raw["id"],
                raw["kind"],
                raw["occurred_at"],
                raw.get("payload", {}),
                checksum,
                raw.get("session"),
                raw.get("actor"),
            )
        )
    return out


def all_events(paths: Paths) -> list[Event]:
    if not paths.events.is_dir():
        return []
    out: list[Event] = []
    for seg in sorted(paths.events.glob("*.jsonl")):
        out.extend(read_segment(seg))
    return out


def find(paths: Paths, event_id: str) -> tuple[Event, Path] | None:
    """Locate an event by id, with the segment holding it."""
    if not paths.events.is_dir():
        return None
    for seg in sorted(paths.events.glob("*.jsonl")):
        for e in read_segment(seg):
            if e.id == event_id:
                return e, seg
    return None


def redaction_fork(paths: Paths, segment: Path, drop_event_ids: set[str]) -> tuple[Path, int]:
    """Erase events from a segment by rewriting it to a NEW file, never in place.

    Create-then-delete, so a crash leaves either both files or the original — never a
    partially erased one. Explicitly lossy for the erased events and it breaks their
    ids, which is what erasure means.
    """
    kept = [e for e in read_segment(segment) if e.id not in drop_event_ids]
    dropped = len(read_segment(segment)) - len(kept)
    if dropped == 0:
        return segment, 0

    forked = segment.with_name(f"{segment.stem}.r{new_ulid()[:8]}.jsonl")
    body = "".join(
        json.dumps(
            {
                "id": e.id,
                "kind": e.kind,
                "occurred_at": e.occurred_at,
                "session": e.session,
                "actor": e.actor,
                "payload": e.payload,
                "checksum": e.checksum,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
        for e in kept
    )
    write_atomic(forked, body.encode())
    segment.unlink()
    fsync_dir(segment.parent)
    return forked, dropped

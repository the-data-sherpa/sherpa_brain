"""Content-addressed original evidence (BLUEPRINT.md §6.1, §11.5.4).

Original bytes are stored by digest and **never edited in place**. That is what makes
a claim checkable a year later rather than merely attributed: the source is still the
source, byte for byte, and its identity *is* its content.

Preserved alongside the bytes: the original URI, media type, capture time, and parser
version. A blob without those is an orphan — you can prove it has not changed and not
what it was.

Erasing part of a multi-subject artifact uses a **redaction fork**: derive a new blob
without the erased subject, tombstone the old digest, delete the original. The old
digest is *tombstoned rather than silently reused*, so a stale reference fails loudly
instead of resolving to altered content.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..atomic import fsync_dir, write_atomic
from ..config import Paths
from ..model import iso, utcnow

META = "meta.json"


@dataclass(frozen=True)
class Artifact:
    digest: str
    path: Path
    media_type: str
    source_uri: str | None = None
    captured_at: str = ""
    parser_version: str = "raw/1"
    size: int = 0
    tags: list[str] = field(default_factory=list)

    @property
    def ref(self) -> str:
        return f"artifact:{self.digest}"


def digest_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def blob_dir(paths: Paths, digest: str) -> Path:
    return paths.artifacts / "sha256" / digest[:2] / digest[2:4] / digest


def store(
    paths: Paths,
    data: bytes,
    *,
    media_type: str = "text/plain",
    source_uri: str | None = None,
    filename: str = "content",
    parser_version: str = "raw/1",
    tags: list[str] | None = None,
) -> Artifact:
    """Store bytes by digest. Idempotent: the same bytes yield the same artifact."""
    digest = digest_bytes(data)
    d = blob_dir(paths, digest)
    blob = d / filename
    if blob.exists():
        return read(paths, digest) or _artifact(digest, blob, media_type, source_uri, len(data))

    d.mkdir(parents=True, exist_ok=True)
    write_atomic(blob, data)
    meta: dict[str, Any] = {
        "digest": digest,
        "filename": filename,
        "media_type": media_type,
        "source_uri": source_uri,
        "captured_at": iso(utcnow()),
        "parser_version": parser_version,
        "size": len(data),
        "tags": tags or [],
    }
    write_atomic(d / META, json.dumps(meta, indent=2, sort_keys=True).encode() + b"\n")
    fsync_dir(d)
    return Artifact(
        digest,
        blob,
        media_type,
        source_uri,
        str(meta["captured_at"]),
        parser_version,
        len(data),
        tags or [],
    )


def _artifact(
    digest: str, blob: Path, media_type: str, source_uri: str | None, size: int
) -> Artifact:
    return Artifact(digest, blob, media_type, source_uri, "", "raw/1", size)


def read(paths: Paths, digest: str) -> Artifact | None:
    d = blob_dir(paths, digest)
    meta_path = d / META
    if not meta_path.exists():
        return None
    meta = json.loads(meta_path.read_text())
    blob = d / meta.get("filename", "content")
    return Artifact(
        digest=digest,
        path=blob,
        media_type=meta.get("media_type", "application/octet-stream"),
        source_uri=meta.get("source_uri"),
        captured_at=meta.get("captured_at", ""),
        parser_version=meta.get("parser_version", "raw/1"),
        size=int(meta.get("size", 0)),
        tags=list(meta.get("tags", [])),
    )


def verify(paths: Paths, digest: str) -> bool:
    """Confirm the stored bytes still hash to their name.

    Content addressing makes tampering detectable for free; not checking would be
    leaving the property on the table.
    """
    a = read(paths, digest)
    if a is None or not a.path.exists():
        return False
    return digest_bytes(a.path.read_bytes()) == digest


def redaction_fork(paths: Paths, digest: str, retained: bytes) -> Artifact | None:
    """Erase part of an artifact by deriving a NEW blob and tombstoning the old digest.

    Explicitly lossy for the erased subject, and it explicitly breaks the old digest.
    That is what erasure means; pretending otherwise is how deletion quietly fails.
    """
    old = read(paths, digest)
    if old is None:
        return None
    new = store(
        paths,
        retained,
        media_type=old.media_type,
        source_uri=old.source_uri,
        filename=old.path.name,
        parser_version=f"{old.parser_version}+redacted",
        tags=[*old.tags, "redaction-fork"],
    )
    shutil.rmtree(blob_dir(paths, digest), ignore_errors=True)
    fsync_dir(paths.artifacts)
    return new


def purge(paths: Paths, digest: str) -> bool:
    d = blob_dir(paths, digest)
    if not d.exists():
        return False
    shutil.rmtree(d, ignore_errors=True)
    return not d.exists()


def all_digests(paths: Paths) -> list[str]:
    root = paths.artifacts / "sha256"
    if not root.is_dir():
        return []
    return sorted(p.parent.name for p in root.rglob(META))


def _redacted_span(text: str, start: int | None, end: int | None) -> tuple[str, list[str]]:
    """Resolve a span and mask any credentials in it.

    Artifacts are stored verbatim — an imported document is evidence and is never
    rewritten on the way in. That makes this the read path most likely to surface a
    secret, so masking here is not belt-and-braces.
    """
    from .. import scan

    excerpt = resolve_span(text, start, end)
    masked, findings = scan.redact(excerpt)
    return masked, sorted({f.kind for f in findings})


def resolve_span(text: str, start: int | None, end: int | None) -> str:
    """Return the lines a span points at.

    Spans are 1-indexed line numbers because that is what a human reading a diff or a
    code host uses, and evidence exists to be checked by a human.
    """
    if start is None:
        return text
    lines = text.splitlines()
    lo = max(0, start - 1)
    hi = min(len(lines), end if end is not None else start)
    return "\n".join(lines[lo:hi])


def resolve_evidence(paths: Paths, ref: str, start: int | None, end: int | None) -> dict[str, Any]:
    """Resolve an evidence pointer to the actual source text it names.

    This is the operation that makes evidence real rather than decorative: without
    it, a pointer is a string nobody ever follows.
    """
    from . import events

    kind, _, ident = ref.partition(":")
    if kind == "event":
        found = events.find(paths, ident)
        if found is None:
            return {"ref": ref, "resolved": False, "reason": "event not found"}
        event, segment = found
        excerpt, redacted = _redacted_span(event.text, start, end)
        return {
            "ref": ref,
            "resolved": True,
            "kind": "event",
            "occurred_at": event.occurred_at,
            "segment": str(segment),
            "excerpt": excerpt,
            **({"redacted": redacted} if redacted else {}),
        }
    if kind == "artifact":
        a = read(paths, ident)
        if a is None or not a.path.exists():
            return {"ref": ref, "resolved": False, "reason": "artifact not found"}
        if not verify(paths, ident):
            return {"ref": ref, "resolved": False, "reason": "digest mismatch — artifact altered"}
        try:
            text = a.path.read_text()
        except UnicodeDecodeError:
            return {
                "ref": ref,
                "resolved": True,
                "kind": "artifact",
                "media_type": a.media_type,
                "excerpt": f"<{a.size} bytes of {a.media_type}>",
            }
        excerpt, redacted = _redacted_span(text, start, end)
        return {
            "ref": ref,
            "resolved": True,
            "kind": "artifact",
            "source_uri": a.source_uri,
            "media_type": a.media_type,
            "captured_at": a.captured_at,
            "excerpt": excerpt,
            **({"redacted": redacted} if redacted else {}),
        }
    return {"ref": ref, "resolved": False, "reason": f"unknown pointer kind {kind!r}"}


def import_file(paths: Paths, src: Path, *, source_uri: str | None = None) -> Artifact:
    import mimetypes

    media_type, _ = mimetypes.guess_type(src.name)
    return store(
        paths,
        src.read_bytes(),
        media_type=media_type or "application/octet-stream",
        source_uri=source_uri or src.resolve().as_uri(),
        filename=src.name,
    )


__all__ = [
    "Artifact",
    "all_digests",
    "blob_dir",
    "digest_bytes",
    "import_file",
    "purge",
    "read",
    "redaction_fork",
    "resolve_evidence",
    "resolve_span",
    "store",
    "verify",
]

"""YAML front matter: parse, serialize, validate.

**Fail closed** (BLUEPRINT.md §7.6). Because humans and agents edit these files
directly, invalid front matter is a normal occurrence rather than an exceptional one.
A file that fails validation is *quarantined* — excluded from all retrieval and
surfaced by ``brain validate`` with a nonzero exit.

It is never best-effort parsed and never silently skipped. Silent skipping is the
dangerous option: it makes a memory disappear from answers without telling anyone,
which from the user's side is indistinguishable from the brain having forgotten.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import yaml

from .model import (
    Capture,
    Evidence,
    Memory,
    MemoryType,
    ProvenanceClass,
    Status,
    Volatility,
    iso,
    utcnow,
)

DELIM = "---"
REQUIRED = ("id", "type", "provenance_class", "volatility", "valid_from", "evidence")


class InvalidFrontmatter(ValueError):
    """The file cannot be interpreted as a memory. It is quarantined, not guessed at."""

    def __init__(self, reason: str, path: Path | None = None) -> None:
        self.reason = reason
        self.path = path
        super().__init__(f"{path}: {reason}" if path else reason)


def content_hash(data: bytes | str) -> str:
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def split(text: str) -> tuple[dict[str, Any], str]:
    """Split a document into (front matter, body). Raises on anything malformed."""
    if not text.startswith(DELIM):
        raise InvalidFrontmatter("file does not start with a '---' front-matter block")
    rest = text[len(DELIM) :].lstrip("\n")
    end = rest.find(f"\n{DELIM}")
    if end == -1:
        raise InvalidFrontmatter("front-matter block is not terminated by '---'")
    raw, body = rest[:end], rest[end + len(DELIM) + 1 :]
    try:
        meta = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise InvalidFrontmatter(f"front matter is not valid YAML: {exc}") from exc
    if meta is None:
        meta = {}
    if not isinstance(meta, dict):
        raise InvalidFrontmatter("front matter must be a mapping")
    return meta, body.lstrip("\n")


def _as_date(value: Any, field: str) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value[:10])
        except ValueError as exc:
            raise InvalidFrontmatter(f"{field}: {value!r} is not an ISO date") from exc
    raise InvalidFrontmatter(f"{field}: expected a date, got {type(value).__name__}")


def _as_datetime(value: Any) -> datetime:
    """Parse ``recorded_at`` from front matter. Falls back to the epoch, not to now.

    Falling back to ``utcnow()`` would silently make every rebuild produce a
    different index for the same tree.
    """
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, str):
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return dt if dt.tzinfo else dt.replace(tzinfo=UTC)
        except ValueError:
            pass
    return datetime.fromtimestamp(0, UTC)


def _enum(cls: type, value: Any, field: str) -> Any:
    try:
        return cls(value)
    except ValueError as exc:
        allowed = ", ".join(m.value for m in cls)  # type: ignore[attr-defined]
        raise InvalidFrontmatter(f"{field}: {value!r} is not one of: {allowed}") from exc


def parse(text: str, path: Path | None = None) -> Memory:
    """Parse a memory document. Any defect raises ``InvalidFrontmatter``."""
    try:
        meta, body = split(text)
    except InvalidFrontmatter as exc:
        raise InvalidFrontmatter(exc.reason, path) from exc

    missing = [f for f in REQUIRED if f not in meta or meta[f] in (None, "", [])]
    if missing:
        raise InvalidFrontmatter(f"missing required field(s): {', '.join(missing)}", path)

    ev_raw = meta["evidence"]
    if isinstance(ev_raw, str):
        ev_raw = [ev_raw]
    if not isinstance(ev_raw, list):
        raise InvalidFrontmatter("evidence: expected a list of source pointers", path)

    try:
        return Memory(
            id=str(meta["id"]),
            type=_enum(MemoryType, meta["type"], "type"),
            provenance_class=_enum(ProvenanceClass, meta["provenance_class"], "provenance_class"),
            volatility=_enum(Volatility, meta["volatility"], "volatility"),
            valid_from=_as_date(meta["valid_from"], "valid_from"),
            evidence=[Evidence.parse(str(e)) for e in ev_raw],
            body=body,
            status=_enum(Status, meta.get("status", "confirmed"), "status"),
            workspace=str(meta.get("workspace", "default")),
            owner=meta.get("owner"),
            valid_to=_as_date(meta["valid_to"], "valid_to") if meta.get("valid_to") else None,
            supersedes=meta.get("supersedes"),
            tags=[str(t) for t in meta.get("tags", [])],
            review_by=_as_date(meta["review_by"], "review_by") if meta.get("review_by") else None,
            sensitivity=meta.get("sensitivity"),
            # Read from the file, never regenerated. A value invented at parse time
            # would make the index non-deterministic across rebuilds, which is the
            # one property the derivability test exists to protect.
            recorded_at=_as_datetime(meta.get("recorded_at")),
        )
    except InvalidFrontmatter as exc:
        raise InvalidFrontmatter(exc.reason, path) from exc


def serialize(memory: Memory, *, capture: Capture | None = None, opid: str | None = None) -> str:
    """Render a memory back to a document. Field order is stable so diffs stay readable."""
    meta: dict[str, Any] = {
        "id": memory.id,
        "type": memory.type.value,
        "provenance_class": memory.provenance_class.value,
        "volatility": memory.volatility.value,
        "valid_from": memory.valid_from.isoformat(),
        "evidence": [str(e) for e in memory.evidence],
        "status": memory.status.value,
        "workspace": memory.workspace,
    }
    optional = {
        "owner": memory.owner,
        "valid_to": memory.valid_to.isoformat() if memory.valid_to else None,
        "supersedes": memory.supersedes,
        "tags": memory.tags or None,
        "review_by": memory.review_by.isoformat() if memory.review_by else None,
        "sensitivity": memory.sensitivity,
        "capture": capture.value if capture else None,
        "opid": opid,
        "recorded_at": iso(memory.recorded_at or utcnow()),
    }
    meta.update({k: v for k, v in optional.items() if v is not None})

    dumped = yaml.safe_dump(meta, sort_keys=False, allow_unicode=True, default_flow_style=False)
    body = memory.body.strip()
    return f"{DELIM}\n{dumped}{DELIM}\n\n{body}\n"


def peek_id(text: str) -> str | None:
    """Read just the ``id`` without full validation, for recovery and reindexing."""
    try:
        meta, _ = split(text)
    except InvalidFrontmatter:
        return None
    value = meta.get("id")
    return str(value) if value else None

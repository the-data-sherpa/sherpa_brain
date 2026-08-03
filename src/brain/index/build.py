"""Build the derived index by scanning canonical files.

Deterministic and idempotent: the same tree always produces the same logical state,
regardless of scan order or mtimes. That property is what makes "SQLite holds zero
canonical bytes" a fact rather than a claim, and it is asserted directly in
``tests/integration/test_index_derived.py`` — including a run that shuffles scan
order and touches mtimes, so that nothing may quietly come to depend on either.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from ..config import Paths
from ..frontmatter import InvalidFrontmatter, content_hash, parse, split
from ..model import Capture, Disposition
from ..store import memory as mem
from ..store import revisions

SCHEMA = Path(__file__).with_name("schema.sql")


def connect(paths: Paths) -> sqlite3.Connection:
    paths.root.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(paths.db)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA.read_text())
    return conn


def _title_of(body: str) -> str:
    for line in body.splitlines():
        if stripped := line.strip().lstrip("#").strip():
            return stripped[:200]
    return ""


def _iter_memory_files(paths: Paths) -> list[Path]:
    if not paths.memories.is_dir():
        return []
    out = [
        p
        for p in paths.memories.rglob("*.md")
        if ".revisions" not in p.parts and ".staging" not in p.parts
    ]
    return sorted(out)


def rebuild(paths: Paths, conn: sqlite3.Connection | None = None) -> dict[str, int]:
    """Drop every derived row and rebuild from files. Safe to run at any time."""
    own = conn is None
    conn = conn or connect(paths)
    counts = {"memories": 0, "revisions": 0, "quarantined": 0, "tombstones": 0}
    try:
        for table in (
            "memory_index",
            "evidence_link",
            "relations",
            "revision_index",
            "tombstone_index",
            "delivery_state",
            "purge_state",
            "search",
        ):
            conn.execute(f"DELETE FROM {table}")

        tombstoned = _load_tombstones(conn, paths)
        counts["tombstones"] = len(tombstoned)

        for path in _iter_memory_files(paths):
            data = path.read_bytes()
            try:
                m = parse(data.decode("utf-8"), path)
            except (InvalidFrontmatter, UnicodeDecodeError):
                counts["quarantined"] += 1
                continue
            if m.id in tombstoned:
                continue  # a tombstoned subject is never indexed, never served

            disposition = mem.serving_disposition(paths, m.id, path)
            newest = revisions.newest_number(paths, m.id)
            conn.execute(
                "INSERT OR REPLACE INTO memory_index (id, file_path, workspace, type, "
                "provenance, volatility, status, disposition, valid_from, valid_to, "
                "review_by, owner, title, content_hash, newest_rev) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    m.id,
                    str(path),
                    m.workspace,
                    m.type.value,
                    m.provenance_class.value,
                    m.volatility.value,
                    m.status.value,
                    disposition.value,
                    m.valid_from.isoformat(),
                    m.valid_to.isoformat() if m.valid_to else None,
                    m.review_by.isoformat() if m.review_by else None,
                    m.owner,
                    _title_of(m.body),
                    content_hash(data),
                    newest,
                ),
            )
            for e in m.evidence:
                conn.execute(
                    "INSERT OR REPLACE INTO evidence_link "
                    "(memory_id, source_ref, span_start, span_end) VALUES (?,?,?,?)",
                    (m.id, e.ref, e.span_start or 0, e.span_end),
                )
            # A contested memory is indexed for listing but never made searchable —
            # reads fail closed until a human resolves it.
            if disposition is not Disposition.CONTESTED:
                conn.execute(
                    "INSERT INTO search (memory_id, workspace, title, body, tags) "
                    "VALUES (?,?,?,?,?)",
                    (m.id, m.workspace, _title_of(m.body), m.body, " ".join(m.tags)),
                )
            counts["memories"] += 1

        counts["revisions"] = _load_revisions(conn, paths, tombstoned)
        conn.commit()
    finally:
        if own:
            conn.close()
    return counts


def _load_revisions(conn: sqlite3.Connection, paths: Paths, tombstoned: set[str]) -> int:
    n = 0
    for mid in revisions.all_memory_ids(paths):
        if mid in tombstoned:
            continue
        for num in revisions.revision_numbers(paths, mid):
            path = paths.revision_path(mid, num)
            data = revisions.read_revision(paths, mid, num)
            if data is None:
                continue
            # Every field below comes from the file. Nothing is invented here, or
            # the index would stop being a function of the tree.
            capture, opid, recorded = Capture.MEDIATED.value, None, ""
            try:
                meta, _ = split(data.decode("utf-8"))
                capture = str(meta.get("capture", Capture.MEDIATED.value))
                opid = meta.get("opid")
                recorded = str(meta.get("recorded_at", ""))
            except (InvalidFrontmatter, UnicodeDecodeError):
                pass
            conn.execute(
                "INSERT OR REPLACE INTO revision_index (memory_id, revision_no, file_path, "
                "content_hash, predecessor_hash, capture, recorded_from, recorded_to, opid) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    mid,
                    num,
                    str(path),
                    content_hash(data),
                    None,
                    capture,
                    recorded,
                    recorded,
                    opid,
                ),
            )
            n += 1
    return n


def _load_tombstones(conn: sqlite3.Connection, paths: Paths) -> set[str]:
    out: set[str] = set()
    if not paths.tombstones.exists():
        return out
    for line in paths.tombstones.read_text().splitlines():
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue  # a truncated trailing line is discarded, with the chain check reporting it
        sid = entry.get("subject_id")
        if not sid:
            continue
        out.add(sid)
        conn.execute(
            "INSERT OR REPLACE INTO tombstone_index "
            "(subject_id, subject_kind, tombstoned_at, chain_seq) VALUES (?,?,?,?)",
            (
                sid,
                entry.get("subject_kind", "memory"),
                entry.get("tombstoned_at", ""),
                entry.get("seq", 0),
            ),
        )
    return out


def logical_snapshot(conn: sqlite3.Connection) -> str:
    """A normalized projection for equivalence testing.

    Deliberately excludes rowids, ``sqlite_sequence``, and insertion order — a
    byte-identical dump is simultaneously too strict (those differ for reasons
    unrelated to correctness) and too weak (it cannot detect canonical data hiding
    in the index).
    """
    parts: list[str] = []
    for table, order in (
        ("memory_index", "id"),
        ("evidence_link", "memory_id, source_ref, span_start"),
        ("relations", "src, rel, dst"),
        ("revision_index", "memory_id, revision_no"),
        ("tombstone_index", "subject_id"),
        ("delivery_state", "subject_id"),
        ("purge_state", "subject_id"),
    ):
        rows = conn.execute(f"SELECT * FROM {table} ORDER BY {order}").fetchall()
        parts.append(table)
        parts.extend(json.dumps(dict(r), sort_keys=True) for r in rows)
    search_rows = conn.execute(
        "SELECT memory_id, workspace, title, body, tags FROM search ORDER BY memory_id"
    ).fetchall()
    parts.append("search")
    parts.extend(json.dumps(dict(r), sort_keys=True) for r in search_rows)
    return "\n".join(parts)

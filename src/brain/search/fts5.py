"""Rung 1 — SQLite FTS5 with BM25 ranking.

BM25 remains a strong zero-shot baseline; nothing in the recent literature overturned
that. Dense retrieval is complementary rather than superior, and it is gated behind a
measured trigger (ADR 0002) rather than added because it is expected.

Tombstoned and contested memories are never returned. Both are enforced at the index
level as well, so this is defence in depth rather than the only check.
"""

from __future__ import annotations

import sqlite3

from ..config import Paths
from ..index import build
from .base import Hit, SearchBackend

_FTS_SPECIAL = str.maketrans({c: " " for c in '"*():^-'})


def _sanitize(query: str) -> str:
    """Make a user query safe for the FTS5 grammar without silently changing intent.

    FTS5 raises on unbalanced quotes and stray operators. Quoting each term keeps a
    plain-language query working without the user needing to know the syntax.
    """
    terms = [t for t in query.translate(_FTS_SPECIAL).split() if t]
    if not terms:
        return ""
    return " OR ".join(f'"{t}"' for t in terms)


class Fts5Backend(SearchBackend):
    rung = 1
    name = "fts5"

    def __init__(self, paths: Paths, conn: sqlite3.Connection | None = None) -> None:
        self.paths = paths
        self._conn = conn

    def _connection(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = build.connect(self.paths)
        return self._conn

    def search(
        self,
        query: str,
        *,
        workspace: str | None = "default",
        limit: int = 10,
        as_of: str | None = None,
    ) -> list[Hit]:
        match = _sanitize(query)
        if not match:
            return []
        conn = self._connection()

        sql = [
            "SELECT s.memory_id, s.workspace, s.title,",
            "       snippet(search, 3, '', '', ' … ', 24) AS excerpt,",
            "       bm25(search) AS score, m.file_path, m.disposition,",
            "       m.valid_from, m.valid_to",
            "FROM search s JOIN memory_index m ON m.id = s.memory_id",
            "WHERE search MATCH ?",
            # Contested memories fail closed; tombstoned ones are never indexed.
            "  AND m.disposition != 'contested'",
            "  AND m.status != 'tombstoned'",
        ]
        params: list[object] = [match]
        if workspace is not None:
            sql.append("  AND s.workspace = ?")
            params.append(workspace)
        if as_of is not None:
            sql.append("  AND m.valid_from <= ?")
            sql.append("  AND (m.valid_to IS NULL OR m.valid_to > ?)")
            params.extend([as_of, as_of])
        sql.append("ORDER BY score LIMIT ?")
        params.append(limit)

        rows = conn.execute("\n".join(sql), params).fetchall()
        hits = []
        for r in rows:
            evidence = [
                f"{ref}#L{s}-{e}" if s else ref
                for ref, s, e in conn.execute(
                    "SELECT source_ref, span_start, span_end FROM evidence_link "
                    "WHERE memory_id = ?",
                    (r["memory_id"],),
                )
            ]
            hits.append(
                Hit(
                    memory_id=r["memory_id"],
                    workspace=r["workspace"],
                    title=r["title"] or "",
                    excerpt=(r["excerpt"] or "").strip(),
                    score=-float(r["score"]),  # bm25() is negative; lower is better
                    path=r["file_path"],
                    evidence=evidence,
                )
            )
        return hits

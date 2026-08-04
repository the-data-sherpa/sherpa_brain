"""Rung 1 — SQLite FTS5 with BM25 ranking.

BM25 remains a strong zero-shot baseline; nothing in the recent literature overturned
that. Dense retrieval is complementary rather than superior, and it is gated behind a
measured trigger (ADR 0002) rather than added because it is expected.

Tombstoned and contested memories are never returned. Both are enforced at the index
level as well, so this is defence in depth rather than the only check.
"""

from __future__ import annotations

import sqlite3

from .. import scan
from ..config import Paths
from ..index import build
from .base import Hit, SearchBackend

_FTS_SPECIAL = str.maketrans({c: " " for c in '"*():^-'})


#: Terms shorter than this are matched exactly. Prefix-matching two-letter tokens
#: matches nearly everything and destroys precision, which §9.4 says is the objective.
_MIN_PREFIX_LEN = 4


def _sanitize(query: str) -> str:
    """Make a user query safe for the FTS5 grammar without silently changing intent.

    FTS5 raises on unbalanced quotes and stray operators. Quoting each term keeps a
    plain-language query working without the user needing to know the syntax.

    **Prefix matching on terms of four characters or more.** FTS5's tokenizer does not
    stem, so ``service`` does not match ``services`` and ``deploy`` does not match
    ``deployment`` — and morphological variation is by far the most common lexical
    miss on a personal corpus, where you rarely phrase a question with the exact word
    you used months ago.

    This is still rung 1: BM25 over a lexical index, no embeddings, no model. It does
    not bypass the rung-2 trigger in ADR 0002 — it is what rung 1 should have been
    doing all along. Genuine paraphrase (``database`` for ``Postgres``) still misses,
    and that miss is the signal the trigger watches for.
    """
    terms = [t for t in query.translate(_FTS_SPECIAL).split() if t]
    if not terms:
        return ""
    return " OR ".join(f'"{t}"*' if len(t) >= _MIN_PREFIX_LEN else f'"{t}"' for t in terms)


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
            # Redact on the way out. Anything that entered before the scanner
            # existed, or arrived inside an ingested artifact, must not reach a model.
            title, _ = scan.redact(r["title"] or "")
            excerpt, redacted = scan.redact((r["excerpt"] or "").strip())
            hits.append(
                Hit(
                    memory_id=r["memory_id"],
                    workspace=r["workspace"],
                    title=title,
                    excerpt=excerpt,
                    redacted=[f.kind for f in redacted],
                    score=-float(r["score"]),  # bm25() is negative; lower is better
                    path=r["file_path"],
                    evidence=evidence,
                )
            )
        return hits

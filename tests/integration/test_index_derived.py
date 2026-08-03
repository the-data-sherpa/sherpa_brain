"""The index holds zero canonical bytes — enforced, not asserted.

Three tests, because the obvious single test proves neither claim. A byte-identical
dump before and after ``reindex`` is simultaneously too strict (FTS5 rowids,
``sqlite_sequence``, and insertion order differ for reasons unrelated to correctness)
and too weak (it cannot detect canonical data hiding in the index).

1. semantic equivalence   — normalized logical projection survives a rebuild
2. adversarial mutation   — corrupt the DB arbitrarily; rebuild restores the truth
3. derivability           — every indexed field is re-derivable from files alone,
                            with scan order shuffled and mtimes touched
"""

from __future__ import annotations

import os
import random
import sqlite3
import time
from datetime import date

from brain.config import Paths
from brain.frontmatter import serialize
from brain.index import build
from brain.model import Evidence, Memory, MemoryType, ProvenanceClass, Volatility
from brain.store import memory as mem


def make(i: int, ws: str = "default", vol: Volatility = Volatility.SLOW) -> Memory:
    return Memory(
        id=f"01K1Z8V4Q00000000000{i:06d}",
        type=MemoryType.SEMANTIC,
        provenance_class=ProvenanceClass.DIRECT_USER_STATEMENT,
        volatility=vol,
        valid_from=date(2026, 8, 3),
        evidence=[Evidence(f"event:01K1Z8V3M00000000000{i:06d}", 10, 14)],
        body=f"# Memory {i}\n\nBody text number {i} about deployment and postgres.",
        workspace=ws,
    )


def seed(paths: Paths, n: int = 12) -> list[Memory]:
    """A fixture corpus covering every type, volatility, workspace, and edge case."""
    out = []
    workspaces = ["default", "work", "side-project"]
    vols = list(Volatility)
    types = list(MemoryType)
    for i in range(n):
        m = make(i, workspaces[i % 3], vols[i % len(vols)])
        m.type = types[i % len(types)]
        body = serialize(m).encode()
        mem.write(paths, m.id, body, None, workspace=m.workspace, memory_type=m.type.value)
        out.append(m)
    # A memory with history, so revision_index is non-trivial.
    first = out[0]
    dest = mem.present_path(paths, first.workspace, first.type.value, first.id)
    first.body += "\n\nAmended."
    mem.write(
        paths,
        first.id,
        serialize(first).encode(),
        mem.present_hash(dest),
        workspace=first.workspace,
        memory_type=first.type.value,
    )
    return out


def test_semantic_equivalence_across_rebuild(paths: Paths) -> None:
    seed(paths)
    conn = build.connect(paths)
    build.rebuild(paths, conn)
    before = build.logical_snapshot(conn)
    conn.close()

    paths.db.unlink()
    conn = build.connect(paths)
    build.rebuild(paths, conn)
    after = build.logical_snapshot(conn)
    conn.close()

    assert before == after


def test_adversarial_mutation_is_repaired_by_rebuild(paths: Paths) -> None:
    """Corrupt the index arbitrarily. If it held anything canonical, this loses it."""
    seed(paths)
    conn = build.connect(paths)
    build.rebuild(paths, conn)
    truth = build.logical_snapshot(conn)

    conn.execute("DELETE FROM memory_index WHERE rowid % 2 = 0")
    conn.execute("UPDATE memory_index SET volatility = 'ephemeral', title = 'corrupted'")
    conn.execute("DELETE FROM evidence_link")
    conn.execute("DELETE FROM search")
    conn.execute("DELETE FROM revision_index")
    conn.commit()
    assert build.logical_snapshot(conn) != truth

    build.rebuild(paths, conn)
    assert build.logical_snapshot(conn) == truth
    conn.close()


def test_derivability_is_independent_of_scan_order_and_mtimes(paths: Paths) -> None:
    """Nothing may be derived from mtime or from the order files are visited."""
    seed(paths)
    conn = build.connect(paths)
    build.rebuild(paths, conn)
    baseline = build.logical_snapshot(conn)
    conn.close()

    files = list(paths.memories.rglob("*.md"))
    random.shuffle(files)
    now = time.time()
    for i, f in enumerate(files):
        os.utime(f, (now - i * 3600, now - i * 3600))

    paths.db.unlink()
    conn = build.connect(paths)
    build.rebuild(paths, conn)
    assert build.logical_snapshot(conn) == baseline
    conn.close()


def test_dropping_the_database_loses_nothing(paths: Paths) -> None:
    """The headline claim, stated as an executable assertion."""
    memories = seed(paths)
    conn = build.connect(paths)
    build.rebuild(paths, conn)
    n_before = conn.execute("SELECT COUNT(*) FROM memory_index").fetchone()[0]
    conn.close()

    paths.db.unlink()
    assert not paths.db.exists()

    conn = build.connect(paths)
    build.rebuild(paths, conn)
    n_after = conn.execute("SELECT COUNT(*) FROM memory_index").fetchone()[0]
    ids = {r[0] for r in conn.execute("SELECT id FROM memory_index")}
    conn.close()

    assert n_before == n_after == len(memories)
    assert ids == {m.id for m in memories}


def test_evidence_survives_rebuild(paths: Paths) -> None:
    seed(paths, 3)
    conn = build.connect(paths)
    build.rebuild(paths, conn)
    rows = conn.execute("SELECT memory_id, source_ref, span_start, span_end FROM evidence_link")
    links = list(rows)
    conn.close()
    assert links, "every memory carries evidence; none was indexed"
    assert all(r[1].startswith("event:") and r[2] == 10 for r in links)


def test_index_schema_declares_nothing_canonical() -> None:
    """A guard against the exception creeping back in."""
    sql = build.SCHEMA.read_text().lower()
    assert "not rebuildable" not in sql
    assert "canonical" in sql  # the comment asserting the opposite is present

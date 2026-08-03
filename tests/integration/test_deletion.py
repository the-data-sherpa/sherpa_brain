"""Deletion — the property this project exists for.

The headline test is ``test_restoring_a_pre_deletion_backup_does_not_resurrect``.
Everything else here exists to make sure the two gates stay separate: local
durability suppresses retrieval, and quorum gates only the receipt. Conflating them
fails *open*, which is the one direction that must never happen.
"""

from __future__ import annotations

import json
import shutil
from datetime import date

import pytest

from brain.config import Paths
from brain.frontmatter import serialize
from brain.index import build
from brain.model import Evidence, Memory, MemoryType, ProvenanceClass, Volatility
from brain.search.fts5 import Fts5Backend
from brain.store import deletion, ledger, revisions
from brain.store import memory as mem


def make(i: int = 0) -> Memory:
    return Memory(
        id=f"01K1Z8V4Q00000000000{i:06d}",
        type=MemoryType.SEMANTIC,
        provenance_class=ProvenanceClass.DIRECT_USER_STATEMENT,
        volatility=Volatility.SLOW,
        valid_from=date(2026, 8, 3),
        evidence=[Evidence("event:01K1Z8V3M0000000000000000", 1, 5)],
        body=f"# Secret {i}\n\nsalamander pipeline credentials rotation note {i}",
    )


def seed(paths: Paths, i: int = 0, revs: int = 2) -> Memory:
    m = make(i)
    mem.write(paths, m.id, serialize(m).encode(), None)
    dest = mem.present_path(paths, "default", "semantic", m.id)
    for r in range(1, revs):
        m.body += f"\n\nrevision {r}"
        mem.write(paths, m.id, serialize(m).encode(), mem.present_hash(dest))
    return m


def test_forget_suppresses_immediately_without_any_network(paths: Paths) -> None:
    """Suppression is gated on local durability alone — never on replication."""
    m = seed(paths)
    assert not deletion.is_tombstoned(paths, m.id)

    result = deletion.forget(paths, m.id, replicate=deletion.NullReplicator())

    assert result.suppressed is True
    assert deletion.is_tombstoned(paths, m.id)
    # ...even though quorum was never met.
    assert result.delivery is deletion.DeliveryState.PENDING
    assert result.complete is False


def test_forget_never_reports_complete_without_quorum(paths: Paths) -> None:
    m = seed(paths)
    r = deletion.forget(paths, m.id, replicate=deletion.NullReplicator())
    assert r.replicas == 1 and r.required == 2
    assert r.delivery is deletion.DeliveryState.PENDING
    assert deletion.pending_deletions(paths) == [{"subject_id": m.id, "replicas": 1, "required": 2}]


def test_purge_removes_present_and_every_revision(paths: Paths) -> None:
    m = seed(paths, revs=4)
    assert len(revisions.revision_numbers(paths, m.id)) == 4

    deletion.forget(paths, m.id, replicate=deletion.NullReplicator())

    assert not mem.present_path(paths, "default", "semantic", m.id).exists()
    assert revisions.revision_numbers(paths, m.id) == []
    # No file anywhere still contains the content.
    for f in paths.memories.rglob("*"):
        if f.is_file():
            assert b"salamander" not in f.read_bytes()


def test_tombstoned_content_is_never_searchable(paths: Paths) -> None:
    m = seed(paths)
    conn = build.connect(paths)
    build.rebuild(paths, conn)
    assert Fts5Backend(paths, conn).search("salamander")

    deletion.forget(paths, m.id, replicate=deletion.NullReplicator())
    build.rebuild(paths, conn)
    assert Fts5Backend(paths, conn).search("salamander") == []
    conn.close()


def test_restoring_a_pre_deletion_backup_does_not_resurrect(paths: Paths, tmp_path) -> None:
    """THE headline test.

    Take a backup. Delete. Restore the *pre-deletion* backup — which of course brings
    the bytes back. Then replay the ledger, which lives outside the restored domain,
    and prove the content does not survive.
    """
    m = seed(paths, revs=3)

    backup = tmp_path / "backup"
    shutil.copytree(paths.memories, backup / "memories")

    deletion.forget(paths, m.id, replicate=deletion.NullReplicator())
    assert not mem.present_path(paths, "default", "semantic", m.id).exists()

    # Naive restore: the deleted bytes are back on disk.
    shutil.rmtree(paths.memories)
    shutil.copytree(backup / "memories", paths.memories)
    assert mem.present_path(paths, "default", "semantic", m.id).exists()

    # The tombstone ledger was never part of that backup, so it still knows.
    report = deletion.resume(paths)

    assert not mem.present_path(paths, "default", "semantic", m.id).exists()
    assert revisions.revision_numbers(paths, m.id) == []
    assert any(r["subject_id"] == m.id for r in report["repurged"])
    for f in paths.memories.rglob("*"):
        if f.is_file():
            assert b"salamander" not in f.read_bytes()

    # And it is not served, either.
    conn = build.connect(paths)
    build.rebuild(paths, conn)
    assert Fts5Backend(paths, conn).search("salamander") == []
    conn.close()


def test_resume_rescans_every_tombstoned_subject_even_with_a_receipt(paths: Paths) -> None:
    """A receipt informs history; it never shortens a scan.

    An earlier design resumed only tombstones *lacking* a purge receipt — which skips
    exactly the IDs whose bytes could be recreated after the receipt was written.
    """
    m = seed(paths)
    deletion.forget(paths, m.id, replicate=deletion.NullReplicator())
    assert ledger.read_chain(paths.purges), "a purge receipt should exist"

    # Something puts the bytes back after the receipt: a restore, an editor, anything.
    resurrected = mem.present_path(paths, "default", "semantic", m.id)
    resurrected.parent.mkdir(parents=True, exist_ok=True)
    resurrected.write_text(serialize(make(0)))
    assert resurrected.exists()

    report = deletion.resume(paths)
    assert not resurrected.exists(), "a prior receipt must not exempt an ID from re-scanning"
    assert any(r["subject_id"] == m.id for r in report["repurged"])


def test_crash_after_tombstone_before_purge_is_resumable(paths: Paths) -> None:
    """Suppression already holds; the residue is cleaned on the next run."""
    m = seed(paths)
    # Simulate the crash window: tombstone durable, purge never ran.
    ledger.append(paths.tombstones, ledger.tombstone_payload(m.id))
    assert deletion.is_tombstoned(paths, m.id)
    assert mem.present_path(paths, "default", "semantic", m.id).exists()

    deletion.resume(paths)
    assert not mem.present_path(paths, "default", "semantic", m.id).exists()


def test_broken_tombstone_chain_refuses_to_serve(paths: Paths) -> None:
    m = seed(paths)
    deletion.forget(paths, m.id, replicate=deletion.NullReplicator())

    lines = paths.tombstones.read_text().splitlines()
    entry = json.loads(lines[0])
    entry["payload"]["subject_id"] = "01TAMPERED0000000000000000"
    lines[0] = json.dumps(entry, sort_keys=True, separators=(",", ":"))
    paths.tombstones.write_text("\n".join(lines) + "\n")

    with pytest.raises(ledger.LedgerError, match=r"altered|Refusing"):
        deletion.verify_all_ledgers(paths)


def test_torn_trailing_line_recovers(paths: Paths) -> None:
    """A crash during append leaves a partial line; that entry was never acked."""
    m = seed(paths)
    deletion.forget(paths, m.id, replicate=deletion.NullReplicator())
    with paths.tombstones.open("a") as fh:
        fh.write('{"seq": 99, "prev_hash": "de')  # torn write

    entries = ledger.read_chain(paths.tombstones)
    assert len(entries) == 1
    assert entries[0].subject_id == m.id


def test_quorum_counts_distinct_replicas_not_ack_entries(paths: Paths) -> None:
    """A replayed ack must not inflate quorum."""
    m = seed(paths)
    deletion.forget(paths, m.id, replicate=deletion.NullReplicator())
    seq, head = ledger.head(paths.tombstones)

    payload = ledger.ack_payload(
        m.id,
        seq,
        head,
        replica_identity="git:aaaaaaaaaaaaaaaa",
        remote_sha="abc123",
        ref="refs/heads/ledger",
        protection_verified=True,
    )
    for _ in range(5):  # the same replica, acking five times
        ledger.append(paths.acks, payload)

    state, n = deletion.delivery_state(paths, m.id)
    assert n == 2, "local + one distinct remote, regardless of ack count"
    assert state is deletion.DeliveryState.CONFIRMED


def test_ack_for_a_different_chain_head_is_ignored(paths: Paths) -> None:
    """An ack must resolve to an exact local tombstone entry."""
    m = seed(paths)
    deletion.forget(paths, m.id, replicate=deletion.NullReplicator())
    ledger.append(
        paths.acks,
        ledger.ack_payload(
            m.id,
            1,
            "0" * 64,  # not the real chain head
            replica_identity="git:bbbbbbbbbbbbbbbb",
            remote_sha="def456",
            ref="refs/heads/ledger",
            protection_verified=True,
        ),
    )
    _, n = deletion.delivery_state(paths, m.id)
    assert n == 1, "an ack referencing an unknown chain head is invalid, not merely unhelpful"


def test_unverified_protection_does_not_count_toward_quorum(paths: Paths) -> None:
    m = seed(paths)
    deletion.forget(paths, m.id, replicate=deletion.NullReplicator())
    seq, head = ledger.head(paths.tombstones)
    ledger.append(
        paths.acks,
        ledger.ack_payload(
            m.id,
            seq,
            head,
            replica_identity="git:cccccccccccccccc",
            remote_sha="ghi789",
            ref="refs/heads/ledger",
            protection_verified=False,  # a rewritable remote is not an anchor
        ),
    )
    _, n = deletion.delivery_state(paths, m.id)
    assert n == 1


def test_purge_receipt_is_only_written_when_absence_is_verified(paths: Paths) -> None:
    m = seed(paths)
    removed, residue = deletion.purge(paths, m.id)
    assert removed and not residue
    receipts = ledger.read_chain(paths.purges)
    assert receipts and receipts[-1].payload["subject_id"] == m.id
    assert "revalidate" in receipts[-1].payload["note"]


def test_deletion_purges_the_query_log(paths: Paths) -> None:
    """The query log is a derived representation, and deletion must reach it.

    Found by a smoke test, not by design review: the log looks like telemetry, but an
    entry pairs a query string with the IDs it returned — and a query is very often a
    fragment of the memory itself. Leaving it behind means the words you asked to
    forget survive in a file nobody thinks of as storage.
    """
    m = seed(paths)
    paths.logs.mkdir(parents=True, exist_ok=True)
    log = paths.logs / "queries.jsonl"
    log.write_text(
        json.dumps({"at": "x", "query": "salamander", "retrieved": [m.id], "cited": None})
        + "\n"
        + json.dumps({"at": "y", "query": "unrelated", "retrieved": ["01OTHER"], "cited": None})
        + "\n"
    )

    deletion.forget(paths, m.id, replicate=deletion.NullReplicator())

    remaining = log.read_text()
    assert "salamander" not in remaining, "the deleted memory's query text survived"
    assert m.id not in remaining
    assert "unrelated" in remaining, "unrelated log entries must be preserved"

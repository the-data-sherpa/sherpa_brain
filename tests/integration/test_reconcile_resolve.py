"""Reconciliation of unwitnessed edits, and crash-safe conflict resolution."""

from __future__ import annotations

import json
from datetime import date

import pytest

from brain.config import Paths
from brain.frontmatter import serialize
from brain.model import Disposition, Evidence, Memory, MemoryType, ProvenanceClass, Volatility
from brain.store import memory as mem
from brain.store import reconcile, resolve, revisions


def make(i: int = 0, body: str = "original") -> Memory:
    return Memory(
        id=f"01K1Z8V4Q00000000000{i:06d}",
        type=MemoryType.SEMANTIC,
        provenance_class=ProvenanceClass.DIRECT_USER_STATEMENT,
        volatility=Volatility.SLOW,
        valid_from=date(2026, 8, 3),
        evidence=[Evidence("event:01K1Z8V3M0000000000000000")],
        body=body,
    )


def seed(paths: Paths, i: int = 0) -> Memory:
    m = make(i)
    mem.write(paths, m.id, serialize(m).encode(), None)
    return m


# -- reconciliation ---------------------------------------------------------------


def test_unwitnessed_edit_is_captured_as_a_revision(paths: Paths) -> None:
    m = seed(paths)
    dest = mem.present_path(paths, "default", "semantic", m.id)
    dest.write_text(serialize(make(0, "edited by hand")))
    assert mem.serving_disposition(paths, m.id, dest) is Disposition.UNWITNESSED

    result = reconcile.reconcile_one(paths, m.id, dest)

    assert isinstance(result, reconcile.Reconciled)
    assert mem.serving_disposition(paths, m.id, dest) is Disposition.SETTLED
    joined = b"".join(
        revisions.read_revision(paths, m.id, n) or b""
        for n in revisions.revision_numbers(paths, m.id)
    )
    assert b"original" in joined and b"edited by hand" in joined


def test_transaction_time_is_an_interval_not_a_point(paths: Paths) -> None:
    """Nobody knows when an unwitnessed edit happened — only that it was in a window."""
    m = seed(paths)
    dest = mem.present_path(paths, "default", "semantic", m.id)
    dest.write_text(serialize(make(0, "edited")))

    result = reconcile.reconcile_one(paths, m.id, dest)
    assert isinstance(result, reconcile.Reconciled)
    assert result.recorded_from != result.recorded_to
    assert result.recorded_from < result.recorded_to


def test_file_in_flux_is_deferred_never_snapshotted(paths: Paths, monkeypatch) -> None:
    """A hash taken mid-save reads a torn file, so an unstable read must not capture."""
    m = seed(paths)
    dest = mem.present_path(paths, "default", "semantic", m.id)
    dest.write_text(serialize(make(0, "changing")))

    calls = {"n": 0}
    real = reconcile.Path.read_bytes

    def flaky(self):  # type: ignore[no-untyped-def]
        calls["n"] += 1
        return real(self) + (b"x" * calls["n"] if self == dest else b"")

    monkeypatch.setattr(reconcile.Path, "read_bytes", flaky)
    result = reconcile.reconcile_one(paths, m.id, dest)

    assert isinstance(result, reconcile.Deferred)
    assert "still being written" in result.reason


def test_editor_sidecar_defers_capture(paths: Paths) -> None:
    m = seed(paths)
    dest = mem.present_path(paths, "default", "semantic", m.id)
    dest.write_text(serialize(make(0, "mid-edit")))
    (dest.parent / f".{dest.name}.swp").write_bytes(b"vim")

    result = reconcile.reconcile_one(paths, m.id, dest)
    assert isinstance(result, reconcile.Deferred)


def test_reconcile_is_idempotent(paths: Paths) -> None:
    m = seed(paths)
    dest = mem.present_path(paths, "default", "semantic", m.id)
    dest.write_text(serialize(make(0, "edited once")))

    assert isinstance(reconcile.reconcile_one(paths, m.id, dest), reconcile.Reconciled)
    n_after_first = len(revisions.revision_numbers(paths, m.id))
    assert reconcile.reconcile_one(paths, m.id, dest) is None
    assert len(revisions.revision_numbers(paths, m.id)) == n_after_first


def test_malformed_edit_is_deferred_to_quarantine_not_captured(paths: Paths) -> None:
    m = seed(paths)
    dest = mem.present_path(paths, "default", "semantic", m.id)
    dest.write_text("no front matter here")

    result = reconcile.reconcile_one(paths, m.id, dest)
    assert result is None or isinstance(result, reconcile.Deferred)
    # It must not have entered the revision log as if it were a valid state.
    joined = b"".join(
        revisions.read_revision(paths, m.id, n) or b""
        for n in revisions.revision_numbers(paths, m.id)
    )
    assert b"no front matter here" not in joined


# -- resolution -------------------------------------------------------------------


def contest(paths: Paths, i: int = 0) -> Memory:
    m = seed(paths, i)
    dest = mem.present_path(paths, "default", "semantic", m.id)
    stale = mem.present_hash(dest)
    dest.write_text(serialize(make(i, "theirs")))
    with pytest.raises(mem.Divergence):
        mem.write(paths, m.id, serialize(make(i, "ours")).encode(), stale)
    return m


def test_resolve_adopts_a_branch_and_keeps_the_loser(paths: Paths) -> None:
    m = contest(paths)
    options = resolve.branches(paths, m.id)
    assert len(options) == 2

    result = resolve.resolve(paths, m.id, take=options[0])

    assert not mem.is_contested(paths, m.id)
    dest = mem.present_path(paths, "default", "semantic", m.id)
    assert mem.serving_disposition(paths, m.id, dest) is Disposition.SETTLED
    # The losing branch is still in the log — a resolution is a decision, not an erasure.
    assert revisions.read_revision(paths, m.id, options[1]) is not None
    assert result.new_revision > max(options)


def test_conflict_marker_is_archived_not_deleted(paths: Paths) -> None:
    """Deleting it would leave recovery unable to tell resolved from never-contested."""
    m = contest(paths)
    result = resolve.resolve(paths, m.id, take=resolve.branches(paths, m.id)[0])

    assert not paths.conflict_path(m.id).exists()
    archived = list(paths.resolved_conflicts.glob(f"{m.id}.*.json"))
    assert archived, "the marker must survive as an audit record"
    assert json.loads(archived[0].read_text())["memory_id"] == m.id
    assert result.archived_marker.endswith(".json")


def test_resolving_an_uncontested_memory_is_refused(paths: Paths) -> None:
    m = seed(paths)
    with pytest.raises(resolve.NotContested):
        resolve.resolve(paths, m.id, take=1)


def test_taking_a_branch_that_is_not_part_of_the_conflict_is_refused(paths: Paths) -> None:
    m = contest(paths)
    with pytest.raises(resolve.UnknownBranch):
        resolve.resolve(paths, m.id, take=999)


def test_no_op_record_survives_resolution(paths: Paths) -> None:
    from brain.store.ops import pending_ops

    m = contest(paths)
    resolve.resolve(paths, m.id, take=resolve.branches(paths, m.id)[0])
    assert pending_ops(paths) == []


def test_contested_memory_reads_fail_closed_until_resolved(paths: Paths) -> None:
    from brain.index import build
    from brain.search.fts5 import Fts5Backend

    m = contest(paths)
    conn = build.connect(paths)
    build.rebuild(paths, conn)
    assert Fts5Backend(paths, conn).search("theirs") == []
    assert Fts5Backend(paths, conn).search("ours") == []

    # branches[0] is the mediated write ("ours"); branches[1] is the editor's.
    resolve.resolve(paths, m.id, take=resolve.branches(paths, m.id)[0])
    build.rebuild(paths, conn)
    assert Fts5Backend(paths, conn).search("ours"), "resolution restores searchability"
    assert Fts5Backend(paths, conn).search("theirs") == [], "the losing branch is not served"
    conn.close()


def test_reconciled_revision_is_marked_as_such_in_history(paths: Paths) -> None:
    """The audit trail must distinguish a witnessed transition from an inferred one.

    Found by a smoke test: without stamping, the reconciled revision recorded
    `capture: mediated` by default, which is precisely the distinction the field
    exists to make.
    """
    from brain.index import build

    m = seed(paths)
    dest = mem.present_path(paths, "default", "semantic", m.id)
    dest.write_text(serialize(make(0, "edited outside the protocol")))

    result = reconcile.reconcile_one(paths, m.id, dest)
    assert isinstance(result, reconcile.Reconciled)

    conn = build.connect(paths)
    build.rebuild(paths, conn)
    rows = conn.execute(
        "SELECT revision_no, capture, recorded_from, recorded_to FROM revision_index "
        "WHERE memory_id = ? ORDER BY revision_no",
        (m.id,),
    ).fetchall()
    conn.close()

    captures = {r["revision_no"]: r["capture"] for r in rows}
    assert captures[1] == "mediated"
    assert captures[result.revision_no] == "reconciled"

    reconciled_row = next(r for r in rows if r["revision_no"] == result.revision_no)
    assert reconciled_row["recorded_from"] != reconciled_row["recorded_to"], (
        "an unwitnessed transition must record an interval, not a point"
    )


def test_reconcile_leaves_the_memory_settled(paths: Paths) -> None:
    """Present is materialized from the stamped bytes, so it stays byte-identical."""
    m = seed(paths)
    dest = mem.present_path(paths, "default", "semantic", m.id)
    dest.write_text(serialize(make(0, "hand edit")))

    reconcile.reconcile_one(paths, m.id, dest)
    assert mem.serving_disposition(paths, m.id, dest) is Disposition.SETTLED
    assert mem.present_hash(dest) == revisions.newest_hash(paths, m.id)
    assert b"hand edit" in dest.read_bytes()

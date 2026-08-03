"""Evidence resolution, verifiable backup/restore, and the eval instruments."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from brain import backup as backup_mod
from brain.config import Paths
from brain.eval import bootstrap, runner
from brain.frontmatter import serialize
from brain.index import build
from brain.model import Evidence, Memory, MemoryType, ProvenanceClass, Volatility
from brain.store import artifacts, deletion, events, ledger
from brain.store import memory as mem

DOC = "line one\nline two about postgres\nline three\nline four\n"


def seed_memory(paths: Paths, i: int, body: str, ev: list[Evidence]) -> Memory:
    m = Memory(
        id=f"01K1Z8V4Q00000000000{i:06d}",
        type=MemoryType.SEMANTIC,
        provenance_class=ProvenanceClass.DIRECT_USER_STATEMENT,
        volatility=Volatility.SLOW,
        valid_from=date(2026, 8, 3),
        evidence=ev,
        body=body,
    )
    mem.write(paths, m.id, serialize(m).encode(), None)
    return m


# -- events -----------------------------------------------------------------------


def test_event_round_trips_and_is_addressable(paths: Paths) -> None:
    e = events.append(paths, "message", {"text": DOC}, session="s1")
    found = events.find(paths, e.id)
    assert found is not None
    assert found[0].text == DOC


def test_corrupted_event_line_is_skipped_not_trusted(paths: Paths) -> None:
    """Evidence that cannot be verified is not evidence."""
    e = events.append(paths, "message", {"text": "genuine"})
    seg = events.segment_path(paths)
    line = seg.read_text().replace('"genuine"', '"tampered"')
    seg.write_text(line)
    assert events.find(paths, e.id) is None


def test_torn_trailing_event_line_recovers(paths: Paths) -> None:
    e = events.append(paths, "message", {"text": "complete"})
    with events.segment_path(paths).open("a") as fh:
        fh.write('{"id": "01ABC", "kind": "mes')
    assert len(events.read_segment(events.segment_path(paths))) == 1
    assert events.find(paths, e.id) is not None


def test_event_redaction_fork_never_edits_in_place(paths: Paths) -> None:
    keep = events.append(paths, "message", {"text": "keep this"})
    drop = events.append(paths, "message", {"text": "salamander"})
    seg = events.segment_path(paths)

    forked, dropped = events.redaction_fork(paths, seg, {drop.id})

    assert dropped == 1
    assert not seg.exists(), "the original segment is deleted, not rewritten"
    assert forked != seg, "erasure creates a NEW file"
    assert b"salamander" not in forked.read_bytes()
    assert events.find(paths, keep.id) is not None


# -- artifacts and evidence resolution ---------------------------------------------


def test_artifact_is_content_addressed_and_idempotent(paths: Paths) -> None:
    a = artifacts.store(paths, DOC.encode(), media_type="text/plain", source_uri="file:///doc")
    b = artifacts.store(paths, DOC.encode())
    assert a.digest == b.digest
    assert artifacts.verify(paths, a.digest)


def test_tampering_with_an_artifact_is_detected(paths: Paths) -> None:
    a = artifacts.store(paths, DOC.encode())
    a.path.write_text("altered content")
    assert not artifacts.verify(paths, a.digest)
    resolved = artifacts.resolve_evidence(paths, a.ref, None, None)
    assert not resolved["resolved"]
    assert "altered" in resolved["reason"]


def test_evidence_pointer_resolves_to_the_actual_source_span(paths: Paths) -> None:
    """Without this, a pointer is an attribution rather than a citation."""
    a = artifacts.store(paths, DOC.encode(), source_uri="file:///doc")
    resolved = artifacts.resolve_evidence(paths, a.ref, 2, 2)
    assert resolved["resolved"]
    assert resolved["excerpt"] == "line two about postgres"
    assert resolved["source_uri"] == "file:///doc"


def test_event_evidence_resolves_with_a_span(paths: Paths) -> None:
    e = events.append(paths, "message", {"text": DOC})
    resolved = artifacts.resolve_evidence(paths, f"event:{e.id}", 3, 4)
    assert resolved["resolved"]
    assert resolved["excerpt"] == "line three\nline four"


def test_unresolvable_pointer_says_so_rather_than_guessing(paths: Paths) -> None:
    assert not artifacts.resolve_evidence(paths, "event:01NOPE", None, None)["resolved"]
    assert not artifacts.resolve_evidence(paths, "wat:xyz", None, None)["resolved"]


def test_artifact_redaction_fork_breaks_the_old_digest(paths: Paths) -> None:
    """A stale reference must fail loudly, never resolve to altered content."""
    a = artifacts.store(paths, b"public part\nsalamander secret\n")
    new = artifacts.redaction_fork(paths, a.digest, b"public part\n")

    assert new is not None and new.digest != a.digest
    assert artifacts.read(paths, a.digest) is None
    assert not artifacts.resolve_evidence(paths, a.ref, None, None)["resolved"]
    assert b"salamander" not in new.path.read_bytes()


# -- backup and restore ------------------------------------------------------------


def test_backup_records_a_verifiable_manifest(paths: Paths) -> None:
    seed_memory(paths, 0, "content one", [Evidence("event:x")])
    manifest = backup_mod.backup(paths)
    assert manifest.files
    assert (
        backup_mod.verify_backup(paths, paths.backups / f"{manifest.generation}.manifest.json")
        == []
    )


def test_backup_detects_a_tampered_copy(paths: Paths) -> None:
    seed_memory(paths, 0, "content one", [Evidence("event:x")])
    manifest = backup_mod.backup(paths)
    mpath = paths.backups / f"{manifest.generation}.manifest.json"
    target = next((paths.backups / manifest.generation).rglob("*.md"))
    target.write_text("tampered")
    assert backup_mod.verify_backup(paths, mpath), "an unverifiable backup is not a backup"


def test_restore_refuses_when_currency_cannot_be_proven(paths: Paths, tmp_path: Path) -> None:
    """A backup's own high-water mark is a lower bound, never proof of currency."""
    m = seed_memory(paths, 0, "salamander note", [Evidence("event:x")])
    manifest = backup_mod.backup(paths)
    mpath = paths.backups / f"{manifest.generation}.manifest.json"

    deletion.forget(paths, m.id, replicate=deletion.NullReplicator())
    paths.tombstones.unlink()  # every ledger replica lost

    with pytest.raises(backup_mod.RestoreRefused, match="nothing was written"):
        backup_mod.restore(paths, mpath)
    # A refusal that leaves the bytes on disk is not a refusal: a later reindex
    # would serve them.
    assert not mem.present_path(paths, "default", "semantic", m.id).exists()


def test_restore_with_a_replica_replays_deletions(paths: Paths, tmp_path: Path) -> None:
    """The headline property, driven through the real backup/restore commands."""
    import shutil

    m = seed_memory(paths, 0, "salamander note", [Evidence("event:x")])
    manifest = backup_mod.backup(paths)
    mpath = paths.backups / f"{manifest.generation}.manifest.json"

    deletion.forget(paths, m.id, replicate=deletion.NullReplicator())
    replica = tmp_path / "offsite-tombstones.jsonl"
    shutil.copy2(paths.tombstones, replica)

    report = backup_mod.restore(paths, mpath, extra_replicas=[replica])

    assert not mem.present_path(paths, "default", "semantic", m.id).exists()
    assert any(r["subject_id"] == m.id for r in report["repurged"])
    for f in paths.memories.rglob("*"):
        if f.is_file():
            assert b"salamander" not in f.read_bytes()


def test_operator_attestation_is_accepted_when_the_ledger_is_intact(paths: Paths) -> None:
    """The stated residual risk: a human vouches, rather than a check silently passing."""
    m = seed_memory(paths, 0, "content", [Evidence("event:x")])
    manifest = backup_mod.backup(paths)
    mpath = paths.backups / f"{manifest.generation}.manifest.json"
    deletion.forget(paths, m.id, replicate=deletion.NullReplicator())
    seq, _ = ledger.head(paths.tombstones)

    report = backup_mod.restore(paths, mpath, attested_seq=seq)
    assert report["currency"]["source"] == "operator-attestation"
    assert not mem.present_path(paths, "default", "semantic", m.id).exists()


def test_attesting_a_sequence_you_cannot_replay_is_refused(paths: Paths) -> None:
    """Attestation says "you are not behind". It does not hand over the entries.

    Found by a smoke test: attesting the head while the ledger itself was missing
    let the restore proceed and **resurrected the deleted content**, with every
    check reporting success. That is precisely the shape of failure this module
    exists to prevent, so a shortfall is now a refusal.
    """
    m = seed_memory(paths, 0, "salamander note", [Evidence("event:x")])
    manifest = backup_mod.backup(paths)
    mpath = paths.backups / f"{manifest.generation}.manifest.json"
    deletion.forget(paths, m.id, replicate=deletion.NullReplicator())
    seq, _ = ledger.head(paths.tombstones)
    paths.tombstones.unlink()  # the entries are gone; only the number is claimed

    with pytest.raises(backup_mod.RestoreRefused, match="not the same as possessing"):
        backup_mod.restore(paths, mpath, attested_seq=seq)
    assert not mem.present_path(paths, "default", "semantic", m.id).exists()
    for f in paths.memories.rglob("*"):
        if f.is_file():
            assert b"salamander" not in f.read_bytes()


def test_equivocating_replicas_fail_closed(paths: Paths, tmp_path: Path) -> None:
    """Equal seq, different chain head: one has been rewritten. Do not guess which."""
    seed_memory(paths, 0, "content", [Evidence("event:x")])
    manifest = backup_mod.backup(paths)
    ledger.append(paths.tombstones, ledger.tombstone_payload("01AAA"))

    forged = tmp_path / "forged.jsonl"
    forged.write_bytes(b"")
    ledger.append(forged, ledger.tombstone_payload("01BBB"))

    proof = backup_mod.prove_currency(paths, manifest, extra_replicas=[forged])
    assert not proof.established
    assert proof.source == "equivocation"


# -- eval --------------------------------------------------------------------------


def golden(tmp_path: Path, body: str) -> Path:
    d = tmp_path / "eval"
    d.mkdir(exist_ok=True)
    (d / "golden.yaml").write_text(body)
    return d


def test_eval_run_scores_with_an_interval_and_n(paths: Paths, tmp_path: Path) -> None:
    seed_memory(paths, 0, "postgres for new services", [Evidence("event:x")])
    build.rebuild(paths)
    d = golden(
        tmp_path,
        "cases:\n"
        '  - id: c1\n    question: "postgres"\n    expect_terms: ["postgres"]\n'
        '  - id: c2\n    question: "nothingatall"\n    should_abstain: true\n',
    )
    record = runner.run(paths, d / "golden.yaml", results_dir=d / "results")
    assert record.score["total"] == 2
    assert record.score["lower"] < record.score["point"] <= record.score["upper"]
    assert record.corpus_size == 1


def test_failures_are_tagged_with_a_taxonomy_category(paths: Paths, tmp_path: Path) -> None:
    """Which category dominates decides where optimization goes (§10.4)."""
    build.rebuild(paths)
    d = golden(
        tmp_path,
        'cases:\n  - id: miss\n    question: "absent"\n    expect_terms: ["absent"]\n',
    )
    record = runner.run(paths, d / "golden.yaml")
    assert record.taxonomy.get("retention") == 1


def test_memory_off_control_arm_scores_separately(paths: Paths, tmp_path: Path) -> None:
    seed_memory(paths, 0, "postgres", [Evidence("event:x")])
    build.rebuild(paths)
    d = golden(
        tmp_path,
        'cases:\n  - id: c1\n    question: "postgres"\n    expect_terms: ["postgres"]\n',
    )
    with_memory = runner.run(paths, d / "golden.yaml", results_dir=d / "results")
    without = runner.run(paths, d / "golden.yaml", memory_off=True, results_dir=d / "results")
    assert with_memory.score["point"] == 1.0
    assert without.score["point"] == 0.0


def test_state_recovery_probe_reconstructs_known_facts(paths: Paths, tmp_path: Path) -> None:
    seed_memory(paths, 0, "I prefer postgres for new services", [Evidence("event:x")])
    build.rebuild(paths)
    d = tmp_path / "eval"
    d.mkdir()
    (d / "state-facts.yaml").write_text(
        "facts:\n"
        '  - id: recovered\n    probe: "postgres services"\n    expect_terms: ["postgres"]\n'
        '  - id: never-stored\n    probe: "favourite colour"\n    expect_terms: ["cerulean"]\n'
    )
    result = runner.state_recovery(paths, d / "state-facts.yaml")
    assert result["total"] == 2
    assert result["recovered"] == 1
    assert result["missed"] == ["never-stored"]


def test_paraphrase_probes_miss_at_rung_one_and_that_is_the_signal(
    paths: Paths, tmp_path: Path
) -> None:
    """A pure-paraphrase probe fails against lexical search — by design, not by bug.

    "database preference" does not lexically overlap "I prefer postgres", and BM25
    does not stem or embed. This is the honest limitation of rung 1, and a probe
    suite weighted toward paraphrase is precisely how you would *detect* that rung 1
    has stopped being sufficient — which is what the rung-2 trigger in ADR 0002 is
    watching for. It would be self-deceiving to write a probe suite that only asks
    questions lexical search can already answer.
    """
    seed_memory(paths, 0, "I prefer postgres for new services", [Evidence("event:x")])
    build.rebuild(paths)
    d = tmp_path / "eval"
    d.mkdir()
    (d / "state-facts.yaml").write_text(
        "facts:\n"
        '  - id: paraphrased\n    probe: "database preference"\n    expect_terms: ["postgres"]\n'
    )
    result = runner.state_recovery(paths, d / "state-facts.yaml")
    assert result["recovered"] == 0
    assert result["missed"] == ["paraphrased"]


def test_slope_refuses_on_a_short_series(paths: Paths, tmp_path: Path) -> None:
    seed_memory(paths, 0, "postgres", [Evidence("event:x")])
    build.rebuild(paths)
    d = golden(
        tmp_path,
        'cases:\n  - id: c1\n    question: "postgres"\n    expect_terms: ["postgres"]\n',
    )
    runner.run(paths, d / "golden.yaml", results_dir=d / "results")
    v = runner.verdict(d / "results")
    assert not v.computable
    assert "floor is 150" in v.reason


def test_bootstrap_drafts_candidates_and_marks_them_as_drafts(paths: Paths, tmp_path: Path) -> None:
    """A synthetic set scored by its own generator measures the generator."""
    seed_memory(paths, 0, "kubernetes ingress annotations for staging", [Evidence("event:x")])
    seed_memory(paths, 1, "postgres connection pooling settings", [Evidence("event:y")])

    candidates = bootstrap.draft(paths)
    assert len(candidates) == 2
    assert all("DRAFT" in c.note for c in candidates)
    assert all(c.expect_ids for c in candidates)

    written = bootstrap.write_templates(tmp_path / "eval", candidates)
    golden_text = Path(written["golden"]).read_text()
    assert "NEVER regenerate" in golden_text
    assert "abstention" in golden_text
    assert Path(written["state_facts"]).exists()


def test_bootstrap_prefers_distinctive_terms(paths: Paths) -> None:
    """A term that appears everywhere makes a question every memory answers."""
    for i in range(4):
        seed_memory(paths, i, f"deployment notes deployment {i} kubernetes", [Evidence("event:x")])
    seed_memory(paths, 9, "deployment notes cerulean flamingo", [Evidence("event:x")])

    candidates = {c.expect_ids[0]: c for c in bootstrap.draft(paths)}
    distinctive = candidates["01K1Z8V4Q00000000000000009"]
    assert "cerulean" in distinctive.question or "flamingo" in distinctive.question

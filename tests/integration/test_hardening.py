"""Output-side redaction, idempotency, budgets, and the health check."""

from __future__ import annotations

import asyncio
import json
from datetime import date, timedelta
from pathlib import Path

import pytest

from brain import doctor, mcp_server, scan
from brain.config import Paths
from brain.frontmatter import serialize
from brain.index import build
from brain.model import Evidence, Memory, MemoryType, ProvenanceClass, Volatility, iso, utcnow
from brain.search.fts5 import Fts5Backend
from brain.store import artifacts, budgets, deletion
from brain.store import memory as mem

LEAKED = "ghp_abcdefghijklmnopqrstuvwxyz0123456789"


def seed_with_secret(paths: Paths, i: int = 0) -> Memory:
    """Write a memory containing a credential, bypassing the write-side scanner.

    This is exactly how one gets there in practice: content that predates the
    scanner, or an edit made directly in an editor.
    """
    m = Memory(
        id=f"01K1Z8V4Q00000000000{i:06d}",
        type=MemoryType.SEMANTIC,
        provenance_class=ProvenanceClass.DIRECT_USER_STATEMENT,
        volatility=Volatility.SLOW,
        valid_from=date(2026, 8, 3),
        evidence=[Evidence("event:x")],
        body=f"deployment token is {LEAKED} for the staging cluster",
    )
    mem.write(paths, m.id, serialize(m).encode(), None)
    build.rebuild(paths)
    return m


# ── output-side redaction (§11.4: scan before persistence AND before model output) ──


def test_redact_masks_credentials_and_reports_them() -> None:
    masked, findings = scan.redact(f"token {LEAKED} here")
    assert LEAKED not in masked
    assert "[REDACTED" in masked
    assert findings


def test_redact_leaves_clean_text_untouched() -> None:
    text = "I prefer Postgres for new services on this project"
    masked, findings = scan.redact(text)
    assert masked == text
    assert findings == []


def test_redact_does_not_mangle_benign_high_entropy_tokens() -> None:
    """A redactor that eats ULIDs and digests makes every excerpt unreadable."""
    text = "memory 01K1Z8V4Q0000000000000000 at sha 3b2f1a9c8d7e6f5a4b3c2d1e0f9a8b7c6d5e4f3a"
    masked, _ = scan.redact(text)
    assert masked == text


def test_search_results_never_carry_a_credential_to_the_model(paths: Paths) -> None:
    """The gap this closes: anything that entered before the scanner, or inside an
    ingested artifact, was returned verbatim."""
    seed_with_secret(paths)
    hits = Fts5Backend(paths).search("deployment")
    assert hits
    blob = " ".join(f"{h.title} {h.excerpt}" for h in hits)
    assert LEAKED not in blob
    assert any(h.redacted for h in hits), "redaction must be visible, not silent"


def test_evidence_resolution_redacts(paths: Paths) -> None:
    """Artifacts are stored verbatim by design, so this is the likeliest leak path."""
    a = artifacts.store(paths, f"line one\nexport TOKEN={LEAKED}\n".encode())
    resolved = artifacts.resolve_evidence(paths, a.ref, 2, 2)
    assert resolved["resolved"]
    assert LEAKED not in resolved["excerpt"]
    assert resolved["redacted"]


def test_redaction_protects_the_model_not_the_disk(paths: Paths) -> None:
    """Masking on read is not a fix — the bytes are still there and must be purged."""
    m = seed_with_secret(paths)
    path = mem.present_path(paths, "default", "semantic", m.id)
    assert LEAKED in path.read_text(), "redaction must not be mistaken for erasure"


class TestMcpRedaction:
    @pytest.fixture(autouse=True)
    def _point(self, paths: Paths, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(mcp_server, "_paths", lambda: paths)

    @staticmethod
    def call(name: str, **args: object) -> dict:
        return json.loads(asyncio.run(mcp_server.mcp.call_tool(name, args)).content[0].text)

    def test_get_redacts_and_says_so(self, paths: Paths) -> None:
        m = seed_with_secret(paths)
        out = self.call("brain.get", id=m.id)
        assert LEAKED not in json.dumps(out)
        assert out["redacted"]
        assert "still contain them" in out["redaction_note"]

    def test_search_redacts(self, paths: Paths) -> None:
        seed_with_secret(paths)
        out = self.call("brain.search", query="deployment")
        assert LEAKED not in json.dumps(out)


# ── idempotency ──────────────────────────────────────────────────────────────────


class TestIdempotency:
    @pytest.fixture(autouse=True)
    def _point(self, paths: Paths, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(mcp_server, "_paths", lambda: paths)

    @staticmethod
    def call(name: str, **args: object) -> dict:
        return json.loads(asyncio.run(mcp_server.mcp.call_tool(name, args)).content[0].text)

    def test_a_retried_write_returns_the_original_result(self, paths: Paths) -> None:
        """Not merely 'avoids a duplicate' — a client that retries after a timeout
        needs the original id back, or it will try again with a fresh key."""
        first = self.call(
            "brain.write", op="propose", content="once", volatility="slow", idempotency_key="k1"
        )
        second = self.call(
            "brain.write", op="propose", content="once", volatility="slow", idempotency_key="k1"
        )
        assert second["id"] == first["id"]
        assert second["idempotent_replay"] is True

        n = sum(
            1
            for f in paths.memories.rglob("*.md")
            if ".revisions" not in f.parts and ".staging" not in f.parts
        )
        assert n == 1, "a retry created a second memory"

    def test_different_keys_create_different_memories(self) -> None:
        a = self.call(
            "brain.write", op="propose", content="a", volatility="slow", idempotency_key="k1"
        )
        b = self.call(
            "brain.write", op="propose", content="b", volatility="slow", idempotency_key="k2"
        )
        assert a["id"] != b["id"]

    def test_no_key_means_no_deduplication(self) -> None:
        a = self.call("brain.write", op="propose", content="x", volatility="slow")
        b = self.call("brain.write", op="propose", content="x", volatility="slow")
        assert a["id"] != b["id"]


def test_idempotency_records_expire(paths: Paths) -> None:
    budgets.remember_key(paths, "old", {"id": "01ABC"})
    stale = json.loads((paths.root / "idempotency.json").read_text())
    stale["old"]["at"] = iso(utcnow() - timedelta(hours=48))
    (paths.root / "idempotency.json").write_text(json.dumps(stale))
    assert budgets.replay(paths, "old") is None


# ── resource budgets ─────────────────────────────────────────────────────────────


def test_query_log_is_trimmed_by_age(paths: Paths) -> None:
    paths.logs.mkdir(parents=True, exist_ok=True)
    log = paths.logs / "queries.jsonl"
    old = iso(utcnow() - timedelta(days=400))
    new = iso(utcnow())
    log.write_text(
        json.dumps({"at": old, "query": "ancient", "retrieved": []})
        + "\n"
        + json.dumps({"at": new, "query": "recent", "retrieved": []})
        + "\n"
    )
    trimmed = budgets.trim_query_log(paths)
    assert trimmed and trimmed.removed == 1
    assert "ancient" not in log.read_text()
    assert "recent" in log.read_text()


def test_a_trim_is_recorded_never_silent(paths: Paths) -> None:
    """A log that quietly loses its tail is indistinguishable from a quiet period."""
    paths.logs.mkdir(parents=True, exist_ok=True)
    old = iso(utcnow() - timedelta(days=400))
    (paths.logs / "queries.jsonl").write_text(
        json.dumps({"at": old, "query": "q", "retrieved": []}) + "\n"
    )
    budgets.trim_query_log(paths)
    assert (paths.logs / "trims.jsonl").exists()


def test_query_log_is_capped_by_count(paths: Paths) -> None:
    paths.logs.mkdir(parents=True, exist_ok=True)
    now = iso(utcnow())
    (paths.logs / "queries.jsonl").write_text(
        "".join(json.dumps({"at": now, "query": str(i), "retrieved": []}) + "\n" for i in range(50))
    )
    trimmed = budgets.trim_query_log(paths, max_lines=10)
    assert trimmed and trimmed.removed == 40
    assert len((paths.logs / "queries.jsonl").read_text().splitlines()) == 10


def test_purge_ledger_compacts_duplicates(paths: Paths) -> None:
    from brain.store import ledger

    for _ in range(5):
        ledger.append(paths.purges, ledger.purge_payload("01SUBJECT", ["/tmp/x"]))
    trimmed = budgets.compact_purge_ledger(paths)
    assert trimmed and trimmed.removed == 4
    assert len(ledger.read_chain(paths.purges)) == 1


def test_tombstones_are_never_compacted(paths: Paths) -> None:
    """Compacting the anti-resurrection authority would mean forgetting what was forgotten."""
    from brain.store import ledger

    for i in range(3):
        ledger.append(paths.tombstones, ledger.tombstone_payload(f"01SUBJ{i}"))
    before = len(ledger.read_chain(paths.tombstones))
    budgets.sweep(paths)
    assert len(ledger.read_chain(paths.tombstones)) == before


# ── doctor ───────────────────────────────────────────────────────────────────────


def test_doctor_flags_an_empty_store(paths: Paths) -> None:
    """Emptiness is the failure mode that actually kills these systems."""
    checks, _ = doctor.run(paths)
    capture = next(c for c in checks if c.name == "capture")
    assert capture.level is doctor.Level.WARN
    assert "disuse" in capture.detail


def test_doctor_flags_a_missing_replica(paths: Paths) -> None:
    checks, _ = doctor.run(paths)
    replica = next(c for c in checks if c.name == "replica")
    assert replica.level is doctor.Level.WARN
    assert "brain ledger init" in replica.fix


def test_doctor_fails_on_a_broken_ledger(paths: Paths) -> None:
    from brain.store import ledger

    ledger.append(paths.tombstones, ledger.tombstone_payload("01SUBJ"))
    entry = json.loads(paths.tombstones.read_text().splitlines()[0])
    entry["payload"]["subject_id"] = "01TAMPERED"
    paths.tombstones.write_text(json.dumps(entry, sort_keys=True, separators=(",", ":")) + "\n")

    checks, worst = doctor.run(paths)
    assert worst is doctor.Level.FAIL
    assert next(c for c in checks if c.name == "ledger-integrity").level is doctor.Level.FAIL


def test_doctor_fails_on_deletion_residue(paths: Paths) -> None:
    """Suppressed but not purged is a real half-state, and it must be loud."""
    from brain.store import ledger

    m = seed_with_secret(paths)
    ledger.append(paths.tombstones, ledger.tombstone_payload(m.id))  # tombstone, no purge

    checks, worst = doctor.run(paths)
    assert worst is doctor.Level.FAIL
    residue = next(c for c in checks if c.name == "deletion-residue")
    assert "not removed" in residue.detail


def test_doctor_is_clean_on_a_healthy_store(paths: Paths, tmp_path: Path) -> None:
    import subprocess

    from brain import backup as backup_mod
    from brain.store.replicate import GitLedgerReplicator

    for i in range(25):
        m = Memory(
            id=f"01K1Z8V4Q00000000000{i:06d}",
            type=MemoryType.SEMANTIC,
            provenance_class=ProvenanceClass.DIRECT_USER_STATEMENT,
            volatility=Volatility.IMMUTABLE,
            valid_from=date(2026, 8, 3),
            evidence=[Evidence("event:x")],
            body=f"memory number {i}",
        )
        mem.write(paths, m.id, serialize(m).encode(), None)
    build.rebuild(paths)
    backup_mod.backup(paths)

    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", "-q", str(remote)], check=True)
    GitLedgerReplicator(paths).init_repo(str(remote))

    checks, worst = doctor.run(paths)
    # A local bare repo cannot prove force-push protection, so a WARN here is correct
    # and honest rather than a bug.
    assert worst is not doctor.Level.FAIL
    assert next(c for c in checks if c.name == "capture").level is doctor.Level.OK
    assert next(c for c in checks if c.name == "backup").level is doctor.Level.OK


def test_doctor_exit_codes(paths: Paths) -> None:
    from typer.testing import CliRunner

    from brain.cli import EXIT_PENDING, app

    r = CliRunner().invoke(app, ["doctor", "--state", str(paths.root)])
    assert r.exit_code == EXIT_PENDING  # empty store warns


def test_timer_units_are_user_scoped_and_reference_this_store(
    paths: Paths, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """User units, never system units — a root service would have more access to
    your memories than you do.

    This asserts the *systemd* unit content, so it pins the systemd writer rather
    than inheriting the host's platform. Otherwise it passes on Linux and fails on
    macOS, where `install_user_timers` correctly writes LaunchAgents instead — the
    macOS equivalents of these properties are asserted in test_platform_support.py.
    """
    from brain import ops

    monkeypatch.setattr(ops, "running_on_darwin", lambda: False)
    monkeypatch.setattr(ops, "UNIT_DIR", tmp_path / "systemd" / "user")
    written = ops.install_user_timers(paths)
    assert written
    service = (tmp_path / "systemd" / "user" / "brain-sync.service").read_text()
    assert str(paths.root) in service
    assert "User=" not in service, "user units must not set User="
    assert "SuccessExitStatus=0 1 3" in service, "pending is a normal state, not a failure"
    timer = (tmp_path / "systemd" / "user" / "brain-backup.timer").read_text()
    assert "Persistent=true" in timer, "a missed backup must run on next boot"


# ── replication, end to end ──────────────────────────────────────────────────────


@pytest.fixture
def replicated(paths: Paths, tmp_path: Path):  # type: ignore[no-untyped-def]
    import subprocess

    from brain.store.replicate import GitLedgerReplicator

    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", "-q", str(remote)], check=True)
    r = GitLedgerReplicator(paths)
    r.init_repo(str(remote))
    return r, remote


def test_replication_pushes_the_ledger_and_acks(paths: Paths, replicated) -> None:  # type: ignore[no-untyped-def]
    """Every earlier test used NullReplicator, so the real git path never ran once —
    and it crashed on the first push, for everyone who followed the runbook.

    `git rev-parse <ref>` prints the REF NAME when the ref does not exist, so the
    truthiness check passed and `-p refs/heads/ledger` was handed to commit-tree.
    """
    import subprocess

    from brain.store import ledger

    r, remote = replicated
    m = seed_with_secret(paths, 0)
    result = deletion.forget(paths, m.id, replicate=r)

    assert result.suppressed
    acks = ledger.read_chain(paths.acks)
    assert acks, "a successful push must be acknowledged"
    assert acks[-1].payload["replica_identity"] == r.identity

    # ...and the bytes are genuinely on the remote, not merely claimed.
    shown = subprocess.run(
        ["git", "--git-dir", str(remote), "show", "refs/heads/ledger:tombstones.jsonl"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert m.id in shown


def test_replication_failure_never_crashes_the_caller(paths: Paths, replicated) -> None:  # type: ignore[no-untyped-def]
    """The deletion is already durable and already suppressed; only the receipt is
    at stake, so a broken remote must degrade to `pending`, not raise."""
    import shutil

    r, remote = replicated
    shutil.rmtree(remote)

    m = seed_with_secret(paths, 1)
    result = deletion.forget(paths, m.id, replicate=r)
    assert result.suppressed
    assert result.delivery is deletion.DeliveryState.PENDING


def test_sync_exits_pending_not_error_when_a_replica_is_configured(  # type: ignore[no-untyped-def]
    paths: Paths, replicated
) -> None:
    from typer.testing import CliRunner

    from brain.cli import EXIT_OK, app

    m = seed_with_secret(paths, 2)
    deletion.forget(paths, m.id, replicate=deletion.NullReplicator())
    r = CliRunner().invoke(app, ["sync", "--state", str(paths.root)])
    assert r.exit_code in (EXIT_OK, 3), f"sync crashed: {r.exit_code}"

"""Regression tests for the four blocking gaps found in the production-readiness audit.

Each of these was a real defect, not a hypothetical. The first is the most serious:
it broke the guarantee that eleven rounds of design review were spent establishing.
"""

from __future__ import annotations

import asyncio
import json
from datetime import date, timedelta
from pathlib import Path

import pytest

from brain import mcp_server
from brain.config import Paths
from brain.frontmatter import parse, serialize
from brain.model import (
    Evidence,
    Memory,
    MemoryType,
    ProvenanceClass,
    Status,
    Volatility,
    utcnow,
)
from brain.store import artifacts, deletion, events, lifecycle, revisions
from brain.store import memory as mem


def make(i: int, body: str, **kw) -> Memory:  # type: ignore[no-untyped-def]
    return Memory(
        id=f"01K1Z8V4Q00000000000{i:06d}",
        type=MemoryType.SEMANTIC,
        provenance_class=ProvenanceClass.DIRECT_USER_STATEMENT,
        volatility=kw.pop("volatility", Volatility.SLOW),
        valid_from=kw.pop("valid_from", date(2026, 8, 3)),
        evidence=kw.pop("evidence", [Evidence("event:x")]),
        body=body,
        **kw,
    )


def seed(paths: Paths, i: int, body: str, **kw) -> Memory:  # type: ignore[no-untyped-def]
    m = make(i, body, **kw)
    mem.write(paths, m.id, serialize(m).encode(), None)
    return m


# ── 1. Unwitnessed edits must never be destroyed by a mediated write ──────────────


def test_write_captures_displaced_bytes_even_when_the_caller_matched(paths: Paths) -> None:
    """THE regression. A caller that reads present immediately before writing always
    satisfies CAS, so an unwitnessed edit sitting in present was silently destroyed.

    The guarantee cannot depend on caller discipline, so capture is unconditional:
    if the displaced bytes are not already in the log, they get published.
    """
    m = seed(paths, 0, "original")
    dest = mem.present_path(paths, "default", "semantic", m.id)

    # Someone edits by hand. Nobody has recorded this state.
    dest.write_text(serialize(make(0, "UNWITNESSED")))

    # A caller reads present *now* and writes from it — CAS passes honestly.
    mem.write(paths, m.id, serialize(make(0, "next")).encode(), mem.present_hash(dest))

    log = b"".join(
        revisions.read_revision(paths, m.id, n) or b""
        for n in revisions.revision_numbers(paths, m.id)
    )
    assert b"original" in log
    assert b"UNWITNESSED" in log, "a committed state was destroyed"
    assert b"next" in log


def test_capture_does_not_falsely_contest(paths: Paths) -> None:
    """Capturing an unrecorded state is not the same as detecting a divergence."""
    m = seed(paths, 0, "base")
    dest = mem.present_path(paths, "default", "semantic", m.id)
    dest.write_text(serialize(make(0, "edited")))

    mem.write(paths, m.id, serialize(make(0, "next")).encode(), mem.present_hash(dest))
    assert not mem.is_contested(paths, m.id), "writing from what you displaced is not a conflict"


def test_normal_writes_do_not_publish_duplicate_revisions(paths: Paths) -> None:
    m = seed(paths, 0, "one")
    dest = mem.present_path(paths, "default", "semantic", m.id)
    mem.write(paths, m.id, serialize(make(0, "two")).encode(), mem.present_hash(dest))
    mem.write(paths, m.id, serialize(make(0, "three")).encode(), mem.present_hash(dest))
    assert revisions.revision_numbers(paths, m.id) == [1, 2, 3]


class TestMcpCorrection:
    """The tool boundary must let a caller prove it is not stale."""

    @pytest.fixture(autouse=True)
    def _point(self, paths: Paths, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(mcp_server, "_paths", lambda: paths)

    @staticmethod
    def call(name: str, **args: object) -> dict:
        result = asyncio.run(mcp_server.mcp.call_tool(name, args))
        return json.loads(result.content[0].text)

    def test_get_returns_a_revision_token(self) -> None:
        mid = self.call("brain.write", op="propose", content="body", volatility="slow")["id"]
        assert self.call("brain.get", id=mid)["revision"] == 1

    def test_stale_correction_diverges_instead_of_overwriting(self, paths: Paths) -> None:
        mid = self.call("brain.write", op="propose", content="base", volatility="slow")["id"]
        rev = self.call("brain.get", id=mid)["revision"]

        dest = mem.present_path(paths, "default", "semantic", mid)
        dest.write_bytes(dest.read_bytes().replace(b"base", b"moved on"))

        out = self.call(
            "brain.write",
            op="correct",
            id=mid,
            content="stale correction",
            volatility="slow",
            expected_revision=rev,
        )
        assert "divergent" in out.get("error", "")
        assert mem.is_contested(paths, mid)

    def test_correction_without_a_token_still_loses_nothing(self, paths: Paths) -> None:
        mid = self.call("brain.write", op="propose", content="base", volatility="slow")["id"]
        dest = mem.present_path(paths, "default", "semantic", mid)
        dest.write_bytes(dest.read_bytes().replace(b"base", b"UNWITNESSED"))

        self.call("brain.write", op="correct", id=mid, content="blind", volatility="slow")
        log = b"".join(
            revisions.read_revision(paths, mid, n) or b""
            for n in revisions.revision_numbers(paths, mid)
        )
        assert b"UNWITNESSED" in log

    def test_unknown_expected_revision_is_refused(self) -> None:
        mid = self.call("brain.write", op="propose", content="base", volatility="slow")["id"]
        out = self.call(
            "brain.write",
            op="correct",
            id=mid,
            content="x",
            volatility="slow",
            expected_revision=99,
        )
        assert "does not exist" in out["error"]


# ── 2. Artifacts and events must be erasable ─────────────────────────────────────


def test_an_ingested_secret_can_be_erased(paths: Paths) -> None:
    """If evidence cannot be erased, 'forgetting' is true of conclusions and false of
    sources — and sources are where the sensitive material usually is."""
    a = artifacts.store(paths, b"meeting notes\nsalamander root password\n")
    assert artifacts.read(paths, a.digest) is not None

    result = deletion.forget(paths, a.digest, kind="artifact", replicate=deletion.NullReplicator())

    assert result.suppressed
    assert artifacts.read(paths, a.digest) is None
    assert deletion.is_tombstoned(paths, a.digest)
    for f in paths.artifacts.rglob("*"):
        if f.is_file():
            assert b"salamander" not in f.read_bytes()


def test_an_event_can_be_erased_without_touching_its_neighbours(paths: Paths) -> None:
    keep = events.append(paths, "message", {"text": "keep this one"})
    drop = events.append(paths, "message", {"text": "salamander secret"})

    deletion.forget(paths, drop.id, kind="event", replicate=deletion.NullReplicator())

    assert events.find(paths, drop.id) is None
    assert events.find(paths, keep.id) is not None
    for f in paths.events.rglob("*"):
        if f.is_file():
            assert b"salamander" not in f.read_bytes()


def test_deleting_evidence_surfaces_the_claims_that_cited_it(paths: Paths) -> None:
    """Erasing evidence is legitimate, but it silently weakens every claim citing it."""
    a = artifacts.store(paths, b"line one\nline two\n")
    seed(paths, 0, "a claim resting on that document", evidence=[Evidence(a.ref, 2, 2)])
    assert deletion.dangling_evidence(paths) == []

    deletion.forget(paths, a.digest, kind="artifact", replicate=deletion.NullReplicator())

    dangling = deletion.dangling_evidence(paths)
    assert len(dangling) == 1
    assert dangling[0]["ref"] == a.ref


def test_an_unknown_kind_is_refused_rather_than_guessed(paths: Paths) -> None:
    from typer.testing import CliRunner

    from brain.cli import EXIT_INVALID, app

    r = CliRunner().invoke(app, ["forget", "01X", "--kind", "wat", "--state", str(paths.root)])
    assert r.exit_code == EXIT_INVALID


# ── 3. Decay and expiry ──────────────────────────────────────────────────────────


def test_ephemeral_memories_lapse(paths: Paths) -> None:
    old = date.today() - timedelta(days=30)
    m = seed(paths, 0, "blocked on the auth bug", volatility=Volatility.EPHEMERAL, valid_from=old)

    report = lifecycle.sweep(paths)

    assert [x.memory_id for x in report.expired] == [m.id]
    dest = mem.present_path(paths, "default", "semantic", m.id)
    assert parse(dest.read_text()).status is Status.EXPIRED


def test_immutable_memories_never_lapse(paths: Paths) -> None:
    """A single decay curve would be wrong for most facts about a person."""
    m = seed(
        paths,
        0,
        "I was born in March",
        volatility=Volatility.IMMUTABLE,
        valid_from=date(1990, 1, 1),
    )
    assert lifecycle.sweep(paths).expired == []
    dest = mem.present_path(paths, "default", "semantic", m.id)
    assert parse(dest.read_text()).status is Status.CONFIRMED


def test_unconfirmed_proposals_decay_on_their_own_clock(paths: Paths) -> None:
    """The property that inverts accretion: confirm it, cite it, or lose it."""
    m = make(0, "an agent guessed this", status=Status.PROPOSED)
    m.recorded_at = utcnow() - timedelta(days=45)
    mem.write(paths, m.id, serialize(m).encode(), None)

    report = lifecycle.sweep(paths)
    assert [x.memory_id for x in report.expired] == [m.id]
    assert "never confirmed" in report.expired[0].reason


def test_a_fresh_proposal_survives(paths: Paths) -> None:
    m = make(0, "recent proposal", status=Status.PROPOSED)
    mem.write(paths, m.id, serialize(m).encode(), None)
    assert lifecycle.sweep(paths).expired == []


def test_expiry_is_not_deletion_and_is_reversible(paths: Paths) -> None:
    """A lapse must be undoable, or decay becomes a trap rather than a policy."""
    old = date.today() - timedelta(days=30)
    m = seed(paths, 0, "recoverable", volatility=Volatility.EPHEMERAL, valid_from=old)
    lifecycle.sweep(paths)

    dest = mem.present_path(paths, "default", "semantic", m.id)
    assert dest.exists(), "expiry is not deletion"
    assert not deletion.is_tombstoned(paths, m.id)

    assert lifecycle.unexpire(paths, m.id)
    assert parse(dest.read_text()).status is Status.CONFIRMED


def test_expired_past_grace_becomes_a_real_deletion(paths: Paths) -> None:
    old = date.today() - timedelta(days=400)
    m = seed(paths, 0, "salamander stale", volatility=Volatility.EPHEMERAL, valid_from=old)
    lifecycle.sweep(paths)  # -> expired
    lifecycle.sweep(paths, grace_days=0)  # -> tombstoned

    assert deletion.is_tombstoned(paths, m.id)
    assert not mem.present_path(paths, "default", "semantic", m.id).exists()


def test_lapsing_is_recorded_as_a_revision_not_a_silent_mutation(paths: Paths) -> None:
    old = date.today() - timedelta(days=30)
    m = seed(paths, 0, "audit me", volatility=Volatility.EPHEMERAL, valid_from=old)
    before = len(revisions.revision_numbers(paths, m.id))
    lifecycle.sweep(paths)
    assert len(revisions.revision_numbers(paths, m.id)) == before + 1


def test_contested_memories_are_left_for_a_human(paths: Paths) -> None:
    """Never compound an open conflict with an automated status change."""
    old = date.today() - timedelta(days=30)
    m = seed(paths, 0, "contested", volatility=Volatility.EPHEMERAL, valid_from=old)
    dest = mem.present_path(paths, "default", "semantic", m.id)
    stale = mem.present_hash(dest)
    dest.write_text(serialize(make(0, "theirs", volatility=Volatility.EPHEMERAL, valid_from=old)))
    with pytest.raises(mem.Divergence):
        mem.write(paths, m.id, serialize(make(0, "ours")).encode(), stale)

    assert lifecycle.sweep(paths).expired == []


def test_upcoming_surfaces_what_is_about_to_lapse(paths: Paths) -> None:
    soon = date.today() - timedelta(days=175)
    seed(paths, 0, "expiring soon", volatility=Volatility.VOLATILE, valid_from=soon)
    upcoming = lifecycle.upcoming(paths, within_days=14)
    assert len(upcoming) == 1
    assert 0 <= upcoming[0]["days"] <= 14


def test_sweep_is_idempotent(paths: Paths) -> None:
    old = date.today() - timedelta(days=30)
    seed(paths, 0, "once", volatility=Volatility.EPHEMERAL, valid_from=old)
    assert len(lifecycle.sweep(paths).expired) == 1
    assert lifecycle.sweep(paths).expired == []


# ── 4. The ledger remote must be reachable from the CLI ──────────────────────────


def test_ledger_status_reports_that_quorum_is_unreachable(paths: Paths) -> None:
    """Silence here is what made every deletion pend forever with no explanation."""
    from typer.testing import CliRunner

    from brain.cli import EXIT_PENDING, app

    r = CliRunner().invoke(app, ["ledger", "status", "--state", str(paths.root)])
    assert r.exit_code == EXIT_PENDING
    assert "Quorum is unreachable" in r.stderr
    assert json.loads(r.stdout)["data"]["remote_configured"] is False


def test_ledger_init_configures_a_replica(paths: Paths, tmp_path: Path) -> None:
    import subprocess

    from brain.store.replicate import GitLedgerReplicator

    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", "-q", str(remote)], check=True)

    r = GitLedgerReplicator(paths)
    r.init_repo(str(remote))

    assert r.remote == str(remote)
    assert r.identity.startswith("git:")
    assert paths.ledger_git.exists()


def test_replica_identity_is_derived_from_the_endpoint(paths: Paths, tmp_path: Path) -> None:
    """Identity must not be self-asserted, or one replica could count twice."""
    from brain.store.replicate import GitLedgerReplicator

    a = GitLedgerReplicator(paths)
    a.init_repo("git@github.com:someone/brain-ledger.git")
    first = a.identity

    b = GitLedgerReplicator(paths)
    b.init_repo("git@github.com:someone/brain-ledger.git")
    assert b.identity == first, "the same endpoint must yield the same identity"

    c = GitLedgerReplicator(paths)
    c.init_repo("git@github.com:someone/other-ledger.git")
    assert c.identity != first


def test_ledger_status_flags_an_unprotected_remote(paths: Paths, tmp_path: Path) -> None:
    """A remote whose history can be rewritten is not an anchor, so its acks are
    rejected at quorum time. Reporting success would mean the operator believes
    deletions are replicated while every one of them still pends."""
    import subprocess

    from typer.testing import CliRunner

    from brain.cli import EXIT_PENDING, app
    from brain.store.replicate import GitLedgerReplicator

    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", "-q", str(remote)], check=True)
    GitLedgerReplicator(paths).init_repo(str(remote))

    r = CliRunner().invoke(app, ["ledger", "status", "--state", str(paths.root)])
    assert r.exit_code == EXIT_PENDING
    assert "could NOT be verified" in r.stderr

"""The consult-before / capture-after workflow, and per-repo workspace isolation."""

from __future__ import annotations

import subprocess
from datetime import date
from pathlib import Path

import pytest

from brain import config, workflow
from brain.config import Paths
from brain.frontmatter import serialize
from brain.index import build
from brain.model import Evidence, Memory, MemoryType, ProvenanceClass, Volatility
from brain.store import memory as mem


def seed(paths: Paths, i: int, body: str, ws: str = "default") -> Memory:
    m = Memory(
        id=f"01K1Z8V4Q00000000000{i:06d}",
        type=MemoryType.SEMANTIC,
        provenance_class=ProvenanceClass.DIRECT_USER_STATEMENT,
        volatility=Volatility.SLOW,
        valid_from=date(2026, 8, 3),
        evidence=[Evidence("event:x")],
        body=body,
        workspace=ws,
    )
    mem.write(paths, m.id, serialize(m).encode(), None, workspace=ws)
    build.rebuild(paths)
    return m


# ── consult ──────────────────────────────────────────────────────────────────────


def test_context_emits_a_label_not_the_memory(paths: Paths) -> None:
    """The design rejects auto-injection: the agent pulls via a visible tool call.

    A title is derived from a body's first line, so for a one-line memory the title
    IS the memory. Truncating to a label is what keeps a pointer a pointer — long
    enough to judge relevance, too short to act on without reading.
    """
    seed(
        paths,
        0,
        "Postgres for new services, because connection pooling bit us badly on MySQL "
        "during the 2026 migration and nobody wants to repeat that",
    )
    result = workflow.context(paths, "which database for a new service?")

    assert result["relevant"] == 1
    rendered = workflow.render_for_hook(result)
    assert "Postgres for new services" in rendered, "enough to judge relevance"
    assert "nobody wants to repeat that" not in rendered, "the body must not arrive whole"
    assert "…" in rendered, "long labels are truncated"
    assert "POINTERS" in rendered and "brain.search" in rendered


def test_context_is_silent_when_nothing_matches(paths: Paths) -> None:
    """A hook that speaks every turn trains you to ignore it."""
    seed(paths, 0, "Postgres for new services")
    result = workflow.context(paths, "what is the capital of France")
    assert result["relevant"] == 0
    assert workflow.render_for_hook(result) == ""


def test_context_survives_a_broken_store(paths: Paths, monkeypatch: pytest.MonkeyPatch) -> None:
    """A memory system that breaks your editor is one you turn off."""

    def boom(*_a: object, **_k: object) -> None:
        raise RuntimeError("index on fire")

    monkeypatch.setattr(workflow.Fts5Backend, "search", boom)
    result = workflow.context(paths, "anything at all")
    assert result["relevant"] == 0
    assert result["degraded"] is True


def test_salient_terms_drop_instruction_noise() -> None:
    """A prompt is mostly instructions to an agent; only the nouns are searchable."""
    terms = workflow.salient_terms("please can you help me update the deployment pipeline")
    assert "deployment" in terms and "pipeline" in terms
    for noise in ("please", "help", "update", "can", "you"):
        assert noise not in terms


def test_morphological_variation_still_matches(paths: Paths) -> None:
    """`service` must find `services` — FTS5 does not stem, so prefixes carry it."""
    seed(paths, 0, "Postgres for new services")
    assert workflow.context(paths, "adding a service")["relevant"] == 1


# ── capture ──────────────────────────────────────────────────────────────────────


def test_no_prompt_when_nothing_changed(paths: Paths, tmp_path: Path) -> None:
    repo = tmp_path / "clean"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    check = workflow.capture_check(paths, cwd=repo)
    assert not check.should_prompt


def test_prompt_when_work_happened_and_nothing_was_captured(paths: Paths, tmp_path: Path) -> None:
    repo = tmp_path / "dirty"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    (repo / "f.txt").write_text("changed")
    check = workflow.capture_check(paths, cwd=repo)
    assert check.should_prompt
    assert check.repo_dirty and not check.wrote_recently


def test_no_prompt_once_something_was_written(paths: Paths, tmp_path: Path) -> None:
    """Writing a memory is what ends the prompt — so it cannot loop."""
    repo = tmp_path / "dirty2"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    (repo / "f.txt").write_text("changed")
    seed(paths, 0, "the lesson from this session")

    check = workflow.capture_check(paths, cwd=repo)
    assert not check.should_prompt
    assert check.wrote_recently


# ── workspace isolation ──────────────────────────────────────────────────────────


def test_workspace_defaults_to_the_git_repository(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """One store, many projects — without this they collapse into each other."""
    monkeypatch.delenv("BRAIN_WORKSPACE", raising=False)
    repo = tmp_path / "my-project"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    assert config.default_workspace(cwd=repo) == "my-project"


def test_explicit_env_overrides_the_repository(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("BRAIN_WORKSPACE", "chosen")
    repo = tmp_path / "ignored"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    assert config.default_workspace(cwd=repo) == "chosen"


def test_outside_a_repository_falls_back(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.delenv("BRAIN_WORKSPACE", raising=False)
    assert config.default_workspace(cwd=tmp_path) in ("default", tmp_path.name)


def test_one_project_does_not_surface_in_another(paths: Paths) -> None:
    """Context collapse is what workspaces exist to prevent."""
    seed(paths, 0, "shared vocabulary deployment note", ws="project-a")
    seed(paths, 1, "shared vocabulary deployment note", ws="project-b")

    a = workflow.context(paths, "deployment", workspace="project-a")
    assert a["relevant"] == 1
    assert a["pointers"][0]["workspace"] == "project-a"

    both = workflow.context(paths, "deployment", workspace=None)
    assert both["relevant"] == 2

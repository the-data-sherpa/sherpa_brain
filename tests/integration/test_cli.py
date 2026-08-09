"""CLI behaviour, especially the exit-code contract.

Exit codes are load-bearing: a caller that cannot distinguish "nothing found" from
"refusing to serve" will treat the second as the first, which turns a fail-closed
design into a fail-open one at the boundary.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner, Result

from brain.cli import EXIT_EMPTY, EXIT_FAIL_CLOSED, EXIT_INVALID, EXIT_OK, app

runner = CliRunner()


@pytest.fixture
def store(tmp_path: Path) -> str:
    d = str(tmp_path / "state")
    assert runner.invoke(app, ["init", "--state", d]).exit_code == EXIT_OK
    return d


def payload(result: Result) -> dict[str, Any]:
    data: dict[str, Any] = json.loads(result.stdout)["data"]
    return data


def test_remember_then_search_then_get(store: str) -> None:
    r = runner.invoke(app, ["remember", "Postgres for new services", "--state", store])
    assert r.exit_code == EXIT_OK
    mid = payload(r)["id"]

    r = runner.invoke(app, ["search", "postgres", "--state", store])
    assert r.exit_code == EXIT_OK
    assert payload(r)["hits"][0]["id"] == mid

    r = runner.invoke(app, ["get", mid, "--state", store])
    assert r.exit_code == EXIT_OK
    assert "Postgres" in payload(r)["content"]


def test_credentials_are_rejected_and_nothing_is_written(store: str) -> None:
    r = runner.invoke(
        app, ["remember", "token ghp_abcdefghijklmnopqrstuvwxyz0123456789", "--state", store]
    )
    assert r.exit_code == EXIT_INVALID
    assert "rejected, never redacted" in r.stderr

    # Nothing reached disk in any form — not even redacted.
    files = list((Path(store) / "memories").rglob("*.md"))
    assert not any(b"ghp_" in f.read_bytes() for f in files)


def test_empty_search_exits_one_not_zero(store: str) -> None:
    r = runner.invoke(app, ["search", "nothingmatchesthis", "--state", store])
    assert r.exit_code == EXIT_EMPTY


def test_missing_get_exits_one(store: str) -> None:
    assert runner.invoke(app, ["get", "01NOTAREALID", "--state", store]).exit_code == EXIT_EMPTY


def test_malformed_file_quarantines_and_validate_fails(store: str) -> None:
    runner.invoke(app, ["remember", "something real", "--state", store])
    bad = Path(store) / "memories" / "default" / "semantic" / "broken.md"
    bad.parent.mkdir(parents=True, exist_ok=True)
    bad.write_text("this is not front matter")

    r = runner.invoke(app, ["validate", "--state", store])
    assert r.exit_code == EXIT_INVALID
    assert payload(r)["quarantined"][0]["path"].endswith("broken.md")

    # And it is excluded from retrieval, not best-effort indexed.
    runner.invoke(app, ["reindex", "--state", store])
    r = runner.invoke(app, ["search", "front matter", "--state", store])
    assert r.exit_code == EXIT_EMPTY


def test_workspace_scoping_defaults_to_deny(store: str) -> None:
    runner.invoke(app, ["remember", "work secret sauce", "--workspace", "work", "--state", store])
    runner.invoke(app, ["remember", "home notes", "--workspace", "default", "--state", store])

    r = runner.invoke(app, ["search", "sauce", "--state", store])
    assert r.exit_code == EXIT_EMPTY, "cross-workspace results must not leak by default"

    r = runner.invoke(app, ["search", "sauce", "--workspace", "work", "--state", store])
    assert r.exit_code == EXIT_OK

    r = runner.invoke(app, ["search", "sauce", "--scope-all", "--state", store])
    assert r.exit_code == EXIT_OK


def test_contested_memory_fails_closed_on_get(store: str) -> None:
    from brain.config import Paths
    from brain.store import memory as mem

    r = runner.invoke(app, ["remember", "original text", "--state", store])
    mid = payload(r)["id"]
    p = Paths(Path(store))

    dest = mem.present_path(p, "default", "semantic", mid)
    stale = mem.present_hash(dest)
    dest.write_bytes(dest.read_bytes().replace(b"original text", b"edited by hand"))
    with pytest.raises(mem.Divergence):
        mem.write(p, mid, dest.read_bytes().replace(b"edited", b"mediated"), stale)

    runner.invoke(app, ["reindex", "--state", store])
    r = runner.invoke(app, ["get", mid, "--state", store])
    assert r.exit_code == EXIT_FAIL_CLOSED
    assert "contested" in r.stderr


def test_reindex_is_lossless(store: str) -> None:
    for i in range(5):
        runner.invoke(app, ["remember", f"memory number {i}", "--state", store])
    before = payload(runner.invoke(app, ["search", "memory", "--limit", "50", "--state", store]))

    r = runner.invoke(app, ["reindex", "--state", store])
    assert r.exit_code == EXIT_OK
    after = payload(runner.invoke(app, ["search", "memory", "--limit", "50", "--state", store]))
    assert {h["id"] for h in before["hits"]} == {h["id"] for h in after["hits"]}


def test_every_query_is_logged(store: str) -> None:
    runner.invoke(app, ["remember", "logged content", "--state", store])
    runner.invoke(app, ["search", "logged", "--state", store])
    log = Path(store) / "logs" / "queries.jsonl"
    assert log.exists()
    entries = [json.loads(ln) for ln in log.read_text().splitlines() if ln.strip()]
    assert entries[-1]["query"] == "logged"
    assert entries[-1]["retrieved"]

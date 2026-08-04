from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from brain.config import Paths


@pytest.fixture(autouse=True)
def _pin_workspace(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the workspace so tests do not depend on the cwd's git repository.

    Without this, `default_workspace()` derives from whatever repo the suite runs
    inside, and CLI tests that write via the CLI but read via a hardcoded
    "default" path silently diverge.
    """
    monkeypatch.setenv("BRAIN_WORKSPACE", "default")


@pytest.fixture
def paths(tmp_path: Path) -> Iterator[Paths]:
    root = tmp_path / "state"
    p = Paths(root)
    for d in p.all_dirs():
        d.mkdir(parents=True, exist_ok=True)
    yield p

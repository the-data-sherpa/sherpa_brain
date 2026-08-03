from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from brain.config import Paths


@pytest.fixture
def paths(tmp_path: Path) -> Iterator[Paths]:
    root = tmp_path / "state"
    p = Paths(root)
    for d in p.all_dirs():
        d.mkdir(parents=True, exist_ok=True)
    yield p

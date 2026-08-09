"""The declared dependency range must be able to run the code that declares it.

`pyproject.toml` once said `mcp>=1.2` while `mcp_server.py` imported `MCPServer` —
the v2 rename of `FastMCP`. A 1.x resolve does not degrade into a lesser feature set;
it raises ImportError before the server starts. The lockfile hid this from anyone
working in the repository, which is exactly why it survived: the defect is invisible
to the developer and live for the consumer who resolves without the lock.

So the floor is asserted here rather than trusted. This is the same failure shape the
adapter targets guard against — a declaration that parses, is honoured, and is wrong.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

PYPROJECT = Path(__file__).resolve().parents[2] / "pyproject.toml"


def _requirement(name: str) -> str:
    deps: list[str] = tomllib.loads(PYPROJECT.read_text())["project"]["dependencies"]
    for dep in deps:
        if dep.split(">")[0].split("<")[0].split("=")[0].split("[")[0].strip() == name:
            return str(dep)
    raise AssertionError(f"{name} is not a declared runtime dependency")


def test_mcp_floor_admits_only_versions_that_can_import_the_server() -> None:
    """`MCPServer` exists from 2.0 onward. Anything below cannot import the module."""
    assert ">=2" in _requirement("mcp").replace(" ", "")


def test_mcp_is_capped_below_the_next_major() -> None:
    """An uncapped major re-advertises compatibility nobody has tested.

    Raise this deliberately once v3 passes the conformance suite — the cap records
    what was verified, so removing it silently is the regression, not the upgrade.
    """
    assert "<3" in _requirement("mcp").replace(" ", "")


def test_the_declared_api_actually_exists_in_the_installed_sdk() -> None:
    """Floor and reality agree: the symbols the server imports are really there.

    Pins the version constraint to the *reason* for it. If a future SDK renames
    `MCPServer` again, this fails next to the constraint that would need changing.
    """
    mcp_server = pytest.importorskip("mcp.server")
    assert hasattr(mcp_server, "MCPServer")
    assert hasattr(mcp_server.MCPServer, "run_stdio_async")

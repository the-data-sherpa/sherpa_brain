from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from mcp.types import CallToolResult

from brain import mcp_server
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


def mcp_call(name: str, **args: Any) -> dict[str, Any]:
    """Drive a tool the way a generic MCP client would: by name, with a dict.

    Shared because three test modules had grown their own copy, and all three drifted
    the same way when the SDK moved to 2.x.

    MCP 2026-07-28 widened a tool result to ``CallToolResult | InputRequiredResult``:
    over a stateless transport a server can ask for input mid-call and be retried
    (MRTR). ``brain`` never does — its four tools are reads and writes with no
    elicitation, and ADR 0001 keeps contested resolution off the MCP surface
    deliberately, so an input request here would be a contract change. Asserting the
    type says that out loud instead of an AttributeError three frames down.

    The content list is a union too, and only ``TextContent`` carries ``.text``.
    """
    result = asyncio.run(mcp_server.mcp.call_tool(name, args))
    assert isinstance(result, CallToolResult), (
        f"{name} returned {type(result).__name__}; these tools never elicit input"
    )
    for block in result.content:
        text = getattr(block, "text", None)
        if text:
            loaded: dict[str, Any] = json.loads(text)
            return loaded
    # v2 renamed the protocol fields to snake_case; the pre-2.x spelling here was
    # dead code that would have raised AttributeError had the text branch ever
    # missed. mypy found it; no test ever reached it.
    return result.structured_content or {}

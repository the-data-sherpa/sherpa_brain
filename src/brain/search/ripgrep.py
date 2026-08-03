"""Rung 0 — ripgrep over the memory directory.

Zero infrastructure: no index to build, nothing to keep in sync. Useful on day one
and as a fallback when the index is being rebuilt.

Its weakness is the honest reason rung 1 exists: personal memory is paraphrase-heavy
and often has no exact token to match. *"What did I decide about the deployment
thing"* has no grep target. That is a real limitation, not a reason to skip the rung.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

from ..config import Paths
from ..frontmatter import InvalidFrontmatter, parse
from ..store import memory as mem
from .base import Hit, SearchBackend


class RipgrepBackend(SearchBackend):
    rung = 0
    name = "ripgrep"

    def __init__(self, paths: Paths) -> None:
        self.paths = paths

    @staticmethod
    def available() -> bool:
        return shutil.which("rg") is not None

    def search(
        self,
        query: str,
        *,
        workspace: str | None = "default",
        limit: int = 10,
        as_of: str | None = None,
    ) -> list[Hit]:
        root = self.paths.memories if workspace is None else self.paths.memories / workspace
        if not root.is_dir() or not query.strip():
            return []

        cmd = [
            "rg",
            "--json",
            "--ignore-case",
            "--fixed-strings",
            "--glob",
            "!.revisions/**",
            "--glob",
            "!.staging/**",
            "--max-count",
            str(limit),
            query,
            str(root),
        ]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30, check=False)
        except (OSError, subprocess.TimeoutExpired):
            return []

        seen: dict[str, Hit] = {}
        for line in proc.stdout.splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get("type") != "match":
                continue
            data = event["data"]
            path = Path(data["path"]["text"])
            excerpt = data["lines"]["text"].strip()
            if hit := self._to_hit(path, excerpt):
                seen.setdefault(hit.memory_id, hit)
            if len(seen) >= limit:
                break
        return list(seen.values())

    def _to_hit(self, path: Path, excerpt: str) -> Hit | None:
        try:
            m = parse(path.read_text(), path)
        except (InvalidFrontmatter, OSError, UnicodeDecodeError):
            return None  # quarantined content is never returned
        if mem.is_contested(self.paths, m.id):
            return None  # contested memories fail closed here too
        title = next(
            (ln.strip().lstrip("#").strip() for ln in m.body.splitlines() if ln.strip()), ""
        )
        return Hit(
            memory_id=m.id,
            workspace=m.workspace,
            title=title,
            excerpt=excerpt,
            score=1.0,  # rung 0 has no ranking; the agent iterates instead
            path=str(path),
            evidence=[str(e) for e in m.evidence],
        )

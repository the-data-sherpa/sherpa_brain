"""Workflow integration: consult before, capture after.

This exists to close the gap that actually kills these systems. Everything else in
this codebase is about a store being *correct*; none of it matters if the store is
empty, and it stays empty as long as writing to it is a thing you have to remember
to do.

**Why this is not an auto-injector.** The obvious implementation — dump relevant
memories into every prompt — contradicts three settled decisions:

- §9.1 the agent pulls through a tool loop; retrieval is not a prefix mutation
- §9.5 mutating the prefix per turn destroys the prompt cache
- §11.6 memory used in an answer must be *visible* in that answer

So ``context`` returns **pointers**, not content: how many relevant memories exist,
their ids, and their titles. Acting on them requires a real ``brain.search`` or
``brain.get`` call, which is visible in the transcript, cache-friendly because it is
appended rather than prefixed, and honest about what informed an answer. It is
§11.3's pointers-only rule applied to context instead of to files.

The hook guarantees you always *look*. The tool call is still how you *read*.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any

from .config import Paths
from .model import utcnow
from .search.fts5 import Fts5Backend

_WORD = re.compile(r"[A-Za-z][A-Za-z0-9_\-./]{2,}")

# Words that match everything and therefore discriminate nothing. A prompt is mostly
# instructions to an agent; only the nouns are worth searching on.
_NOISE = frozenset(
    [
        "the",
        "and",
        "for",
        "with",
        "that",
        "this",
        "from",
        "have",
        "has",
        "are",
        "was",
        "were",
        "will",
        "would",
        "should",
        "could",
        "can",
        "you",
        "your",
        "our",
        "their",
        "they",
        "them",
        "then",
        "than",
        "what",
        "when",
        "where",
        "which",
        "who",
        "how",
        "why",
        "all",
        "any",
        "some",
        "such",
        "only",
        "just",
        "now",
        "new",
        "use",
        "used",
        "using",
        "into",
        "over",
        "under",
        "after",
        "before",
        "please",
        "help",
        "make",
        "made",
        "need",
        "needs",
        "want",
        "wants",
        "like",
        "also",
        "more",
        "most",
        "very",
        "get",
        "got",
        "let",
        "lets",
        "add",
        "adds",
        "added",
        "fix",
        "fixes",
        "fixed",
        "run",
        "runs",
        "ran",
        "see",
        "saw",
        "look",
        "looks",
        "change",
        "changes",
        "changed",
        "update",
        "updates",
        "updated",
        "write",
        "writes",
        "wrote",
        "read",
        "reads",
        "code",
        "file",
        "files",
        "line",
        "lines",
        "test",
        "tests",
        "function",
        "functions",
        "method",
        "methods",
        "implement",
        "implements",
        "implementation",
        "about",
        "into",
        "out",
        "off",
        "down",
        "up",
    ]
)


@dataclass
class Pointer:
    id: str
    title: str
    workspace: str

    def as_dict(self) -> dict[str, str]:
        return {"id": self.id, "title": self.title, "workspace": self.workspace}


def salient_terms(text: str, limit: int = 8) -> list[str]:
    """The nouns worth searching on.

    Longest-first rather than frequency-first: in a short prompt every term appears
    once, and the longer token is almost always the more specific one.
    """
    seen: dict[str, None] = {}
    for w in _WORD.findall(text):
        low = w.lower()
        if low in _NOISE or len(low) < 4:
            continue
        seen.setdefault(low, None)
    return sorted(seen, key=len, reverse=True)[:limit]


def context(
    paths: Paths,
    text: str,
    *,
    workspace: str | None = "default",
    limit: int = 5,
) -> dict[str, Any]:
    """Pointers to memories related to ``text``. Never their content.

    Designed to be called from a hook on every prompt, so it must be fast, quiet, and
    incapable of breaking the session: any failure returns "nothing relevant" rather
    than raising. A memory system that blocks your editor is a memory system you turn
    off.
    """
    terms = salient_terms(text)
    if not terms:
        return {"relevant": 0, "pointers": [], "terms": []}

    try:
        hits = Fts5Backend(paths).search(" ".join(terms), workspace=workspace, limit=limit)
    except Exception:
        return {"relevant": 0, "pointers": [], "terms": terms, "degraded": True}

    pointers = [Pointer(h.memory_id, h.title or "(untitled)", h.workspace) for h in hits]
    return {
        "relevant": len(pointers),
        "terms": terms,
        "pointers": [p.as_dict() for p in pointers],
    }


#: A label long enough to judge relevance, short enough that you cannot act on it.
#: Titles are derived from the first line of a body, so for a one-line memory the
#: title *is* the memory — truncation is what keeps a pointer a pointer.
LABEL_CHARS = 56


def render_for_hook(result: dict[str, Any]) -> str:
    """One short block for a UserPromptSubmit hook. Empty string when there is nothing.

    Says what exists and where, then stops. The content requires a tool call, which
    is what makes memory use visible in the answer (§11.6) rather than an invisible
    prefix mutation (§9.1).

    Silence when nothing matches is deliberate: a hook that speaks on every prompt
    trains you to ignore it, and then it is worse than not having it.
    """
    if not result.get("relevant"):
        return ""
    lines = [
        f"brain: {result['relevant']} related memory/ies "
        f"(matched: {', '.join(result['terms'][:4])})",
    ]
    for p in result["pointers"]:
        label = p["title"]
        if len(label) > LABEL_CHARS:
            label = label[:LABEL_CHARS].rstrip() + "…"
        lines.append(f"  - {p['id']}  {label}  [{p['workspace']}]")
    lines.append(
        "  These are POINTERS. Read them with brain.search / brain.get before "
        "deciding — do not act on the labels alone."
    )
    return "\n".join(lines)


@dataclass
class CaptureCheck:
    wrote_recently: bool
    memories_written: int
    repo_dirty: bool
    should_prompt: bool
    reason: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "wrote_recently": self.wrote_recently,
            "memories_written": self.memories_written,
            "repo_dirty": self.repo_dirty,
            "should_prompt": self.should_prompt,
            "reason": self.reason,
        }


def capture_check(
    paths: Paths, *, window_minutes: int = 240, cwd: Path | None = None
) -> CaptureCheck:
    """Did this stretch of work produce anything worth remembering, and was it captured?

    Deliberately conservative about nagging. A prompt on every stop trains you to
    dismiss it, at which point the reminder is worse than nothing — it is a habit of
    ignoring the thing you installed to build a habit.

    Heuristic: if the working tree changed and nothing was written to the brain in the
    window, ask once. Otherwise stay quiet.
    """
    cutoff = utcnow() - timedelta(minutes=window_minutes)
    written = 0
    if paths.memories.is_dir():
        for f in paths.memories.rglob("*.md"):
            if ".revisions" in f.parts or ".staging" in f.parts:
                continue
            try:
                if f.stat().st_mtime >= cutoff.timestamp():
                    written += 1
            except OSError:
                continue

    dirty = False
    try:
        proc = subprocess.run(
            ["git", "status", "--porcelain"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
            cwd=cwd or Path.cwd(),
        )
        dirty = proc.returncode == 0 and bool(proc.stdout.strip())
    except (OSError, subprocess.SubprocessError):
        pass

    if written:
        return CaptureCheck(True, written, dirty, False, f"{written} memory/ies written recently")
    if not dirty:
        return CaptureCheck(False, 0, False, False, "no working-tree changes to draw a lesson from")
    return CaptureCheck(
        False,
        0,
        True,
        True,
        "the working tree changed and nothing was written to the brain",
    )

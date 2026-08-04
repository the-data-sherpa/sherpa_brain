"""The retrieval tool boundary (BLUEPRINT.md §9.2).

The agentic loop is the control flow; the *implementation* of search is a detail
behind this interface. Those are independent decisions, and every prior version of
this project's research conflated them at least once — most clearly by specifying
"`brain search` = ripgrep", which smuggles a retrieval-implementation choice into a
control-flow proposal.

Rungs climb only when a written trigger fires (ADR 0002). Phase 1 ships rungs 0 and 1.
Nothing else is met on day one, by construction.

    rung 0  ripgrep
    rung 1  SQLite FTS5 / BM25
    rung 2  + local dense retrieval, RRF     <- tenure trigger, with a kill criterion
    rung 3  + reranker
    rung 4  + external vector/search service
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Hit:
    """One result, always carrying enough to cite it.

    ``evidence`` and ``excerpt`` are not optional extras: a result you cannot trace
    to a source is a claim without provenance, which is the thing this project
    exists to avoid.
    """

    memory_id: str
    workspace: str
    title: str
    excerpt: str
    score: float
    path: str
    evidence: list[str] = field(default_factory=list)
    unresolved_conflict: str | None = None
    #: Credential classes masked on the way out (§11.4). Non-empty means the stored
    #: bytes still contain them — redaction protects the model, not the disk.
    redacted: list[str] = field(default_factory=list)


class SearchBackend(ABC):
    """Every rung satisfies this. Callers never learn which one they are talking to."""

    #: For diagnostics and the eval log, never for behaviour.
    rung: int = 0
    name: str = "abstract"

    @abstractmethod
    def search(
        self,
        query: str,
        *,
        workspace: str | None = "default",
        limit: int = 10,
        as_of: str | None = None,
    ) -> list[Hit]:
        """Return hits, most relevant first.

        ``workspace`` defaults to the current one — scoping is **deny by default**.
        Cross-workspace search requires passing ``None`` explicitly, because context
        collapse is a scope-default failure before it is anything else (§11.6).

        Note this is a relevance boundary, not a security boundary: agent-native
        file tools can read any memory regardless. ADR 0004 states that plainly.
        """
        raise NotImplementedError

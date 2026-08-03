"""The evaluation harness (BLUEPRINT.md §10).

Three instruments, because no single one answers the question:

- **golden set** — a frozen question set, re-run weekly. The signal is the *slope*
  against corpus size, not the level.
- **state-recovery probe** — can the store reconstruct facts you definitely told it,
  cold, with no conversational context? Task success saturates without memory, so a
  behavioural measure alone will report that the brain is worthless. This is the
  direct audit rather than the indirect one.
- **memory-off control** — what does the same question set score with no store at
  all? Necessary, and by itself misleading; run both.

Every failure is tagged with a **failure taxonomy** category before anything is
optimized (§10.4). Which category dominates is not knowable a priori, and optimizing
the wrong term is the most expensive mistake available here.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

import yaml

from ..config import Paths
from ..model import iso, utcnow
from ..search.fts5 import Fts5Backend
from .stats import Score, SlopeVerdict, slope_verdict, wilson


class FailureCategory(StrEnum):
    """From A-TMA. Each needs a different fix, so each must be counted separately."""

    RETENTION = "retention"  # never stored, or lost
    RETRIEVAL = "retrieval"  # exists but was not found
    RELEVANCE = "relevance"  # found, but wrong for this context
    CONSISTENCY = "consistency"  # conflicting facts stored at once
    UNTAGGED = "untagged"  # awaiting human judgment


@dataclass
class Case:
    id: str
    question: str
    expect_ids: list[str] = field(default_factory=list)
    expect_terms: list[str] = field(default_factory=list)
    workspace: str | None = "default"
    as_of: str | None = None
    should_abstain: bool = False
    note: str = ""


@dataclass
class CaseResult:
    id: str
    passed: bool
    retrieved: list[str]
    category: str = FailureCategory.UNTAGGED.value
    detail: str = ""


@dataclass
class Run:
    at: str
    corpus_size: int
    score: dict[str, Any]
    results: list[dict[str, Any]]
    taxonomy: dict[str, int]

    def to_json(self) -> bytes:
        return json.dumps(asdict(self), indent=2, sort_keys=True).encode() + b"\n"


def load_cases(path: Path) -> list[Case]:
    if not path.exists():
        return []
    raw = yaml.safe_load(path.read_text()) or {}
    return [Case(**c) for c in raw.get("cases", [])]


def corpus_size(paths: Paths) -> int:
    if not paths.memories.is_dir():
        return 0
    return sum(
        1
        for p in paths.memories.rglob("*.md")
        if ".revisions" not in p.parts and ".staging" not in p.parts
    )


def run_case(paths: Paths, case: Case, *, memory_off: bool = False) -> CaseResult:
    """Score one case.

    ``memory_off`` short-circuits retrieval entirely — the control arm. It is not a
    simulation of a worse system; it is the honest question of whether the store
    contributes anything.
    """
    if memory_off:
        return CaseResult(
            case.id,
            passed=case.should_abstain,
            retrieved=[],
            category=FailureCategory.RETENTION.value if not case.should_abstain else "",
            detail="memory-off control arm",
        )

    hits = Fts5Backend(paths).search(
        case.question, workspace=case.workspace, limit=10, as_of=case.as_of
    )
    got = [h.memory_id for h in hits]

    if case.should_abstain:
        passed = not hits
        return CaseResult(
            case.id,
            passed,
            got,
            category="" if passed else FailureCategory.RELEVANCE.value,
            detail="" if passed else "returned results where abstention was correct",
        )

    if case.expect_ids:
        passed = any(i in got for i in case.expect_ids)
        detail = "" if passed else f"expected one of {case.expect_ids}"
    elif case.expect_terms:
        blob = " ".join(f"{h.title} {h.excerpt}" for h in hits).lower()
        missing = [t for t in case.expect_terms if t.lower() not in blob]
        passed = not missing
        detail = "" if passed else f"missing terms: {missing}"
    else:
        passed = bool(hits)
        detail = "" if passed else "no results"

    category = ""
    if not passed:
        # A first-pass guess only. §10.4 requires a HUMAN to tag the first fifty
        # failures, because the difference between 'not found' and 'not stored' is
        # exactly the judgment that decides where optimization goes.
        category = FailureCategory.RETENTION.value if not hits else FailureCategory.RETRIEVAL.value
    return CaseResult(case.id, passed, got, category, detail)


def run(
    paths: Paths,
    golden: Path,
    *,
    memory_off: bool = False,
    results_dir: Path | None = None,
) -> Run:
    cases = load_cases(golden)
    results = [run_case(paths, c, memory_off=memory_off) for c in cases]
    correct = sum(1 for r in results if r.passed)
    score = wilson(correct, len(results))

    taxonomy: dict[str, int] = {}
    for r in results:
        if not r.passed and r.category:
            taxonomy[r.category] = taxonomy.get(r.category, 0) + 1

    record = Run(
        at=iso(utcnow()),
        corpus_size=corpus_size(paths),
        score={
            "correct": score.correct,
            "total": score.total,
            "point": score.point,
            "lower": score.lower,
            "upper": score.upper,
            "memory_off": memory_off,
        },
        results=[asdict(r) for r in results],
        taxonomy=taxonomy,
    )
    if results_dir is not None and cases:
        results_dir.mkdir(parents=True, exist_ok=True)
        suffix = "memory-off" if memory_off else "with-memory"
        stamp = record.at.replace(":", "").replace("-", "")
        (results_dir / f"{stamp}.{suffix}.json").write_bytes(record.to_json())
    return record


def state_recovery(paths: Paths, facts_path: Path) -> dict[str, Any]:
    """Ask the store to reconstruct facts you know are true, cold.

    Task success saturates even with no memory at all, so the memory-off delta will
    often show approximately nothing. That does not mean the store is worthless — it
    means tasks are completable without it. What a second brain is *for* is being a
    faithful, auditable model of what you decided, which is a property of the store
    rather than of any downstream task. This measures that directly.
    """
    if not facts_path.exists():
        return {"recovered": 0, "total": 0, "score": None, "note": "no state-facts file"}
    raw = yaml.safe_load(facts_path.read_text()) or {}
    facts = raw.get("facts", [])
    backend = Fts5Backend(paths)

    recovered, misses = 0, []
    for fact in facts:
        probe = fact.get("probe") or fact.get("statement", "")
        expect = [t.lower() for t in fact.get("expect_terms", [])]
        hits = backend.search(probe, workspace=fact.get("workspace", "default"), limit=5)
        blob = " ".join(f"{h.title} {h.excerpt}" for h in hits).lower()
        if expect and all(t in blob for t in expect):
            recovered += 1
        else:
            misses.append(fact.get("id", probe[:40]))

    score: Score | None = wilson(recovered, len(facts)) if facts else None
    return {
        "recovered": recovered,
        "total": len(facts),
        "score": str(score) if score else None,
        "missed": misses,
    }


def load_series(results_dir: Path) -> list[Score]:
    """The frozen-set score history, oldest first, with-memory runs only."""
    if not results_dir.is_dir():
        return []
    out: list[tuple[str, Score]] = []
    for f in sorted(results_dir.glob("*.with-memory.json")):
        data = json.loads(f.read_text())
        s = data["score"]
        out.append((data["at"], Score(s["correct"], s["total"], s["lower"], s["upper"])))
    return [s for _, s in sorted(out)]


def verdict(results_dir: Path) -> SlopeVerdict:
    return slope_verdict(load_series(results_dir))

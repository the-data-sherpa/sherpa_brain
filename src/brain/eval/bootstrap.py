"""Draft golden-set candidates from the real corpus (BLUEPRINT.md §10.1).

The golden set has to be about *your* memories, and nobody can write 150 of those
cold. This drafts candidates from what you have actually stored, for you to accept,
edit, or reject.

It deliberately does **not** auto-accept anything. A synthetic set scored by its own
generator measures the generator's assumptions, which is precisely the failure mode
§10.8 criticizes LoCoMo for — and a set built that way would be tuned to reward
over-retrieval, the exact behaviour the design is trying to avoid.

No model calls: candidates are derived mechanically from the memory's own distinctive
terms. A rough draft you edit beats a fluent one you trust.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

import yaml

from ..config import Paths
from ..frontmatter import InvalidFrontmatter, parse

STOPWORDS = frozenset(
    [
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "but",
        "by",
        "for",
        "from",
        "has",
        "have",
        "i",
        "in",
        "is",
        "it",
        "its",
        "of",
        "on",
        "or",
        "that",
        "the",
        "to",
        "was",
        "were",
        "will",
        "with",
        "this",
        "these",
        "those",
        "there",
        "their",
        "they",
        "them",
        "then",
        "than",
        "we",
        "you",
        "your",
        "my",
        "me",
        "not",
        "no",
        "do",
        "does",
        "did",
        "can",
        "could",
        "should",
        "would",
        "about",
        "into",
        "over",
        "under",
        "after",
        "before",
        "when",
        "where",
        "which",
        "who",
        "whom",
        "whose",
        "what",
        "how",
        "why",
        "also",
        "more",
        "most",
        "some",
        "such",
        "only",
        "own",
        "same",
        "so",
        "too",
        "very",
        "just",
        "now",
        "new",
        "use",
        "used",
        "using",
    ]
)

_WORD = re.compile(r"[A-Za-z][A-Za-z0-9_\-]{2,}")


@dataclass
class Candidate:
    id: str
    question: str
    expect_ids: list[str]
    expect_terms: list[str]
    workspace: str
    note: str


def distinctive_terms(body: str, corpus_freq: Counter[str], limit: int = 3) -> list[str]:
    """Terms that are common in this memory and rare across the corpus.

    A term that appears everywhere makes a question every memory answers, which
    measures nothing.
    """
    words = [w.lower() for w in _WORD.findall(body) if w.lower() not in STOPWORDS]
    local = Counter(words)
    scored = sorted(
        local.items(),
        key=lambda kv: (kv[1] / (1 + corpus_freq[kv[0]]), len(kv[0])),
        reverse=True,
    )
    return [w for w, _ in scored[:limit]]


def draft(paths: Paths, limit: int = 200) -> list[Candidate]:
    """Draft one candidate per memory. Every one needs human review before use."""
    if not paths.memories.is_dir():
        return []

    memories = []
    corpus_freq: Counter[str] = Counter()
    for p in sorted(paths.memories.rglob("*.md")):
        if ".revisions" in p.parts or ".staging" in p.parts:
            continue
        try:
            m = parse(p.read_text(), p)
        except (InvalidFrontmatter, UnicodeDecodeError):
            continue
        memories.append(m)
        corpus_freq.update({w.lower() for w in _WORD.findall(m.body) if w.lower() not in STOPWORDS})

    out = []
    for m in memories[:limit]:
        terms = distinctive_terms(m.body, corpus_freq)
        if not terms:
            continue
        out.append(
            Candidate(
                id=f"draft-{m.id[:10].lower()}",
                question=" ".join(terms),
                expect_ids=[m.id],
                expect_terms=terms[:2],
                workspace=m.workspace,
                note=(
                    "DRAFT — rewrite the question the way you would actually ask it. "
                    "A question built from the memory's own words tests lexical match, "
                    "not recall."
                ),
            )
        )
    return out


TEMPLATE_HEADER = """\
# Golden set — the frozen question set (BLUEPRINT.md §10.1)
#
# RULES
#   1. Re-run weekly. NEVER regenerate. The signal is the SLOPE against corpus
#      size, not the level, and a regenerated set has no slope.
#   2. Grow to 150+ items before trusting the slope. Below that, week-to-week
#      binomial noise swamps the effect — the runner refuses to compute it.
#   3. Cover every category below. A set that is all easy lookups measures
#      whether search works, not whether the brain remembers.
#
# CATEGORIES TO COVER
#   exact-identifier   a name, path, or symbol that must match exactly
#   paraphrase         asked in words the memory does not use
#   multi-session      needs two or more memories combined
#   temporal           an as_of question, where the answer changed over time
#   contradiction      two memories disagree; the current one must win
#   abstention         the brain should return NOTHING — set should_abstain: true
#   deletion           previously known, since forgotten; must not come back
#
# `brain eval bootstrap` drafts candidates from your corpus. They are DRAFTS:
# rewrite each question the way you would actually ask it. A question built from
# the memory's own words tests lexical matching, not recall.

cases:
"""

WORKED_EXAMPLES = """\
  # --- worked examples: delete these once you have your own ---

  - id: example-exact-identifier
    question: "brain.sqlite3"
    expect_terms: ["derived", "index"]
    note: "exact-identifier — rare token, must match precisely"

  - id: example-paraphrase
    question: "what did I settle on for storing things long term"
    expect_terms: ["canonical"]
    note: "paraphrase — deliberately avoids the words the memory uses"

  - id: example-temporal
    question: "which database for this project"
    as_of: "2026-06-01"
    expect_terms: ["postgres"]
    note: "temporal — the answer must be what was true THEN, not now"

  - id: example-abstention
    question: "what is my bank account number"
    should_abstain: true
    note: "abstention — returning anything here is a failure, not a near miss"

  - id: example-deletion
    question: "the thing I asked you to forget"
    should_abstain: true
    note: "deletion — a tombstoned subject must never resurface"
"""

STATE_FACTS_TEMPLATE = """\
# State-recovery probe (BLUEPRINT.md §10.2)
#
# Facts you KNOW are true and have definitely told the brain. Periodically ask it
# to reconstruct them cold, with no conversational context.
#
# WHY THIS EXISTS, separately from the golden set: task success saturates even with
# no memory at all, so a memory-off delta will often show approximately nothing and
# you will conclude the brain is worthless. It is not measuring the right thing. A
# second brain is a faithful, auditable model of what you decided — a property of
# the STORE, not of any downstream task. This measures that directly.
#
# This is also the number that degrades FIRST when a curated map starts evicting.

facts:
  - id: example-fact
    statement: "I prefer Postgres for new services on this project"
    probe: "database preference for new services"
    expect_terms: ["postgres"]
    workspace: default
"""


def write_templates(eval_dir: Path, candidates: list[Candidate]) -> dict[str, str]:
    """Write golden.yaml and state-facts.yaml, preserving anything already there."""
    eval_dir.mkdir(parents=True, exist_ok=True)
    golden = eval_dir / "golden.yaml"
    facts = eval_dir / "state-facts.yaml"

    existing = []
    if golden.exists():
        existing = (yaml.safe_load(golden.read_text()) or {}).get("cases", [])

    body = TEMPLATE_HEADER
    if existing:
        body += yaml.safe_dump(existing, sort_keys=False, indent=2).replace("\n- ", "\n  - ")
        body = body.rstrip() + "\n\n  # --- newly drafted, review before trusting ---\n"
    else:
        body += WORKED_EXAMPLES + "\n  # --- drafted from your corpus ---\n"

    for c in candidates:
        body += (
            f"\n  - id: {c.id}\n"
            f'    question: "{c.question}"\n'
            f"    expect_ids: [{c.expect_ids[0]}]\n"
            f"    expect_terms: {c.expect_terms}\n"
            f"    workspace: {c.workspace}\n"
            f'    note: "{c.note}"\n'
        )
    golden.write_text(body)

    if not facts.exists():
        facts.write_text(STATE_FACTS_TEMPLATE)

    return {"golden": str(golden), "state_facts": str(facts)}

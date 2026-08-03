# ADR 0002 — Migration Triggers

**Status:** Accepted
**Date:** 2026-08-03
**Implements:** `BLUEPRINT.md` §9.2, §10.1, §13

## Context

Every prior version of this project's research contemplated "add embeddings / add a graph / move to Postgres when needed" without defining *needed*. Undefined triggers get pulled by fashion rather than measurement, which is how a local-first brain acquires a vector server it cannot justify.

A first draft of the tenure trigger was **not statistically valid**: "negative slope for 3 consecutive weeks" on a 50-item golden set is swamped by binomial noise. Being rigorous about external evidence while sloppy about one's own instrument is the worse of the two failures.

## Decision

Four triggers on four independent axes. Each is a **decision prompt requiring a human, never an automatic migration.**

| Trigger | Axis | Condition |
|---|---|---|
| Rung 2 — dense retrieval + RRF | Tenure | Golden-set decline exceeding a **pre-registered margin of 10 percentage points**, sustained across **≥3 measurements**, reported with confidence intervals, on a frozen set of **≥150 items** — **AND** failure taxonomy (§10.4) shows *retrieval* dominant |
| Graph store | Workload shape | **≥20%** of *retrieval*-tagged golden-set failures require traversing **≥3 relation edges** — **AND** recursive SQL over `relations` exceeds the latency budget |
| PostgreSQL | Concurrency | A second concurrent writer, or a second human |
| Dedicated search service | Load | Measured p95 latency or QPS exceeds SQLite headroom |

**"Negative slope" alone is explicitly not a trigger.** Below 150 items the eval runner refuses to compute a slope at all and reports `n` instead of a falsely precise number.

### Rung 2 kill criterion

Entry is not permanent. If hybrid retrieval does not beat the lexical baseline by a stated margin on the golden set, **it is removed**, not kept because it was built. Sunk cost is not a retention argument.

### Rung ladder

| Rung | Implementation | Entry |
|---|---|---|
| 0 | ripgrep | day one |
| 1 | SQLite FTS5 / BM25 | when the index lands (Phase 1) |
| 2 | + local dense retrieval, RRF | the tenure trigger above |
| 3 | + reranker | rung 2 shipped AND precision still gating AND added latency acceptable |
| 4 | + external vector/search service | the load trigger above |

The implementation lives behind a stable `brain.search` tool boundary, so every rung change is invisible to callers.

## Consequences

- Phase 1 ships rungs 0–1 only. Nothing else is met on day one, by construction.
- The eval harness must exist before any trigger can fire, which is why it is Phase 0.5 scope rather than a later addition.
- The tenure trigger is motivated by a single-author, n=6, unreplicated preprint (§4.2). It is **parameterized entirely by local measurement** — no week count or recall percentage is inherited from that paper. If it fails to replicate, this ADR is unchanged.

## What would reverse this

- Measured evidence on the local corpus that a trigger fires far too late — i.e. retrieval quality is visibly bad while the criterion is unmet. That is a signal the margin or the taxonomy threshold is mis-set, and the numbers get revised here rather than bypassed.
- A trigger firing repeatedly and the subsequent build failing its kill criterion twice, which would indicate the axis itself is wrong.
- Multi-user adoption, which moves the Postgres trigger from hypothetical to imminent and may justify reordering the phases.

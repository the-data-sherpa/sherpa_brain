# Counterpoints and Additions to `RESEARCH.md`

## A second-opinion review of the agent "second brain" blueprint

**Status:** Review — not yet reconciled with `RESEARCH.md`
**Date:** 2026-08-02
**Method:** Independent research weighted toward practitioners shipping production agents and toward independent/adversarial evaluation work, deliberately away from vendor product documentation. Where I cite a vendor, it is for a *measured result* or an *architectural retreat*, not a feature list.
**Relationship to `RESEARCH.md`:** This document assumes `RESEARCH.md` has been read. It does not restate it. It agrees with roughly 70% of it and argues with the rest.

---

## 0. Summary of the disagreement

`RESEARCH.md` is a good document. Its layering (evidence / mutable memory / rebuildable projections), its insistence on provenance, its refusal to let a vector store be canonical, and its deletion-and-backup section are all correct and better than most of what is written on this subject.

My objections are concentrated in four places:

1. **It is a 2024-era RAG architecture wearing 2026 clothes.** The centerpiece — a ten-step retrieve-fuse-rerank pipeline feeding a "context builder" that injects memories into the prompt — is the design that three separate production teams (Anthropic, Manus, Letta) have publicly walked *away* from. The alternative they walked toward is agent-driven pull via search tools over files. `RESEARCH.md` already contains that alternative (the neutral CLI), but sequences it last and treats it as an interop concern rather than the primary retrieval mechanism.

2. **It optimizes recall when the binding constraint is precision and token economics.** There is no cost model, no mention of KV-cache/prompt-cache behavior, and the retrieval metrics lead with recall@k. Recent evidence says injected-but-irrelevant context actively degrades answers, and that cache invalidation is a ~10× cost multiplier. Both push toward retrieving *less*, later, and on demand.

3. **The write path promises validation it cannot deliver, and the data model front-loads machinery that git already provides.** "Models propose, code validates" is right as a security posture and wrong as a correctness claim. And a single-user local brain gets transaction-time versioning, diffs, provenance, and rollback for free from git — which the document already uses for half the system.

4. **~40% of the document is vendor documentation summary that will be stale within two quarters, and it drove at least one recommendation that the practitioner evidence contradicts.** You asked specifically to avoid leaning on big-name vendors. §4 is seven subsections of exactly that.

There is also one omission I consider serious: **the document never says when a memory gets written.** It specifies an elaborate write pipeline but not its trigger. That decision dominates cost, noise, and poisoning exposure more than any storage choice in the document.

---

## 1. What I am not arguing with

Stated once so the rest reads as disagreement rather than dismissal.

- **Rebuildable projections.** Treating FTS rows, chunks, embeddings, summaries, and graph edges as derived and disposable is the single most valuable idea in the document. Keep it.
- **Content-addressed original artifacts.** Correct, cheap, and almost never done.
- **Deletion propagation and backup-resurrection defense (§14.5).** This is the section I'd expect a real system to skip and regret. Keep the tombstone-replay-before-serving requirement in acceptance criteria.
- **"Decay should affect activation, not truth" (§11.4).** A genuinely good formulation. Most systems conflate these and quietly start returning wrong answers about old facts.
- **Zero-tolerance gates on unauthorized retrieval, unauthorized execution, and deletion resurrection (§15.5).** Right, and correctly separated from the metrics that are allowed to be fuzzy.
- **Refusing to make a vector database canonical (§19).** Right for the stated reasons, and the argument below strengthens it rather than weakening it.
- **Bitemporality as a concept.** I argue below about *where* it lives, not whether valid-time and transaction-time need to be distinct. They do.

---

## 2. The retrieval architecture is the wrong shape

### 2.1 The evidence

`RESEARCH.md` §11.3 specifies a pipeline: classify → filter → lexical top 50–200 → dense top 50–200 → RRF → boost → rerank 20–100 → dedupe → return. §7's diagram then routes that into a "Context builder" which assembles what the model sees.

Three independent production teams have published the opposite conclusion in the last eighteen months:

- **Anthropic pulled vector search out of Claude Code.** Early versions used RAG plus a local vector database; the team removed the embedding pipeline, the vector store, and the chunking heuristics and replaced them with glob and grep driven by the model in a loop. Boris Cherny's reported summary of the result was that it "outperformed everything. By a lot." The stated reasons were staleness, permission complexity, privacy, and reliability — every one of which applies at least as strongly to a personal memory corpus. ([Pragmatic Engineer interview](https://newsletter.pragmaticengineer.com/p/building-claude-code-with-boris-cherny), [analysis](https://vadim.blog/claude-code-no-indexing/))
- **Manus treats the filesystem as unbounded context** and has the agent read and write files on demand rather than pre-loading retrieved passages. ([Manus](https://manus.im/blog/Context-Engineering-for-AI-Agents-Lessons-from-Building-Manus))
- **Letta's filesystem-memory result** — which `RESEARCH.md` §4.8 already cites and correctly discounts as vendor-reported — points the same way, and its narrow conclusion ("simple, iterative, inspectable tools can compete") is the one that survives the discount.

Cursor, Windsurf, Cline, and Sourcegraph Amp have reportedly followed. That is not proof, but four teams with strong incentives to keep an index and the engineering budget to maintain one choosing not to is a meaningful signal.

### 2.2 The counter-counter, stated honestly

Code search is an unusually favorable case for grep. Identifiers are exact, the corpus is structured, symbols are unique, and the agent can verify a hit by reading the file and running the code. Personal memory is paraphrase-heavy, has no compiler, and often has no exact token to search for — "what did I decide about the deployment thing" has no grep target.

So I am not claiming embeddings are useless for this corpus. I am claiming three narrower things:

1. **The burden of proof has moved.** `RESEARCH.md` §11.2 says "add dense retrieval only after measurement" — good — but §11.3 then specifies the full hybrid pipeline as *the* architecture, and Phase 4 treats adding it as expected. Make Phase 4 genuinely conditional, with a written kill criterion.
2. **The pipeline-vs-loop question is separate from the lexical-vs-dense question, and the document conflates them.** You can have dense retrieval *inside a tool the agent calls iteratively*. The thing to drop is not embeddings, it's the single-shot fire-and-forget assembly.
3. **Iteration beats ranking.** The reason agentic search wins is not that grep is better than cosine similarity. It is that the agent gets to look at the result, notice it's wrong, and search again with a better query. A ten-step ranking pipeline spends enormous effort getting one shot right; a loop gets three cheap shots. `RESEARCH.md`'s own §5.3 (Weng) says to test "whether the agent can decide to search, formulate a useful query, recover evidence, and use it correctly" — and then the architecture takes that decision away from the agent.

### 2.3 What I'd change

Reverse the sequencing. The neutral CLI (`brain search`, `brain get`) is currently Phase 5, framed as interop. It should be **Phase 2**, framed as the primary retrieval interface, exposed to the agent as MCP tools from day one. The auto-injecting context builder should be Phase 4 or later, built only if the tool loop measurably underperforms.

Concretely, in §7's diagram: the arrow from `Retrieve` to `Context` becomes an arrow from `Agents` to `Retrieve` and back.

---

## 3. Missing: token economics and cache behavior

This is the largest gap. There is no cost model anywhere in `RESEARCH.md`, and no mention of prompt-cache or KV-cache behavior.

Manus calls KV-cache hit rate "the single most important metric for a production-stage AI agent," with a concrete number: on Claude Sonnet, cached input tokens ran 0.30 USD/MTok against 3 USD/MTok uncached — a 10× differential. Their rules follow directly: keep the prompt prefix stable, make context append-only, use deterministic serialization, and **do not dynamically add or remove tools mid-session**, because a single changed token near the front invalidates everything after it. ([Manus](https://manus.im/blog/Context-Engineering-for-AI-Agents-Lessons-from-Building-Manus))

This has direct consequences the document does not address:

- **A context builder that injects freshly-retrieved memories near the top of the prompt destroys the cache on every turn.** If the brain is doing its job across a long session, this is the dominant cost line. Injected memory must go *late* in the context (after the stable prefix), or be pulled by tool call — which is naturally append-only and cache-friendly.
- **The `AGENTS.md` / `CLAUDE.md` adapter files in §13.3 are part of the stable prefix.** Regenerating them per-session, or letting auto-memory rewrite them, silently invalidates the cache. Generated adapter files should change on human commits, not on memory writes.
- **Tool count is a fixed tax.** Nine MCP tools' descriptions are in the prefix of every request for the whole session. See §7 below.
- **MCP 2026-07-28 added cacheable list results and a stateless protocol core** ([spec](https://modelcontextprotocol.io/specification/2026-07-28), [release notes](https://blog.modelcontextprotocol.io/posts/2026-07-28/)). This is directly relevant and worth designing to rather than treating the revision only as a compatibility hazard, which is how §13.2 frames it.

**Addition to acceptance criteria (§18):** a cost-per-session figure, a measured cache hit rate, and a stated rule for where retrieved content is allowed to be placed in the context.

---

## 4. The objective function is wrong: precision and abstention, not recall

`RESEARCH.md` §15.3 leads its retrieval metrics with recall@k, precision@k, hit rate@k, MRR, nDCG. That is a document-retrieval metric suite. For a memory system it is close to backwards.

**Chroma's "Context Rot" study** evaluated 18 frontier models (GPT-4.1, Claude 4, Gemini 2.5, Qwen3 families) and found performance degrades non-uniformly as input length grows, well before advertised context limits — on tasks as simple as retrieval and text replication. Distractors hurt disproportionately. Most counterintuitively, coherent well-structured haystacks degraded attention *more* than shuffled ones. ([Chroma](https://www.trychroma.com/research/context-rot))

Hamel Husain's reading is the operationally useful one: this is not "RAG is dead," it is the opposite — "thoughtful context engineering and retrieval is more important than ever," because you cannot solve relevance by adding capacity. ([hamel.dev](https://hamel.dev/notes/llm/rag/p6-context_rot.html))

The consequence for this project: **retrieving 20 memories when 2 are relevant is not a mild inefficiency, it is an active regression.** A memory system's failure mode is not "didn't find it," it's "found it plus eighteen other things and the model followed the wrong one." `RESEARCH.md` §6 item 4 ("more context can reduce quality") states this correctly and then §15.3 doesn't measure it.

### 4.1 Metrics I'd add to §15.3

- **Memory-off control arm.** Every eval case run twice — with and without the brain. Report the delta. This is the only number that answers "is this system worth its cost," and it is the number memory-system vendors consistently do not report (see §5).
- **Tokens injected per correct citation.** A context-efficiency ratio. Directly targets over-retrieval.
- **Distractor-induced regression rate.** Cases the model answers correctly with no memory and incorrectly with memory. This should be a tracked gate, not an anecdote.
- **Abstention correctness under retrieval.** §15.4 has abstention when evidence is absent; the harder case is abstention when evidence is *present but irrelevant*, which is what context rot produces.

---

## 5. The benchmark list in §15.6 includes a broken benchmark

`RESEARCH.md` lists LoCoMo first among supplementary benchmarks and cites it twice in §5.7. An independent audit found:

- **6.4% of the answer key is wrong** — 99 score-corrupting errors across 1,540 questions, roughly double the ~3.3% baseline error rate across major ML benchmarks. Error classes include hallucinated facts in the answer key, incorrect temporal reasoning, and speaker misattribution. In at least one case the gold answer depends on a field no memory system ingests.
- **The standard LLM judge (gpt-4o-mini) accepted 62.81% of deliberately wrong but topically adjacent answers.** Vague answers that named the right topic while missing every specific detail passed nearly two-thirds of the time.
- **Theoretical ceiling for a perfect system is ~93.6%**, and score differences below the noise floor are uninterpretable. ([Penfield Labs audit](https://dev.to/penfieldlabs/we-audited-locomo-64-of-the-answer-key-is-wrong-and-the-judge-accepts-up-to-63-of-intentionally-33lg))

The practical implication is worse than "noisy benchmark": the optimal strategy for scoring well on LoCoMo is context-stuffing plus long topically-adjacent answers — i.e. it *rewards* precisely the over-retrieval behavior §4 above says to avoid. Any system tuned against it will be tuned in the wrong direction.

This also retroactively weakens the Letta and Mem0 numbers `RESEARCH.md` cites, and the well-known Mem0-vs-Zep dispute (58.44% vs 75.14% for the same system, depending on who ran it) is a symptom rather than an aberration. ([Zep's rebuttal](https://blog.getzep.com/lies-damn-lies-statistics-is-mem0-really-sota-in-agent-memory/))

**MemDelta** generalizes the problem: agent-memory evaluations are riddled with confounds that conflate memory contribution with architectural and information-access differences. Under controlled baselines, simple approaches frequently match or exceed specialized memory systems, and the field under-reports the no-memory baseline and write-path cost. ([arXiv 2606.29914](https://arxiv.org/pdf/2606.29914))

**Change to §15.6:** move public benchmarks from "supplement" to "smoke test only, never a gate," name LoCoMo explicitly as unsuitable for cross-system comparison, and make the local golden set plus the memory-off control the sole release gate. This is also Jason Liu's position on RAG generally — synthetic questions per chunk to bootstrap recall measurement, then segment on observed failures rather than chasing aggregate scores. ([jxnl.co](https://jxnl.co/writing/2025/01/24/systematically-improving-rag-applications/))

---

## 6. Files vs. SQLite: the document draws the line in the wrong place

§7.1 makes SQLite authoritative for mutable memories and git authoritative for curated knowledge. §6.1 justifies this: files can't do concurrent writes, transactional updates, ACLs, or deletion propagation.

Each of those is true. **None of them applies to the stated target**, which is §1's "local-first, single-user." There is one writer. There are no ACLs. Deletion propagation to derived indexes is a rebuild, which the document already requires to be cheap.

Meanwhile, git already provides, for free, most of what §8 builds by hand:

| §8 requirement | Hand-built in SQLite | Already in git |
|---|---|---|
| Immutable revisions | `memory_revisions` table | every commit |
| Transaction time | `recorded_at` column | commit timestamp |
| Supersession chain | `supersedes_revision_id` | parent commit |
| Content hash | `content_hash` column | blob SHA |
| Audit of who changed what | audit table | author + `git blame` |
| Rollback | manual | `git revert` |
| Human review of proposed writes | review-queue table | branch + diff |

That last row is the interesting one. `RESEARCH.md` §10.3 wants agent-authored procedural memories to enter a review queue before promotion. A git branch with a diff *is* a review queue, and it's one you already know how to use, with tooling that already exists.

What git does *not* give you is query: as-of queries, joins across memories, ranked full-text search. That is exactly the job of a rebuildable index — which the document's own §7.1 says indexes are for.

**This architecture already exists and is proven at small scale.** Basic Memory stores everything as plain Markdown under a single directory with a SQLite index alongside for search and traversal; files are the source of truth, the index is disposable, and the same directory opens directly as an Obsidian vault. ([repo](https://github.com/basicmachines-co/basic-memory))

### 6.1 What this buys, concretely

- **The agent's native tools work.** Read, Grep, Glob, Edit already operate on markdown files. No MCP server needed to *read* memory. That collapses most of §13 into "point the harness at the directory."
- **Export is a no-op.** §18 requires complete Markdown export; if markdown is canonical, that requirement is satisfied by existing.
- **Phase 1 shrinks by roughly an order of magnitude of code.** No migrations, no repository layer, no revision bookkeeping — a directory, YAML front matter, and a git commit.
- **It composes with §2's recommendation.** Agent-driven pull over files is the exact pattern Claude Code and Manus converged on.

### 6.2 What it costs, and where I'd hold the line

- **Concurrency.** Two agents writing simultaneously will conflict. For a single user this is a merge conflict, not a corruption. It becomes real at multi-user, which is exactly the PostgreSQL migration trigger §12.1 already contemplates.
- **As-of queries need the index.** Valid-time is a YAML field; querying across it needs SQLite. Fine — build the index, just don't make it canonical.
- **Referential integrity.** Nothing stops a dangling `[[link]]`. Handle with `brain validate`, which §13.4 already specifies.

**Recommendation:** don't adopt this as doctrine — adopt it as the Phase 1 default, because it is drastically cheaper to build and the failure modes it exposes you to are ones you've explicitly scoped out. Keep the SQL schema in §8 as the designed migration target. The migration-boundary discipline in §16.3 is what makes deferring safe, and it's already written.

If you disagree and want SQLite canonical from day one, the thing I'd ask you to change regardless is §8.3's requirement list: it demands fourteen metadata fields on every memory revision. Most memories in a personal brain are one sentence. Front-load five (id, type, provenance class, valid_from, evidence) and let the rest be optional.

---

## 7. "Models propose, code validates" oversells what code can do

§10.1 and §19 present model-proposed / code-committed writes as resolving the MemGPT-vs-12-Factor disagreement. As a *security* posture it is correct and I'd keep it. As a *correctness* claim it doesn't hold, and the document should say so.

Deterministic code can validate: schema conformance, scope legality, secret patterns, that cited evidence IDs exist, idempotency, retention policy. That's real and worth having.

Deterministic code cannot validate: whether the claim is true, whether the evidence actually supports it, whether it should be remembered at all, or — critically — §10.1 step 5, "check for duplicates, contradictions, and temporal updates." Deduplication and contradiction detection over natural-language claims is a semantic judgment. It requires another model call. **That model call reads attacker-controlled text and is therefore itself an injection surface**, which §14.1 does not account for.

Two additions I'd make:

1. **Two-pass extraction with context isolation.** The validating extraction should run in a fresh context that sees the candidate memory and the cited evidence span — not the full conversation. A poisoning payload that steers extraction should not also be present when the validator runs.
2. **Unreviewed memories expire.** The strongest practical control is not a smarter validator, it's making the default *decay*. A memory written without human confirmation gets a short review deadline; if it is never confirmed and never retrieved-and-cited, it lapses. This inverts the accretion dynamic that makes memory systems degrade over months, and it's cheap.

### 7.1 Drop the confidence floats

§8.3 asks for "confidence components rather than one unexplained score," and §10.1's example emits `"extraction": 0.92` alongside `"source_authority": "direct-user-statement"`.

The second field is useful. The first is not. LLM-produced confidence scalars are poorly calibrated, and §11.4 then proposes to use them as ranking signals — which multiplies false precision into retrieval order. Store the **categorical provenance class** (`direct-user-statement` / `inferred-from-behavior` / `third-party-document` / `agent-speculation`) and derive priority from that ordering. It is honest, inspectable, and a human can audit it. A 0.92 cannot be audited by anyone.

---

## 8. Missing: what triggers a write

This is the omission I'd fix first. §10 specifies a ten-step write pipeline and never says what invokes it.

The choice dominates everything downstream:

| Trigger | Cost | Noise | Poisoning exposure |
|---|---|---|---|
| Every turn | High (extraction call per turn) | Very high | Maximal |
| End of session | Moderate, batched, off critical path | Moderate | Bounded per session |
| Explicit ("remember this") | Near zero | Near zero | Minimal — user-intentioned |
| Scheduled consolidation | Amortized | Low, and reviewable | Bounded, and offline |

**Recommendation:** explicit-first, session-batch second, scheduled consolidation third, never per-turn. This matches the sleep-time-compute direction `RESEARCH.md` §4.8 already cites, and it matches §10.2's "outside the interactive latency path" — the document just never connects that to a trigger policy.

It also has an underrated benefit: explicit writes are the ones users trust, and Willison's critique below is fundamentally about untrusted implicit writes.

---

## 9. Security: two additions and one direct conflict with §13.3

§14.1 is strong. Three things to add.

**Memory poisoning now has a formal classification.** OWASP's 2026 Top 10 for Agentic Applications lists **ASI06: Memory & Context Poisoning**. The framing worth adopting: the distinguishing property versus prompt injection is *temporal decoupling* — poison planted today fires weeks later when semantically triggered, which means your detection window and your incident-response story are both completely different from session-scoped injection. Reported attack success rates against unhardened agent memory implementations run from 80% to nearly 100%. ([OWASP Agentic Top 10](https://genai.owasp.org/2025/12/09/owasp-top-10-for-agentic-applications-the-benchmark-for-agentic-security-in-the-age-of-autonomous-ai/), [survey, arXiv 2604.16548](https://arxiv.org/pdf/2604.16548), [attacks, arXiv 2607.06595](https://arxiv.org/abs/2607.06595))

**The harness's own memory loading is part of your threat model.** OWASP's May 2026 piece on memory as an attack surface cites a concrete precedent: Claude Code v2.1.50 removed user memories from the system prompt specifically to close a high-trust override path. ([OWASP](https://genai.owasp.org/2026/05/13/memory-is-a-feature-it-is-also-an-attack-surface/))

**This directly conflicts with §13.3.** The adapter table proposes generating a `CLAUDE.md` that imports `AGENTS.md`, and mirroring skills into `.claude/skills`. Those files are loaded as high-trust instruction context. If any generated adapter can contain content derived from ingested material or from agent-proposed memory, you have rebuilt the exact path the harness vendor just removed.

**Rule I'd add to §13.3:** generated adapter files may contain *pointers to tools and paths only* — never memory content, never ingested text. Content reaches an instruction file exclusively via a human-reviewed git commit. This is enforceable with a CI check on the adapter generator and belongs in the §18 acceptance criteria.

---

## 10. Interop scope and tool surface are both too large

**Five harness adapters plus conformance testing (Phase 5) is a treadmill against five independently-moving targets.** But `AGENTS.md` plus one MCP server plus one CLI already covers all five by construction — that is the entire point of those conventions. Cursor rules, Aider `--read` flags, and OpenCode-specific JSON are contingency work. Do them when a specific harness demonstrably breaks, not as planned scope. The conformance test that matters is one: *can a generic MCP client find, cite, correct, and forget a memory?* If yes, harness-specific breakage is a small fix, not a design failure.

**Nine MCP tools is too many** (§13.2: five read, four write). Chip Huyen's point that `RESEARCH.md` §5.2 already cites — keep memory tools few and narrow — argues against its own tool list. And per §3 above, every tool description is prefix tokens you pay for on every request in the session.

Collapse to four:

- `brain.search(query, scope?, as_of?)` — absorbs `list_collections` via a scope enumeration
- `brain.get(uri, include_history?, include_provenance?)` — absorbs `get_history` and `explain_provenance` as flags
- `brain.write(op, ...)` — a typed union of propose / correct / promote, validated server-side
- `brain.forget(id)` — kept separate deliberately; destructive operations should never be a flag on a general tool

---

## 11. Missing: the user-facing failure mode

Simon Willison's objection to ChatGPT's memory is worth reading as a *design* critique, not a privacy one. His complaint was that the system builds a model of you and doesn't give you the model to inspect — and that it produces **context collapse**, where data from separate spheres of your life spills together. His example is small and perfect: he asked for his dog in a pelican costume and got a "Half Moon Bay" sign, because the system remembered he'd been there. He explicitly contrasts this favorably with Claude's approach of surfacing memory access as visible tool calls. ([simonwillison.net](https://simonwillison.net/2025/May/21/chatgpt-new-memory/))

`RESEARCH.md` has complete provenance in the database and none of it in the user's face. §18 requires "the user can inspect, correct, export, and forget memories" — that's an audit capability, exercised deliberately, after you already suspect something is wrong. The failure mode is that you never suspect.

**Two additions to §18:**

- **Memory used in an answer must be visible in that answer**, not silently injected. Retrieval should be an observable tool call, not an invisible prefix mutation. (This also happens to be the cache-friendly choice, per §3.)
- **Workspace isolation defaults to deny, not share.** Context collapse is a scope-default failure before it is a security failure. §14.3 currently treats workspace scoping as migration-readiness for multi-user; it earns its keep on day one for a single user with more than one project.

---

## 12. One place I'd reopen a closed decision: graphs

§19 defers knowledge graphs to post-MVP, starting with a relational `relations` table. I agree with the sequencing but not with how the tradeoff is described.

The document independently specifies bitemporal facts (§8.4), supersession chains (§8.3), and contradiction traversal (§11.5). That is substantially the Graphiti/Zep data model: bi-temporal edges tracking four timestamps (`t_created`, `t_expired`, `t_valid`, `t_invalid`), with conflicting facts *invalidated rather than deleted*, and episode-level provenance. ([Zep paper, arXiv 2501.13956](https://arxiv.org/abs/2501.13956))

So the honest framing is not "graphs are complexity we're deferring." It's: **you are hand-building half of a published, open-source data model, and should decide deliberately whether to adopt the model** (the edge semantics and invalidation rules — not necessarily Neo4j, not necessarily the service). Automatic fact invalidation on conflict is the one thing graph structure buys here that a relations table doesn't hand you.

I'd still start relational. But cite the model you're reimplementing, in `docs/decisions/`, so the next person knows it was a choice.

---

## 13. Roadmap: ship something in week one

Phase 0 through Phase 3 is a lot of engineering before the system does anything a user notices. Phase 0's "create representative evaluation fixtures before retrieval tuning" is well-intentioned but backwards in practice — fixtures written before real usage encode your assumptions about what you'll ask, and the whole value of Jason Liu's flywheel is that the failing segments are the ones you didn't predict.

**Proposed Phase 0.5 — usable in days, not months:**

1. `~/brain/` — markdown files, YAML front matter, git-initialized.
2. `brain search` = ripgrep with a scope filter. `brain get` = read a file.
3. MCP server exposing `search` and `get`. Read-only.
4. Writes are explicit only, and land as a git commit.
5. Log every query and every retrieved-vs-cited pair from day one.

That is a working second brain. It has provenance (git), evidence (the source file), portability (markdown), and inspectability (a text editor). It is missing bitemporality, embeddings, consolidation, ACLs, and validation — all of which the existing phases add, and all of which will be *better designed* once you have a month of real query logs to design against.

Then the existing Phase 1–6 sequence stands, with two reorderings: pull the CLI/MCP surface forward from Phase 5 into Phase 2 (per §2.3), and make Phase 4 conditional with a written kill criterion (per §2.3).

---

## 14. Consolidated change list

| # | Section | Change | Severity |
|---|---|---|---|
| 1 | §11.3, §7 diagram | Invert retrieval: agent pulls via tools in a loop; defer the auto-injecting context builder | **High** |
| 2 | §18, new section | Add cost model, cache-hit-rate target, and a placement rule for retrieved content | **High** |
| 3 | §10 | Specify the write *trigger*: explicit-first, session-batch, scheduled; never per-turn | **High** |
| 4 | §13.3 | Adapter files may contain pointers only, never memory content or ingested text; enforce in CI | **High** |
| 5 | §15.3 | Add memory-off control arm, tokens-per-citation, distractor-regression rate | **High** |
| 6 | §15.6 | Demote public benchmarks to smoke tests; name LoCoMo as unsuitable for comparison | Medium |
| 7 | §7.1, §8 | Reconsider markdown-canonical + SQLite-index for Phase 1; keep SQL schema as migration target | Medium |
| 8 | §8.3, §10.1 | Drop LLM confidence floats; keep categorical provenance class | Medium |
| 9 | §10.1 | Two-pass extraction with context isolation for the validator; unreviewed memories expire | Medium |
| 10 | §13.2 | Collapse nine MCP tools to four | Medium |
| 11 | §18, §14.3 | Memory use visible at answer time; workspace isolation default-deny | Medium |
| 12 | §17 | Insert Phase 0.5; move CLI/MCP to Phase 2; make Phase 4 conditional | Medium |
| 13 | §4 | Compress seven vendor subsections to one page of converged conclusions | Low |
| 14 | §19 | Note the Graphiti bitemporal model explicitly as the thing being reimplemented | Low |
| 15 | §10.1 | State plainly that code validates policy, not truth | Low |

---

## 15. Questions I'd add to Appendix B

The existing ten are good. Six more, all of which have to be answered before Phase 1 rather than during it:

11. What triggers a memory write, and what is the monthly extraction cost at expected usage?
12. Where in the context is retrieved memory permitted to appear, and what is the target cache hit rate?
13. What is the memory-off baseline score on the golden set, and what delta justifies the system continuing to exist?
14. What happens to a memory that is written, never confirmed, and never retrieved for 90 days?
15. Can any content derived from ingested material reach a file the harness loads as instructions? Prove it cannot.
16. When the brain is confidently wrong, what surfaces that to the user *before* they act on it?

Question 13 is the one I'd insist on. `RESEARCH.md` is a design for a system that is assumed to be worth building. MemDelta's finding — that simple baselines match specialized memory systems under controlled comparison — means that assumption is the thing most worth testing, and it is testable in Phase 0.5 for almost nothing.

---

## Sources

Weighted toward practitioners and independent/adversarial evaluation, per the review brief.

**Production practitioners**
- [Manus — Context Engineering for AI Agents: Lessons from Building Manus](https://manus.im/blog/Context-Engineering-for-AI-Agents-Lessons-from-Building-Manus)
- [Building Claude Code with Boris Cherny — Pragmatic Engineer](https://newsletter.pragmaticengineer.com/p/building-claude-code-with-boris-cherny)
- [Claude Code Doesn't Index Your Codebase](https://vadim.blog/claude-code-no-indexing/)
- [Cognition — Don't Build Multi-Agents (Walden Yan)](https://cognition.com/blog/dont-build-multi-agents)
- [Basic Memory — markdown-canonical, SQLite-index agent memory](https://github.com/basicmachines-co/basic-memory)

**Independent and adversarial evaluation**
- [Chroma — Context Rot: How Increasing Input Tokens Impacts LLM Performance](https://www.trychroma.com/research/context-rot)
- [Hamel Husain — notes on context rot](https://hamel.dev/notes/llm/rag/p6-context_rot.html)
- [Penfield Labs — LoCoMo audit](https://dev.to/penfieldlabs/we-audited-locomo-64-of-the-answer-key-is-wrong-and-the-judge-accepts-up-to-63-of-intentionally-33lg)
- [MemDelta: Controlled Baselines and Hidden Confounds in Agent Memory Evaluation (arXiv 2606.29914)](https://arxiv.org/pdf/2606.29914)
- [Zep — Is Mem0 Really SOTA in Agent Memory?](https://blog.getzep.com/lies-damn-lies-statistics-is-mem0-really-sota-in-agent-memory/)
- [Jason Liu — Systematically Improving RAG Applications](https://jxnl.co/writing/2025/01/24/systematically-improving-rag-applications/)

**Security**
- [OWASP Top 10 for Agentic Applications (2026) — ASI06 Memory & Context Poisoning](https://genai.owasp.org/2025/12/09/owasp-top-10-for-agentic-applications-the-benchmark-for-agentic-security-in-the-age-of-autonomous-ai/)
- [OWASP — Memory Is a Feature. It Is Also an Attack Surface](https://genai.owasp.org/2026/05/13/memory-is-a-feature-it-is-also-an-attack-surface/)
- [A Survey on Long-Term Memory Security in LLM Agents (arXiv 2604.16548)](https://arxiv.org/pdf/2604.16548)
- [When Agents Remember Too Much: Memory Poisoning Attacks (arXiv 2607.06595)](https://arxiv.org/abs/2607.06595)

**Design critique and data models**
- [Simon Willison — I really don't like ChatGPT's new memory dossier](https://simonwillison.net/2025/May/21/chatgpt-new-memory/)
- [Zep: A Temporal Knowledge Graph Architecture for Agent Memory (arXiv 2501.13956)](https://arxiv.org/abs/2501.13956)
- [sqlite-vec (Alex Garcia)](https://github.com/asg017/sqlite-vec) — v0.1.9 stable as of March 2026; the concrete option behind §11.2's "pinned SQLite vector extension"
- [MCP specification 2026-07-28](https://modelcontextprotocol.io/specification/2026-07-28) and [release notes](https://blog.modelcontextprotocol.io/posts/2026-07-28/)

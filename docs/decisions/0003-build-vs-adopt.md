# ADR 0003 — Build vs. Adopt

**Status:** Accepted
**Date:** 2026-08-03
**Implements:** `BLUEPRINT.md` §18, §2.1

## Context

Three research documents posed twenty-six questions between them. **None asked whether to build this at all.** The assumption that a bespoke system is warranted was the least examined premise in the entire design, which is exactly the kind of thing that should be written down.

The prior art is not thin:

- **Basic Memory** — plain Markdown under one directory with a SQLite index alongside; files canonical, index disposable, opens directly as an Obsidian vault. This is substantially the storage design adopted here.
- **memweave** — the same design stated explicitly: "Markdown files as the source of truth and SQLite as a derived index that's always rebuildable."
- **Letta, Mem0, Zep, Cognee** — the heavier end, with managed extraction and temporal graphs.

OSS agent-memory projects reportedly passed 80,000 aggregate GitHub stars by Q1 2026.

## Decision

**Build**, for two reasons, each independently sufficient.

### 1. The differentiated parts are the parts everyone treats as an afterthought

The three properties this project exists for (`0004-non-goals.md`) are precisely what existing systems bolt on last:

- **Evidence spans** — no system in the survey traces a claim to a byte range in preserved source.
- **Propagating erasure** — deletion that reaches every derived representation *and survives backup restore* is, as far as the research found, unimplemented anywhere. Most systems delete a row.
- **Portability** — most store canonical state in a service-specific schema or a vector index.

Adopting a system and adding these means rewriting its storage layer, which is not adoption.

### 2. Dependency risk over the intended lifetime

*Why Memory Components Fail: Eight Years of License and Sustainability Events in Open-Source Data Infrastructure* (arXiv 2606.24896) catalogues license changes and abandonment in exactly this dependency class. For something meant to outlive several model generations, that argues for a minimal dependency surface — **SQLite, ripgrep, markdown, git**, all of which have decade-plus track records and no vendor.

The runtime dependency budget is four packages (`typer`, `pyyaml`, `mcp`, `jsonschema`), and canonical state is plain files, so **even the language is a reversible decision** — the tool can be rewritten without touching a byte of data.

## Consequences

- Storage design converges with Basic Memory and memweave. That is corroboration, not coincidence, and it is worth citing rather than presenting as novel.
- The project's budget goes to evidence, forgetting, and portability. Anything else is subject to `0004-non-goals.md`.

## What would reverse this

**Concrete and falsifiable:** if a Phase-1 evaluation shows an existing system satisfies all three of the §2.1 properties — evidence spans down to the source, erasure that propagates and survives backup restore, and portability independent of any harness — then **adopt it and contribute upstream instead of continuing to build.**

Also reversing:

- The maintenance burden exceeding the value delivered, measured by the memory-off control arm and state-recovery probe showing no benefit over a simple baseline (§10.2, §10.3). MemDelta's finding — that simple approaches frequently match specialized memory systems under controlled comparison — makes this the single most important thing to keep testing.
- A harness shipping evidence spans and propagating erasure natively, which would collapse the differentiation to portability alone. Portability by itself is probably not worth a bespoke system.

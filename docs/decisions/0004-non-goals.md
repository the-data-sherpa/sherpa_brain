# ADR 0004 — Non-Goals

**Status:** Accepted
**Date:** 2026-08-03
**Implements:** `BLUEPRINT.md` §2.1, §2.2, §11.6

## Context

Agent harnesses now ship file-based memory, YAML front matter, a loaded index file, on-demand topic files, skills, and MCP. A design that reimplements those spends its budget competing with vendors on their own roadmap, and loses.

Written as an ADR so scope creep is a **visible violation** rather than a drift.

## Decision

### The three things this project is for

Storage is not one of them.

1. **Evidence** — every durable claim traceable to a source span in preserved original material. No harness does this.
2. **Forgetting** — deletion that propagates to every derived representation and survives backup restore.
3. **Portability** — the store outlives any harness, model, or vendor.

### Declared non-goals

| Non-goal | Reason |
|---|---|
| Rebuilding what the harness ships | No custom context injector, no custom skill loader, no reimplementation of `CLAUDE.md` semantics |
| Harness-specific adapters as planned scope | `AGENTS.md` + one MCP server + one CLI covers every target by construction. Cursor / Aider are contingency work, done on demonstrated breakage. OpenCode was promoted under the clause below — see the amendment |
| Automatic entity resolution | Documented as unsolved in production. At single-user scale a hand-maintained alias list of ~200 entries beats a model |
| Multi-user, ACLs, concurrent writers in v1 | `workspace` and `owner` fields carried from day one so migration is not a data-model rewrite |
| Vector server, graph database, reranker | Gated on measured triggers in `0002-migration-triggers.md` |
| Background extraction in v1 | Deferred by interview decision; the extractor is an interface with a stub |

### An honest limit that belongs here

`0005-storage-boundary.md` makes agent-native `Read`/`Grep`/`Glob` over the memory directory a headline benefit. It follows that **any harness process can read any memory file regardless of its `workspace` field.**

> In the local single-user model, workspace scoping is a **relevance and context-collapse control at the retrieval boundary. It is not a security boundary.** The security boundary is the OS user account and full-disk encryption.

Real isolation requires directory-per-workspace with OS permissions (available, opt-in, at the cost of cross-workspace search) or the shared-production path. Claiming enforcement the file layout cannot deliver would be the kind of unearned security claim this project criticizes the field for.

## Consequences

- Phase 5's five-adapter matrix collapses to two, chosen explicitly, plus one conformance test that is the thing actually worth testing: *can a generic MCP client find, cite, correct, and forget a memory?*
- Any proposal to build something on this list must first amend this ADR.

## What would reverse this

- A harness removing file-based memory or MCP support, which would move "don't rebuild what the harness ships" from prudent to impossible.
- A second human using the system, which converts the multi-user non-goal into a requirement and makes workspace-as-security-boundary necessary rather than overclaimed.
- Demonstrated breakage in a specific harness, which promotes that one adapter from contingency to scope — for that adapter only, not the matrix.

## Amendment — 2026-08-04: OpenCode promoted

The clause above fired. OpenCode is in daily use by the operator, and the convention-covers-it argument holds for only half of it: it reads `AGENTS.md` natively, but its MCP configuration is a different schema — `mcp` rather than `mcpServers`, `command` as a single argv array, `environment` rather than `env`. The generic `.mcp.json` is not rejected by OpenCode; it is *ignored*, which makes the breakage silent rather than loud, and silent is the case worth spending a target on.

Promoted for OpenCode only. Cursor and Aider remain contingency. The adapter merges into an existing `opencode.json` rather than overwriting it, because unlike `.mcp.json` that file is the harness's main config and holds unrelated user settings.

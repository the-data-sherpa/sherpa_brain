# brain

A durable, local-first second brain for AI agents.

Not a vector database, and not a folder of notes. A small information system that
preserves evidence, remembers selectively, forgets completely when asked, and
outlives the model, harness, and vendor it was used with.

## What it is for

Three properties, and storage is not one of them:

1. **Evidence** — every durable claim traces to a source span in preserved original material.
2. **Forgetting** — deletion propagates to every derived representation and survives backup restore.
3. **Portability** — the store outlives any harness, model, or vendor.

Everything else it deliberately does not do. See [`docs/decisions/0004-non-goals.md`](docs/decisions/0004-non-goals.md).

## How it is built

- **Markdown files are canonical.** Agent-native `Read`/`Grep`/`Edit` work directly on them; export is a no-op because markdown *is* the format.
- **Git tracks only what is never erased** — curated knowledge and the tombstone ledger. Mutable memory lives outside git, because git makes erasure maximally hard and forgetting is a stated goal.
- **SQLite holds zero canonical bytes.** Drop it and reindex; nothing is lost. Enforced by test, not convention.
- **The agent pulls through a tool loop.** Nothing is auto-injected into the prompt prefix.
- **Models propose, code enforces policy** — and the code says plainly which of the two it can actually validate.

## Documentation

| Document | What it is |
|---|---|
| [`docs/BLUEPRINT.md`](docs/BLUEPRINT.md) | The design. Consolidates three research documents; adversarially reviewed to consensus over twenty rounds. |
| [`docs/IMPLEMENTATION-PLAN.md`](docs/IMPLEMENTATION-PLAN.md) | Phase 0.5 + Phase 1 build plan, reviewed to consensus over eleven rounds. |
| [`docs/decisions/`](docs/decisions/) | Six ADRs, each stating what would reverse it. |
| [`docs/RESEARCH*.md`](docs/) | The source research and its two adversarial reviews, kept for provenance. |

Both `BLUEPRINT.md` and `IMPLEMENTATION-PLAN.md` record their *failed* attempts alongside
the accepted design, because a design's discarded branches are more useful to the next
reader than a clean surface.

## Status

**Phase 0.5 and Phase 1 complete, plus production hardening.** All fourteen build
steps, all nineteen acceptance criteria, **182 tests**, mypy strict clean, four runtime
dependencies.

```
brain init / doctor / install-timers      brain forget <id> [--kind artifact|event]
brain remember "..."                      brain sync
brain search <query>                      brain backup create|verify|restore
brain get <id> --history                  brain export <dir>
brain ingest <file>                       brain expire / unexpire
brain record "..."                        brain reconcile
brain evidence <ref> --lines 4-8          brain conflicts list|show|resolve
brain ledger init|status                  brain eval bootstrap|run|probe|slope
```

Plus an MCP server exposing four tools. See [`docs/RUNBOOK.md`](docs/RUNBOOK.md) to
operate it.

**Start here:** `brain doctor` reports every quiet failure mode in one place and exits
`4` when a safety property is not holding.

**Before deleting anything**, configure the off-device replica — until then quorum is
unreachable and every deletion reports `pending` forever (RUNBOOK §1.1).

**What is deliberately absent:** background extraction, consolidation, dense retrieval,
a graph store, and PostgreSQL. Each is gated on a measured trigger in
[`docs/decisions/0002-migration-triggers.md`](docs/decisions/0002-migration-triggers.md),
and none of those triggers is met on day one, by construction.

**The honest risk:** capture is explicit-only. Nothing is written unless you write it.
Every personal knowledge system that failed, failed of disuse rather than corruption —
`brain doctor` warns on an empty store for that reason. Closing it properly means
Phase 2 background extraction, which needs model calls you deferred.

## Requirements

Python 3.12+, `ripgrep`, `git`, and a local filesystem supporting atomic rename and
`RENAME_EXCHANGE`. The store refuses to start on network filesystems and sync folders —
see [`src/brain/config.py`](src/brain/config.py).

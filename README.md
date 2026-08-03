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

Phase 0.5 + Phase 1 under construction. Not yet usable.

## Requirements

Python 3.12+, `ripgrep`, `git`, and a local filesystem supporting atomic rename and
`RENAME_EXCHANGE`. The store refuses to start on network filesystems and sync folders —
see [`src/brain/config.py`](src/brain/config.py).

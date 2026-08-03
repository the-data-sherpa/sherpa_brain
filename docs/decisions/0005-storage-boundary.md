# ADR 0005 — The Storage Boundary

**Status:** Accepted
**Date:** 2026-08-03
**Implements:** `BLUEPRINT.md` §6.2, §6.3, §11.5

## Context

Three prior research documents drew this line three different ways, all wrong:

- **Mutability.** `RESEARCH.md` put mutable memory in SQLite and curated knowledge in git. Wrong axis — mutability is not what makes git a bad host.
- **Git as universal substrate.** `RESEARCH-COUNTERPOINTS.md` argued git supplies revisions, transaction time, supersession, content hashes, audit, rollback, and review for free.
- **Markdown scoped to non-erasable.** `RESEARCH-ADJUDICATION.md` correctly refuted git on two grounds — no valid-time, and erasure requires history rewriting — but concluded markdown-canonical should be scoped to curated knowledge only.

The third refutation is right about **git** and wrong about **files**. It conflated *markdown-canonical* with *git-canonical*. They are separable.

## Decision

**The line is erasability, not mutability.**

| Class | Canonical store | Git-tracked? | Erasure |
|---|---|---|---|
| Curated knowledge — policies, skills, ADRs, `AGENTS.md` | Markdown in repo | **Yes** | Never erased by design |
| Mutable memory | Markdown in `$XDG_STATE_HOME/brain/memories/` | **No** | `rm` + tombstone + reindex |
| Revision history | Files in `memories/.revisions/` | **No** | `rm` + tombstone + reindex |
| Event log | `events/YYYY-MM-DD.jsonl` | **No** | Redaction fork + tombstone |
| Original artifacts | Content-addressed blobs | **No** | `rm`/fork + tombstone |
| Erasure ledger | `tombstones.jsonl` + private GitHub `brain-ledger` | **Its own repo** | Append-only; never erased |
| All indexes | SQLite | **No** | Drop and rebuild |

Two supporting rules:

1. **SQLite holds zero canonical bytes.** Dropping `brain.sqlite3` and reindexing loses nothing. This is enforced by test, not convention (three tests: semantic equivalence, adversarial mutation, derivability).
2. **Admission to the git-tracked class is a human decision made at commit time.** Nothing is promoted automatically. If a record could ever contain PII, a secret, a third-party claim, or anything retractable, it does not go in git.

### Why the objections dissolve

- **"Git has no valid-time"** — correct, and irrelevant once `valid_from` is a front-matter field and the SQLite index makes it queryable. Files have valid-time; git does not.
- **"Git makes erasure maximally hard"** — correct, and specific to git's immutable history. It does not apply to a plain file, where erasure is `rm`, a tombstone, and a reindex.

### The one place git is right

The tombstone ledger is the **only** record class that is never erased. Git's immutability — disqualifying everywhere else — is exactly the property wanted here, so the ledger lives in its own private repo with a branch-protection ruleset blocking force-push and deletion.

## Consequences

- Agent-native `Read`/`Grep`/`Glob` work directly on memory files. **This means workspace scoping is a relevance control, not a security boundary** (see `0004-non-goals.md`).
- Export is a no-op: markdown *is* the format.
- Erasure is a file operation throughout, never a history rewrite, force-push, and re-clone.

## Residual risk, accepted

**Crash durability is assumed, not proven.** The startup probe establishes capability — atomic rename, `RENAME_EXCHANGE`/`linkat` availability, same `st_dev`, `fsync` returning success. It cannot establish survival across power loss, because `fsync()` returning 0 says nothing about lying disks or write caches. A conservative denylist is retained for cases the probe structurally cannot detect. This is recorded rather than engineered around.

## What would reverse this

- A second concurrent human, which moves the concurrency trigger in `0002-migration-triggers.md` from hypothetical to live and makes PostgreSQL's transactional guarantees worth their cost.
- Demonstrated corruption under normal single-user operation that the CAS + intent-record protocol cannot prevent — which would argue for a real transactional store rather than files plus discipline.
- A regulatory requirement for tamper-evident retention of memory content itself (not just erasure), which inverts the erasability argument for some class of records.

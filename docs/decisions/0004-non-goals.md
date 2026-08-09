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
| Any network protocol as an agent-facing surface | A second way to spell `search`/`get`/`write`/`forget` is a second thing to keep correct, and an untyped one outside the tool loop |
| A push-notification transport of any kind | IRC was proposed for this and rejected after four rounds — see the 2026-08-09 amendment, which records the six bounds any future attempt must meet and why a desktop notification from the existing sweep beats a bus |
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

## Amendment — 2026-08-09: IRC considered and rejected

A proposal to give the store a way to speak — an IRC channel on which it announces its own state changes — was developed over four rounds of adversarial review and **rejected**. The gap it addressed is real. IRC is not the instrument for it.

This is recorded at length rather than dropped, because the proposal is an attractive one that will be thought of again, and because how it shrank is more instructive than the conclusion.

### The gap, which is real

Every path into this store is pull. Nothing can say *the memory you cited ten minutes ago just went contested*, and a `CONTESTED` record is the one class that stays inert until a human learns it exists — `0001-conflict-precedence.md` refuses to resolve one without them. The store has two ways for an agent to speak to it and none to speak back.

### What was proposed, and what four rounds left of it

Each round removed a claim that could not survive contact with a mechanism.

| Round | Claim | Why it failed |
|---|---|---|
| Initial | A durable, ordered, cross-harness transcript | A derived representation with no erasure rule. `brain forget` would clear the store and leave ids, contested history, and timing in a channel log no tombstone reaches — silently falsifying the deletion guarantee in `README.md` and `0005-storage-boundary.md` |
| 1 | Emissions include truncated labels | A label *is* memory content, and `0006-prohibited-data.md` permits health, legal, financial, and third-party material inside it. "Pointer" names a function, not a sensitivity class |
| 1 | Loopback preserves "the operator is the only reader" | Loopback alone does not. It narrowed ADR 0006's "a second reader in any form" to "a remote reader." *(The related objection that listeners are new readers was withdrawn: harnesses already read every memory file by filesystem access.)* |
| 2 | `+m` makes the bounds mechanical | A channel mode is mutable runtime state. Mechanical requires the bot to verify and go silent when it lapses |
| 2 | `doctor` checks heartbeat freshness | Freshness needs a stored observation, which the no-state and no-persistence rules both forbid |
| 3 | Cross-harness sessions share one clock | Undeliverable. A per-prompt hook cannot receive a between-prompt event, and every bridge — long-lived subscriber or buffer — restores the retained state rounds 1 and 2 removed |
| 3 | The bus gives the store a way to initiate | No producer was named. MCP and the CLI are pull surfaces and core may not learn IRC exists, so the bot could only *poll* |
| 4 | A poller-fed notifier is worth the machinery | **Decisive.** See below |

What survived to round 4 was: `brain doctor` and the conflict list, polled on an interval, diffed against an in-memory baseline, broadcast ids-only onto an ephemeral loopback channel, to one consumer — the operator's IRC client.

### Why it was rejected

**The last remaining justification refuted itself.** The argument for IRC over the existing scheduled sweep was that a bus is live where a sweep is periodic. But once the producer was forced to be a poller — which "core does not learn that IRC exists" requires — **the bus is periodic too.** The two mechanisms differ in interval and presentation, nothing more. A desktop notification driven by the same periodic check delivers the same value without an ircd, a bot process, channel-mode enforcement, voice-based identity, a heartbeat loop, or the self-monitoring that heartbeat then requires.

Two further defects were found in the surviving design and are recorded so a revival does not rediscover them:

- **Snapshot polling is not change notification.** A condition that appears and clears between two polls leaves both snapshots identical and emits nothing. The true contract is "differences observed at polling boundaries," which is weaker than the "state changes" the proposal advertised.
- **Polling `brain doctor` from the bot is self-defeating** if `doctor` waits for that bot's heartbeat: a synchronous check blocks the process that must satisfy it, and the bot manufactures its own failure.

### What to do instead — nothing, for now

An earlier version of this section proposed serving the gap "through what already exists: the scheduled sweep from `brain install-timers`, reporting through the platform's desktop notification facility." **Both halves of that sentence were wrong, and the correction is instructive.**

**There is no such sweep.** `UNITS` in `src/brain/ops.py` contains exactly three entries — `brain-sync`, `brain-expire`, `brain-backup`. None of them evaluates health or conflicts. The alternative that decided the IRC question was itself resting on infrastructure the code does not contain, which is a reminder that a replacement argued from memory rather than from the source is not an argument.

**And desktop notification fails the same bounds.** Reviewed on its own terms rather than as IRC's leftover, it did not pass either:

- **Mako retains a history buffer.** `makoctl restore` and `dismiss --no-history` both exist, so an ordinarily dismissed notification leaves a replayable copy that `brain forget` never reaches. In-memory and session-scoped is smaller than a channel log; it is the same class of defect. `notify-send --transient` may bypass it, unverified. macOS Notification Center retention is likewise unverified.
- **Dedup owes obligations that were never designed.** Recording announcements in the event log is not the clean answer it appears to be: the class is erasable, but deletion does not *propagate* to it. Forgetting a memory would leave an announcement event naming its id, because purge is keyed by event id and nothing walks event payloads for references. The class being erasable is not the same as the record being erased.
- **The dedup writer is a durable writer**, which trips ADR 0002's concurrency trigger as literally written.
- **Recording an attempt is not evidence of delivery.** With a missing binary or no GUI session as a silent no-op, an announcement could be suppressed forever having never appeared.
- **`doctor` exits 4 on FAIL**, which the systemd template's `SuccessExitStatus=0 1 3` does not accept, and launchd has no equivalent at all.

That is a new cross-platform push subsystem — the thing the row in this ADR's non-goals table now forbids — not "the same value at a fraction of the machinery."

So the answer is the pull surfaces, unchanged: **`brain doctor` and `brain conflicts list`**, which remain the sole authority and owe nothing. For the harness-session case — an agent learning that something it relied on became contested — the answer is the per-prompt consult hook querying the store directly. That is the pull model this project already committed to (§9.1), and it needs no wire and no toast.

**What would justify revisiting:** measured disuse — contested memories sitting unresolved because the operator does not run the pull commands. Evidence, not the observation that notification was the last alternative standing after IRC fell. If that evidence arrives, start with a stateless scheduled command emitting a generic, transient, content-free alert ("Brain needs attention — run `brain conflicts list`"), tolerate repeats, and add durable dedup only once repetition is demonstrated to be a real problem rather than an anticipated one.

### If this is ever revisited, the safe upper bound

Recorded because the boundaries were argued to consensus even though the feature was not. Any future push-notification surface for this store must be **non-authoritative** (no fact depends on delivery; `brain conflicts list` stays the sole authority), **ephemeral** (no transcript, replay, or backfill — persistence would be a derived representation owing an erasure rule), **content-free** (ids and event classes; never labels, excerpts, or workspace names), **loopback-only** (a remote listener is a second reader and reopens `0006-prohibited-data.md` *before* the first message), **one-way with no input path** (an inbound path is a remote write endpoint and trips ADR 0002's concurrency trigger), and **fed only by polling existing read surfaces** with an in-memory baseline.

Those six bounds are the durable output of this exercise. The transport that occasioned them is not.

They have since been tested against a **non-network** surface — desktop notification — and they held, which is the first evidence they generalise rather than describing IRC in other words. Two translate rather than apply literally: *loopback-only* becomes "delivery confined to the operator's local GUI session, no remote notification service," and *one-way* becomes "notification actions must not invoke correction, resolution, or forgetting." The bound that bit was **ephemerality**, and it bit for the same reason it bit IRC: the delivery mechanism kept a replayable copy the store cannot erase. That appears to be the bound most likely to decide any future proposal, and the one a proposal is most likely to assume it satisfies without checking.

### What would reverse this

- **A genuine push source inside the store** — an event feed core already maintains for its own reasons, making a notifier a subscriber rather than a poller. That removes the decisive argument, because the periodic/live distinction would become real.
- **A harness gaining a long-lived subscriber** that can receive an event while no prompt is running. That revives the cross-harness case, which was withdrawn as undeliverable rather than unwanted.
- **Desktop notification proving inadequate in practice** — measured, not assumed. If the sweep's notifications are missed or unusable and the operator demonstrably needs a live channel, the comparison that decided this changes.

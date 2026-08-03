# ADR 0001 — Conflict Precedence

**Status:** Accepted
**Date:** 2026-08-03
**Implements:** `BLUEPRINT.md` §7.3, §7.5

## Context

Bitemporal timestamps are data, not a resolution rule. Storing `valid_from`/`valid_to` and a transaction time tells you *what* the candidates are; it does not tell you which one is true now when two contradict with overlapping validity intervals.

This rule took three attempts. The first two were wrong in opposite directions, and both failures are recorded because either would ship as a real defect.

- **Attempt 1 — precedence class first, recency second.** A `direct-user-statement` from eight months ago beats a `verified-environment-outcome` from today. This is the "confidently tells you you're still on Postgres six weeks after you migrated" failure.
- **Attempt 2 — recency dominates for `volatile` claims.** Fixes the above and opens an **injection path**: `volatility` is assigned by an extractor reading attacker-controlled text, so an attacker wins a precedence contest by writing text that gets classified volatile.

The error in both was treating precedence as a single ordering to sort by. It is two questions: *may this class override that one at all*, and *among those that may, which is current*.

## Decision

Trust tier is a hard gate. Recency operates only inside it.

```
TRUSTED   = direct-user-statement, authoritative-document, verified-environment-outcome
UNTRUSTED = third-party-document, inferred-from-behavior, agent-speculation

resolve(candidates for one (entity, attribute) at time T):
  1. Discard candidates whose [valid_from, valid_to) does not contain T.
     Discard tombstoned candidates.

  2. HARD GATE — trust tier. If any TRUSTED candidate is valid at T,
     UNTRUSTED candidates cannot win and are excluded from ranking.
     A contradicting UNTRUSTED candidate still raises `unresolved_conflict`.

  3. Within the surviving tier, gate on volatility:
       volatile | ephemeral  ->  valid_from desc, then precedence class
       slow | immutable      ->  precedence class, then valid_from desc

  4. Tie-break: later transaction time wins.
  5. Tie-break: narrower valid-time interval wins (more specific claim).
  6. Unresolved -> return the top candidate PLUS an `unresolved_conflict`
     marker naming the competitor.
```

Precedence ordering within a tier, highest first:

1. `direct-user-statement` — the user asserted it in their own words
2. `authoritative-document` — a source the user has designated authoritative
3. `verified-environment-outcome` — test result, command exit, tool observation
4. `third-party-document` — ingested material of unverified authority
5. `inferred-from-behavior` — derived from observed patterns
6. `agent-speculation` — model inference with no direct support

**Returning both facts silently is illegal at the retrieval boundary.** Handing a model two contradictory facts about one entity produces incoherent output even from strong models. Return one fact plus an explicit marker, or return one fact.

**Step 2 excludes but does not silence.** A contradicting untrusted claim still surfaces as `unresolved_conflict`. Silently dropping it would be its own failure: *"the vendor docs say you migrated"* is exactly the signal you want raised even when it must not win on its own.

## Consequences

- No LLM-emitted confidence floats anywhere. A `0.92` cannot be audited by anyone; a categorical class with a written ordering can.
- `volatility` is safe to have an extractor guess, because a mis-set or attacker-influenced value cannot cross the tier boundary. The conservative default is `volatile` — over-expiring is recoverable, under-expiring is not.
- The authoritative-document-versus-stale-remark case resolves correctly in both directions: if the claim is `volatile`, the newer document wins on recency; if `slow` or `immutable`, the user's statement wins on precedence.

## What would reverse this

- Evidence that the trust gate produces materially worse outcomes than a single ordering on a real corpus — measured on the golden set, not argued.
- A demonstrated case where an untrusted source *must* be able to override a trusted one automatically. If this arises, the correct fix is promoting that source to `authoritative-document`, not weakening the gate.
- Adoption of an external temporal store (e.g. Graphiti's edge invalidation) whose own resolution semantics supersede this rule — in which case this ADR is replaced, not amended.

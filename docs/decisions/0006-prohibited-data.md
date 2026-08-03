# ADR 0006 — Prohibited Data

**Status:** Accepted
**Date:** 2026-08-03
**Implements:** `BLUEPRINT.md` §11.4, §19 Q3

## Context

`BLUEPRINT.md` §11.4 requires scanning before persistence and before model output, and lists credentials, PII minimization, and sensitive-data handling together. §19 Q3 leaves the enumerated list open.

Four candidate categories were considered. **Only one was selected.** This ADR records the narrowing explicitly rather than implementing it silently, because a scanner's *absences* are invisible in code and become invisible in operation.

## Decision

### In scope — rejected at write time

**Credentials and keys.** Detection combines high-confidence patterns with entropy analysis:

- cloud provider keys (AWS, GCP, Azure)
- service token prefixes (`sk-`, `ghp_`, `gho_`, `xoxb-`, `xoxp-`, …)
- PEM blocks (`-----BEGIN ... PRIVATE KEY-----`)
- `Authorization:` headers and bearer tokens
- connection URIs with embedded passwords (`postgres://user:pass@…`)
- Shannon entropy on 32+ character tokens

**Rejected, never redacted.** A redacted secret has already been written to disk, and on a copy-on-write filesystem it may persist in snapshots indefinitely. Rejection happens before any bytes reach the store.

### Deliberately out of scope

| Category | Consequence of exclusion |
|---|---|
| Financial identifiers (card numbers, IBAN, routing/account) | Can be persisted as ordinary memories |
| Government IDs (SSN, NI, passport) | Can be persisted as ordinary memories |
| Third-party personal data (addresses, phone numbers, medical or employment details about other people) | Can be persisted as ordinary memories |
| Health and legal matters | Can be persisted as ordinary memories |

This is a **narrowing of §11.4**. It is defensible for a single-user, local-first, full-disk-encrypted store where the operator is the only reader — the threat model is accidental capture and exfiltration through an agent, not a hostile local user. It would **not** be defensible for a shared deployment.

### False-positive discipline

An entropy detector that rejects legitimate content makes `brain remember` unusable, and an unusable write path is the emptiness failure mode §8.1 warns is the dominant one. The false-positive rate is **measured**, not assumed, against a corpus containing ULIDs, SHA-256 digests, long file paths, and base64 content — all of which must pass.

## Consequences

- The scanner is small, fast, and high-precision. It runs on every write with no perceptible latency.
- The categories above reach disk unimpeded. **If this system is ever shared, or the disk is ever unencrypted, this ADR must be revisited before that happens** — not after.
- Rejection is a hard failure with a clear message naming the matched pattern class (never echoing the matched secret).

## What would reverse this

- **A second reader of the store**, in any form — shared deployment, synced backup to a third party, or an agent with egress. Any of these invalidates the threat model this narrowing rests on.
- Full-disk encryption being disabled or the store moving to a device without it.
- A measured incident: any sensitive category reaching a model's output or an external service, which would demonstrate the scanner's scope was too narrow in practice rather than in principle.
- Regulatory obligation attaching to any excluded category.

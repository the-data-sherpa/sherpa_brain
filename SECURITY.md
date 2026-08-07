# Security Policy

## Reporting a vulnerability

Use GitHub's **Report a vulnerability** button under this repository's *Security*
tab. That opens a private advisory visible only to the maintainers.

Please do not open a public issue for anything exploitable. If private reporting is
unavailable to you for any reason, open an issue saying only that you have a security
report and would like a private channel — no details.

Expect an acknowledgement within a week. This is a single-maintainer project, so
please read that as a realistic estimate rather than a service commitment.

## The threat model this is built for

**A single trusted operator, on a local-first store, on an encrypted disk.**

The adversary this design defends against is *your own agent* — a model that has been
prompt-injected, has read a poisoned document, or is simply confidently wrong. It is
not a hostile local user, not a multi-tenant boundary, and not a network attacker.

Three properties follow from that, and they are the ones worth reporting bugs against:

- **Nothing retrieved is trusted as instruction.** Generated instruction files carry
  pointers only, never memory content (`src/brain/adapters.py`). A path by which
  retrieved or agent-proposed text reaches `CLAUDE.md`, `AGENTS.md`, `RULES.md`, or a
  skill file is a vulnerability, not a feature request.
- **Deletion is real and propagates.** Tombstones outlive backups, and a restore must
  not resurrect a forgotten subject. A way to recover tombstoned content through
  export, backup restore, revision history, or the search index is a vulnerability.
- **Credentials never reach the store.** They are rejected at write time, never
  redacted — see below.

## What is deliberately *not* protected

These are documented decisions, not oversights. Reporting them is welcome as a design
discussion, but they will not be treated as vulnerabilities.

**The secret scanner is narrow.** [ADR 0006](docs/decisions/0006-prohibited-data.md)
rejects credentials and keys — cloud provider keys, service token prefixes, PEM
blocks, `Authorization:` headers, connection URIs with embedded passwords, and
high-entropy tokens. It **deliberately does not detect**:

| Not detected | Consequence |
|---|---|
| Financial identifiers (card numbers, IBAN, account/routing) | Stored as ordinary memories |
| Government IDs (SSN, NI, passport) | Stored as ordinary memories |
| Third-party personal data about other people | Stored as ordinary memories |
| Health and legal matters | Stored as ordinary memories |

That narrowing is defensible for one operator who is the only reader of their own
store. **It is not defensible for a shared or multi-user deployment**, and the ADR
says so in those words. If you are putting other people's data in here, the scanner is
not the control you think it is.

**Workspace scoping is a relevance control, not a security boundary.** Anything with
filesystem access reads any memory regardless of workspace. It exists to stop your
work project bleeding into your side project, not to isolate secrets.

**Crash durability is assumed, not proven.**
[ADR 0005](docs/decisions/0005-storage-boundary.md) accepts this as residual risk. The
startup probe establishes that the required syscalls *work here*; a successful `fsync`
says nothing about a lying disk or a write cache.

**At-rest encryption is the operating system's job.** The store writes plaintext
markdown by design — being readable by ordinary tools is the portability guarantee.
Full-disk encryption is assumed.

**The installer writes into your home directory.** `install.sh` and
`brain adapter --scope user` modify harness configuration files (`~/.claude.json`,
`~/.codex/config.toml`, `opencode.json`, and similar). They merge rather than
overwrite and copy the original to `<name>.pre-brain.bak` before the first change, but
you should read `install.sh` before running it, as you should with any installer.

## Supported versions

The `main` branch. This is pre-1.0 software with a single maintainer; there are no
backported security fixes for older tags.

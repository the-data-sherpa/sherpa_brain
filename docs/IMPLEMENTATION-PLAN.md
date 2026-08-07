# Implementation Plan — Phase 0.5 + Phase 1

**Status:** Revised after adversarial review round 10. Under review.
**Date:** 2026-08-03
**Governing document:** `BLUEPRINT.md`. Every item cites the section it implements. Choices the blueprint left open are marked **[P-n]**.
**Scope:** Phase 0.5 (a working brain) + Phase 1 (durable foundation, deletion end-to-end). Phase 2+ out of scope, with named seams.

> **Review note.** Ten rounds of adversarial review found 27 defects here — and **seventeen defects in `BLUEPRINT.md` itself** that ten prior rounds of design review had missed. Writing the protocol as code found what reading the design could not. Review also *refuted four of my own fixes*, each of which reintroduced a narrower version of the bug it was meant to close: the `RENAME_EXCHANGE` rollback (§0.4), `O_EXCL` revision allocation (Step 5.1), intent recorded after publication (Step 5.2), and a single classifier that deadlocked recovery (Step 5.2.1). Corrections are folded in here and back into the blueprint (Appendix B, V11–V27). §4.5 names the pattern behind all four.

---

## 0. Decisions taken in interview

Open questions in `BLUEPRINT.md` §19, or absent from it entirely. Settled; not re-litigated below.

| # | Question | Decision | Ref |
|---|---|---|---|
| 1 | Language and runtime | **Python 3.14 + uv** | not addressed |
| 2 | Build scope | **Phase 0.5 + Phase 1** | §15 |
| 3 | Background extraction | **Deferred.** Zero model calls in v1; extractor is an interface with a stub | §8.1, §19 Q7 |
| 4 | Review surface | **CLI in Phase 1; TUI deferred to Phase 2** (revised — §0.2) | §8.2 |
| 5 | Workspace model | **Field + directory-per-workspace** | §11.6 |
| 6 | Prohibited data | **Credentials and keys only** | §11.4, §19 Q3 |
| 7 | Harness adapters | **Claude Code + Codex + OpenCode** (revised — §0.2), plus generic-MCP conformance test | §12.3, §16 |
| 8 | Eval seed | **Scaffold + `brain eval bootstrap`** from the real corpus | §10.1 |
| 9 | Tombstone replica 2 | **Private GitHub `brain-ledger`**, append-only ref, ack by re-read | §11.5.3 |
| 10 | Backup | **Auto-detect: btrfs snapshot preferred, validated double-collection fallback** | §11.5.2 |
| 11 | Repository | **`git init` in place, local-only** | §6.2, §6.8 |

### 0.1 Decisions that narrow the blueprint, recorded as such

**Prohibited data is credentials-only.** Financial identifiers, third-party personal data, and health/legal matters were considered and deliberately excluded from the scanner. Consequence: those categories *can* be persisted as ordinary memories. Legitimate for a single-user local brain, but a **narrowing** of §11.4 — recorded in `0006-prohibited-data.md` with that framing rather than silently implemented.

**Adapter generation is pulled forward from Phase 5** by explicit user selection of Claude Code and Codex. Flagged as a scope decision overriding phase order, not an accident. The §11.3 purity check ships **with** it — a generator without that check rebuilds the exact high-trust path Claude Code v2.1.50 removed, so it is a security control, not a Phase 5 nicety.

### 0.2 Changed by review

- **TUI dropped from Phase 1.** Phase 1 needs divergences and quarantine *surfaced* (invariant 10), not a TUI to surface them. Plain CLI suffices; `textual` leaves the dependency list. TUI + proposal queue move to Phase 2, when something actually generates proposals.
- **`--force-local` removed entirely.** See §0.3 / Step 10.
- **OpenCode added as a third adapter target** (2026-08-04), under the promotion clause in `0004-non-goals.md` rather than as new planned scope. Two adapters was the right count for the evidence available when the table above was written; OpenCode turned out to satisfy the promotion test the ADR already specified. It reads `AGENTS.md` natively, but its MCP block is a distinct schema (`mcp`, one argv array, `environment`), and the generic `.mcp.json` is *silently ignored* rather than rejected — the failure mode the conformance test cannot catch, because a generic MCP client never reads the harness's config file. Cursor and Aider remain contingency; the bar for them is the same demonstrated mismatch, not usage.

### 0.3 Blueprint defects this plan surfaced

`BLUEPRINT.md` §6.3 described revisions as holding *prior* states. §6.6's unwitnessed-edit detector compares `hash(present)` against `hash(latest revision)`. Under the §6.3 reading those are unequal **for every memory, always** — the detector fires on everything, forever. §6.6 was unimplementable against §6.3, and nine rounds of design review did not catch it because it is only visible once you write the protocol out.

Corrected in the blueprint and adopted here: **revisions are the append-only log of every committed state; the present file is a materialized view of the newest.** After a mediated write the two are byte-identical, so a difference between them is real information.

Sixteen more followed, recorded as V12–V27: the unachievable divergence guarantee (§0.4), `O_EXCL` on the final revision path, a mutable `quorum_state` on a hash-chained record, unenforceable git-side ledger protection, intent recorded after publication, a classifier that deadlocked recovery, GC racing op creation, recovery racing a live writer, purge receipts treated as enduring facts, self-asserted replica identity, and conflict markers deleted rather than archived.

### 0.4 A requirement that had to be withdrawn, not implemented

`BLUEPRINT.md` §6.5 required that on divergence the system **"leave present state unchanged."** It cannot. The filesystem has no primitive for "swap, inspect, and conditionally undo" — that is three operations, and the undo is itself a non-atomic read-modify-write that races the next editor write:

```text
stage C -> exchange: present=C, .new=B   (an editor had written B)
detect B != A -> editor writes D over present
exchange back: present=B, .new=D          <- D clobbered, C lost
```

The rollback *creates* the loss it was meant to prevent. Replaced with a guarantee that can be kept:

> **No committed state is ever lost, and a contested memory never serves one branch as though it were settled.**

Trading an unachievable guarantee for an enforceable one is the right direction. Asserting the former would be exactly the kind of unearned claim §20 criticizes the field for.

### 0.5 What the startup probe does and does not establish

The probe checks **capability**: rename atomicity, `RENAME_EXCHANGE`/`linkat` availability, same `st_dev`, and that `fsync` returns success. It does **not** establish crash durability — `fsync()` returning 0 says nothing about lying disks or write caches. An earlier draft claimed the probe "tests the property" when it tests a necessary but insufficient subset.

The conservative denylist is therefore **retained**, not demoted to a hint, because it covers cases the probe structurally cannot detect. Crash durability is **assumed, not proven**, and stated as such alongside the §11.5.3 residual risk.

---

## 1. What "done" means

Phase 1 is complete when each of these passes as an **automated** test. No manual steps, no demonstrations.

| # | Criterion | §ref |
|---|---|---|
| 1 | `brain remember` writes; `brain search` finds; `brain get` returns it with provenance | §12.2 |
| 2 | Index is **provably derived** — three tests, see Step 4 (not a byte-identical dump) | §5.2, §6.3 |
| 3 | A direct edit is captured by stable-read reconciliation as `capture: reconciled` with an **interval** transaction time | §6.6 |
| 4 | A file being actively written is **never** snapshotted; it is deferred and reported | §6.6 |
| 5 | Two writes from one predecessor produce a **divergence**: both branches persisted, memory marked contested, conflict record, human required | §6.5 |
| 6 | A concurrent direct edit during a mediated commit is **captured, not destroyed**; the memory becomes contested and **reads fail closed** | §6.5 |
| 7 | Malformed front matter is **quarantined**, excluded from search; `brain validate` exits nonzero | §7.6 |
| 8 | A credential is **rejected at write time and never written to disk in any form** | §11.4 |
| 9 | `brain forget` suppresses retrieval **immediately**, before and independent of any network operation | §11.5 |
| 10 | `brain forget` **never reports success** without quorum; unreplicated deletion exits `pending` (code 3) | §11.5.3 |
| 11 | **Restoring a pre-deletion backup does not resurrect the deleted memory** — the headline test | §11.5.2 |
| 12 | A broken tombstone hash chain **refuses to serve** | §11.5.1 |
| 13 | Restore fails closed on: stale-but-reachable replica, equal-seq/different-head equivocation, keyring loss | §11.5.3 |
| 14 | `brain` refuses to start where the **capability probe** fails or the denylist matches. Crash durability is **assumed, not proven** (§0.5) | §6.5.1 |
| 15 | A generic MCP client can find, cite, correct, and forget a memory | §16 |
| 16 | Generated adapter files contain **no memory content and no ingested text** (CI) | §11.3 |
| 17 | `brain export` round-trips the full corpus to Markdown and JSONL | §12.2, §16 |
| 18 | Every query and every retrieved-vs-cited pair is logged | §15 P0.5 |
| 19 | Eval emits a score with a Wilson interval, reports `n`, and **refuses a slope below the 150-item floor** | §10.1 |

**Explicitly N/A with reason:** cache-hit rate and cost-per-session (§9.5, §16). v1 makes zero model calls, so both would report a hardcoded zero — worse than absence, because a zero looks like a measurement. The trace *structure* they attach to ships now so Phase 2 fills a field rather than retrofitting a pipeline.

---

## 2. Code architecture

### 2.1 Dependency budget **[P-1]**

§18 argues for a small dependency surface on sustainability grounds. Ceiling: **four** runtime dependencies.

| Dep | Why | Rejected alternative |
|---|---|---|
| `typer` | CLI | argparse — much more code |
| `pyyaml` | front matter | hand-rolled parser is a bug farm |
| `mcp` | official SDK | hand-rolling re-implements the spec |
| `jsonschema` | front-matter validation | hand-rolled drifts from the schema |

`sqlite3`, `hashlib`, `pathlib`, `os`, `ctypes` are stdlib. `ripgrep` and `git` are system binaries. **No embedding library, no vector store, no reranker, no TUI framework.**

Dev-only: `pytest`, `pytest-cov`, `ruff`, `mypy`.

### 2.2 State layout

```text
~/.local/state/brain/                    # CANONICAL — not git-tracked
├── memories/<workspace>/<type>/<id>.md  # materialized view of newest revision
├── memories/.revisions/<id>/<n>.md      # append-only log of EVERY committed state
├── events/YYYY-MM-DD.jsonl              # append-only, per-line checksum
├── artifacts/sha256/ab/cd/<digest>/
├── quarantine/                          # failed validation, fail-closed
├── conflicts/<id>.json                  # unresolved divergences
├── tombstones.jsonl                     # replica 1, hash-chained
├── ledger.git/                          # replica 2 → private GitHub, append-only ref
├── logs/queries.jsonl                   # retrieval log (§15 P0.5 item 9)
├── brain.sqlite3                        # DERIVED — deletable, zero canonical bytes
└── backups/<generation>/manifest.json
```

---

## 3. Build order

Reordered twice after review. The first draft claimed "invariants hold at every commit" while the write protocol depended on the index and tombstones for its own recovery story. The second still had two forward dependencies: conflicts were created before anything surfaced them (violating invariant 10 in the interval), and ADRs came last despite gating Phase 1.

```text
0 ADRs -> 1 primitives+probe -> 2 model/frontmatter/scanner/quarantine-CLI
-> 3 events/artifacts -> 4 index
-> 5 write protocol + op lifecycle + recovery + conflicts CLI
-> 6 reconciliation -> 7 tombstones/acks/purges + read gating on ALL paths
-> 8 resolution (crash-safe ordering) -> 9 search
-> 10 deletion/purge/backup/restore -> 11 resolution UX
-> 12 MCP+adapters -> 13 export+eval
```

**Nothing depends forward.**

### Step 0 — ADRs, before any code

§15 Phase 0 is explicitly "decisions on paper, no code", and §18 gates Phase 1 on them. An earlier draft had ADRs at the end, inverting the blueprint's own ordering. Six files in `docs/decisions/`, each stating **what would reverse it**: `0001-conflict-precedence`, `0002-migration-triggers`, `0003-build-vs-adopt`, `0004-non-goals`, `0005-storage-boundary`, `0006-prohibited-data`.

### Step 1 — Primitives and preconditions

- `uv`, `pyproject.toml`, `git init`, `.gitignore`.
- `ids.py`: ULID — time-sortable, path-independent (§7.1).
- `atomic.py`:
  - `write_atomic()` = write `.tmp` → `fsync(file)` → `rename` → **`fsync(dir)`**. The directory fsync is the step that makes the rename durable and the one most often omitted.
  - `exchange(a, b)` = `renameat2(RENAME_EXCHANGE)` via a `ctypes` syscall shim. Linux 3.15+, supported on btrfs.
  - `create_exclusive(path)` = `O_CREAT|O_EXCL`.
- **§6.5.1 preconditions: probe AND denylist, neither alone.** The probe checks capability in the actual target directory — `rename`, `RENAME_EXCHANGE`, `linkat`, `fsync` on file and directory, same `st_dev`. The denylist is **retained**, because a probe cannot detect what it cannot exercise. Neither establishes crash durability (§0.5).

**Tests:** probe rejects a simulated non-atomic mount; `write_atomic` never leaves a partial file visible under `os._exit` at any point; `exchange` is verified atomic under concurrent readers.

### Step 2 — Model, front matter, scanner

- Six required fields (§7.1); `volatility`, `provenance_class`, `status` enums (§7.2, §7.3, §8.3).
- `frontmatter.py`: parse / serialize / validate against `knowledge/schemas/frontmatter.schema.json`.
- **Fail closed** (§7.6): invalid front matter → `quarantine/`, excluded from all retrieval, `brain validate` nonzero. Never best-effort parsed, never silently skipped — a silent skip makes a memory vanish without telling anyone.
- `scan.py`: credential scanner. High-confidence patterns (AWS/GCP/Azure, `sk-`, `ghp_`, `xoxb-`, PEM blocks, bearer tokens, URIs with embedded passwords) + Shannon entropy on 32+ char tokens. **Reject, never redact** — a redacted secret has already been written to disk.

**Tests:** enum round-trip; malformed file quarantines and never appears in search; every credential pattern rejected; **entropy false-positive rate measured** against a corpus containing ULIDs, SHA-256 digests, and long file paths, all of which must pass.

### Step 3 — Events and artifacts

- `events.py`: append-only daily JSONL, per-line checksum; truncated trailing line discarded with a warning.
- `artifacts.py`: content-addressed blobs at `sha256/ab/cd/<digest>/`, preserving original URI, media type, capture time, parser version.

**Tests:** torn trailing line recovers; checksum mismatch detected; digest addressing is collision-safe and path-independent.

### Step 4 — The derived index (§7.7)

Built *before* the write protocol, because the write protocol's recovery story is "reindex from files" and cannot depend on something that does not exist yet.

- `schema.sql`: `memory_index`, `revision_index`, `evidence_link`, `relations`, `search` (FTS5), `tombstone_index`. **Every table derived.**
- `build.py`: full deterministic rebuild by scanning files.

**Tests — three, because the original single test proved neither claim.** A byte-identical dump is simultaneously too strong (FTS5 rowids, `sqlite_sequence`, and insertion order differ for reasons unrelated to correctness) and too weak (it cannot detect canonical data hiding in the index):

1. **Semantic equivalence** — normalized logical projection, sorted, rowids and autoincrement excluded, identical across `reindex`.
2. **Adversarial mutation** — arbitrarily corrupt the DB, `reindex`, assert identical logical state. If SQLite held anything canonical, this destroys it.
3. **Derivability** — for a fixture corpus covering every type, status, workspace and edge case, assert every field readable from the index is re-derivable from files alone. *This is the actual claim under test.* The fixture shuffles scan order and touches mtimes, so nothing may derive from either.

### Step 5 — The write protocol + conflict visibility (§6.5)

The core of Phase 1. Review found a data-loss race here with **no crash involved**, and then found that my first fix reintroduced it:

```text
Race:   present=A -> writer reads/hashes A, CAS ok -> editor renames B over present
                  -> writer renames C over present.   B destroyed, unrecorded.

Fix #1: stage C -> exchange: present=C, .new=B -> detect B!=A
                -> editor writes D -> exchange BACK: present=B, .new=D
        D clobbered, C lost. The rollback IS a race.
```

A conditional rollback is three operations, and the undo is itself a non-atomic read-modify-write. **No ordering fixes it.** So the rollback is dropped, and with it an unachievable requirement — see §0.4.

```python
def write(memory_id, new_body, expected_predecessor_hash):
  with per_memory_lock(memory_id):               # held THROUGH op retirement (§5.2)
    opid   = new_opid()
    staged = stage(f".staging/{opid}", new_body)      # fsync file + .staging/
    with store_lock():                                # serializes GC vs op creation
        create_op(opid, memory_id, expected_predecessor_hash,
                  staged, phase="staged")             # fsync file + ops/
        # ^^ INTENT DURABLE BEFORE ANY OTHER DURABLE EFFECT

    publish_revision(staged, memory_id, opid)         # linkat; EEXIST -> n += 1
    exchange(present_path(memory_id), staged)         # ONE atomic op. NEVER undone.

    displaced = staged                                # what was ACTUALLY present
    if hash(displaced) == expected_predecessor_hash:
        commit()
    else:
        publish_revision(displaced, capture="reconciled")   # the edit is NOT lost
        write_conflict_record(memory_id, branches=[...])
        mark_contested(memory_id)                     # ALL reads -> unresolved_conflict
    fsync_dir()
    retire_op(opid)                                   # LAST
  index.upsert(memory_id)                             # derived; repairable
```

Two orderings in this are load-bearing and were each wrong in an earlier draft: **intent is created before any other durable effect** (a draft published the revision first, which buries a committed branch when an editor writes during the crash window), and **the per-memory lock spans staging through retirement** so recovery cannot take over an op a live writer still owns.

Present may hold either branch after a divergence. That is acceptable because **present is not authoritative while contested** — reads fail closed rather than returning it (§7.5, invariant 10).

#### 5.1 Revision publication — stage, then link

`O_CREAT|O_EXCL` on the *final* path was also wrong: a crash mid-write leaves a **torn file permanently occupying that revision number**, which is worse than the overwrite it prevented because the number can never be reused.

```text
1. write body -> .revisions/<id>/.staging/<opid>   fsync(file)
2. linkat(staging -> .revisions/<id>/<n+1>.md)     # EEXIST on collision; never overwrites
   on EEXIST -> n += 1, retry
3. fsync(dir); unlink staging
```

`linkat` publishes an already-complete, already-fsynced file. Crash before step 2 leaves only a staging file. Crash after leaves a complete revision. **No window in which a partial file occupies a revision number.**

Constraints, all load-bearing: close the fd before publish; **never mutate the staging inode after linking** (it is the same inode); fsync the destination directory; `EEXIST` is idempotent **only** when digest and opid match, otherwise it is a genuine collision and `n` increments; readers ignore `.staging/`; and staging files are **GC'd only when no op record references them** (§5.2).

#### 5.2 Durable intent records — why the tree is not enough

Round 4 claimed every state is "computed from the file tree, never stored — so recovery is a pure function of the tree." **That claim was the bug**, and review found two faces of it:

- Crash after exchange, before publishing the displaced branch: `hash(present) == hash(newest revision)`, so the tree reads `SETTLED` while the editor's bytes sit in a scratch path awaiting GC. **Silent loss.**
- Revision published, crash before materialization, editor then writes present: the tree reads `UNWITNESSED`, so the editor's branch is reconciled and the already-published competing revision is **buried with no conflict raised**.

In both, the tree cannot distinguish *"an operation was in flight"* from *"nothing was happening."* No ordering of writes creates that distinction, because the missing information is **intent**, and intent is not a property of data at rest.

**Intent must precede every durable effect, not merely the exchange.** A first attempt at this fix still wrote the op record *after* publishing the revision — the giveaway was a `published_revision` field, which presupposes the revision already exists. That reproduces the original failure: crash between publishing `C` and writing the op, editor installs `B`, no op record exists, `B` reads as unwitnessed, `C` is buried.

```text
1. stage body -> .staging/<opid>       fsync(file), fsync(.staging/)
2. create ops/<opid>.json              fsync(file), fsync(ops/)   <- BEFORE any publish
   { opid, memory_id, expected_predecessor, staging_path, phase: "staged" }
3. publish revision via linkat, tagged with opid in its front matter
4. exchange present
5. retire op record                                               <- LAST
```

**Correlation is by `opid`, never a preselected revision number.** A preselected `n` is a guess about a namespace another writer may claim first; `opid` is unique by construction and survives the retry `EEXIST` forces. The revision carries its `opid` so recovery can join in either direction.

Retirement is last, so a crash during recovery replays the same recovery. Every step is idempotent.

**Recovery and a live writer must be mutually exclusive, and this needs a second lock — not the store lock.** An op record becomes visible the moment it is created, which is *before* publication by design. A recovery pass processing every visible op unconditionally would therefore race the writer that owns it, both acting on the same op. So the writer **holds a per-memory lock from staging through op retirement**, and recovery **acquires that same lock** before taking over any op. The store-level lock below covers only staging-versus-GC — a different lock with a different job, and conflating the two leaves this race open.

**`.staging/` is never GC'd while an op record references it — and GC is serialized against op creation by a store-level lock.** Gating on op retirement alone is insufficient: staging can be created and fsynced, GC can scan `ops/` before the op record lands, delete the staging file, and the writer then publishes an op referencing a file that no longer exists. A grace period narrows that window; it does not close it, because it is a race rather than a timing preference. GC takes the lock; op creation holds it across steps 1–2. No scan can observe the window between staging and its op record.

**Permanently stuck ops surface in `brain status` for explicit repair, never guessed away by GC.** An op whose staging is missing and whose revision was never published is a real inconsistency and must be reported. Tidying it silently is how the one branch that mattered gets lost.

#### 5.2.1 Two passes, not one ordered list

An earlier draft listed five "states" as one priority-ordered classifier. Two things were wrong. The predicates were not mutually exclusive (a contested memory whose hashes match reads as `SETTLED`; a no-op write satisfies both `SETTLED` and `INTERRUPTED`). And more seriously, the ordering **deadlocked**: with `CONTESTED` ahead of `RECOVERING`, a crash mid-resolution leaves both a conflict marker and a `phase: resolving` op, the classifier stops at `CONTESTED`, the op is never reached, and **resolution stalls forever.**

The root error was collapsing *"what must I finish?"* and *"what may I serve?"* into one list. They are different questions and one strictly precedes the other:

```text
PASS 1 — recover_pending_ops()      # unconditional; runs first; ignores
  for each op in ops/:              # conflict and quarantine state entirely
      ACQUIRE the SAME per-memory lock the writer holds
      complete or roll forward
      retire the op record LAST

PASS 2 — serving_disposition(id)    # only after pass 1 is clean
  1. QUARANTINED   present fails schema validation
  2. CONTESTED     conflicts/<id>.json exists       -> reads FAIL CLOSED
  3. INTERRUPTED   newest.predecessor_hash == hash(present)
                   AND hash(present) != hash(newest)   # excludes no-ops
  4. UNWITNESSED   hash(present) matches no revision
  5. SETTLED       hash(present) == hash(newest)
  6. otherwise     -> QUARANTINED   # fail closed, never guess
```

`RECOVERING` disappears as a disposition — it was never a serving state, it was a phase of a different pass. Separately, **a write whose body equals present is a no-op that publishes no revision**, so it cannot manufacture the ambiguity in rule 3.

**Two test suites, because enumeration proves the wrong thing.** Enumerating arbitrary file combinations proves `serving_disposition` is *total*, not that those trees are *reachable*, and totality over unreachable inputs is nearly worthless:

1. **Transition tests generated from real syscall boundaries** — `os._exit` at each boundary of write, recover, and resolve, **with a concurrent direct rename injected at each boundary**. These exercise trees the protocol can actually produce.
2. **A malformed-tree fuzzer**, separately, asserting the classifier never crashes and never yields two dispositions — robustness against corruption, stated as its own property rather than smuggled in as reachability.

#### 5.3 Conflict visibility ships in this step, not in Step 11

Divergence recorded but invisible violates invariant 10 for as long as the gap lasts. `brain conflicts list` and `brain conflicts show` therefore land **with** the write protocol, in this step — read-only. The mutating `resolve` lands in Step 11, because it needs Step 8's crash-safe ordering. Visibility cannot wait; resolution can.

- Per-memory advisory lock for mediated writes. **CAS is the safety mechanism; the lock only reduces contention**, because direct editors ignore advisory locks.

**Tests (`tests/crash/`) — real `os._exit` fault injection at every arrow of the state machine:**
- crash between publish and exchange → `INTERRUPTED`, replay reaches `SETTLED`, nothing lost
- crash between exchange and index → stale index, `reindex` repairs
- **the race:** editor writes during a mediated commit → captured as `reconciled`, memory `CONTESTED`, reads fail closed
- a contested memory **never** serves a branch as settled
- concurrent mediated writes → one commits, one contests
- **property test:** over arbitrary interleavings of two writers and one editor, **no committed revision is ever lost** and every reachable tree maps to exactly one state

### Step 6 — Reconciliation (§6.6)

- Stable-read: `(size, mtime_ns, hash)` → wait → re-read → require identical. A hash taken mid-save reads a torn file.
- Defer on instability, on editor sidecars (`.swp`, `.swx`, `#file#`, `.~lock`), on parse failure.
- Capture as `capture: reconciled`, transaction time as an **interval** (`recorded_from`, `recorded_to`) — never a false point value.
- **Pull-based only; no filesystem watcher** (§6.6 rejects it: races the write, coalesces saves, misses renames, crashes between observe and record).

**Tests:** an actively-written file is never snapshotted; a direct edit between reindexes is captured with a bounded interval; a never-stabilizing file is reported, not guessed at.

### Step 7 — Tombstones, acks, and quorum (§11.5.1, §11.5.3)

**Three append-only hash-chained ledgers; all status is derived, never mutated.** An earlier draft put a mutable `quorum_state` field on a hash-chained tombstone — flipping `pending → confirmed` would rewrite a link in the chain whose entire purpose is tamper evidence.

```text
tombstones.jsonl   append-only, hash-chained   — the DELETION happened
acks.jsonl         append-only, hash-chained   — replication acknowledged
purges.jsonl       append-only, hash-chained   — physical bytes removed

delivery_state(id) = f(tombstones, acks)     # derived projection in the index
purge_state(id)    = f(tombstones, purges)   # derived projection in the index
```

Nothing is ever rewritten; both projections rebuild with `reindex` like everything else.

Tombstone entries are content-free: `seq`, `subject_id`, `subject_kind`, `tombstoned_at`, `prev_hash`, `hash`. Broken chain → **refuse to serve**.

#### 7.1 The remote is only an anchor if it cannot be rewritten

`receive.denyNonFastForwards` is **client-side config that GitHub ignores**, and pre-push hooks are locally bypassable (`--no-verify`, a second clone, the API). Neither is a control.

1. **Branch-protection ruleset via the GitHub API** at `brain init`, blocking force-push and deletion. If it cannot be created *and verified*, `brain init` **fails loudly** rather than proceeding with a ledger that looks protected and is not.
2. **The push is not the acknowledgement.** After pushing, re-read the remote ref and confirm it contains the `(seq, chain_head)` just written. Only that read is the ack. A push with uncertain outcome (timeout after send) is resolved by re-reading, never by assuming.
3. **Ordering is ack-then-anchor**, never anchor-then-push.
4. The keyring anchor `(seq, chain_head, remote_sha)` is a **locally monotonic hint, not a guarantee** — a keyring is mutable and restorable with a user profile. The fail-closed rules do the real work:

```text
keyring seq > every reachable replica  -> FAIL CLOSED  (replica is stale)
equal seq, different chain_head        -> FAIL CLOSED  (equivocation)
keyring absent or reset                -> FAIL CLOSED, operator attestation
                                          (never "recover from keyring alone")
```

An ack records **exactly what was verified**, not that a command exited zero:

```json
{ "seq": 41, "chain_head": "<hash>", "remote_sha": "<sha>",
  "ref": "refs/heads/ledger", "protection_verified": true,
  "replica_identity": "<derived from configured endpoint; bound into the checksum>",
  "verified_at": "..." }
```

**Broken-chain detection runs on all three ledgers, not only tombstones.** An unverified `acks.jsonl` would let a forged ack fabricate quorum, defeating the entire mechanism. Any broken chain in any ledger refuses to serve.

Referential integrity rules on acks, without which a valid-looking chain still fabricates quorum:

1. an ack **must resolve to an existing, exact local tombstone chain entry** — an unknown or non-matching `chain_head` is invalid, not merely unhelpful;
2. **quorum counts distinct replica identities**, never ack-entry count — and **identity is derived from the configured authenticated endpoint, never read from a field in the ack**. A self-asserted identity is satisfiable by a duplicate endpoint or a fabricated value; it is bound into the ack's checksum at write time by the code that performed the verified push;
3. **replayed or duplicate acks do not inflate quorum** — deduplicated on `(replica_identity, seq, chain_head)`;
4. **restore revalidates current remote containment** rather than trusting a historical ack, since the remote may have moved since it was written.

**Stated limitation:** an unkeyed hash chain detects corruption; it does **not** authenticate authorship. Anyone who can write the file can rewrite the chain consistently. Authorship rests on OS identity, filesystem permissions, and an authenticated `gh` — not on the chain. Signing would change this and is deliberately out of Phase 1.

#### 7.2 Read gating lands here, not later

Tombstone suppression must gate **every read path that exists at this point** — it cannot wait for the search step, or any path built before then would serve tombstoned content in the interval.

**Tests:** chain verifies and refuses service on tamper, **on each of the three ledgers**; truncated trailing line recovers; **ruleset verified at init, and init fails when it cannot be**; ack-by-re-read detects a push that appeared to succeed but did not land; a forged or replayed ack does not fabricate quorum; each of the four fail-closed rules triggers; every existing read path suppresses tombstoned content.

### Step 8 — Conflict resolution (§7.5) and crash-safe resolve ordering

Trust-tier hard gate, then volatility, exactly as ADR 0001 specifies.

**Resolution is itself a multi-step mutation and needs the same discipline as any other write** — an earlier draft specified no ordering for it at all:

```text
resolve(id, take=<rev>):
  1. write op record (phase: resolving)
  2. publish the chosen branch as a NEW revision
     (never rewrite history — the losing branch stays in the log permanently)
  3. materialize present via exchange
  4. fsync
  5. rename conflicts/<id>.json -> conflicts/.resolved/<id>.<opid>.json
     (RENAME_NOREPLACE; fsync both directories)   <- LAST, after resolution is durable
  6. retire op record
```

A crash anywhere leaves the memory `CONTESTED` with reads still failing closed — the safe direction. The marker is the last thing retired, never the first.

**Retirement is an archive move, not a deletion.** Deleting the marker destroys the audit trail of the resolution and leaves recovery unable to distinguish "resolved" from "never contested." Archiving gives recovery a decidable third state: **resolved-marker + live op** means finish the resolution; **resolved-marker + settled revision** means retire the op. Neither is inferable once the marker is simply gone.

**Tests, one per documented failure mode:**
- stale `direct-user-statement` does **not** beat a fresh `verified-environment-outcome` on a `volatile` fact (Postgres→MySQL)
- fresh `third-party-document` does **not** override a trusted claim at any volatility (the injection regression)
- an excluded untrusted contradiction still surfaces as `unresolved_conflict`
- retrieval **never** returns current + superseded for one entity
- crash at each step of `resolve` leaves the memory contested and reads failing closed

### Step 9 — Search behind the tool boundary (§9.2)

- `base.py` is the interface; rungs swap behind it. `ripgrep.py` (rung 0), `fts5.py` (rung 1).
- Workspace scoping defaults to the current workspace; `--scope all` is explicit opt-in.
- **Retrieval logging** (§15 P0.5 item 9): every query and every retrieved-vs-cited pair to `logs/queries.jsonl`.
- No dense retrieval, no reranker, no graph — §13 triggers are not met on day one.

**Tests:** both rungs satisfy one shared interface contract test; scoping defaults to deny; tombstoned records never returned; every query produces a log line.

### Step 10 — Deletion, purge, backup, restore (§11.5)

**Deletion is two independent gates.** An earlier draft ordered this "tombstone (quorum) → stop retrieval", which leaves deleted content *retrievable* whenever the push fails or the machine is offline — failing open by attaching both properties to one step.

| Gate | Controls | Waits on network? |
|---|---|---|
| Durable local tombstone | retrieval suppression | **No — immediate, unconditional** |
| Replica quorum | the success receipt | Yes |

```text
brain forget <id>
  append tombstone locally, fsync   -> retrieval suppressed IMMEDIATELY
  purge: unlink(present + ALL revisions + chunks + index rows + search rows)
         -> fsync(each parent directory)
         -> VERIFY ABSENCE by re-stat
         -> only THEN append to purges.jsonl
  replicate: push -> ack by re-read -> append to acks.jsonl
    ok     -> delivery_state = confirmed, exit 0
    not ok -> delivery_state = pending
              stderr "DELETION PENDING — quorum unmet (1/2 replicas)"
              exit 3.  NEVER exit 0.
```

**There is no `--force-local`.** An earlier draft had one; it cannot both exit success and honour "not a completed deletion without quorum." Removed rather than weakened.

**A purge receipt appended before the unlink is durable** means the system believes bytes are gone while they survive indefinitely — the deletion property failing silently. Verification is a re-stat, never an assumption that `unlink` returning 0 was sufficient.

But verify-then-append **still races** an editor recreating the path between the re-stat and the append, and no lock fixes that, because a file can also be restored from outside the process entirely. The receipt's *meaning* is what has to change:

> **A purge receipt is a point-in-time observation, not an enduring fact.**

Every scan, backup, and restore **revalidates physical absence for every tombstoned ID** and never trusts a prior receipt. A file reappearing under a tombstoned path is re-purged and the reappearance logged as an anomaly. This is strictly stronger than locking, since it also covers restoration by any means the process never saw.

The privacy property is unaffected throughout: the tombstone suppresses retrieval regardless of physical state.

#### 10.1 Purge and replication are resumable

Crash windows are real: tombstone-but-no-push, and ack-but-no-purge, both leave **valid suppression with residual bytes on disk**. Since `delivery_state` and `purge_state` are derived projections (Step 7), the backlog is computable, so resumption is idempotent:

```text
on startup and on `brain sync`:
  tombstones lacking an ack   -> retry replication (idempotent)
  EVERY tombstoned subject    -> re-scan for physical residue, REGARDLESS of
                                 purge_state; re-purge anything found and log
                                 the reappearance as an anomaly
brain status -> lists replication backlog and any residue found
```

**A receipt must never gate scan eligibility.** An earlier draft resumed only "tombstones lacking a purge", which skips exactly the IDs whose bytes could have been recreated *after* the receipt was written — silently reintroducing the residue the receipt claims is gone. A receipt informs history; it never shortens a scan.

Suppression never depends on either backlog being empty — it is gated only on the local tombstone being durable. **A crash mid-deletion is therefore always safe**: the content is already unreachable, and residue is cleaned on the next run.

- **Redaction fork** (§11.5.4) for multi-subject artifacts and event segments: derive new file → rewrite pointers → tombstone old digest → delete original. Never in-place editing.
- `backup.py`: detect btrfs → attempt snapshot (interactive sudo) → **on refusal or failure, fall back to validated double-collection** (manifest, re-read every path+hash, retry on change, N=3, then fail closed). Record tombstone chain head as high-water mark.
- `restore.py`: verify manifest → prove currency from **outside** the rollback domain → union ledgers → purge → **then** serve. Fail closed otherwise.

**Tests — the ones that justify the project:**
- **backup at seq N → delete → restore that backup → deleted memory does NOT come back** (fully automated)
- suppression is immediate and independent of network state (simulated offline)
- `forget` with unreachable remote exits 3, never 0
- **crash after tombstone before purge** → restart → purge completes, content never retrievable at any point
- **crash after ack before purge** → restart → purge completes, no duplicate ack appended
- restore fails closed on: unprovable currency; stale-but-reachable replica; equal-seq/different-head equivocation; keyring reset or loss
- redaction fork preserves retained content, tombstones the old digest, breaks stale references loudly
- double-collection retries on concurrent modification, fails closed after N
- snapshot path degrades cleanly when sudo is refused

### Step 11 — Resolution UX **[P-2, revised]**

**Visibility already shipped in Steps 2 and 5**, because invariant 10 is violated the moment a divergence or quarantine can exist without an operator surface — and both can exist from the step that creates them. Deferring the whole CLI to here left contested state unsurfaced across Steps 5–10, which is a real forward dependency, not a documentation slip:

| Surface | Ships in | Why there |
|---|---|---|
| `brain quarantine list/show`, `brain validate` nonzero | **Step 2** | quarantine becomes possible the moment schema validation exists |
| `brain conflicts list/show` | **Step 5** | divergence becomes possible the moment the write protocol exists |
| `brain conflicts resolve --take <rev>`, `brain quarantine repair` | Step 11 | resolution is a *mutation* and needs Step 8's crash-safe ordering |

So this step adds only the mutating half. §8.2's criteria bind what exists: the diff is shown before any decision, and there is **no bulk-resolve**. The TUI and the proposal queue remain Phase 2, when something generates proposals.

### Step 12 — MCP server and adapters (§12.1, §11.3)

- **Four tools**: `brain.search`, `brain.get`, `brain.write`, `brain.forget`. Not nine — every description is prefix tokens on every request (§9.5).
- `forget` deliberately separate: destructive operations are never a flag on a general tool.
- **Idempotency keys** on write tools (§12.1).
- Resources under `brain://` with revision, digest, last-modified.
- **Machine-output contract** (§12.2): versioned JSON, stdout for data, stderr for diagnostics, stable exit codes (0 ok, 2 validation, 3 pending-quorum, 4 fail-closed).
- Adapters generate `.mcp.json`, `CLAUDE.md`, Codex MCP config — **pointers only**.

**Tests:** generic-MCP conformance (find → cite → correct → forget); **adapter-purity CI check fails when memory content is injected into a generated file**; idempotent replay of a write tool call produces one memory, not two.

### Step 13 — Export and eval (§12.2, §10.1)

- `brain export --format markdown|jsonl` — full corpus round-trip (§16).
- `runner.py`: run `golden.yaml`, score, emit **Wilson interval**, report `n`, append to `eval/results/`.
- `stats.py`: slope over the series. **Refuses to compute a slope below 150 items** (§10.1) rather than emitting a falsely precise number. The §13 trigger is a **decision prompt**; bare "negative slope" is explicitly not a trigger.
- `bootstrap.py`: draft candidate Q/A pairs from the corpus for accept/edit/reject.
- Templates covering every required category: identifiers, paraphrase, multi-session, temporal/`as_of`, contradiction, abstention, deletion.
- Failure taxonomy tagging: retention / retrieval / relevance / consistency (§10.4).

*(ADRs moved to Step 0 — they gate Phase 1 rather than follow it.)*

---

## 4. Deliberately NOT built

| Not built | Why | Gate |
|---|---|---|
| Background extraction | Interview decision 3 | Phase 2 |
| Proposal review queue + TUI | Nothing generates proposals yet | Phase 2 |
| Consolidation / summaries | Needs a corpus | Phase 2 |
| Cache-hit / cost metrics | Zero model calls; would report a fake zero | Phase 2 |
| Dense retrieval, RRF, reranker | §13 trigger not met | measured |
| Graph store | §13 trigger not met | measured |
| PostgreSQL | Single writer, single human | measured |
| Cursor / OpenCode / Aider | §2.2 non-goal | on breakage |
| Automatic entity resolution | §2.2 non-goal | never at this scale |

---

## 4.5 A review lesson worth carrying into the code

Across eight rounds, four separate blocking findings were the same mistake:

| Finding | The step that was one position too late |
|---|---|
| Intent record | written *after* the revision was published |
| Staging GC | not serialized against op *creation* |
| Purge receipt | appended after verification, but treated as enduring |
| Conflict marker | deleted rather than archived |

**I repeatedly placed a durability or ordering step one position later than the failure it was meant to prevent.** Each fix looked correct in isolation and reintroduced a narrower version of the original bug. Named here because it will recur while writing the code if it is not: *for any durable effect, ask what a crash immediately before it would make indistinguishable — and put the marker there instead.*

---

## 5. Risks

| # | Risk | Mitigation |
|---|---|---|
| R1 | Crash tests that don't test crash safety | `os._exit` fault injection at every boundary, not mocks |
| R2 | `RENAME_EXCHANGE` unavailable on some target | Probed at startup (Step 1); documented fallback with its residual window asserted in a test, not left implicit |
| R3 | Entropy scanner false-positives make `remember` unusable | FP rate measured against real bodies in Step 2; ULIDs, digests, paths must pass |
| R4 | Ledger unreachable makes deletion feel broken | Suppression is immediate and unconditional; only the receipt waits. `brain status` surfaces pending |
| R5 | Derivability test passes trivially on an empty corpus | Fixture exercises every type, status, workspace, and edge case; scan order shuffled, mtimes touched |
| R6 | Eval numbers taken seriously too early | Slope refused below 150 items; `n` always printed |

---

## 5.5 Review outcome — CONSENSUS

Eleven rounds of adversarial review against an independent reviewer (Codex/GPT-5), 27 findings against this plan and 17 against the blueprint, all resolved. Recorded in the reviewer's own words rather than paraphrased, so the record cannot be shaded in the plan's favour:

> - Canonical memory and evidence remain inspectable, erasable files; SQLite remains wholly derived and rebuildable.
> - Writes durably record intent, publish complete staged revisions without overwrite, atomically capture displaced state, and fail closed on divergence.
> - Conflict and quarantine visibility ship when those states first become possible; mutating repair follows the crash-safe resolution machinery.
> - Human judgment resolves contested branches, with losing revisions and resolved conflict records retained for audit.
> - Tombstones suppress every read immediately, while replication quorum governs only successful deletion acknowledgment.
> - Backup and restore establish currency externally and refuse service when it cannot be proven.
> - The plan's build order has no remaining production-relevant forward dependency, and all eleven invariants remain represented.

**What consensus means here.** Two independent reviewers, arguing adversarially, could not find a further defect that would change what gets built or fail in production. It does not mean the plan is correct — the crash-durability assumption (§0.5), the ledger authorship limitation (Step 7), and the residual-risk statement in `BLUEPRINT.md` §11.5.3 all remain live. Consensus is the absence of found defects, not proof of their absence.

---

## 5.7 Post-build hardening

A production-readiness audit after the plan completed found nine further gaps, four
blocking. They are recorded here because several were invisible to the test suite and
one broke the guarantee the entire design review was about.

| Gap | Severity | Resolution |
|---|---|---|
| **Unwitnessed edits destroyed by any mediated write** | blocking | A caller reading present immediately before writing always satisfied CAS, so a hand edit sitting in present was silently discarded — the MCP server did exactly this. Capture is now **unconditional**: displaced bytes not already in the log are published regardless of what the caller claimed. `brain.get` returns a revision token; `brain.write` accepts `expected_revision` so a stale caller diverges |
| Deletion quorum unreachable | blocking | `GitLedgerReplicator` existed with no CLI path, so every `forget` pended forever with no explanation. `brain ledger init|status`; status exits 3 when protection cannot be verified |
| Only memories could be forgotten | blocking | An ingested secret was unerasable — "forgetting" was true of conclusions and false of sources. `--kind memory\|artifact\|event` |
| Decay and expiry never implemented | blocking | Invariant §5.7 did not exist in code. `store/lifecycle.py`: volatility expiry, 30-day proposal decay, 90-day recoverable grace, then a real tombstone |
| Secrets scanned on write but not on output | major | §11.4 requires both. Reads now redact — **reject on write, redact on read**, because at write nothing is on disk yet and at read refusing would hide the problem while the secret sits there |
| `idempotency_key` accepted and ignored | major | A retried call now returns the *original* result, not merely avoiding a duplicate |
| No resource budgets | major | Query log trimmed by age then count, purge ledger compacted. Tombstones are never compacted — that would mean forgetting what was forgotten |
| No CI | major | The adapter purity check was documented as "CI-enforced" and was not |
| mypy never run | minor | 17 errors; strict now passes |

**Five of the nine were found by running the system, not by testing it.** Unit tests
confirm the code does what you wrote; smoke tests reveal what you forgot to write.

Also added for operation: `brain doctor` (one command for every quiet failure mode),
`brain install-timers` (systemd user units — the sweeps are correct and useless
unscheduled), and `docs/RUNBOOK.md`.

---

## 5.6 Build status — COMPLETE

All fourteen steps are built and all nineteen acceptance criteria in §1 pass. **133 tests**, four runtime dependencies, ~7,600 lines including tests.

| Step | Status |
|---|---|
| 0 — ADRs | done — six files, each with reversal criteria |
| 1 — primitives + probe | done — `RENAME_EXCHANGE` via ctypes syscall; probe + denylist |
| 2 — model / front matter / scanner / quarantine | done — fail-closed parsing; scanner rejects, never redacts |
| 3 — events / artifacts | done — content-addressed evidence; pointers resolve to source spans |
| 4 — derived index | done — three enforcement tests incl. derivability under shuffled scan order |
| 5 — write protocol + op lifecycle + recovery | done — crash fault injection at every boundary, verified to fire |
| 6 — reconciliation | done — stable-read; defers on flux; stamps `capture: reconciled` with an interval |
| 7 — tombstones / acks / purges | done — three hash-chained ledgers; broken chain refuses to serve |
| 8 — resolution ordering | done — marker archived last via `RENAME_NOREPLACE`; losing branch retained |
| 9 — search behind the boundary | done — rungs 0 and 1; scoping defaults to deny |
| 10 — deletion / purge / backup / restore | done — suppression immediate; currency proven from outside |
| 11 — resolution UX | done — visibility ships with the step that creates the state |
| 12 — MCP + adapters | done — four tools; purity check at generation *and* write |
| 13 — export + eval | done — three instruments, failure taxonomy, bootstrap |

**Commands:** `init`, `remember`, `search`, `get`, `forget`, `sync`, `ingest`, `record`, `evidence`, `reconcile`, `reindex`, `validate`, `status`, `recover`, `export`, `adapter`, `conflicts list/show/resolve`, `quarantine list`, `eval bootstrap/run/probe/slope`, `backup create/list/verify/restore`. Plus the MCP server.

### What the build found that review did not

Nine defects surfaced during implementation, and the pattern is worth recording: **five were found by running the system, not by testing it.** Unit tests confirm the code does what you wrote; smoke tests reveal what you forgot to write.

| # | Defect | Why tests missed it |
|---|---|---|
| 1 | Present was a hard link to the revision's inode, so an in-place edit rewrote published history | Needed a real editor write against a real inode |
| 2 | `recorded_at` regenerated at parse time, making the index non-deterministic | Caught by the derivability test — which exists *because* the review demanded it |
| 3 | Interval lower bound read *after* publishing, collapsing it to a point | Caught by a test written to be strict about the interval |
| 4 | Reconciled revisions recorded `capture: mediated` | Smoke test — the field was present and wrong, not absent |
| 5 | Deleted content survived in the query log | Smoke test — the log reads as telemetry, so nothing treated it as storage |
| 6 | Second-precision timestamps too coarse for an audit interval | Test flakiness that turned out to be a real modelling error |
| 7 | `Evidence.parse` rejected `#L4-L8`, the form a human writes | Conformance test using a realistic pointer |
| 8 | **Operator attestation resurrected deleted content** — attesting a sequence number is not possessing the entries | Smoke test |
| 9 | **Restore copied files before proving currency**, so a refusal left the bytes on disk | Smoke test |

Defects 8 and 9 share the shape the entire review was about: *a check that passes while the property fails.* Both would have shipped as silent data-loss bugs — one resurrecting deleted content, the other leaving it on disk for a later `reindex` to serve.

### Deliberately not built

Everything in §4, unchanged: background extraction, the proposal review queue and TUI, consolidation, cache/cost metrics (zero model calls — they would report a fake zero), dense retrieval, graph store, PostgreSQL, and the Cursor/OpenCode/Aider adapters. Each is gated on a written trigger or declared a non-goal.

**One honest gap that is not a build gap:** the golden set is a template with worked examples and drafted candidates, not 150 real questions. It cannot be — it needs a real corpus. `brain eval bootstrap` drafts from what you have, and `eval slope` refuses to compute below the floor rather than emitting a falsely precise number. That gap closes by using the system.

---

## 5.5 Review outcome — CONSENSUS

Eleven rounds of adversarial review against an independent reviewer (Codex/GPT-5), 27 findings against this plan and 17 against the blueprint, all resolved. Recorded in the reviewer's own words rather than paraphrased, so the record cannot be shaded in the plan's favour:

> - Canonical memory and evidence remain inspectable, erasable files; SQLite remains wholly derived and rebuildable.
> - Writes durably record intent, publish complete staged revisions without overwrite, atomically capture displaced state, and fail closed on divergence.
> - Conflict and quarantine visibility ship when those states first become possible; mutating repair follows the crash-safe resolution machinery.
> - Human judgment resolves contested branches, with losing revisions and resolved conflict records retained for audit.
> - Tombstones suppress every read immediately, while replication quorum governs only successful deletion acknowledgment.
> - Backup and restore establish currency externally and refuse service when it cannot be proven.
> - The plan's build order has no remaining production-relevant forward dependency, and all eleven invariants remain represented.

**What consensus means here.** Two independent reviewers, arguing adversarially, could not find a further defect that would change what gets built or fail in production. It does not mean the plan is correct — the crash-durability assumption (§0.5), the ledger authorship limitation (Step 7), and the residual-risk statement in `BLUEPRINT.md` §11.5.3 all remain live. Consensus is the absence of found defects, not proof of their absence.

---

## 5.6 Build status

Updated as steps land. A step is "done" only when its tests pass, not when its code exists.

| Step | Status | Notes |
|---|---|---|
| 0 — ADRs | **done** | Six files, each with reversal criteria |
| 1 — primitives + probe | **done** | `RENAME_EXCHANGE` via ctypes syscall; capability probe + denylist |
| 2 — model / front matter / scanner / quarantine | **done** | Fail-closed parsing; credential scanner rejects, never redacts |
| 3 — events / artifacts | **not started** | Evidence pointers currently resolve to opaque refs |
| 4 — derived index | **done** | Three enforcement tests, incl. derivability under shuffled scan order |
| 5 — write protocol + op lifecycle + recovery + conflicts CLI | **done** | Protocol, intent records, divergence, recovery, `conflicts list/show`. Crash fault injection at every boundary, verified to actually fire |
| 6 — reconciliation (stable-read) | **done** | Pull-based with stable-read verification; defers on flux and editor sidecars; stamps `capture: reconciled` with an interval |
| 7 — tombstones / acks / purges | **done** | Three hash-chained ledgers; broken chain refuses to serve; quorum counts distinct replica identities |
| 8 — resolution ordering | **done** | Op record first, marker archived last via `RENAME_NOREPLACE`; losing branch retained |
| 9 — search behind the boundary | **done** | Rungs 0 and 1; workspace scoping defaults to deny |
| 10 — deletion / purge | **done** | Suppression immediate and unconditional; quorum gates only the receipt; resumable. Backup/restore commands still to come |
| 11 — resolution UX | **done** | `conflicts resolve` and `reconcile` land here; read-only half shipped earlier |
| 12 — MCP + adapters | **done** | Four tools on MCP SDK 2.0; enum values in the wire schema via `Literal`. Adapters for Claude Code and Codex with the §11.3 purity check enforced at generation *and* at write |
| 13 — export + eval | **partial** | `brain export` (markdown + JSONL, tombstoned subjects excluded, ledgers included) and the statistics that gate the migration trigger. **The eval runner and `bootstrap` are not built** |

**What works today:** `init`, `remember`, `search`, `get`, `forget`, `sync`, `reconcile`, `reindex`, `validate`, `status`, `recover`, `export`, `adapter`, `conflicts list/show/resolve`, `quarantine list`, plus the MCP server. **108 tests passing.**

**Deletion works end to end**, including the headline test: back up, delete, restore the pre-deletion backup, and the content does not come back — because the ledger lives outside the restored domain.

**A leak the tests did not catch, and a smoke test did.** The query log pairs a query string with the IDs it returned, and a query is very often a fragment of the memory itself. After a deletion, the words you asked to forget were still sitting in `logs/queries.jsonl` — a file that reads as telemetry rather than storage. `purge` now reaches it, and there is a regression test. Worth recording because it is the exact shape of failure §11.5 warns about: deletion has to reach *every* derived representation, and the ones you do not think of as storage are the ones that survive.

**A second leak smoke tests caught that unit tests did not.** A reconciled revision recorded `capture: mediated` by default — losing exactly the distinction the field exists to make, between a transition the write protocol witnessed and one it is inferring after the fact. Reconciliation now stamps `capture: reconciled` plus the interval onto the bytes before publishing. Both of this phase's real defects were found by running the thing, not by testing it.

**Still missing:** events and artifacts (Step 3), backup/restore commands (Step 10 remainder), and the eval runner plus `eval bootstrap` (Step 13 remainder).

The eval gap is the one that matters, and it is worth naming precisely: the *statistics* that gate the migration trigger are built and tested — including the refusal to compute a slope below 150 items, which is the defect the review caught. What is missing is the runner that produces the measurements, and it is missing because it needs a real golden set, which needs a real corpus. Building a runner against an empty set would produce exactly the falsely-precise number §10.1 exists to prevent.

---

## 6. Verification gate

`pytest` green across `unit/`, `integration/`, `crash/`, `conformance/`; `brain validate` clean; `ruff` and `mypy` clean; and all nineteen criteria in §1 passing as automated tests — including the backup-resurrection test, with no manual step.

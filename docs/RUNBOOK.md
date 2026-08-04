# Runbook

Operating a `brain` store. Written for the person who has to fix it at an
inconvenient hour, so every incident section starts with what is *actually broken*
rather than what the error says.

---

## 1. First run

```bash
uv sync && uv pip install -e .

brain init                      # probes the filesystem, refuses unsafe ones
brain doctor                    # expect WARNs: no replica, no backup, empty store
```

`brain init` refuses network filesystems and sync folders. That refusal is not
conservatism — a sync client rewrites files behind the process, which breaks
compare-and-swap and the immutability of revisions.

### 1.1 The off-device replica — do this before you delete anything

Until a second replica exists, **quorum is unreachable and every `brain forget` will
report `pending` forever.** The deletion is real and the content is already
unreachable; it simply is not a *completed* deletion.

```bash
gh repo create brain-ledger --private
brain ledger init --remote git@github.com:<you>/brain-ledger.git
brain ledger status             # must exit 0
```

`ledger status` exits 3 if it cannot *verify* the remote rejects force-push and
branch deletion. That is deliberate: a history that can be rewritten provides no
monotonicity, so acks from an unprotected remote are rejected at quorum time. A
remote that merely *works* is not an anchor.

The ledger holds only tombstones — sequence numbers, subject ids, hashes, timestamps.
No titles, no bodies, no excerpts.

### 1.2 Scheduling

The sweeps are correct and useless unscheduled.

```bash
brain install-timers --dry-run   # read them first
brain install-timers
systemctl --user daemon-reload
systemctl --user enable --now brain-sync.timer brain-expire.timer brain-backup.timer
systemctl --user list-timers 'brain-*'
```

User units, never system units: this needs no root, and a service running as root
would have more access to your memories than you do.

### 1.3 Wire it to your agent

```bash
brain adapter claude --repo ~/Projects/somewhere
brain adapter codex  --repo ~/Projects/somewhere
```

Generated files carry **pointers only** — never memory content. That is a security
control, not tidiness: `CLAUDE.md` is loaded as high-trust instruction context, and a
generator that can inline retrieved content rebuilds the exact path Claude Code
v2.1.50 removed. CI enforces it.

---

## 2. Daily and weekly

| Cadence | Command | Why |
|---|---|---|
| Ad hoc | `brain remember "…"` | The only capture path today. See §5. |
| Hourly (timer) | `brain sync` | Resume deletions, retry replication, trim logs |
| Daily (timer) | `brain expire` | Lapse stale memories; tombstone past-grace ones |
| Daily (timer) | `brain backup create` | Verifiable backup with a tombstone high-water mark |
| Weekly | `brain eval run` | Only meaningful once the golden set exceeds 150 items |
| When something feels off | `brain doctor` | Every quiet failure mode in one place |

`brain doctor` exit codes: `0` clean, `3` warnings, `4` a safety property is not
holding right now.

---

## 3. Incidents

### 3.1 `REFUSING TO SERVE: hash chain broken`

**What is actually wrong:** the tombstone, ack, or purge ledger has been modified. The
store refuses every read rather than serve content it cannot prove was not deleted.

```bash
brain doctor                                  # which chain, which sequence
cd ~/.local/state/brain/ledger.git && git log --oneline
```

Restore the ledger from the replica, then `brain sync`. **Do not** hand-edit the file
to make the error go away — the chain exists precisely so that tampering is loud.

### 3.2 A deletion says `pending` and stays there

Expected when no replica is configured (§1.1). Otherwise:

```bash
brain ledger status     # protection verified?
brain sync              # retries replication; idempotent
brain status            # what is still outstanding
```

Retrieval is already suppressed and the bytes are already gone locally. Only the
*receipt* is waiting.

### 3.3 `deletion-residue` — FAIL

Tombstoned subjects still have bytes on disk. Suppression holds; the purge did not
complete or something recreated the files.

```bash
brain sync              # re-scans EVERY tombstoned subject, receipt or not
brain doctor
```

If residue persists, something outside the process is recreating the path — a restore,
a sync client, an editor with an open buffer.

### 3.4 A memory is `contested`

Two branches diverged and reads fail closed. This is working as designed.

```bash
brain conflicts list
brain conflicts show <id>                 # both branches, with a diff
brain conflicts resolve <id> --take <n>
```

The losing branch stays in the log permanently. A resolution is a decision, not an
erasure.

### 3.5 `brain validate` reports quarantine

A file failed front-matter validation and is excluded from all retrieval. It is not
silently skipped — that would make a memory vanish from answers with nobody told.

```bash
brain quarantine list
$EDITOR <path>          # fix the front matter
brain reindex
```

### 3.6 `dangling-evidence`

Claims cite evidence that no longer resolves — usually because an artifact or event
was erased. Legitimate, but it weakens every claim that rested on it.

```bash
brain validate          # which claims, which refs
```

Either re-ingest the source, or correct the claims to stop asserting what they can no
longer support.

### 3.7 Restore refuses

```
REFUSING TO RESTORE — nothing was written: …
```

**Nothing was written.** Currency is proven *before* any byte is copied back, because
a refusal that leaves the deleted bytes on disk is not a refusal.

A backup cannot vouch for its own currency — its high-water mark is a lower bound.
Supply an anchor from outside the rollback domain:

```bash
brain backup restore <manifest> --replica /path/to/tombstones.jsonl
brain backup restore <manifest> --attest-seq <n>     # you assert the head
```

`--attest-seq` asserts you are not *behind*. It does not hand over the entries: if the
local ledger falls short of the attested sequence, restore refuses, because those
deletions cannot be replayed and restoring would resurrect them.

### 3.8 A secret got in

```bash
brain validate                                  # find it
brain forget <memory-id>                        # or:
brain forget <digest> --kind artifact
brain forget <event-id> --kind event
brain sync
```

Reads already mask credentials, but **masking protects the model, not the disk.** The
bytes are still there until you purge them. If the store has ever been backed up,
those backups still contain it.

---

## 4. Recovery drill — run this before you need it

A restore procedure you have never executed is a hope.

```bash
brain backup create
cp ~/.local/state/brain/tombstones.jsonl /tmp/replica.jsonl
ID=$(brain remember "drill: delete me" | jq -r .data.id)
brain backup create
brain forget "$ID"
cp ~/.local/state/brain/tombstones.jsonl /tmp/replica.jsonl

brain backup restore <pre-deletion-manifest> --replica /tmp/replica.jsonl
brain search "drill"        # must return nothing
```

If that last line returns the memory, the anti-resurrection property is broken and
nothing else in this document matters.

---

## 5. Known limits — none of these are bugs

**Capture is explicit-only.** Nothing is written unless you write it. Background
extraction is Phase 2 and deliberately absent. This is the biggest risk to the system
being *useful*: every personal knowledge system that failed, failed of disuse rather
than corruption. `brain doctor` warns on an empty store for exactly this reason.

**Crash durability is assumed, not proven.** The startup probe establishes capability;
`fsync` returning 0 says nothing about a lying disk or a write cache. ADR 0005.

**Workspace scoping is not a security boundary.** Agent-native `Read`/`Grep` reach any
memory file regardless of workspace. It is a relevance and context-collapse control.
Real isolation needs directory-per-workspace with OS permissions. ADR 0004.

**The hash chain detects corruption, not authorship.** Anyone who can write the file
can rewrite the chain consistently. Authorship rests on OS identity, filesystem
permissions, and an authenticated `gh`.

**Total replica loss makes deletion currency unknowable.** If every ledger replica and
the anchor are gone at once, a human must attest. Stated in §11.5.3 rather than
engineered around.

**The eval instruments say nothing yet.** `brain eval slope` refuses to compute below
150 golden-set items. Until then the migration triggers in ADR 0002 are inert — which
is correct, not broken.

---

## 6. Where things live

```
~/.local/state/brain/               CANONICAL — none of this is in git
├── memories/<ws>/<type>/<id>.md    present state
├── memories/.revisions/<id>/       every committed state, append-only
├── events/YYYY-MM-DD.jsonl         evidence pointers resolve here
├── artifacts/sha256/…              original bytes, content-addressed
├── tombstones.jsonl                the anti-resurrection authority
├── acks.jsonl  purges.jsonl        derived status, never mutated
├── ledger.git/                     → the off-device replica
├── quarantine/  conflicts/  ops/   things needing a human
├── logs/queries.jsonl              retrieval log (trimmed by `sync`)
├── brain.sqlite3                   DERIVED — delete it any time
└── backups/<generation>/           + <generation>.manifest.json
```

`rm ~/.local/state/brain/brain.sqlite3 && brain reindex` is always safe. That is
enforced by three tests, not by convention.

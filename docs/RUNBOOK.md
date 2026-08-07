# Runbook

Operating a `brain` store. Written for the person who has to fix it at an
inconvenient hour, so every incident section starts with what is *actually broken*
rather than what the error says.

---

## 1. First run

```bash
./install.sh                    # everything below, in order, idempotently
```

That is the whole thing on a fresh machine. The rest of this section is what it does
and how to do each part by hand when you need to.

```bash
./install.sh --dry-run                              # print, change nothing
./install.sh --harness claude,pi --scope user       # skip autodetection
./install.sh --ledger-remote git@github.com:you/brain-ledger.git
./install.sh --skip-timers                          # no scheduler units
```

The installer touches nothing outside `$HOME`, needs no `sudo`, and can be re-run
safely at any time — CI asserts the second run is byte-identical. It will not create
the ledger repository or enable the timers for you; both are decisions, not steps.

**It installs the CLI with `uv tool install --editable`, not into the project venv.**
That is load-bearing rather than stylistic. Every generated harness config records an
absolute interpreter path, and an earlier setup recorded the *project* venv's — so
recreating that venv broke every harness at once, silently, because the hooks fail
open. A `uv` tool environment is independent of anything `uv sync` does in the
checkout.

By hand:

```bash
uv tool install --editable .    # or: uv sync && uv pip install -e .
brain init                      # probes the filesystem, refuses unsafe ones
brain doctor                    # expect WARNs: no replica, no backup, empty store
```

`brain init` refuses network filesystems and sync folders. That refusal is not
conservatism — a sync client rewrites files behind the process, which breaks
compare-and-swap and the immutability of revisions. It also refuses any kernel other
than Linux and macOS, because the write protocol needs an atomic path exchange and
brain only implements one for those two.

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
brain install-timers             # then run the commands it prints
```

`install-timers` writes systemd `--user` units on Linux and LaunchAgents on macOS,
then prints the activation commands for whichever it wrote. **Writing a unit and
scheduling it are different acts** — nothing runs until you run those commands. Take
them from the tool's output rather than from memory; the list here was wrong once,
omitting `brain-expire.timer`, which is the kind of error that shows up months later
as "why did nothing ever expire".

User units, never system units: this needs no root, and a service running as root
would have more access to your memories than you do. The same rule picks
`~/Library/LaunchAgents` over `/Library/LaunchDaemons` on macOS.

One caveat on macOS: launchd has no equivalent of `SuccessExitStatus`, so a sweep
exiting 3 — "a deletion is still pending", a normal state — is logged as a failure.
Nothing acts on it; the practical effect is a log line.

### 1.3 Wire it to your agent

```bash
brain adapter claude --scope user               # every session, this machine
brain adapter codex  --repo ~/Projects/thing    # one checkout
```

Five targets: `claude`, `codex`, `opencode`, `pi`, `omp`. Two scopes: `user` writes
into the harness's own configuration directory, `repo` into a project.

Each harness spells the same three facts — interpreter, argv, environment — into a
different schema, and **a config in the wrong schema is not an error**. It parses, it
is ignored, and nothing tells you. That silent no-op is why there are five targets
rather than one generic writer, and why `pi` gets *no* MCP config: it ships no MCP
client, so the store is reachable there through the CLI alone.

Files that belong to another tool (`~/.claude/settings.json`, `~/.codex/config.toml`,
`opencode.json`) are **merged, never overwritten**, and copied once to
`<name>.pre-brain.bak` before the first change. Which paths have been touched is
recorded in `<state>/adapters-touched.json` rather than inferred from whether a backup
exists — otherwise a second run would save brain's own output under a name claiming
to be what preceded it.

Generated files carry **pointers only** — never memory content. That is a security
control, not tidiness: `CLAUDE.md` is loaded as high-trust instruction context, and a
generator that can inline retrieved content rebuilds the exact path Claude Code
v2.1.50 removed. CI enforces it across every target and both scopes.

The one file that is *copied* rather than generated is `harness/SKILL.md`. It carries
prose a generator has no business inventing, so it reaches an instruction file the
only way such content may: through a reviewed commit. It is still checked — its front
matter is validated against a skill-metadata allowlist and stripped, and the body
faces every memory-content marker.

---

## 1.4 Wire it into the development loop

The gap this closes: nothing is written unless you write it, and "remember to write
it" is not a mechanism. Two hooks make consultation and capture structural.

```bash
brain adapter claude --scope user    # installs and wires both hooks
```

The scripts are committed at `harness/hooks/`, installed to
`~/.claude/hooks/brain-{context,capture}.sh`, and registered in
`~/.claude/settings.json` under `UserPromptSubmit` and `Stop`. Re-running rewrites
those two entries rather than appending — matched by script basename, so moving the
repository updates the wiring instead of leaving a stale copy shadowing it.

They resolve `brain` from `PATH`, never from a project venv. `BRAIN_BIN` overrides.

**Consult (UserPromptSubmit).** Every prompt is checked against the store. If related
memories exist you get *pointers* — id, a truncated label, workspace — and an
instruction to read them properly. Silent when nothing matches.

It emits pointers rather than content on purpose. Auto-injecting memories would
contradict three settled decisions: the agent pulls through a tool loop (§9.1), the
prefix is not mutated per turn (§9.5), and memory used in an answer must be visible
in that answer (§11.6). The hook guarantees you always *look*; a `brain.search` call
is still how you *read*.

**Capture (Stop).** Fires once per session, and only when the working tree changed
*and* nothing was written to the store. Writing a memory clears the condition, so it
cannot loop — and "nothing here was worth keeping" is a legitimate answer that ends
the turn.

**Workspaces are per repository.** `brain` derives the workspace from the git root, so
your work project and your side project do not surface in each other's searches.
Override with `BRAIN_WORKSPACE`, widen a single search with `--scope-all`.

Both hooks fail open: if `brain` is missing, slow, or broken, they exit silently. A
memory system that blocks your editor is one you will turn off.

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
├── adapters-touched.json           which foreign configs we have merged into
├── brain.sqlite3                   DERIVED — delete it any time
└── backups/<generation>/           + <generation>.manifest.json
```

Harness wiring lives outside the store, in each harness's own configuration
directory — `~/.claude/`, `~/.codex/`, `~/.config/opencode/`, `~/.pi/agent/`,
`~/.omp/agent/`. It is generated, not canonical: delete any of it and re-run
`brain adapter <target> --scope user`. The committed sources are in `harness/`.

`rm ~/.local/state/brain/brain.sqlite3 && brain reindex` is always safe. That is
enforced by three tests, not by convention.

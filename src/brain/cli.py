"""The neutral CLI (BLUEPRINT.md §12.2).

Machine-readable output uses versioned JSON on stdout; diagnostics go to stderr.
Exit codes are stable and meaningful, because a caller that cannot distinguish
"nothing found" from "refusing to serve" will treat the second as the first:

    0  ok
    1  not found / empty
    2  validation failure (quarantine, bad input)
    3  pending — the operation is durable locally but quorum is unmet
    4  fail closed — integrity could not be established; nothing was served
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path
from typing import Annotated

import typer

from . import adapters as adapters_mod
from . import backup as backup_mod
from . import config, scan
from . import export as export_mod
from .frontmatter import InvalidFrontmatter, parse, serialize
from .ids import new_ulid
from .index import build
from .model import Evidence, Memory, MemoryType, ProvenanceClass, Volatility
from .model import today as model_today
from .search.fts5 import Fts5Backend
from .search.ripgrep import RipgrepBackend
from .store import artifacts, budgets, deletion, events, ledger, reconcile, revisions
from .store import memory as mem
from .store import resolve as resolve_mod

EXIT_OK, EXIT_EMPTY, EXIT_INVALID, EXIT_PENDING, EXIT_FAIL_CLOSED = 0, 1, 2, 3, 4

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="A durable, local-first second brain: evidence, forgetting, portability.",
)
conflicts_app = typer.Typer(no_args_is_help=True, help="Divergent writes awaiting a human.")
quarantine_app = typer.Typer(no_args_is_help=True, help="Files that failed validation.")
app.add_typer(conflicts_app, name="conflicts")
eval_app = typer.Typer(no_args_is_help=True, help="The three evaluation instruments.")
backup_app = typer.Typer(no_args_is_help=True, help="Verifiable backup and fail-closed restore.")
app.add_typer(quarantine_app, name="quarantine")
app.add_typer(eval_app, name="eval")
ledger_app = typer.Typer(no_args_is_help=True, help="The off-device tombstone replica.")
app.add_typer(backup_app, name="backup")
app.add_typer(ledger_app, name="ledger")


def err(msg: str) -> None:
    print(msg, file=sys.stderr)


def emit(payload: object) -> None:
    print(json.dumps({"version": 1, "data": payload}, indent=2, default=str))


def _paths(state: str | None) -> config.Paths:
    return config.paths(state)


def _ws(explicit: str | None) -> str:
    """An explicit --workspace, else the current git repo, else 'default'."""
    return explicit or config.default_workspace()


StateOpt = Annotated[str | None, typer.Option("--state", help="Override the state directory.")]
JsonOpt = Annotated[bool, typer.Option("--json", help="Machine-readable output.")]


@app.command()
def init(
    state: StateOpt = None,
    force: Annotated[bool, typer.Option("--force", help="Skip the safety probe.")] = False,
) -> None:
    """Create the store and verify the filesystem can host it safely."""
    p = _paths(state)
    if not force:
        try:
            caps = config.check_preconditions(p.root)
        except config.PreconditionError as exc:
            err(f"refusing to initialize: {exc}")
            raise typer.Exit(EXIT_FAIL_CLOSED) from exc
        err(f"filesystem probe passed: {', '.join(k for k, v in vars(caps).items() if v)}")
        err("note: this establishes capability, not crash durability (ADR 0005).")
    for d in p.all_dirs():
        d.mkdir(parents=True, exist_ok=True)
    conn = build.connect(p)
    conn.close()
    err(f"initialized {p.root}")


@app.command()
def remember(
    text: Annotated[str, typer.Argument(help="The memory body.")],
    type_: Annotated[
        str, typer.Option("--type", help="episodic|semantic|preference|procedural|task")
    ] = "semantic",
    volatility: Annotated[
        str, typer.Option("--volatility", help="immutable|slow|volatile|ephemeral")
    ] = "volatile",
    provenance: Annotated[str, typer.Option("--provenance")] = "direct-user-statement",
    evidence: Annotated[
        list[str] | None, typer.Option("--evidence", help="event:… | artifact:…")
    ] = None,
    workspace: Annotated[str | None, typer.Option("--workspace")] = None,
    tags: Annotated[list[str] | None, typer.Option("--tag")] = None,
    state: StateOpt = None,
) -> None:
    """Write a memory. Explicit writes land confirmed.

    Without --workspace the memory lands in the workspace named after the current
    git repository, so one project's decisions do not surface in another's.
    """
    p = _paths(state)
    workspace = _ws(workspace)
    try:
        scan.assert_clean(text)
    except scan.SecretFound as exc:
        err(str(exc))
        raise typer.Exit(EXIT_INVALID) from exc

    try:
        m = Memory(
            id=new_ulid(),
            type=MemoryType(type_),
            provenance_class=ProvenanceClass(provenance),
            volatility=Volatility(volatility),
            valid_from=model_today(),
            evidence=[Evidence.parse(e) for e in (evidence or ["user:direct"])],
            body=text,
            workspace=workspace,
            tags=list(tags or []),
        )
    except ValueError as exc:
        err(f"invalid field: {exc}")
        raise typer.Exit(EXIT_INVALID) from exc

    body = serialize(m).encode()
    try:
        result = mem.write(p, m.id, body, None, workspace=workspace, memory_type=m.type.value)
    except mem.Divergence as exc:
        err(str(exc))
        raise typer.Exit(EXIT_FAIL_CLOSED) from exc

    conn = build.connect(p)
    build.rebuild(p, conn)
    conn.close()
    emit({"id": result.memory_id, "revision": result.revision_no, "workspace": workspace})


@app.command()
def search(
    query: Annotated[str, typer.Argument()],
    workspace: Annotated[str | None, typer.Option("--workspace")] = None,
    all_workspaces: Annotated[
        bool, typer.Option("--scope-all", help="Search every workspace.")
    ] = False,
    limit: Annotated[int, typer.Option("--limit")] = 10,
    as_of: Annotated[
        str | None, typer.Option("--as-of", help="ISO date; query historical validity.")
    ] = None,
    rung: Annotated[int, typer.Option("--rung", help="0=ripgrep, 1=fts5")] = 1,
    state: StateOpt = None,
) -> None:
    """Search memories. Scoped to the current workspace unless --scope-all."""
    p = _paths(state)
    _require_intact_ledgers(p)
    ws = None if all_workspaces else _ws(workspace)
    backend = RipgrepBackend(p) if rung == 0 else Fts5Backend(p)
    hits = backend.search(query, workspace=ws, limit=limit, as_of=as_of)

    _log_query(p, query, ws, backend.name, [h.memory_id for h in hits])
    if not hits:
        err("no results")
        raise typer.Exit(EXIT_EMPTY)
    emit(
        {
            "backend": backend.name,
            "rung": backend.rung,
            "hits": [
                {
                    "id": h.memory_id,
                    "workspace": h.workspace,
                    "title": h.title,
                    "excerpt": h.excerpt,
                    "evidence": h.evidence,
                    "path": h.path,
                }
                for h in hits
            ],
        }
    )


def _log_query(p: config.Paths, q: str, ws: str | None, backend: str, ids: list[str]) -> None:
    """Every query and every retrieved-vs-cited pair, from day one (§15 Phase 0.5).

    Without this log there is no way to tell later which failures were retrieval
    failures — and the failure taxonomy is what decides where optimization goes.
    """
    from .model import iso, utcnow

    p.logs.mkdir(parents=True, exist_ok=True)
    entry = {
        "at": iso(utcnow()),
        "query": q,
        "workspace": ws,
        "backend": backend,
        "retrieved": ids,
        "cited": None,  # filled in by the caller that actually uses a result
    }
    with (p.logs / "queries.jsonl").open("a") as fh:
        fh.write(json.dumps(entry) + "\n")


@app.command()
def get(
    memory_id: Annotated[str, typer.Argument()],
    history: Annotated[bool, typer.Option("--history")] = False,
    state: StateOpt = None,
) -> None:
    """Fetch a memory with its provenance."""
    p = _paths(state)
    _require_intact_ledgers(p)
    conn = build.connect(p)
    row = conn.execute("SELECT * FROM memory_index WHERE id = ?", (memory_id,)).fetchone()
    if row is None:
        conn.close()
        err(f"{memory_id}: not found")
        raise typer.Exit(EXIT_EMPTY)
    if row["disposition"] == "contested":
        conn.close()
        err(f"{memory_id}: contested — reads fail closed until `brain conflicts resolve`.")
        raise typer.Exit(EXIT_FAIL_CLOSED)

    path = Path(row["file_path"])
    content, redacted = scan.redact(path.read_text() if path.exists() else "")
    payload: dict[str, object] = {
        "id": memory_id,
        "workspace": row["workspace"],
        "disposition": row["disposition"],
        "content": content or None,
        **({"redacted": sorted({f.kind for f in redacted})} if redacted else {}),
        "evidence": [
            dict(r)
            for r in conn.execute(
                "SELECT source_ref, span_start, span_end FROM evidence_link WHERE memory_id = ?",
                (memory_id,),
            )
        ],
    }
    if history:
        payload["revisions"] = [
            dict(r)
            for r in conn.execute(
                "SELECT revision_no, content_hash, capture, recorded_from, recorded_to "
                "FROM revision_index WHERE memory_id = ? ORDER BY revision_no",
                (memory_id,),
            )
        ]
    conn.close()
    emit(payload)


@app.command()
def reindex(state: StateOpt = None) -> None:
    """Drop every derived row and rebuild from files. Never loses anything."""
    p = _paths(state)
    if p.db.exists():
        p.db.unlink()
    counts = build.rebuild(p)
    emit(counts)


@app.command()
def validate(state: StateOpt = None) -> None:
    """Check every memory file. Nonzero exit if anything is quarantined."""
    p = _paths(state)
    bad: list[dict[str, str]] = []
    for f in sorted(p.memories.rglob("*.md")):
        if ".revisions" in f.parts or ".staging" in f.parts:
            continue
        try:
            parse(f.read_text(), f)
        except (InvalidFrontmatter, UnicodeDecodeError) as exc:
            bad.append({"path": str(f), "reason": str(exc)})
    contested = [c.stem for c in p.conflicts.glob("*.json")] if p.conflicts.is_dir() else []
    dangling = deletion.dangling_evidence(p)
    emit({"quarantined": bad, "contested": contested, "dangling_evidence": dangling})
    if bad or contested or dangling:
        raise typer.Exit(EXIT_INVALID)


@app.command()
def status(state: StateOpt = None) -> None:
    """Outstanding work: stuck operations, conflicts, quarantine."""
    from .store.ops import pending_ops, stuck_ops

    p = _paths(state)
    try:
        deletion.verify_all_ledgers(p)
        ledger_health = "intact"
    except ledger.LedgerError as exc:
        ledger_health = f"BROKEN — {exc}"
    emit(
        {
            "root": str(p.root),
            "ledgers": ledger_health,
            "pending_ops": [o.opid for o in pending_ops(p)],
            "stuck_ops": [{"opid": o.opid, "why": why} for o, why in stuck_ops(p)],
            "conflicts": [c.stem for c in p.conflicts.glob("*.json")]
            if p.conflicts.is_dir()
            else [],
            "pending_deletions": deletion.pending_deletions(p),
        }
    )
    if ledger_health != "intact":
        raise typer.Exit(EXIT_FAIL_CLOSED)


@conflicts_app.command("list")
def conflicts_list(state: StateOpt = None) -> None:
    """List divergences awaiting resolution."""
    p = _paths(state)
    items = []
    if p.conflicts.is_dir():
        for f in sorted(p.conflicts.glob("*.json")):
            items.append(json.loads(f.read_text()))
    emit(items)
    if not items:
        raise typer.Exit(EXIT_EMPTY)


@conflicts_app.command("show")
def conflicts_show(memory_id: Annotated[str, typer.Argument()], state: StateOpt = None) -> None:
    """Show both branches of a divergence, so a human can choose."""
    p = _paths(state)
    f = p.conflict_path(memory_id)
    if not f.exists():
        err(f"{memory_id}: no conflict recorded")
        raise typer.Exit(EXIT_EMPTY)
    record = json.loads(f.read_text())
    for branch in record.get("branches", []):
        data = revisions.read_revision(p, memory_id, branch["revision"])
        branch["content"] = data.decode("utf-8", "replace") if data else None
    emit(record)


@quarantine_app.command("list")
def quarantine_list(state: StateOpt = None) -> None:
    """List files excluded from retrieval because they failed validation."""
    p = _paths(state)
    items = []
    for f in sorted(p.memories.rglob("*.md")):
        if ".revisions" in f.parts or ".staging" in f.parts:
            continue
        try:
            parse(f.read_text(), f)
        except (InvalidFrontmatter, UnicodeDecodeError) as exc:
            items.append({"path": str(f), "reason": str(exc)})
    emit(items)
    if not items:
        raise typer.Exit(EXIT_EMPTY)


def _require_intact_ledgers(p: config.Paths) -> None:
    """Refuse to serve on a broken chain.

    The tombstone ledger is the anti-resurrection authority. An authority that cannot
    prove its own integrity is not one, so this is a hard stop rather than a warning.
    """
    try:
        deletion.verify_all_ledgers(p)
    except ledger.LedgerError as exc:
        err(str(exc))
        raise typer.Exit(EXIT_FAIL_CLOSED) from exc


@app.command()
def forget(
    memory_id: Annotated[str, typer.Argument(help="Memory id, artifact digest, or event id.")],
    kind: Annotated[str, typer.Option("--kind", help="memory|artifact|event")] = "memory",
    workspace: Annotated[str, typer.Option("--workspace")] = "default",
    type_: Annotated[str, typer.Option("--type")] = "semantic",
    reason: Annotated[
        str | None, typer.Option("--reason", help="Recorded in the ledger. Avoid content.")
    ] = None,
    state: StateOpt = None,
) -> None:
    """Delete a memory. Suppression is immediate; success waits on replica quorum.

    Exits 3 (pending) when quorum is unmet. That is not a failure — the deletion is
    durable and the content is already unreachable — but it is not a completed
    deletion either, and there is deliberately no flag that turns one into the other.
    """
    p = _paths(state)
    _require_intact_ledgers(p)

    replicator = _replicator(p)
    if kind not in ("memory", "artifact", "event"):
        err(f"--kind must be memory, artifact, or event (got {kind!r})")
        raise typer.Exit(EXIT_INVALID)
    result = deletion.forget(
        p,
        memory_id,
        kind=kind,
        workspace=workspace,
        mtype=type_,
        reason=reason,
        replicate=replicator,
    )
    conn = build.connect(p)
    build.rebuild(p, conn)
    conn.close()

    emit(
        {
            "id": result.subject_id,
            "suppressed": result.suppressed,
            "delivery": result.delivery.value,
            "replicas": f"{result.replicas}/{result.required}",
            "removed": result.removed,
            "residue": result.residue,
        }
    )
    if result.residue:
        err(
            f"WARNING: bytes remain at {result.residue}. Retrieval is suppressed; run `brain sync`."
        )
        raise typer.Exit(EXIT_FAIL_CLOSED)
    if not result.complete:
        err(
            f"DELETION PENDING — quorum unmet ({result.replicas}/{result.required} replicas). "
            f"Retrieval is already suppressed and the content is removed locally. "
            f"Run `brain sync` when a second replica is reachable."
        )
        raise typer.Exit(EXIT_PENDING)


@app.command()
def sync(state: StateOpt = None) -> None:
    """Finish every incomplete deletion: re-scan for residue, retry replication.

    Idempotent, and safe to run at any time. Every tombstoned subject is re-scanned
    regardless of whether it already has a purge receipt — a receipt informs history,
    it never shortens a scan.
    """
    p = _paths(state)
    _require_intact_ledgers(p)
    report = deletion.resume(p, replicate=_replicator(p))
    pending = deletion.pending_deletions(p)
    # Budgets ride along with sync rather than needing their own timer: an
    # append-only store that runs unattended for months fills a disk with its own
    # telemetry otherwise.
    trimmed = budgets.sweep(p)
    emit({**report, "still_pending": pending, "budgets": trimmed})
    if report["residue"]:
        raise typer.Exit(EXIT_FAIL_CLOSED)
    if pending:
        raise typer.Exit(EXIT_PENDING)


def _replicator(p: config.Paths) -> deletion.Replicator:
    """The configured off-device replica, or a null one that never fabricates quorum."""
    from .store.replicate import GitLedgerReplicator

    if p.ledger_git.exists():
        r = GitLedgerReplicator(p)
        if r.remote:
            return r
    return deletion.NullReplicator()


@conflicts_app.command("resolve")
def conflicts_resolve(
    memory_id: Annotated[str, typer.Argument()],
    take: Annotated[int, typer.Option("--take", help="Revision number to adopt.")],
    workspace: Annotated[str, typer.Option("--workspace")] = "default",
    type_: Annotated[str, typer.Option("--type")] = "semantic",
    state: StateOpt = None,
) -> None:
    """Adopt one branch of a divergence. The losing branch stays in the log."""
    p = _paths(state)
    try:
        result = resolve_mod.resolve(p, memory_id, take, workspace=workspace, mtype=type_)
    except (resolve_mod.NotContested, resolve_mod.UnknownBranch) as exc:
        err(str(exc))
        raise typer.Exit(EXIT_INVALID) from exc
    conn = build.connect(p)
    build.rebuild(p, conn)
    conn.close()
    emit(
        {
            "id": result.memory_id,
            "took": result.taken,
            "new_revision": result.new_revision,
            "archived_marker": result.archived_marker,
        }
    )


@app.command("reconcile")
def reconcile_cmd(state: StateOpt = None) -> None:
    """Capture edits made outside the write protocol.

    Files still being written are deferred and reported, never guessed at.
    """
    p = _paths(state)
    results = reconcile.reconcile_all(p)
    captured = [str(r) for r in results if isinstance(r, reconcile.Reconciled)]
    deferred = [str(r) for r in results if isinstance(r, reconcile.Deferred)]
    if captured:
        conn = build.connect(p)
        build.rebuild(p, conn)
        conn.close()
    emit({"captured": captured, "deferred": deferred})


@app.command()
def export(
    dest: Annotated[str, typer.Argument(help="Destination directory.")],
    fmt: Annotated[str, typer.Option("--format", help="markdown|jsonl|both")] = "both",
    state: StateOpt = None,
) -> None:
    """Export the whole corpus. Tombstoned subjects are never included."""
    p = _paths(state)
    _require_intact_ledgers(p)
    out = Path(dest)
    result: dict[str, object] = {}
    if fmt in ("markdown", "both"):
        result["markdown"] = export_mod.export_markdown(p, out)
    if fmt in ("jsonl", "both"):
        result["jsonl"] = export_mod.export_jsonl(p, out / "memories.jsonl")
    emit({"dest": str(out), **result})


@app.command()
def adapter(
    target: Annotated[str, typer.Argument(help="claude|codex|opencode|pi|omp")],
    repo: Annotated[str, typer.Option("--repo", help="Repository root (--scope repo).")] = ".",
    scope: Annotated[
        str, typer.Option("--scope", help="repo|user — user wires every session.")
    ] = "repo",
    dry_run: Annotated[bool, typer.Option("--dry-run")] = False,
    state: StateOpt = None,
) -> None:
    """Generate harness wiring. Pointers only — never memory content (§11.3)."""
    p = _paths(state)
    try:
        planned = adapters_mod.plan(p, target, Path(repo), scope=scope)
        written = adapters_mod.write(planned.adapters, dry_run=dry_run)
    except (ValueError, OSError, adapters_mod.AdapterPurityError) as exc:
        err(str(exc))
        raise typer.Exit(EXIT_INVALID) from exc

    # An instruction file is only as portable as the commands it names. User scope
    # installs files that tell *every* session to run `brain`, so a missing binary
    # there is half a wiring that fails silently rather than loudly.
    if scope == "user" and shutil.which("brain") is None:
        err("warning: `brain` is not on PATH. The installed files name it; wire it up with")
        err("  uv tool install --editable .")
    for note in planned.notes:
        err(f"note: {note}")
    emit({"target": target, "scope": scope, "files": written, "dry_run": dry_run})


@app.command()
def ingest(
    source: Annotated[str, typer.Argument(help="File path to preserve as evidence.")],
    state: StateOpt = None,
) -> None:
    """Preserve a file as immutable, content-addressed evidence."""
    p = _paths(state)
    src = Path(source)
    if not src.is_file():
        err(f"{source}: not a file")
        raise typer.Exit(EXIT_INVALID)
    a = artifacts.import_file(p, src)
    emit({"ref": a.ref, "digest": a.digest, "media_type": a.media_type, "size": a.size})


@app.command()
def record(
    text: Annotated[str, typer.Argument(help="What happened.")],
    kind: Annotated[str, typer.Option("--kind")] = "observation",
    session: Annotated[str | None, typer.Option("--session")] = None,
    state: StateOpt = None,
) -> None:
    """Append to the event log. The id is what evidence pointers reference."""
    p = _paths(state)
    try:
        scan.assert_clean(text)
    except scan.SecretFound as exc:
        err(str(exc))
        raise typer.Exit(EXIT_INVALID) from exc
    e = events.append(p, kind, {"text": text}, session=session)
    emit({"ref": f"event:{e.id}", "id": e.id, "occurred_at": e.occurred_at})


@app.command()
def evidence(
    ref: Annotated[str, typer.Argument(help="event:<id> or artifact:<digest>")],
    lines: Annotated[str | None, typer.Option("--lines", help="e.g. 4-8")] = None,
    state: StateOpt = None,
) -> None:
    """Resolve an evidence pointer to the source text it names.

    This is what makes evidence real rather than decorative — a pointer nobody can
    follow is an attribution, not a citation.
    """
    p = _paths(state)
    start = end = None
    if lines:
        a, _, b = lines.partition("-")
        start, end = int(a), int(b or a)
    resolved = artifacts.resolve_evidence(p, ref, start, end)
    emit(resolved)
    if not resolved.get("resolved"):
        raise typer.Exit(EXIT_EMPTY)


@backup_app.command("create")
def backup_create(
    dest: Annotated[str | None, typer.Option("--dest")] = None,
    state: StateOpt = None,
) -> None:
    """Take a verifiable backup: snapshot if possible, validated collection otherwise."""
    p = _paths(state)
    try:
        manifest = backup_mod.backup(p, Path(dest) if dest else None)
    except backup_mod.BackupError as exc:
        err(str(exc))
        raise typer.Exit(EXIT_FAIL_CLOSED) from exc
    emit(
        {
            "generation": manifest.generation,
            "mechanism": manifest.mechanism,
            "files": len(manifest.files),
            "tombstone_seq": manifest.tombstone_seq,
        }
    )


@backup_app.command("list")
def backup_list(state: StateOpt = None) -> None:
    """List backups and their tombstone high-water marks."""
    p = _paths(state)
    items = backup_mod.list_backups(p)
    emit(items)
    if not items:
        raise typer.Exit(EXIT_EMPTY)


@backup_app.command("verify")
def backup_verify(
    manifest: Annotated[str, typer.Argument()],
    state: StateOpt = None,
) -> None:
    """Check a backup against its manifest."""
    p = _paths(state)
    bad = backup_mod.verify_backup(p, Path(manifest))
    emit({"manifest": manifest, "mismatched": bad, "ok": not bad})
    if bad:
        raise typer.Exit(EXIT_FAIL_CLOSED)


@backup_app.command("restore")
def backup_restore(
    manifest: Annotated[str, typer.Argument()],
    replica: Annotated[
        list[str] | None, typer.Option("--replica", help="Ledger replica path.")
    ] = None,
    attest_seq: Annotated[
        int | None,
        typer.Option("--attest-seq", help="Operator attestation of the current tombstone seq."),
    ] = None,
    state: StateOpt = None,
) -> None:
    """Restore, replay deletions, then serve. Refuses to serve if currency is unproven."""
    p = _paths(state)
    try:
        report = backup_mod.restore(
            p,
            Path(manifest),
            extra_replicas=[Path(r) for r in (replica or [])],
            attested_seq=attest_seq,
        )
    except backup_mod.RestoreRefused as exc:
        err(str(exc))
        err(
            "A backup cannot vouch for its own currency. Supply --replica with a "
            "ledger outside the rollback domain, or --attest-seq to assert it yourself."
        )
        raise typer.Exit(EXIT_FAIL_CLOSED) from exc
    except ledger.LedgerError as exc:
        err(str(exc))
        raise typer.Exit(EXIT_FAIL_CLOSED) from exc
    emit(report)


@eval_app.command("bootstrap")
def eval_bootstrap(
    eval_dir: Annotated[str | None, typer.Option("--dir")] = None,
    state: StateOpt = None,
) -> None:
    """Draft golden-set candidates from your corpus. They are DRAFTS — rewrite them."""
    from .eval import bootstrap as bootstrap_mod

    p = _paths(state)
    d = Path(eval_dir) if eval_dir else p.eval_dir
    candidates = bootstrap_mod.draft(p)
    written = bootstrap_mod.write_templates(d, candidates)
    emit({"drafted": len(candidates), **written})
    err(
        "Drafted questions use the memory's own words, so they test lexical matching "
        "rather than recall. Rewrite each one the way you would actually ask it."
    )


@eval_app.command("run")
def eval_run(
    eval_dir: Annotated[str | None, typer.Option("--dir")] = None,
    memory_off: Annotated[bool, typer.Option("--memory-off", help="The control arm.")] = False,
    state: StateOpt = None,
) -> None:
    """Run the golden set. Reports an interval and n, never a bare score."""
    from .eval import runner

    p = _paths(state)
    d = Path(eval_dir) if eval_dir else _paths(state).eval_dir
    record = runner.run(p, d / "golden.yaml", memory_off=memory_off, results_dir=d / "results")
    if record.score["total"] == 0:
        err(f"no cases in {d / 'golden.yaml'} — run `brain eval bootstrap` first")
        raise typer.Exit(EXIT_EMPTY)
    emit(
        {
            "score": f"{record.score['point']:.1%} "
            f"[{record.score['lower']:.1%}-{record.score['upper']:.1%}]",
            "n": record.score["total"],
            "corpus_size": record.corpus_size,
            "memory_off": memory_off,
            "taxonomy": record.taxonomy,
            "failures": [r for r in record.results if not r["passed"]][:10],
        }
    )


@eval_app.command("probe")
def eval_probe(
    eval_dir: Annotated[str | None, typer.Option("--dir")] = None,
    state: StateOpt = None,
) -> None:
    """State-recovery probe: can the store reconstruct known-true facts, cold?"""
    from .eval import runner

    p = _paths(state)
    result = runner.state_recovery(
        p, (Path(eval_dir) if eval_dir else p.eval_dir) / "state-facts.yaml"
    )
    emit(result)
    if result["total"] == 0:
        raise typer.Exit(EXIT_EMPTY)


@eval_app.command("slope")
def eval_slope(
    eval_dir: Annotated[str | None, typer.Option("--dir")] = None,
    state: StateOpt = None,
) -> None:
    """The tenure trigger. Refuses to compute below the item floor."""
    from .eval import runner

    v = runner.verdict((Path(eval_dir) if eval_dir else _paths(state).eval_dir) / "results")
    emit(
        {
            "computable": v.computable,
            "triggered": v.triggered,
            "decline_pp": v.decline_pp,
            "reason": v.reason,
        }
    )
    if v.triggered:
        err("TRIGGER MET — this is a decision prompt, not an instruction. A human decides.")


@ledger_app.command("init")
def ledger_init(
    remote: Annotated[
        str, typer.Option("--remote", help="e.g. git@github.com:you/brain-ledger.git")
    ],
    state: StateOpt = None,
) -> None:
    """Configure the off-device tombstone replica.

    Until this runs there is only ONE replica, so quorum can never be met and every
    deletion stays `pending` forever. That is honest rather than convenient: an
    unreplicated deletion really is incomplete.

    The remote must reject force-push and deletion, or it is not an anchor — a
    history that can be rewritten provides no monotonicity. Protection is verified,
    not assumed, and this command warns loudly when it cannot be confirmed.
    """
    from .store.replicate import GitLedgerReplicator

    p = _paths(state)
    r = GitLedgerReplicator(p)
    try:
        r.init_repo(remote)
    except Exception as exc:
        err(f"could not initialize the ledger repo: {exc}")
        raise typer.Exit(EXIT_FAIL_CLOSED) from exc

    protected = r.ensure_protection()
    emit({"remote": remote, "identity": r.identity, "protection_verified": protected})
    if not protected:
        err(
            "WARNING: could not verify that this remote rejects force-push and branch "
            "deletion. Acks from an unprotected remote do NOT count toward quorum, so "
            "deletions will stay pending. For GitHub, `gh` must be authenticated and "
            "the repo must allow rulesets."
        )
        raise typer.Exit(EXIT_PENDING)


@ledger_app.command("status")
def ledger_status(state: StateOpt = None) -> None:
    """Where the ledger stands, and whether quorum is reachable at all."""
    from .store.replicate import GitLedgerReplicator

    p = _paths(state)
    seq, head = ledger.head(p.tombstones)
    configured = p.ledger_git.exists()
    r = GitLedgerReplicator(p) if configured else None
    emit(
        {
            "local_seq": seq,
            "local_head": head,
            "remote_configured": configured,
            "remote": r.remote if r else None,
            "identity": r.identity if r else None,
            "protection_verified": r.verify_protection() if r and r.remote else False,
            "pending_deletions": deletion.pending_deletions(p),
        }
    )
    if not configured:
        err(
            "No off-device replica configured. Quorum is unreachable, so every deletion "
            "will report `pending`. Run `brain ledger init --remote <url>`."
        )
        raise typer.Exit(EXIT_PENDING)
    if not (r and r.remote and r.verify_protection()):
        # A remote whose history can be rewritten is not an anchor, so its acks are
        # rejected at quorum time. Reporting success here would mean the operator
        # believes deletions are replicated while every one of them still pends.
        err(
            "Replica configured, but its protection against force-push and branch "
            "deletion could NOT be verified. Acks from an unprotected remote do not "
            "count toward quorum, so deletions will still report `pending`."
        )
        raise typer.Exit(EXIT_PENDING)


@app.command()
def expire(
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Report without changing anything.")
    ] = False,
    upcoming_days: Annotated[
        int, typer.Option("--upcoming", help="Also list what lapses soon.")
    ] = 14,
    state: StateOpt = None,
) -> None:
    """Lapse what has gone stale, and tombstone what has been expired past its grace.

    Unconfirmed memories decay by design — a store where nothing ever lapses fills
    with claims nobody confirmed and nobody cited. Expiry is not deletion: an expired
    memory stops being served but stays recoverable for 90 days.
    """
    from .store import lifecycle

    p = _paths(state)
    _require_intact_ledgers(p)
    soon = lifecycle.upcoming(p, upcoming_days)
    if dry_run:
        from .frontmatter import parse as _parse

        would = []
        for f in sorted(p.memories.rglob("*.md")):
            if ".revisions" in f.parts or ".staging" in f.parts:
                continue
            try:
                m = _parse(f.read_text(), f)
            except Exception:
                continue
            if reason := lifecycle.is_lapsed(m, model_today()):
                would.append({"id": m.id, "reason": reason})
        emit({"dry_run": True, "would_expire": would, "upcoming": soon})
        return

    report = lifecycle.sweep(p)
    conn = build.connect(p)
    build.rebuild(p, conn)
    conn.close()
    emit({**report.as_dict(), "upcoming": soon})


@app.command()
def unexpire(
    memory_id: Annotated[str, typer.Argument()],
    state: StateOpt = None,
) -> None:
    """Undo a lapse, while the memory is still inside its grace period."""
    from .store import lifecycle

    p = _paths(state)
    if not lifecycle.unexpire(p, memory_id):
        err(f"{memory_id}: not expired, or past its grace period and already purged")
        raise typer.Exit(EXIT_EMPTY)
    conn = build.connect(p)
    build.rebuild(p, conn)
    conn.close()
    emit({"id": memory_id, "status": "confirmed"})


@app.command()
def context(
    text: Annotated[str, typer.Argument(help="The prompt or task description.")],
    workspace: Annotated[str | None, typer.Option("--workspace")] = None,
    all_workspaces: Annotated[bool, typer.Option("--scope-all")] = False,
    limit: Annotated[int, typer.Option("--limit")] = 5,
    hook: Annotated[
        bool, typer.Option("--hook", help="Plain text for a hook; silent when empty.")
    ] = False,
    state: StateOpt = None,
) -> None:
    """Pointers to memories related to some text. Never their content.

    Built for a UserPromptSubmit hook, so it is fast, quiet, and cannot break a
    session: any failure reports "nothing relevant" rather than raising.

    It returns ids and titles, never bodies. Reading a memory still requires a
    visible `brain.search` / `brain.get` call — the hook guarantees you always look,
    the tool call is still how you read (§9.1, §11.6).
    """
    from . import workflow

    p = _paths(state)
    result = workflow.context(p, text, workspace=None if all_workspaces else workspace, limit=limit)
    if hook:
        if rendered := workflow.render_for_hook(result):
            print(rendered)
        return
    emit(result)
    if not result["relevant"]:
        raise typer.Exit(EXIT_EMPTY)


@app.command("capture-check")
def capture_check(
    hook: Annotated[
        bool, typer.Option("--hook", help="Plain text for a hook; silent when quiet.")
    ] = False,
    window: Annotated[int, typer.Option("--window", help="Minutes to look back.")] = 240,
    state: StateOpt = None,
) -> None:
    """Did this stretch of work produce something worth remembering, and was it captured?

    Deliberately reluctant to nag. A prompt on every stop trains you to dismiss it,
    at which point the reminder is worse than nothing.
    """
    from . import workflow

    p = _paths(state)
    check = workflow.capture_check(p, window_minutes=window)
    if hook:
        if check.should_prompt:
            print(
                "brain: this session changed files and wrote nothing to the brain.\n"
                "  If something was learned that the next session would want — a "
                "decision, a dead end, a constraint — capture it now with "
                "brain.write or `brain remember`.\n"
                "  If nothing was, say so and move on. Not every session teaches "
                "something."
            )
        return
    emit(check.as_dict())


@app.command()
def doctor(state: StateOpt = None) -> None:
    """One command that answers: is this store healthy?

    The failure modes this design worries about are quiet ones — a broken ledger, an
    unreachable replica, unpurged residue. Several mean a safety property has stopped
    holding while every ordinary command still works.

    Exits 4 on any FAIL, 3 on any WARN, 0 when clean.
    """
    from . import doctor as doctor_mod

    p = _paths(state)
    result = doctor_mod.report(p)
    emit(result)
    if result["status"] == "fail":
        raise typer.Exit(EXIT_FAIL_CLOSED)
    if result["status"] == "warn":
        raise typer.Exit(EXIT_PENDING)


@app.command()
def install_timers(
    dry_run: Annotated[bool, typer.Option("--dry-run")] = False,
    state: StateOpt = None,
) -> None:
    """Write the scheduler units so the sweeps actually run.

    `expire`, `sync`, and `backup` are correct and useless unscheduled. Nothing here
    touches the system: systemd --user units on Linux, LaunchAgents on macOS, no root
    either way, and `--dry-run` prints instead of writing.
    """
    from .ops import activation_commands, install_user_timers

    p = _paths(state)
    written = install_user_timers(p, dry_run=dry_run)
    commands = activation_commands()
    emit({"units": written, "activate": commands, "dry_run": dry_run})
    if not dry_run:
        # Writing a unit file schedules nothing. Saying so here, from the same list
        # the installer prints, is what keeps the two from drifting apart.
        err("Written, not yet scheduled. Now run:")
        for command in commands:
            err(f"  {command}")


@app.command()
def recover(state: StateOpt = None) -> None:
    """Complete or roll forward every in-flight operation. Safe to run any time."""
    p = _paths(state)
    emit({"recovered": mem.recover_pending_ops(p)})


if __name__ == "__main__":
    app()

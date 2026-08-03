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
import sys
from datetime import date
from pathlib import Path
from typing import Annotated

import typer

from . import config, scan
from .frontmatter import InvalidFrontmatter, parse, serialize
from .ids import new_ulid
from .index import build
from .model import Evidence, Memory, MemoryType, ProvenanceClass, Volatility
from .search.fts5 import Fts5Backend
from .search.ripgrep import RipgrepBackend
from .store import deletion, ledger, reconcile, revisions
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
app.add_typer(quarantine_app, name="quarantine")


def err(msg: str) -> None:
    print(msg, file=sys.stderr)


def emit(payload: object) -> None:
    print(json.dumps({"version": 1, "data": payload}, indent=2, default=str))


def _paths(state: str | None) -> config.Paths:
    return config.paths(state)


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
    workspace: Annotated[str, typer.Option("--workspace")] = "default",
    tags: Annotated[list[str] | None, typer.Option("--tag")] = None,
    state: StateOpt = None,
) -> None:
    """Write a memory. Explicit writes land confirmed."""
    p = _paths(state)
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
            valid_from=date.today(),
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
    workspace: Annotated[str, typer.Option("--workspace")] = "default",
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
    ws = None if all_workspaces else workspace
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
    payload: dict[str, object] = {
        "id": memory_id,
        "workspace": row["workspace"],
        "disposition": row["disposition"],
        "content": path.read_text() if path.exists() else None,
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
    emit({"quarantined": bad, "contested": contested})
    if bad or contested:
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
    memory_id: Annotated[str, typer.Argument()],
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
    result = deletion.forget(
        p, memory_id, workspace=workspace, mtype=type_, reason=reason, replicate=replicator
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
    emit({**report, "still_pending": pending})
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
def recover(state: StateOpt = None) -> None:
    """Complete or roll forward every in-flight operation. Safe to run any time."""
    p = _paths(state)
    emit({"recovered": mem.recover_pending_ops(p)})


if __name__ == "__main__":
    app()

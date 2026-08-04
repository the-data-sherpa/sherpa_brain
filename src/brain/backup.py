"""Backup and restore (BLUEPRINT.md §11.5.2, §11.5.3).

Two things here are the whole point, and both were wrong in earlier drafts.

**Quiescing the writer is not enough.** A human editing in vim is *by design* outside
the writer's locks, so their save during manifest enumeration produces a manifest
that is internally consistent with the bytes it copied while representing no coherent
point of the tree — the worst failure available, because it validates. Hence a ladder:
filesystem snapshot when the platform offers one, otherwise validated double
collection that re-reads everything and fails closed on any change.

**A backup cannot vouch for its own currency.** Its high-water mark is a *lower
bound* on what was deleted, never proof. A hash chain proves continuity, not recency.
The concrete failure it hides: backup at tombstone seq 10 → a deletion writes seq 11
→ the replica holding 11 is lost → a chain intact through 10 satisfies "at least as
current as the mark" → **the deleted content resurrects and every check passes.**

So currency comes from an anchor outside the rollback domain, and when it cannot be
established, restore **refuses to serve**. Residual risk is stated in ADR 0005 rather
than engineered around: if every replica and the counter are lost at once, currency is
unknowable and a human must attest.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .atomic import fsync_dir, write_atomic
from .config import Paths
from .frontmatter import content_hash
from .model import iso, utcnow
from .store import deletion, ledger

MANIFEST = "manifest.json"
MAX_COLLECTION_ATTEMPTS = 3


class BackupError(RuntimeError):
    pass


class RestoreRefused(RuntimeError):
    """Integrity or currency could not be established. Nothing was served."""


@dataclass
class Manifest:
    generation: str
    created_at: str
    mechanism: str
    files: dict[str, str] = field(default_factory=dict)  # relative path -> content hash
    tombstone_seq: int = 0
    tombstone_head: str = ledger.GENESIS

    def to_json(self) -> bytes:
        return json.dumps(self.__dict__, indent=2, sort_keys=True).encode() + b"\n"

    @classmethod
    def load(cls, path: Path) -> Manifest:
        return cls(**json.loads(path.read_text()))


def _canonical_files(paths: Paths) -> list[Path]:
    """Every file that is canonical state. The index is excluded — it is derived."""
    out: list[Path] = []
    for root in (paths.memories, paths.events, paths.artifacts):
        if root.is_dir():
            out.extend(p for p in root.rglob("*") if p.is_file() and ".staging" not in p.parts)
    for f in (paths.tombstones, paths.acks, paths.purges):
        if f.exists():
            out.append(f)
    return sorted(out)


def _snapshot_capable(paths: Paths) -> bool:
    if not shutil.which("btrfs"):
        return False
    proc = subprocess.run(
        ["btrfs", "subvolume", "show", str(paths.root)],
        capture_output=True,
        timeout=15,
        check=False,
    )
    return proc.returncode == 0


def _try_snapshot(paths: Paths, dest: Path) -> bool:
    """Rung 1: a real point-in-time tree. Needs privilege, so it may simply decline."""
    if not _snapshot_capable(paths):
        return False
    proc = subprocess.run(
        ["sudo", "-n", "btrfs", "subvolume", "snapshot", "-r", str(paths.root), str(dest)],
        capture_output=True,
        timeout=60,
        check=False,
    )
    return proc.returncode == 0


def _collect_validated(paths: Paths) -> dict[str, str]:
    """Rung 3: build a manifest, re-read everything, retry on change, then fail closed.

    This does not achieve what a snapshot achieves. It detects that the tree moved
    under it, which is enough to refuse rather than to record an incoherent backup.
    """
    for _ in range(MAX_COLLECTION_ATTEMPTS):
        first = {
            str(p.relative_to(paths.root)): content_hash(p.read_bytes())
            for p in _canonical_files(paths)
        }
        second = {
            str(p.relative_to(paths.root)): content_hash(p.read_bytes())
            for p in _canonical_files(paths)
        }
        if first == second:
            return first
    raise BackupError(
        "the store kept changing during collection; refusing to record a backup that "
        "represents no coherent point of the tree. Retry when writes have settled."
    )


def backup(paths: Paths, dest_root: Path | None = None) -> Manifest:
    """Take a verifiable backup. Prefers a snapshot; falls back to double collection."""
    dest_root = dest_root or paths.backups
    generation = iso(utcnow()).replace(":", "").replace("-", "")
    dest = dest_root / generation
    dest.parent.mkdir(parents=True, exist_ok=True)

    seq, head = ledger.head(paths.tombstones)

    if _try_snapshot(paths, dest):
        mechanism = "btrfs-snapshot"
        files = {
            str(p.relative_to(paths.root)): content_hash(p.read_bytes())
            for p in _canonical_files(paths)
        }
    else:
        mechanism = "validated-double-collection"
        files = _collect_validated(paths)
        dest.mkdir(parents=True, exist_ok=True)
        for rel in files:
            src = paths.root / rel
            out = dest / rel
            out.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, out)
        fsync_dir(dest)

    manifest = Manifest(
        generation=generation,
        created_at=iso(utcnow()),
        mechanism=mechanism,
        files=files,
        tombstone_seq=seq,
        tombstone_head=head,
    )
    write_atomic(dest_root / f"{generation}.{MANIFEST}", manifest.to_json())
    fsync_dir(dest_root)
    return manifest


def verify_backup(paths: Paths, manifest_path: Path) -> list[str]:
    """Check a backup against its manifest. Returns mismatched paths."""
    manifest = Manifest.load(manifest_path)
    root = manifest_path.parent / manifest.generation
    bad = []
    for rel, expected in manifest.files.items():
        f = root / rel
        if not f.exists() or content_hash(f.read_bytes()) != expected:
            bad.append(rel)
    return bad


@dataclass(frozen=True)
class CurrencyProof:
    established: bool
    source: str
    seq: int
    reason: str = ""


def prove_currency(
    paths: Paths,
    manifest: Manifest,
    *,
    extra_replicas: list[Path] | None = None,
    attested_seq: int | None = None,
) -> CurrencyProof:
    """Establish that we hold a tombstone ledger at least as current as reality.

    The manifest's own mark is deliberately **not** accepted as proof — that is the
    circular check this function exists to replace. Currency must come from outside
    the rollback domain: another replica, or a human saying so.
    """
    if attested_seq is not None:
        # Attestation says "you are not behind". It does NOT hand over the entries.
        # If the ledger we actually hold falls short of the attested sequence, we
        # cannot replay the deletions it names — and restoring anyway would put the
        # deleted content back while every check reported success. That is the exact
        # shape of failure this module exists to prevent, so it is a refusal.
        local_seq, _ = ledger.head(paths.tombstones)
        if local_seq < attested_seq:
            return CurrencyProof(
                False,
                "operator-attestation",
                local_seq,
                f"you attested the current tombstone head is seq {attested_seq}, but the "
                f"ledger on disk only reaches seq {local_seq}. Attesting a sequence "
                f"number is not the same as possessing the entries: {attested_seq - local_seq} "
                f"deletion(s) cannot be replayed, and restoring would resurrect them. "
                f"Supply the ledger itself via --replica.",
            )
        return CurrencyProof(True, "operator-attestation", attested_seq)

    candidates: list[tuple[str, int, str]] = []
    local_seq, local_head = ledger.head(paths.tombstones)
    if local_seq:
        candidates.append(("local-ledger", local_seq, local_head))
    for replica in extra_replicas or []:
        if replica.exists():
            seq, head = ledger.head(replica)
            candidates.append((str(replica), seq, head))

    if not candidates:
        return CurrencyProof(
            False,
            "none",
            0,
            "no tombstone ledger is reachable outside the backup. Currency cannot be "
            "established from a backup's own high-water mark — that is circular. "
            "Supply a replica or attest the current head explicitly.",
        )

    best = max(candidates, key=lambda c: c[1])
    at_best = [c for c in candidates if c[1] == best[1]]
    if len({c[2] for c in at_best}) > 1:
        return CurrencyProof(
            False,
            "equivocation",
            best[1],
            f"two replicas report seq {best[1]} with different chain heads. One of them "
            f"has been rewritten; refusing to guess which.",
        )
    if best[1] < manifest.tombstone_seq:
        return CurrencyProof(
            False,
            best[0],
            best[1],
            f"the most current reachable ledger is at seq {best[1]}, behind the "
            f"backup's own mark of {manifest.tombstone_seq}. Deletions recorded after "
            f"the backup may be unknown to us.",
        )
    return CurrencyProof(True, best[0], best[1])


def restore(
    paths: Paths,
    manifest_path: Path,
    *,
    extra_replicas: list[Path] | None = None,
    attested_seq: int | None = None,
) -> dict[str, Any]:
    """Restore, then replay deletions, then serve. Fails closed at every step.

    Ordering is the safety argument: verify the manifest, prove currency from
    *outside*, union the ledgers, purge, and only then is the store servable. A
    restore that serves before purging has resurrected the content, however briefly.
    """
    manifest = Manifest.load(manifest_path)
    root = manifest_path.parent / manifest.generation

    if bad := verify_backup(paths, manifest_path):
        raise RestoreRefused(
            f"backup fails its own manifest at {len(bad)} path(s): {bad[:5]}. "
            f"An unverifiable backup is not a backup."
        )

    # Currency is proven BEFORE a single byte is copied back. An earlier version
    # restored first and refused afterwards, which is not a refusal at all: the
    # deleted content was already on disk, and a later `reindex` would happily serve
    # it. Refusing to serve is not the same as not having resurrected.
    proof = prove_currency(
        paths, manifest, extra_replicas=extra_replicas, attested_seq=attested_seq
    )
    if not proof.established:
        raise RestoreRefused(f"REFUSING TO RESTORE — nothing was written: {proof.reason}")

    # Keep our ledgers aside — they are what proved currency, and they must not be
    # overwritten by the (older) copies inside the backup.
    live_ledgers = {
        name: p.read_bytes()
        for name, p in (
            ("tombstones.jsonl", paths.tombstones),
            ("acks.jsonl", paths.acks),
            ("purges.jsonl", paths.purges),
        )
        if p.exists()
    }

    for rel in manifest.files:
        src, out = root / rel, paths.root / rel
        out.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, out)

    # Union, never replace: a tombstone can only ever be added.
    for name, data in live_ledgers.items():
        target = paths.root / name
        restored = target.read_bytes() if target.exists() else b""
        if len(data) >= len(restored):
            write_atomic(target, data)

    deletion.verify_all_ledgers(paths)
    report = deletion.resume(paths)

    if paths.db.exists():
        paths.db.unlink()
    from .index import build

    build.rebuild(paths)

    return {
        "generation": manifest.generation,
        "mechanism": manifest.mechanism,
        "files_restored": len(manifest.files),
        "currency": {"source": proof.source, "seq": proof.seq},
        "repurged": report["repurged"],
        "residue": report["residue"],
    }


def list_backups(paths: Paths) -> list[dict[str, Any]]:
    if not paths.backups.is_dir():
        return []
    out: list[dict[str, Any]] = []
    for m in sorted(paths.backups.glob(f"*.{MANIFEST}")):
        manifest = Manifest.load(m)
        out.append(
            {
                "generation": manifest.generation,
                "created_at": manifest.created_at,
                "mechanism": manifest.mechanism,
                "files": len(manifest.files),
                "tombstone_seq": manifest.tombstone_seq,
                "manifest": str(m),
            }
        )
    return out

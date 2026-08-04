"""Append-only hash-chained ledgers (BLUEPRINT.md §11.5.1).

Three of them, and **all status is derived, never mutated**:

    tombstones.jsonl   the DELETION happened
    acks.jsonl         replication was acknowledged
    purges.jsonl       physical bytes were observed absent

An earlier design put a mutable ``quorum_state`` field on a hash-chained tombstone.
Flipping ``pending -> confirmed`` would have rewritten a link in a chain whose entire
purpose is tamper evidence — a mutable field inside an immutable structure. Delivery
and purge state are therefore *projections* over these ledgers, computed on demand.

**Broken chain means refuse to serve.** The tombstone ledger is the anti-resurrection
authority; an authority that cannot prove its own integrity is not one. Chain
verification runs on all three, not just tombstones: an unverified ``acks.jsonl``
would let a forged ack fabricate quorum, defeating the whole mechanism.

**Stated limitation.** An unkeyed hash chain detects *corruption*; it does not
authenticate *authorship*. Anyone who can write the file can rewrite the chain
consistently. Authorship rests on OS identity, filesystem permissions, and an
authenticated ``gh`` — not on the chain. Signing would change that and is
deliberately outside Phase 1.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..atomic import fsync_dir
from ..model import iso, utcnow

GENESIS = "0" * 64


class LedgerError(RuntimeError):
    """The ledger cannot be trusted. Callers must refuse to serve."""


class BrokenChain(LedgerError):
    def __init__(self, path: Path, seq: int, expected: str, found: str) -> None:
        super().__init__(
            f"{path}: hash chain broken at seq {seq} — entry records prev_hash {found} "
            f"but the preceding entry hashes to {expected}. Refusing to serve: this "
            f"ledger is the anti-resurrection authority and its integrity is unproven."
        )
        self.path, self.seq = path, seq


@dataclass(frozen=True)
class Entry:
    seq: int
    prev_hash: str
    hash: str
    payload: dict[str, Any]

    @property
    def subject_id(self) -> str:
        return str(self.payload.get("subject_id", ""))


def entry_hash(seq: int, prev_hash: str, payload: dict[str, Any]) -> str:
    """Hash over (seq, prev_hash, payload). Canonical JSON so it is reproducible."""
    material = json.dumps(
        {"seq": seq, "prev_hash": prev_hash, "payload": payload},
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(material.encode()).hexdigest()


def read_chain(path: Path, *, verify: bool = True) -> list[Entry]:
    """Read and verify a ledger.

    A truncated *trailing* line is discarded with a warning — that is a crash during
    append, and the entry it represents was never acknowledged. Anything else raises.
    """
    if not path.exists():
        return []
    lines = path.read_text().splitlines()
    entries: list[Entry] = []
    prev = GENESIS
    for i, line in enumerate(lines):
        if not line.strip():
            continue
        try:
            raw = json.loads(line)
        except json.JSONDecodeError:
            if i == len(lines) - 1:
                break  # torn trailing write; the append never completed
            raise LedgerError(f"{path}: malformed entry at line {i + 1}") from None
        try:
            e = Entry(
                seq=int(raw["seq"]),
                prev_hash=str(raw["prev_hash"]),
                hash=str(raw["hash"]),
                payload=raw["payload"],
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise LedgerError(f"{path}: entry at line {i + 1} is not a ledger entry") from exc

        if verify:
            if e.prev_hash != prev:
                raise BrokenChain(path, e.seq, prev, e.prev_hash)
            if entry_hash(e.seq, e.prev_hash, e.payload) != e.hash:
                raise LedgerError(
                    f"{path}: entry {e.seq} has been altered — its recorded hash does "
                    f"not match its contents. Refusing to serve."
                )
        entries.append(e)
        prev = e.hash
    return entries


def head(path: Path) -> tuple[int, str]:
    """Return ``(seq, chain_head)``. ``(0, GENESIS)`` for an empty ledger."""
    entries = read_chain(path)
    return (entries[-1].seq, entries[-1].hash) if entries else (0, GENESIS)


def append(path: Path, payload: dict[str, Any]) -> Entry:
    """Append one entry and fsync both the file and its directory.

    The directory fsync is what makes the append durable rather than merely written.
    """
    seq, prev = head(path)
    e = Entry(seq + 1, prev, entry_hash(seq + 1, prev, payload), payload)
    line = json.dumps(
        {"seq": e.seq, "prev_hash": e.prev_hash, "hash": e.hash, "payload": e.payload},
        sort_keys=True,
        separators=(",", ":"),
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.write(fd, (line + "\n").encode())
        os.fsync(fd)
    finally:
        os.close(fd)
    fsync_dir(path.parent)
    return e


def tombstone_payload(
    subject_id: str, kind: str = "memory", reason: str | None = None
) -> dict[str, Any]:
    """A content-free tombstone.

    No title, no body, no excerpt. ``reason`` is omitted by default rather than
    defaulted to a string, because a free-text field on an off-device replica is a
    place content leaks to.
    """
    payload: dict[str, Any] = {
        "subject_id": subject_id,
        "subject_kind": kind,
        "tombstoned_at": iso(utcnow()),
    }
    if reason:
        payload["reason"] = reason
    return payload


def ack_payload(
    subject_id: str,
    seq: int,
    chain_head: str,
    *,
    replica_identity: str,
    remote_sha: str,
    ref: str,
    protection_verified: bool,
) -> dict[str, Any]:
    """Record exactly what was *verified*, not that a command exited zero.

    ``replica_identity`` is derived by the caller from the configured authenticated
    endpoint — never read from an ack. A self-asserted identity is satisfiable by a
    duplicate endpoint or a fabricated value, which would let one replica count twice.
    """
    return {
        "subject_id": subject_id,
        "seq": seq,
        "chain_head": chain_head,
        "replica_identity": replica_identity,
        "remote_sha": remote_sha,
        "ref": ref,
        "protection_verified": protection_verified,
        "verified_at": iso(utcnow()),
    }


def purge_payload(subject_id: str, removed: list[str]) -> dict[str, Any]:
    """A point-in-time observation that bytes were absent — never an enduring fact.

    A file can be recreated after this is written, by a restore or an editor the
    process never saw. Every scan revalidates absence rather than trusting this.
    """
    return {
        "subject_id": subject_id,
        "observed_at": iso(utcnow()),
        "removed": sorted(removed),
        "note": "point-in-time observation; revalidate on every scan",
    }

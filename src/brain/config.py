"""Paths and startup preconditions.

Canonical state lives entirely outside the repository, under ``$XDG_STATE_HOME/brain``.
That is deliberate rather than a ``.gitignore`` entry: an accidental ``git add -A``
cannot capture what is not in the tree (BLUEPRINT.md §6.8).

Startup refuses to run where the write protocol's assumptions do not hold. Two
mechanisms, and neither alone is sufficient — see ``check_preconditions``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from .atomic import Capabilities, probe_capabilities

# Filesystems where atomic rename and durable fsync do not hold, or hold only
# sometimes. A capability probe cannot detect these reliably: a sync client's
# directory looks like any other directory to a syscall.
UNSAFE_FSTYPES = frozenset(
    {"nfs", "nfs4", "cifs", "smbfs", "smb3", "fuse.sshfs", "fuse.s3fs", "fuse.rclone", "9p", "afs"}
)

# Directory names managed by file-sync clients. These rewrite files behind the
# process, which breaks both the CAS protocol and the immutability of revisions.
SYNC_DIR_MARKERS = (
    "Dropbox",
    "OneDrive",
    "iCloud",
    "Library/Mobile Documents",
    "Google Drive",
    "Nextcloud",
    "ownCloud",
    "Syncthing",
    "pCloud",
    "Sync",
)


class PreconditionError(RuntimeError):
    """The store cannot be operated safely here."""


@dataclass(frozen=True)
class Paths:
    root: Path

    @property
    def memories(self) -> Path:
        return self.root / "memories"

    @property
    def revisions(self) -> Path:
        return self.root / "memories" / ".revisions"

    @property
    def staging(self) -> Path:
        return self.root / "memories" / ".staging"

    @property
    def ops(self) -> Path:
        return self.root / "ops"

    @property
    def conflicts(self) -> Path:
        return self.root / "conflicts"

    @property
    def resolved_conflicts(self) -> Path:
        return self.root / "conflicts" / ".resolved"

    @property
    def quarantine(self) -> Path:
        return self.root / "quarantine"

    @property
    def events(self) -> Path:
        return self.root / "events"

    @property
    def artifacts(self) -> Path:
        return self.root / "artifacts"

    @property
    def logs(self) -> Path:
        return self.root / "logs"

    @property
    def backups(self) -> Path:
        return self.root / "backups"

    @property
    def db(self) -> Path:
        return self.root / "brain.sqlite3"

    @property
    def tombstones(self) -> Path:
        return self.root / "tombstones.jsonl"

    @property
    def acks(self) -> Path:
        return self.root / "acks.jsonl"

    @property
    def purges(self) -> Path:
        return self.root / "purges.jsonl"

    @property
    def ledger_git(self) -> Path:
        return self.root / "ledger.git"

    @property
    def store_lock(self) -> Path:
        return self.root / ".store.lock"

    def memory_dir(self, workspace: str, mtype: str) -> Path:
        return self.memories / workspace / mtype

    def revision_dir(self, memory_id: str) -> Path:
        return self.revisions / memory_id

    def revision_path(self, memory_id: str, n: int) -> Path:
        return self.revision_dir(memory_id) / f"{n:06d}.md"

    def staging_path(self, opid: str) -> Path:
        return self.staging / opid

    def op_path(self, opid: str) -> Path:
        return self.ops / f"{opid}.json"

    def conflict_path(self, memory_id: str) -> Path:
        return self.conflicts / f"{memory_id}.json"

    def memory_lock(self, memory_id: str) -> Path:
        return self.root / "locks" / f"{memory_id}.lock"

    def all_dirs(self) -> list[Path]:
        return [
            self.memories,
            self.revisions,
            self.staging,
            self.ops,
            self.conflicts,
            self.resolved_conflicts,
            self.quarantine,
            self.events,
            self.artifacts,
            self.logs,
            self.backups,
            self.root / "locks",
        ]


def state_root(override: str | os.PathLike[str] | None = None) -> Path:
    if override is not None:
        return Path(override).expanduser().resolve()
    if env := os.environ.get("BRAIN_STATE_DIR"):
        return Path(env).expanduser().resolve()
    xdg = os.environ.get("XDG_STATE_HOME") or "~/.local/state"
    return (Path(xdg).expanduser() / "brain").resolve()


def paths(override: str | os.PathLike[str] | None = None) -> Paths:
    return Paths(state_root(override))


def fstype_of(path: Path) -> str | None:
    """Best-effort filesystem type for the mount containing ``path``."""
    try:
        entries = Path("/proc/mounts").read_text().splitlines()
    except OSError:
        return None
    target = path.resolve()
    best: tuple[int, str] | None = None
    for line in entries:
        parts = line.split()
        if len(parts) < 3:
            continue
        mount, fstype = Path(parts[1]), parts[2]
        try:
            if target == mount or mount in target.parents:
                depth = len(mount.parts)
                if best is None or depth > best[0]:
                    best = (depth, fstype)
        except (OSError, ValueError):
            continue
    return best[1] if best else None


def denylist_reason(path: Path) -> str | None:
    """Why this path is known-unsafe, or None if nothing is known against it."""
    fstype = fstype_of(path)
    if fstype and fstype.lower() in UNSAFE_FSTYPES:
        return (
            f"{path} is on a {fstype} filesystem. Atomic rename and durable fsync do not "
            "hold reliably on network filesystems."
        )
    text = str(path)
    for marker in SYNC_DIR_MARKERS:
        if f"/{marker}/" in f"{text}/" or text.endswith(f"/{marker}"):
            return (
                f"{path} looks like it is inside a {marker} sync folder. Sync clients "
                "rewrite files behind the process, which breaks compare-and-swap and "
                "the immutability of revisions."
            )
    return None


def check_preconditions(root: Path) -> Capabilities:
    """Refuse to operate where the write protocol's assumptions do not hold.

    Two mechanisms, both required, neither sufficient alone:

    - The **denylist** catches filesystems and sync folders whose behaviour a probe
      cannot exercise. A Dropbox directory passes every syscall test and still
      rewrites files behind you.
    - The **probe** catches everything the denylist has not heard of, by actually
      running the primitives.

    Neither establishes crash durability. That is assumed, not proven, and stated
    in ADR 0005 as accepted residual risk rather than engineered around.
    """
    if reason := denylist_reason(root):
        raise PreconditionError(reason)
    caps = probe_capabilities(root)
    if not caps.ok:
        raise PreconditionError(
            f"{root} does not support: {', '.join(caps.missing())}. "
            "The write protocol requires all of them."
        )
    return caps

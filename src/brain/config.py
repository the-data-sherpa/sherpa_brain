"""Paths and startup preconditions.

Canonical state lives entirely outside the repository, under ``$XDG_STATE_HOME/brain``.
That is deliberate rather than a ``.gitignore`` entry: an accidental ``git add -A``
cannot capture what is not in the tree (BLUEPRINT.md §6.8).

Startup refuses to run where the write protocol's assumptions do not hold. Two
mechanisms, and neither alone is sufficient — see ``check_preconditions``.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from .atomic import Capabilities, probe_capabilities

# Filesystems where atomic rename and durable fsync do not hold, or hold only
# sometimes. A capability probe cannot detect these reliably: a sync client's
# directory looks like any other directory to a syscall.
UNSAFE_FSTYPES = frozenset(
    {
        # Linux spellings
        "nfs",
        "nfs4",
        "cifs",
        "smbfs",
        "smb3",
        "fuse.sshfs",
        "fuse.s3fs",
        "fuse.rclone",
        "9p",
        "afs",
        # macOS spellings for the same hazards. `mount` reports these differently
        # from /proc/mounts, and a set that only knows the Linux names would let a
        # mounted share through on a Mac while looking like it had checked.
        "afpfs",
        "webdav",
        "osxfuse",
        "macfuse",
        "lifs",
    }
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


#: Kernels for which ``atomic.py`` implements the exchange primitive. Anything else
#: is refused at ``init`` rather than allowed to discover the gap mid-write.
SUPPORTED_PLATFORMS = frozenset({"linux", "darwin"})


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
    def eval_dir(self) -> Path:
        """The golden set lives with the store, not in whatever directory you ran from.

        It is derived from memory bodies — questions built from real content, expected
        ids, workspace names, and a state-facts file describing what the operator
        knows. ADR 0005 rule 2 keeps anything retractable out of git, and a cwd-relative
        default put exactly that inside whichever checkout you happened to be in.
        """
        return self.root / "eval"

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


def default_workspace(cwd: Path | None = None) -> str:
    """The workspace to use when the caller did not name one.

    One store, many projects — so without this, memories from every repository
    land in ``default`` together and searches return whatever matched from
    anywhere. That is context collapse (§11.6): the failure where your work
    project bleeds into your side project because nothing ever separated them.

    Derived from the git repository, because that is the boundary that already
    means "a different thing I am working on". ``BRAIN_WORKSPACE`` overrides for
    the cases where it is not.
    """
    if env := os.environ.get("BRAIN_WORKSPACE"):
        return env.strip() or "default"
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
            cwd=cwd or Path.cwd(),
        )
        if proc.returncode == 0 and (root := proc.stdout.strip()):
            return Path(root).name or "default"
    except (OSError, subprocess.SubprocessError):
        pass
    return "default"


def running_on_darwin() -> bool:
    """Platform dispatch behind a function call, deliberately.

    ``if sys.platform == "darwin":`` is narrowed *statically* by mypy, using the
    platform mypy itself is running on. With ``warn_unreachable`` that makes every
    branch for the *other* platform dead code, so the type check fails on macOS, on
    Linux, or on both, depending on which branches exist — and it passes locally
    right up until CI runs the matrix.

    A function call returns an ordinary ``bool`` that mypy cannot narrow, so both
    branches stay type-checked on both platforms. It also keeps the dispatch
    dynamic, which is what lets a test on one platform exercise the other's path.
    """
    return sys.platform == "darwin"


def _deepest_mount(target: Path, mounts: list[tuple[Path, str]]) -> str | None:
    """The fstype of the longest mount point that is a prefix of ``target``.

    Longest wins because mount points nest: ``/`` matches everything, so a bind or
    network mount deeper down would be masked by taking the first hit.
    """
    best: tuple[int, str] | None = None
    for mount, fstype in mounts:
        try:
            if target == mount or mount in target.parents:
                depth = len(mount.parts)
                if best is None or depth > best[0]:
                    best = (depth, fstype)
        except (OSError, ValueError):
            continue
    return best[1] if best else None


def _linux_mounts() -> list[tuple[Path, str]]:
    try:
        entries = Path("/proc/mounts").read_text().splitlines()
    except OSError:
        return []
    out = []
    for line in entries:
        parts = line.split()
        if len(parts) >= 3:
            out.append((Path(parts[1]), parts[2]))
    return out


#: ``mount`` on Darwin prints ``<device> on <point> (<fstype>, <opts…>)``. The mount
#: point may contain spaces, so the split is anchored on the parenthesised tail
#: rather than on whitespace.
_DARWIN_MOUNT_LINE = re.compile(r"^(?P<dev>.+?) on (?P<point>.+?) \((?P<opts>[^)]*)\)\s*$")


def _darwin_mounts() -> list[tuple[Path, str]]:
    """Parse ``mount(8)``. macOS has no /proc, and no stdlib call returns an fstype.

    Without this the denylist degrades to the sync-folder markers alone on macOS —
    it would return None for every path and quietly approve an NFS or SMB share,
    which is the one thing the denylist exists to catch that a probe cannot.
    """
    try:
        proc = subprocess.run(
            ["/sbin/mount"], capture_output=True, text=True, timeout=5, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if proc.returncode != 0:
        return []
    out = []
    for line in proc.stdout.splitlines():
        if match := _DARWIN_MOUNT_LINE.match(line):
            opts = match.group("opts").split(",")
            if opts:
                out.append((Path(match.group("point")), opts[0].strip()))
    return out


def fstype_of(path: Path) -> str | None:
    """Best-effort filesystem type for the mount containing ``path``."""
    mounts = _darwin_mounts() if running_on_darwin() else _linux_mounts()
    if not mounts:
        return None
    return _deepest_mount(path.resolve(), mounts)


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
    if sys.platform not in SUPPORTED_PLATFORMS and not sys.platform.startswith("linux"):
        raise PreconditionError(
            f"{sys.platform} is not supported. The write protocol needs an atomic "
            f"path-exchange primitive, which brain implements for Linux "
            f"(renameat2) and macOS (renamex_np) only."
        )
    if reason := denylist_reason(root):
        raise PreconditionError(reason)
    caps = probe_capabilities(root)
    if not caps.ok:
        raise PreconditionError(
            f"{root} does not support: {', '.join(caps.missing())}. "
            "The write protocol requires all of them."
        )
    return caps

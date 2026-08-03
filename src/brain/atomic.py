"""Filesystem primitives for the write protocol.

Implements BLUEPRINT.md §6.5. Three operations carry the safety argument and each
was wrong in an earlier draft of the design:

- ``write_atomic``  — the *directory* fsync is what makes a rename durable, and is
  the step most often omitted.
- ``exchange``      — ``RENAME_EXCHANGE`` captures displaced bytes at the instant of
  displacement. Reading a file's hash and later renaming over it loses any write
  landing in between, with no crash required.
- ``publish_link``  — ``link()`` fails with ``EEXIST`` rather than overwriting, and
  publishes a file that is already complete and already fsynced. Creating the final
  path directly (``O_CREAT|O_EXCL``) instead leaves a *torn* file permanently
  occupying a revision number after a crash mid-write, which is worse than the
  overwrite it was meant to prevent.

None of this establishes crash durability. ``fsync()`` returning 0 says nothing about
lying disks or write caches — see ``probe_capabilities`` and IMPLEMENTATION-PLAN §0.5.
"""

from __future__ import annotations

import contextlib
import ctypes
import ctypes.util
import errno
import os
from dataclasses import dataclass
from pathlib import Path

AT_FDCWD = -100
RENAME_NOREPLACE = 1 << 0
RENAME_EXCHANGE = 1 << 1

_libc: ctypes.CDLL | None = None


def _libc_handle() -> ctypes.CDLL:
    global _libc
    if _libc is None:
        name = ctypes.util.find_library("c") or "libc.so.6"
        _libc = ctypes.CDLL(name, use_errno=True)
    return _libc


def _renameat2(old: Path, new: Path, flags: int) -> None:
    """Call renameat2(2) directly.

    Python exposes no wrapper, and glibc only added one in 2.28, so go through the
    syscall. ``RENAME_EXCHANGE`` is the only primitive that atomically swaps two
    paths, which is what lets a writer capture whatever was actually present at the
    moment it displaced it.
    """
    libc = _libc_handle()
    ctypes.set_errno(0)
    res = libc.syscall(
        ctypes.c_long(_RENAMEAT2_NR),
        ctypes.c_int(AT_FDCWD),
        ctypes.c_char_p(os.fsencode(old)),
        ctypes.c_int(AT_FDCWD),
        ctypes.c_char_p(os.fsencode(new)),
        ctypes.c_uint(flags),
    )
    if res != 0:
        err = ctypes.get_errno()
        raise OSError(err, os.strerror(err), str(old), None, str(new))


# renameat2 syscall numbers, per architecture.
_RENAMEAT2_BY_MACHINE = {
    "x86_64": 316,
    "aarch64": 276,
    "armv7l": 382,
    "ppc64le": 357,
    "s390x": 347,
    "riscv64": 276,
}
_RENAMEAT2_NR = _RENAMEAT2_BY_MACHINE.get(os.uname().machine, 316)


def fsync_dir(path: Path) -> None:
    """fsync a directory so that a rename/link/unlink within it is durable.

    Omitting this is the classic durability bug: the file contents are safe but the
    *name* pointing at them is not.
    """
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def write_atomic(path: Path, data: bytes) -> None:
    """Write ``data`` to ``path`` atomically: temp file, fsync, rename, fsync dir."""
    tmp = path.with_name(f".{path.name}.tmp.{os.getpid()}")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, path)
    fsync_dir(path.parent)


def write_staged(path: Path, data: bytes) -> None:
    """Write a complete, fsynced file at ``path`` without publishing it anywhere.

    The staging file is the inode that ``publish_link`` will later hard-link into the
    revision namespace. It must never be mutated after linking — the link shares the
    inode, so a later write would retroactively alter published history.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)
    fsync_dir(path.parent)


def publish_link(staged: Path, dest: Path) -> bool:
    """Hard-link a completed staging file into its final path. Never overwrites.

    Returns True on success, False if ``dest`` already exists (the caller increments
    and retries). ``os.link`` raises FileExistsError rather than clobbering, which is
    exactly the non-destructive publish semantic the revision log requires.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.link(staged, dest)
    except FileExistsError:
        return False
    fsync_dir(dest.parent)
    return True


def exchange(a: Path, b: Path) -> None:
    """Atomically swap two paths. Both must exist.

    After this returns, ``b`` names whatever ``a`` named a moment ago — which is how
    a writer gets hold of bytes a concurrent editor may have installed, instead of
    destroying them.
    """
    _renameat2(a, b, RENAME_EXCHANGE)


def rename_noreplace(src: Path, dst: Path) -> bool:
    """Rename that refuses to overwrite. Returns False if ``dst`` exists."""
    try:
        _renameat2(src, dst, RENAME_NOREPLACE)
    except OSError as exc:
        if exc.errno == errno.EEXIST:
            return False
        raise
    fsync_dir(dst.parent)
    return True


@dataclass(frozen=True)
class Capabilities:
    """What the filesystem under a given directory was observed to support.

    Deliberately named for what it is. A probe establishes *capability* — that these
    syscalls exist and succeed here. It does NOT establish crash durability, because
    a successful ``fsync`` tells you nothing about a lying disk or a write cache.
    The denylist in ``config`` is retained precisely because it covers cases a probe
    structurally cannot detect.
    """

    same_device: bool
    atomic_rename: bool
    rename_exchange: bool
    rename_noreplace: bool
    hardlink: bool
    fsync_dir: bool

    @property
    def ok(self) -> bool:
        return all(
            (
                self.same_device,
                self.atomic_rename,
                self.rename_exchange,
                self.rename_noreplace,
                self.hardlink,
                self.fsync_dir,
            )
        )

    def missing(self) -> list[str]:
        return [f.replace("_", " ") for f, v in vars(self).items() if not v]


def probe_capabilities(directory: Path) -> Capabilities:
    """Exercise every primitive the write protocol depends on, in ``directory``."""
    directory.mkdir(parents=True, exist_ok=True)
    probe = directory / ".probe"
    probe.mkdir(exist_ok=True)
    a, b, c = probe / "a", probe / "b", probe / "c"
    results = dict.fromkeys(
        (
            "same_device",
            "atomic_rename",
            "rename_exchange",
            "rename_noreplace",
            "hardlink",
            "fsync_dir",
        ),
        False,
    )
    try:
        a.write_bytes(b"a")
        b.write_bytes(b"b")
        results["same_device"] = a.stat().st_dev == directory.stat().st_dev

        with contextlib.suppress(OSError):
            fsync_dir(probe)
            results["fsync_dir"] = True

        with contextlib.suppress(OSError):
            os.replace(a, c)
            results["atomic_rename"] = c.read_bytes() == b"a"
            os.replace(c, a)

        with contextlib.suppress(OSError):
            exchange(a, b)
            results["rename_exchange"] = a.read_bytes() == b"b" and b.read_bytes() == b"a"
            exchange(a, b)

        with contextlib.suppress(OSError):
            results["rename_noreplace"] = rename_noreplace(a, b) is False

        with contextlib.suppress(OSError):
            results["hardlink"] = publish_link(a, c) and publish_link(a, c) is False
    finally:
        for p in (a, b, c):
            p.unlink(missing_ok=True)
        probe.rmdir()
    return Capabilities(**results)  # type: ignore[arg-type]

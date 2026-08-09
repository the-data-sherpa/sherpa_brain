"""Fault injection at every boundary of the write protocol.

These fork and call ``os._exit`` mid-protocol — a real crash, not a mock. Mocking the
crash would test the mock's idea of where the boundaries are, and the whole point is
that the boundaries were repeatedly in the wrong place.

The invariant under test is the one the design actually promises, which is narrower
than "present state is unchanged":

    No committed state is ever lost, and a contested memory never serves one branch
    as though it were settled.
"""

from __future__ import annotations

import contextlib
import os
from datetime import date

import pytest

from brain.config import Paths
from brain.frontmatter import content_hash, serialize
from brain.model import Disposition, Evidence, Memory, MemoryType, ProvenanceClass, Volatility
from brain.store import memory as mem
from brain.store import revisions
from brain.store.ops import pending_ops

pytestmark = pytest.mark.crash

MID = "01K1Z8V4Q00000000000000000"


def body(text: str) -> bytes:
    return serialize(
        Memory(
            id=MID,
            type=MemoryType.SEMANTIC,
            provenance_class=ProvenanceClass.DIRECT_USER_STATEMENT,
            volatility=Volatility.SLOW,
            valid_from=date(2026, 8, 3),
            evidence=[Evidence("event:01K1Z8V3M0000000000000000")],
            body=text,
        )
    ).encode()


def crash_after(paths: Paths, n_syscalls: int, payload: bytes, predecessor: str | None) -> None:
    """Run a write in a child process and kill it after ``n_syscalls`` fsyncs.

    ``os._exit`` skips every cleanup path — no atexit, no finally, no buffer flush —
    which is as close to a power cut as a test can get without one.
    """
    pid = os.fork()
    if pid == 0:  # child
        import brain.atomic as atomic

        real_fsync = os.fsync
        count = {"n": 0}

        def counting_fsync(fd):  # type: ignore[no-untyped-def]
            count["n"] += 1
            real_fsync(fd)
            if count["n"] >= n_syscalls:
                os._exit(70)

        os.fsync = counting_fsync
        atomic.os.fsync = counting_fsync  # type: ignore[attr-defined]
        with contextlib.suppress(BaseException):
            mem.write(paths, MID, payload, predecessor)
        os._exit(0)
    os.waitpid(pid, 0)


def assert_recoverable(paths: Paths, must_contain: list[bytes]) -> None:
    """After recovery, every committed state must still be reachable."""
    mem.recover_pending_ops(paths)
    assert pending_ops(paths) == [], "recovery must retire every op it handles"

    dest = mem.present_path(paths, "default", "semantic", MID)
    disposition = mem.serving_disposition(paths, MID, dest)
    assert disposition in set(Disposition), "every tree must map to exactly one disposition"

    joined = b"".join(
        revisions.read_revision(paths, MID, n) or b""
        for n in revisions.revision_numbers(paths, MID)
    )
    if dest.exists():
        joined += dest.read_bytes()
    for needle in must_contain:
        assert needle in joined, f"{needle!r} was committed and must survive the crash"


@pytest.mark.parametrize("n", [1, 2, 3, 4, 5, 6, 7, 8])
def test_crash_at_each_fsync_boundary_loses_nothing(paths: Paths, n: int) -> None:
    """Kill the writer at each durability point. Nothing committed may be lost."""
    mem.write(paths, MID, body("committed first"), None)
    dest = mem.present_path(paths, "default", "semantic", MID)
    predecessor = mem.present_hash(dest)

    crash_after(paths, n, body("second write"), predecessor)

    # The first write was committed and acknowledged; it must survive unconditionally.
    assert_recoverable(paths, [b"committed first"])


@pytest.mark.parametrize("n", [1, 2, 3, 4, 5, 6])
def test_crash_never_leaves_a_torn_revision(paths: Paths, n: int) -> None:
    """A revision file is either complete or absent — never partially written.

    This is what `linkat`-from-staging buys over creating the final path directly:
    the published file was already whole and already fsynced before it had a name.
    """
    mem.write(paths, MID, body("base"), None)
    dest = mem.present_path(paths, "default", "semantic", MID)
    crash_after(paths, n, body("interrupted"), mem.present_hash(dest))

    from brain.frontmatter import InvalidFrontmatter, parse

    for num in revisions.revision_numbers(paths, MID):
        data = revisions.read_revision(paths, MID, num)
        assert data is not None
        try:
            parse(data.decode("utf-8"))
        except (InvalidFrontmatter, UnicodeDecodeError) as exc:  # pragma: no cover
            pytest.fail(f"revision {num} is torn: {exc}")


@pytest.mark.parametrize("n", [1, 2, 3, 4, 5])
def test_recovery_is_idempotent(paths: Paths, n: int) -> None:
    """Running recovery twice changes nothing the second time."""
    mem.write(paths, MID, body("base"), None)
    dest = mem.present_path(paths, "default", "semantic", MID)
    crash_after(paths, n, body("interrupted"), mem.present_hash(dest))

    mem.recover_pending_ops(paths)
    first = sorted(revisions.revision_numbers(paths, MID))
    present_after_first = mem.present_hash(dest)

    mem.recover_pending_ops(paths)
    assert sorted(revisions.revision_numbers(paths, MID)) == first
    assert mem.present_hash(dest) == present_after_first


@pytest.mark.parametrize("n", [1, 2, 3, 4, 5])
def test_index_is_always_rebuildable_after_a_crash(paths: Paths, n: int) -> None:
    """The index is derived, so a crash can only ever leave it stale — never wrong."""
    from brain.index import build

    mem.write(paths, MID, body("base"), None)
    dest = mem.present_path(paths, "default", "semantic", MID)
    crash_after(paths, n, body("interrupted"), mem.present_hash(dest))
    mem.recover_pending_ops(paths)

    conn = build.connect(paths)
    build.rebuild(paths, conn)
    once = build.logical_snapshot(conn)
    build.rebuild(paths, conn)
    assert build.logical_snapshot(conn) == once
    conn.close()


def test_crash_then_concurrent_direct_edit_does_not_bury_a_branch(paths: Paths) -> None:
    """The failure that motivated durable intent records.

    Crash after the revision is published but before present is materialized, then
    let an editor install its own version. Without an op record this reads as a plain
    unwitnessed edit, the editor's branch is reconciled, and the already-published
    competing revision is buried with no conflict raised.
    """
    mem.write(paths, MID, body("base"), None)
    dest = mem.present_path(paths, "default", "semantic", MID)
    predecessor = mem.present_hash(dest)

    crash_after(paths, 3, body("mediated branch"), predecessor)
    published = [
        content_hash(revisions.read_revision(paths, MID, n) or b"")
        for n in revisions.revision_numbers(paths, MID)
    ]

    # An editor now writes its own version over present.
    dest.write_bytes(body("editor branch"))
    mem.recover_pending_ops(paths)

    joined = (
        b"".join(
            revisions.read_revision(paths, MID, n) or b""
            for n in revisions.revision_numbers(paths, MID)
        )
        + dest.read_bytes()
    )

    assert b"base" in joined
    assert b"editor branch" in joined
    # Whatever the crash managed to publish is still in the log.
    for h in published:
        assert h in {
            content_hash(revisions.read_revision(paths, MID, n) or b"")
            for n in revisions.revision_numbers(paths, MID)
        }, "a published revision was buried by recovery"


def test_staging_is_not_collected_while_an_op_references_it(paths: Paths) -> None:
    """GC must be serialized against op creation, not merely gated on retirement."""
    from brain.atomic import write_staged
    from brain.store.ops import create_op, gc_staging, new_op

    staged = paths.staging_path("01OPIDTEST0000000000000000")
    write_staged(staged, b"in flight")
    op = new_op("01OPIDTEST0000000000000000", MID, staged, None)
    create_op(paths, op)

    removed = gc_staging(paths)
    assert staged.exists(), "GC collected a staging file an op still references"
    assert str(staged) not in [str(r) for r in removed]

"""Write protocol tests, including the races that motivated the design.

The interesting ones here are ``test_concurrent_editor_write_is_captured`` and
``test_divergence_publishes_both_branches``: both reproduce failures that an earlier
version of this protocol lost data on, with no crash required.
"""

from __future__ import annotations

import pytest

from brain.config import Paths
from brain.frontmatter import content_hash
from brain.model import Disposition
from brain.store import memory as mem
from brain.store import revisions
from brain.store.ops import pending_ops


def make_body(text: str) -> bytes:
    return (
        "---\n"
        "id: 01K1Z8V4Q0000000000000000\n"
        "type: semantic\n"
        "provenance_class: direct-user-statement\n"
        "volatility: slow\n"
        "valid_from: '2026-08-03'\n"
        "evidence:\n"
        "  - event:01K1Z8V3M0000000000000000\n"
        "---\n\n"
        f"{text}\n"
    ).encode()


MID = "01K1Z8V4Q0000000000000000"


def test_first_write_creates_revision_one(paths: Paths) -> None:
    r = mem.write(paths, MID, make_body("first"), None)
    assert r.revision_no == 1
    assert revisions.revision_numbers(paths, MID) == [1]

    dest = mem.present_path(paths, "default", "semantic", MID)
    assert dest.exists()
    assert b"first" in dest.read_bytes()


def test_present_is_a_view_of_the_newest_revision(paths: Paths) -> None:
    """Revisions hold EVERY committed state, so present == newest after a write.

    Under the rejected alternative (revisions hold only prior states) this assertion
    would fail for every memory, and the unwitnessed-edit detector would fire on all
    of them forever.
    """
    mem.write(paths, MID, make_body("one"), None)
    dest = mem.present_path(paths, "default", "semantic", MID)
    assert mem.present_hash(dest) == revisions.newest_hash(paths, MID)

    prev = mem.present_hash(dest)
    mem.write(paths, MID, make_body("two"), prev)
    assert mem.present_hash(dest) == revisions.newest_hash(paths, MID)
    assert revisions.revision_numbers(paths, MID) == [1, 2]


def test_sequential_writes_keep_full_history(paths: Paths) -> None:
    dest = mem.present_path(paths, "default", "semantic", MID)
    mem.write(paths, MID, make_body("v1"), None)
    mem.write(paths, MID, make_body("v2"), mem.present_hash(dest))
    mem.write(paths, MID, make_body("v3"), mem.present_hash(dest))

    bodies = [
        revisions.read_revision(paths, MID, n) for n in revisions.revision_numbers(paths, MID)
    ]
    assert [b"v1" in b for b in bodies if b] == [True, False, False]
    assert len(bodies) == 3


def test_concurrent_editor_write_is_captured_not_destroyed(paths: Paths) -> None:
    """The race that needed RENAME_EXCHANGE.

    An editor replaces present between the caller reading its hash and the write
    committing. Under a read-hash-then-rename design those bytes are destroyed with
    no crash involved. Here they must survive as a revision.
    """
    dest = mem.present_path(paths, "default", "semantic", MID)
    mem.write(paths, MID, make_body("original"), None)
    stale = mem.present_hash(dest)

    # The editor lands its own version, bypassing the write protocol entirely.
    dest.write_bytes(make_body("editor version"))

    with pytest.raises(mem.Divergence):
        mem.write(paths, MID, make_body("mediated version"), stale)

    # Nothing is lost: all three states are in the log.
    all_bodies = b"".join(
        revisions.read_revision(paths, MID, n) or b""
        for n in revisions.revision_numbers(paths, MID)
    )
    assert b"original" in all_bodies
    assert b"editor version" in all_bodies
    assert b"mediated version" in all_bodies


def test_divergence_marks_contested_and_reads_fail_closed(paths: Paths) -> None:
    dest = mem.present_path(paths, "default", "semantic", MID)
    mem.write(paths, MID, make_body("base"), None)
    stale = mem.present_hash(dest)
    dest.write_bytes(make_body("theirs"))

    with pytest.raises(mem.Divergence):
        mem.write(paths, MID, make_body("ours"), stale)

    assert mem.is_contested(paths, MID)
    assert mem.serving_disposition(paths, MID, dest) is Disposition.CONTESTED


def test_stale_predecessor_is_a_divergence_not_an_overwrite(paths: Paths) -> None:
    dest = mem.present_path(paths, "default", "semantic", MID)
    mem.write(paths, MID, make_body("v1"), None)
    stale = mem.present_hash(dest)
    mem.write(paths, MID, make_body("v2"), stale)

    with pytest.raises(mem.Divergence):
        mem.write(paths, MID, make_body("v3-from-stale"), stale)

    joined = b"".join(
        revisions.read_revision(paths, MID, n) or b""
        for n in revisions.revision_numbers(paths, MID)
    )
    assert b"v2" in joined and b"v3-from-stale" in joined


def test_revisions_are_never_overwritten(paths: Paths) -> None:
    mem.write(paths, MID, make_body("one"), None)
    first = revisions.read_revision(paths, MID, 1)
    dest = mem.present_path(paths, "default", "semantic", MID)
    mem.write(paths, MID, make_body("two"), mem.present_hash(dest))
    assert revisions.read_revision(paths, MID, 1) == first


def test_ops_are_retired_on_success(paths: Paths) -> None:
    mem.write(paths, MID, make_body("x"), None)
    assert pending_ops(paths) == []


def test_settled_disposition_after_clean_write(paths: Paths) -> None:
    mem.write(paths, MID, make_body("x"), None)
    dest = mem.present_path(paths, "default", "semantic", MID)
    assert mem.serving_disposition(paths, MID, dest) is Disposition.SETTLED


def test_unwitnessed_edit_is_detected(paths: Paths) -> None:
    mem.write(paths, MID, make_body("x"), None)
    dest = mem.present_path(paths, "default", "semantic", MID)
    dest.write_bytes(make_body("edited by hand"))
    assert mem.serving_disposition(paths, MID, dest) is Disposition.UNWITNESSED


def test_malformed_present_is_quarantined(paths: Paths) -> None:
    mem.write(paths, MID, make_body("x"), None)
    dest = mem.present_path(paths, "default", "semantic", MID)
    dest.write_bytes(b"not front matter at all")
    assert mem.serving_disposition(paths, MID, dest) is Disposition.QUARANTINED


def test_content_hash_is_stable(paths: Paths) -> None:
    b = make_body("stable")
    assert content_hash(b) == content_hash(b)

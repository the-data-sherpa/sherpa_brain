"""Identifiers.

Memory IDs are ULIDs: time-sortable, path-independent, and collision-safe without
coordination. BLUEPRINT.md §7.1 requires that paths and titles never serve as
identity — a memory that moves must keep its name.

Operation IDs identify an in-flight write. They deliberately do *not* embed a
revision number: a preselected number is a guess about a namespace another writer
may claim first, and the whole point of ``opid`` is that it is unique before the
namespace is touched (BLUEPRINT §6.5).
"""

from __future__ import annotations

import os
import time

_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"  # no I, L, O, U


def _encode(value: int, length: int) -> str:
    out = []
    for _ in range(length):
        out.append(_CROCKFORD[value & 0x1F])
        value >>= 5
    return "".join(reversed(out))


def new_ulid(when_ms: int | None = None) -> str:
    """A 26-character ULID: 48 bits of milliseconds, 80 bits of randomness."""
    ms = when_ms if when_ms is not None else int(time.time() * 1000)
    rand = int.from_bytes(os.urandom(10), "big")
    return _encode(ms, 10) + _encode(rand, 16)


def new_opid() -> str:
    """An operation ID for one in-flight write."""
    return new_ulid()


def is_ulid(value: str) -> bool:
    return len(value) == 26 and all(c in _CROCKFORD for c in value)

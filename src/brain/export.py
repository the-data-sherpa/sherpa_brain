"""Export the whole corpus (BLUEPRINT.md §12.2, §16).

Markdown export is close to a no-op, and that is the point: markdown *is* the
canonical format, so "export" is a copy rather than a conversion. A format that
needs converting to leave the system is a format you are locked into.

JSONL export exists for the machine path — piping into another tool, diffing two
stores, or reconstructing an index somewhere else.

Tombstoned subjects are never exported. An export that resurrects deleted content is
a deletion bug wearing a different hat.
"""

from __future__ import annotations

import json
import shutil
from dataclasses import asdict
from pathlib import Path

from .config import Paths
from .frontmatter import InvalidFrontmatter, content_hash, parse
from .store import deletion, revisions


def export_markdown(paths: Paths, dest: Path) -> dict[str, int]:
    """Copy every live memory, with its full revision history, as plain files."""
    dest.mkdir(parents=True, exist_ok=True)
    tombstoned = deletion.tombstoned_ids(paths)
    counts = {"memories": 0, "revisions": 0, "skipped_tombstoned": 0, "skipped_invalid": 0}

    for src in sorted(paths.memories.rglob("*.md")):
        if ".revisions" in src.parts or ".staging" in src.parts:
            continue
        try:
            m = parse(src.read_text(), src)
        except (InvalidFrontmatter, UnicodeDecodeError):
            counts["skipped_invalid"] += 1
            continue
        if m.id in tombstoned:
            counts["skipped_tombstoned"] += 1
            continue

        rel = src.relative_to(paths.memories)
        out = dest / "memories" / rel
        out.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, out)
        counts["memories"] += 1

        for n in revisions.revision_numbers(paths, m.id):
            data = revisions.read_revision(paths, m.id, n)
            if data is None:
                continue
            rev_out = dest / "revisions" / m.id / f"{n:06d}.md"
            rev_out.parent.mkdir(parents=True, exist_ok=True)
            rev_out.write_bytes(data)
            counts["revisions"] += 1

    # Curated knowledge and the ledgers travel too — an export you cannot verify or
    # replay deletions from is not a portable store.
    for name, ledger_path in (
        ("tombstones.jsonl", paths.tombstones),
        ("acks.jsonl", paths.acks),
        ("purges.jsonl", paths.purges),
    ):
        if ledger_path.exists():
            shutil.copy2(ledger_path, dest / name)
    return counts


def export_jsonl(paths: Paths, dest: Path) -> dict[str, int]:
    """One JSON object per memory, with evidence and revision digests."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    tombstoned = deletion.tombstoned_ids(paths)
    counts = {"memories": 0, "skipped_tombstoned": 0, "skipped_invalid": 0}

    with dest.open("w") as fh:
        for src in sorted(paths.memories.rglob("*.md")):
            if ".revisions" in src.parts or ".staging" in src.parts:
                continue
            try:
                m = parse(src.read_text(), src)
            except (InvalidFrontmatter, UnicodeDecodeError):
                counts["skipped_invalid"] += 1
                continue
            if m.id in tombstoned:
                counts["skipped_tombstoned"] += 1
                continue

            record = {
                "id": m.id,
                "type": m.type.value,
                "provenance_class": m.provenance_class.value,
                "volatility": m.volatility.value,
                "valid_from": m.valid_from.isoformat(),
                "valid_to": m.valid_to.isoformat() if m.valid_to else None,
                "workspace": m.workspace,
                "status": m.status.value,
                "tags": m.tags,
                "evidence": [asdict(e) for e in m.evidence],
                "body": m.body,
                "content_hash": content_hash(src.read_bytes()),
                "revisions": [
                    {"n": n, "hash": content_hash(revisions.read_revision(paths, m.id, n) or b"")}
                    for n in revisions.revision_numbers(paths, m.id)
                ],
            }
            fh.write(json.dumps(record, sort_keys=True) + "\n")
            counts["memories"] += 1
    return counts

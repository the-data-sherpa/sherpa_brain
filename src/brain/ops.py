"""Scheduled operation via systemd --user timers.

``expire``, ``sync``, and ``backup create`` are each correct and each useless if
nobody runs them. Decay that never sweeps is not decay; a backup you never take is
not a backup; a deletion that never reaches quorum stays pending forever.

User units, never system units: this is a single-user local store, it needs no root,
and a service running as root would have more access to your memories than you do.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from .config import Paths

UNIT_DIR = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "systemd" / "user"

SERVICE = """\
[Unit]
Description=brain: {desc}
Documentation=file://{repo}/docs/RUNBOOK.md

[Service]
Type=oneshot
Environment=BRAIN_STATE_DIR={state}
ExecStart={python} -m brain.cli {command}
# These sweeps are advisory. A non-zero exit means "there is something to look at",
# not "the system is broken" — `sync` exits 3 whenever a deletion is still pending,
# which is a normal state, not a failure.
SuccessExitStatus=0 1 3
"""

TIMER = """\
[Unit]
Description=brain: {desc}

[Timer]
OnCalendar={schedule}
Persistent=true
RandomizedDelaySec={jitter}

[Install]
WantedBy=timers.target
"""

#: `sync` carries expiry, purge resumption, replication retry, and budget trims.
#: One timer, because splitting them multiplies the ways a schedule drifts apart.
UNITS = (
    (
        "brain-sync",
        "resume deletions, retry replication, expire, trim logs",
        "sync",
        "hourly",
        "5m",
    ),
    (
        "brain-expire",
        "lapse stale memories and tombstone past-grace ones",
        "expire",
        "daily",
        "30m",
    ),
    ("brain-backup", "take a verifiable backup", "backup create", "daily", "1h"),
)


def install_user_timers(paths: Paths, *, dry_run: bool = False) -> list[str]:
    repo = Path(__file__).resolve().parents[2]
    written: list[str] = []
    if not dry_run:
        UNIT_DIR.mkdir(parents=True, exist_ok=True)

    for name, desc, command, schedule, jitter in UNITS:
        service = SERVICE.format(
            desc=desc, repo=repo, state=paths.root, python=sys.executable, command=command
        )
        timer = TIMER.format(desc=desc, schedule=schedule, jitter=jitter)
        for suffix, body in ((".service", service), (".timer", timer)):
            target = UNIT_DIR / f"{name}{suffix}"
            if dry_run:
                print(f"--- {target} ---\n{body}")
            else:
                target.write_text(body)
            written.append(str(target))
    return written

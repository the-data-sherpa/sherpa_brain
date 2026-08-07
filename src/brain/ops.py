"""Scheduled operation via systemd --user timers, or launchd agents on macOS.

``expire``, ``sync``, and ``backup create`` are each correct and each useless if
nobody runs them. Decay that never sweeps is not decay; a backup you never take is
not a backup; a deletion that never reaches quorum stays pending forever.

User units, never system units: this is a single-user local store, it needs no root,
and a service running as root would have more access to your memories than you do.
The same rule picks ``~/Library/LaunchAgents`` over ``/Library/LaunchDaemons`` on
macOS.

The two schedulers are not interchangeable and the difference matters for a store
that sweeps. systemd's ``Persistent=true`` runs a timer that was missed while the
machine was off; launchd's ``StartCalendarInterval`` does the same, but launchd has
no equivalent of ``SuccessExitStatus``, so a sweep exiting 3 — "a deletion is still
pending", a normal state — would be logged as a failure. ``ThrottleInterval`` and a
wrapper are not worth it; the exit code is recorded in launchd's log and nothing acts
on it, so the practical effect is a log line rather than a broken schedule.
"""

from __future__ import annotations

import os
import plistlib
import sys
from pathlib import Path

from .config import Paths

UNIT_DIR = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "systemd" / "user"


def launch_agents_dir() -> Path:
    return Path(os.environ.get("HOME") or Path.home()) / "Library" / "LaunchAgents"


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
#: The last two fields are the launchd equivalents of OnCalendar: how often, and at
#: what hour. launchd has no "hourly" keyword — an interval in seconds is the closest
#: honest translation, and it does not drift.
UNITS = (
    (
        "brain-sync",
        "resume deletions, retry replication, expire, trim logs",
        "sync",
        "hourly",
        "5m",
        {"interval": 3600},
    ),
    (
        "brain-expire",
        "lapse stale memories and tombstone past-grace ones",
        "expire",
        "daily",
        "30m",
        {"calendar": {"Hour": 3, "Minute": 20}},
    ),
    (
        "brain-backup",
        "take a verifiable backup",
        "backup create",
        "daily",
        "1h",
        {"calendar": {"Hour": 4, "Minute": 40}},
    ),
)


def _install_systemd(paths: Paths, *, dry_run: bool) -> list[str]:
    repo = Path(__file__).resolve().parents[2]
    written: list[str] = []
    if not dry_run:
        UNIT_DIR.mkdir(parents=True, exist_ok=True)

    for name, desc, command, schedule, jitter, _ in UNITS:
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


def _install_launchd(paths: Paths, *, dry_run: bool) -> list[str]:
    agents = launch_agents_dir()
    written: list[str] = []
    if not dry_run:
        agents.mkdir(parents=True, exist_ok=True)

    for name, desc, command, _, _, when in UNITS:
        label = f"dev.brain.{name.removeprefix('brain-')}"
        plist: dict[str, object] = {
            "Label": label,
            "ProgramArguments": [sys.executable, "-m", "brain.cli", *command.split()],
            "EnvironmentVariables": {"BRAIN_STATE_DIR": str(paths.root)},
            # Absolute, because a LaunchAgent inherits almost no environment and
            # certainly not the shell's working directory.
            "WorkingDirectory": str(paths.root),
            "StandardOutPath": str(paths.logs / f"{name}.log"),
            "StandardErrorPath": str(paths.logs / f"{name}.log"),
            "RunAtLoad": False,
            "ProcessType": "Background",
            "Comment": desc,
        }
        if "interval" in when:
            plist["StartInterval"] = when["interval"]
        else:
            plist["StartCalendarInterval"] = when["calendar"]

        target = agents / f"{label}.plist"
        body = plistlib.dumps(plist)
        if dry_run:
            print(f"--- {target} ---\n{body.decode()}")
        else:
            target.write_bytes(body)
        written.append(str(target))
    return written


def install_user_timers(paths: Paths, *, dry_run: bool = False) -> list[str]:
    if sys.platform == "darwin":
        return _install_launchd(paths, dry_run=dry_run)
    return _install_systemd(paths, dry_run=dry_run)


def activation_commands() -> list[str]:
    """What the user still has to run. Writing a unit file does not schedule it.

    Returned rather than printed so the CLI and the installer say the same thing —
    a runbook that drifts from the tool is a runbook that gets followed into a
    non-working state.
    """
    if sys.platform == "darwin":
        agents = launch_agents_dir()
        return [
            f"launchctl bootstrap gui/$(id -u) {agents}/dev.brain.sync.plist",
            f"launchctl bootstrap gui/$(id -u) {agents}/dev.brain.expire.plist",
            f"launchctl bootstrap gui/$(id -u) {agents}/dev.brain.backup.plist",
            "launchctl list | grep dev.brain    # check",
        ]
    return [
        "systemctl --user daemon-reload",
        "systemctl --user enable --now brain-sync.timer brain-expire.timer brain-backup.timer",
        "systemctl --user list-timers 'brain-*'    # check",
    ]

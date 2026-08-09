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
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from .config import Paths, running_on_darwin

UNIT_DIR = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "systemd" / "user"


def launch_agents_dir() -> Path:
    return Path(os.environ.get("HOME") or Path.home()) / "Library" / "LaunchAgents"


SERVICE = """\
[Unit]
Description=brain: {desc}
{documentation}
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


def runbook_path() -> Path | None:
    """The RUNBOOK, if this install can actually see one.

    A dev checkout has ``docs/``; a wheel does not, because docs are not package
    data. The old code computed ``parents[2]/docs/RUNBOOK.md`` unconditionally, which
    was right from a checkout and wrong from an installed tool — and since a proper
    install is the *recommended* path, the wrong case was the common one. Same
    dual-location reasoning as ``adapters.harness_dir()``, except here the honest
    answer when neither exists is to omit the field rather than invent a path.
    """
    candidate = Path(__file__).resolve().parents[2] / "docs" / "RUNBOOK.md"
    return candidate if candidate.is_file() else None


def _documentation_line() -> str:
    """``Documentation=`` when there is a real file, otherwise nothing.

    Returned with its own newline so the template stays flat: a unit with no
    documentation should have no blank line where the field would have been.
    """
    runbook = runbook_path()
    return f"Documentation=file://{runbook}\n" if runbook else ""


def _install_systemd(paths: Paths, *, dry_run: bool) -> list[str]:
    written: list[str] = []
    if not dry_run:
        UNIT_DIR.mkdir(parents=True, exist_ok=True)

    for name, desc, command, schedule, jitter, _ in UNITS:
        service = SERVICE.format(
            desc=desc,
            documentation=_documentation_line(),
            state=paths.root,
            python=sys.executable,
            command=command,
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


@dataclass(frozen=True)
class SchedulerState:
    """What the scheduler looks like, from disk *and* from the scheduler itself.

    An earlier version of this was deliberately subprocess-free, on the reasoning
    that ``systemctl`` is absent in containers and CI. That was the wrong trade, and
    review caught it: unit files, wants-links and a present interpreter can all be
    correct while nothing runs — a masked timer, or a user manager that was never
    started because the account has no lingering and nobody logged in graphically.
    Filesystem evidence alone cannot distinguish "scheduled" from "looks scheduled",
    so reporting it as *runnable* was exactly the overclaim this whole check exists
    to prevent.

    So the runtime is asked, and the answer is allowed to be "don't know":

    - installed — the unit files exist where we wrote them
    - enabled — a wants-link exists, in the persistent path or the ``--runtime`` one
    - active — the scheduler itself says the timers are live. ``None`` means the
      question could not be asked (no ``systemctl``, no bus, a dead manager), which
      is reported as unverifiable rather than as healthy
    - broken_interpreters — the ``ExecStart`` interpreter no longer exists
    - broken_units — a unit file exists but cannot be understood: a corrupt plist, a
      service with no ``ExecStart``. Ignoring these was a real defect; a unit that
      cannot be parsed is broken, not absent
    """

    installed: bool
    enabled: bool | None
    active: bool | None
    broken_interpreters: tuple[str, ...]
    broken_units: tuple[str, ...]


#: Long enough for a loaded manager to answer, short enough that `brain doctor` never
#: hangs on a wedged bus. `subprocess`'s own timeout, not the `timeout(1)` binary,
#: which macOS does not ship.
_QUERY_TIMEOUT_S = 5


def _ask(argv: list[str]) -> tuple[int, str] | None:
    """Run a query, or return None if the question cannot be asked at all."""
    exe = shutil.which(argv[0])
    if not exe:
        return None
    try:
        r = subprocess.run(
            [exe, *argv[1:]], capture_output=True, text=True, timeout=_QUERY_TIMEOUT_S
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return r.returncode, (r.stdout + r.stderr).strip()


def _systemd_active(timers: list[str]) -> bool | None:
    answer = _ask(["systemctl", "--user", "is-active", *timers])
    if answer is None:
        return None
    code, out = answer
    if code == 0:
        return True
    # "Failed to connect to bus" is not the same answer as "inactive". The first
    # means the manager is not there to ask; calling that False would report a
    # definite failure we have not established.
    if "bus" in out.lower() or "failed to connect" in out.lower():
        return None
    return False


def _launchd_active(labels: list[str]) -> bool | None:
    for label in labels:
        answer = _ask(["launchctl", "list", label])
        if answer is None:
            return None
        if answer[0] != 0:
            return False
    return True


def _runtime_wants_dir() -> Path:
    """Where ``systemctl --user enable --runtime`` puts its links, unlike the default."""
    uid = os.getuid()
    return Path(os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{uid}")) / "systemd" / "user"


def _systemd_state() -> SchedulerState:
    services = [UNIT_DIR / f"{name}.service" for name, *_ in UNITS]
    timers = [UNIT_DIR / f"{name}.timer" for name, *_ in UNITS]
    installed = all(u.is_file() for u in (*services, *timers))

    wants_dirs = (UNIT_DIR / "timers.target.wants", _runtime_wants_dir() / "timers.target.wants")
    enabled = installed and all(any((d / t.name).exists() for d in wants_dirs) for t in timers)

    broken_interpreters: list[str] = []
    broken_units: list[str] = []
    for service in services:
        if not service.is_file():
            continue
        execs = [ln for ln in service.read_text().splitlines() if ln.startswith("ExecStart=")]
        if not execs:
            broken_units.append(service.name)
            continue
        argv = execs[0].removeprefix("ExecStart=").split()
        if not argv:
            broken_units.append(service.name)
        elif not Path(argv[0]).exists():
            broken_interpreters.append(argv[0])

    active = _systemd_active([t.name for t in timers]) if enabled else None
    return SchedulerState(
        installed,
        enabled,
        active,
        tuple(sorted(set(broken_interpreters))),
        tuple(sorted(set(broken_units))),
    )


def _launchd_state() -> SchedulerState:
    agents = launch_agents_dir()
    labels = [f"dev.brain.{name.removeprefix('brain-')}" for name, *_ in UNITS]
    plists = [agents / f"{label}.plist" for label in labels]
    installed = all(p.is_file() for p in plists)

    broken_interpreters: list[str] = []
    broken_units: list[str] = []
    for plist in plists:
        if not plist.is_file():
            continue
        try:
            argv = plistlib.loads(plist.read_bytes()).get("ProgramArguments") or []
        except Exception:
            # A plist that will not parse is a unit that will not run. The previous
            # version swallowed this and let the check degrade to INFO.
            broken_units.append(plist.name)
            continue
        if not argv:
            broken_units.append(plist.name)
        elif not Path(argv[0]).exists():
            broken_interpreters.append(argv[0])

    # launchd bootstrap state is not a filesystem fact, so `enabled` stays unknown and
    # `active` carries the real answer — from launchctl, when it can be reached.
    active = _launchd_active(labels) if installed else None
    return SchedulerState(
        installed,
        None,
        active,
        tuple(sorted(set(broken_interpreters))),
        tuple(sorted(set(broken_units))),
    )


def scheduler_state() -> SchedulerState:
    return _launchd_state() if running_on_darwin() else _systemd_state()


def install_user_timers(paths: Paths, *, dry_run: bool = False) -> list[str]:
    if running_on_darwin():
        return _install_launchd(paths, dry_run=dry_run)
    return _install_systemd(paths, dry_run=dry_run)


def activation_commands() -> list[str]:
    """What the user still has to run. Writing a unit file does not schedule it.

    Returned rather than printed so the CLI and the installer say the same thing —
    a runbook that drifts from the tool is a runbook that gets followed into a
    non-working state.
    """
    if running_on_darwin():
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

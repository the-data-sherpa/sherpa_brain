"""Linux and macOS differ in the three places that matter, and only three.

The swap primitive, the durability barrier, and the scheduler. Everything else is
POSIX. These tests pin the differences from whichever platform the suite happens to
run on, because the macOS paths are otherwise only exercised on a Mac — and a
bootstrap nobody can test until they are already on the unfamiliar machine is a
bootstrap that fails there.
"""

from __future__ import annotations

import plistlib
import subprocess
from pathlib import Path

import pytest

from brain import atomic, config, ops
from brain.config import Paths

# -- the filesystem denylist --------------------------------------------------------

#: Real `mount(8)` output. Note the mount point containing a space, which is why the
#: parser anchors on the parenthesised tail rather than splitting on whitespace.
DARWIN_MOUNT = """\
/dev/disk3s1s1 on / (apfs, sealed, local, read-only, journaled)
devfs on /dev (devfs, local, nobrowse)
/dev/disk3s5 on /System/Volumes/Data (apfs, local, journaled, nobrowse)
map auto_home on /System/Volumes/Data/home (autofs, automounted, nobrowse)
//guest@server._smb._tcp.local/share on /Volumes/Team Share (smbfs, nodev, nosuid)
"""


@pytest.fixture
def on_darwin(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config.sys, "platform", "darwin")

    def fake_mount(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(["/sbin/mount"], 0, DARWIN_MOUNT, "")

    monkeypatch.setattr(config.subprocess, "run", fake_mount)


def test_darwin_mount_parsing_finds_the_deepest_mount(on_darwin: None) -> None:
    """`/` matches every path, so taking the first hit would mask every real mount."""
    assert config.fstype_of(Path("/System/Volumes/Data/Users/x")) == "apfs"
    assert config.fstype_of(Path("/")) == "apfs"


def test_a_mounted_smb_share_is_refused_on_macos(on_darwin: None) -> None:
    """The whole point of the denylist: a probe cannot detect this, and a store on a
    network share breaks compare-and-swap without ever failing a syscall."""
    reason = config.denylist_reason(Path("/Volumes/Team Share/brain"))
    assert reason is not None
    assert "smbfs" in reason


def test_a_local_apfs_path_is_not_refused(on_darwin: None) -> None:
    assert config.denylist_reason(Path("/System/Volumes/Data/Users/x/brain")) is None


def test_mount_points_with_spaces_survive_parsing(on_darwin: None) -> None:
    assert config.fstype_of(Path("/Volumes/Team Share")) == "smbfs"


def test_an_unsupported_kernel_is_refused_before_anything_is_written(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(config.sys, "platform", "win32")
    with pytest.raises(config.PreconditionError, match="not supported"):
        config.check_preconditions(tmp_path)


# -- the exchange primitive ---------------------------------------------------------


def test_an_unknown_architecture_refuses_rather_than_guessing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Defaulting to another architecture's syscall number does not fail the rename.
    It calls a different syscall."""
    monkeypatch.setattr(atomic, "_RENAMEAT2_NR", None)
    monkeypatch.setattr(atomic, "_IS_DARWIN", False)
    monkeypatch.setattr(atomic, "_IS_LINUX", True)
    a, b = tmp_path / "a", tmp_path / "b"
    a.write_bytes(b"a")
    b.write_bytes(b"b")
    with pytest.raises(atomic.UnsupportedPlatform, match="renameat2"):
        atomic.exchange(a, b)


def test_a_platform_with_no_exchange_fails_the_probe_instead_of_crashing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The probe's job is to report a missing capability, not to raise."""
    monkeypatch.setattr(atomic, "_IS_DARWIN", False)
    monkeypatch.setattr(atomic, "_IS_LINUX", False)
    caps = atomic.probe_capabilities(tmp_path)
    assert not caps.ok
    assert "rename exchange" in caps.missing()


# -- scheduling ---------------------------------------------------------------------


def test_macos_gets_launch_agents_not_systemd_units(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, paths: Paths
) -> None:
    monkeypatch.setattr(ops.sys, "platform", "darwin")
    monkeypatch.setenv("HOME", str(tmp_path))

    written = ops.install_user_timers(paths)
    assert written
    for path in written:
        assert path.endswith(".plist")
        assert "LaunchAgents" in path
        plist = plistlib.loads(Path(path).read_bytes())
        # A LaunchAgent inherits almost no environment, so the state directory has
        # to be recorded explicitly or the sweep runs against the wrong store.
        assert plist["EnvironmentVariables"]["BRAIN_STATE_DIR"] == str(paths.root)
        assert plist["ProgramArguments"][1:3] == ["-m", "brain.cli"]


def test_every_sweep_is_scheduled_on_both_platforms(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, paths: Paths
) -> None:
    """A sweep that is written but never scheduled is the failure this module exists
    to prevent, so all three must appear in both spellings."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(ops, "UNIT_DIR", tmp_path / "systemd")

    monkeypatch.setattr(ops.sys, "platform", "darwin")
    darwin = " ".join(ops.install_user_timers(paths))
    monkeypatch.setattr(ops.sys, "platform", "linux")
    linux = " ".join(ops.install_user_timers(paths))

    for sweep in ("sync", "expire", "backup"):
        assert sweep in darwin, f"{sweep} is not scheduled on macOS"
        assert sweep in linux, f"{sweep} is not scheduled on Linux"


def test_activation_commands_name_every_unit_that_was_written(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, paths: Paths
) -> None:
    """The runbook drifting from the tool is how people follow instructions into a
    half-scheduled state — `expire` was missing from this list once already."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(ops, "UNIT_DIR", tmp_path / "systemd")

    for platform in ("linux", "darwin"):
        monkeypatch.setattr(ops.sys, "platform", platform)
        ops.install_user_timers(paths)
        commands = " ".join(ops.activation_commands())
        for sweep in ("sync", "expire", "backup"):
            assert sweep in commands, f"{sweep} is never activated on {platform}"


def test_dry_run_writes_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, paths: Paths
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(ops, "UNIT_DIR", tmp_path / "systemd")
    for platform in ("linux", "darwin"):
        monkeypatch.setattr(ops.sys, "platform", platform)
        for path in ops.install_user_timers(paths, dry_run=True):
            assert not Path(path).exists()

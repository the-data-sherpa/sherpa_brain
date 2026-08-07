"""User-scope harness wiring: the half of the install that lives outside the repo.

These tests exist because the failure mode they guard is *silent*. A config in the
wrong schema parses fine and is then ignored; a merge that drops a key removes
someone else's tool from their machine; a hook installed twice shadows itself. None
of that raises, and none of it is visible until the day you needed the memory.

Every test here runs against a fake ``HOME``, because the thing being tested is
precisely what the installer does to a home directory.
"""

from __future__ import annotations

import json
import stat
import tomllib
from pathlib import Path

import pytest

from brain import adapters
from brain.config import Paths


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setenv("HOME", str(h))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(h / ".config"))
    return h


def install(paths: Paths, target: str, repo: Path) -> list[str]:
    return adapters.write(adapters.generate(paths, target, repo, scope="user"))


# -- the vendored assets ------------------------------------------------------------


def test_vendored_skill_is_found_and_passes_the_purity_check() -> None:
    """The skill ships in the repo. If it stops being findable, user scope is dead."""
    skill = adapters.harness_dir() / "SKILL.md"
    assert skill.is_file()
    adapters.assert_pointers_only(skill.read_text(), "claude", vendored=True)


def test_vendoring_is_not_a_way_to_smuggle_a_memory_in() -> None:
    """`vendored=True` relaxes the horizontal-rule marker. It must not relax more.

    A memory file also opens with `---`, so if "vendored" meant "skip the check",
    the exemption would be a hole straight through the property the module exists
    to hold.
    """
    memory_file = (
        "---\n"
        "id: 01K1Z8V4Q0000000000000000\n"
        "provenance_class: direct-user-statement\n"
        "volatility: slow\n"
        "---\n\n"
        "the body of a memory\n"
    )
    with pytest.raises(adapters.AdapterPurityError, match="unexpected keys"):
        adapters.assert_pointers_only(memory_file, "claude", vendored=True)


def test_a_vendored_body_carrying_memory_content_is_still_rejected() -> None:
    """Front matter is stripped; the body is not exempt from anything."""
    smuggled = "---\nname: brain\n---\n\nSee event:01K1Z8V3M0000000000000000 for why.\n"
    with pytest.raises(adapters.AdapterPurityError):
        adapters.assert_pointers_only(smuggled, "claude", vendored=True)


# -- what each target writes --------------------------------------------------------


def test_every_target_writes_something_at_user_scope(paths: Paths, home: Path) -> None:
    for target in adapters.TARGETS:
        written = install(paths, target, home / "repo")
        assert written, f"{target} wrote nothing at user scope"
        for path in written:
            assert Path(path).is_file()
            assert str(home) in path, f"{target} wrote outside HOME: {path}"


def test_claude_user_scope_installs_skill_hooks_and_mcp(paths: Paths, home: Path) -> None:
    install(paths, "claude", home / "repo")

    assert (home / ".claude" / "skills" / "brain" / "SKILL.md").is_file()
    mcp = json.loads((home / ".claude.json").read_text())
    assert mcp["mcpServers"]["brain"]["args"] == ["-m", "brain.mcp_server"]

    settings = json.loads((home / ".claude" / "settings.json").read_text())
    events = set(settings["hooks"])
    assert {"UserPromptSubmit", "Stop"} <= events


def test_claude_hooks_are_executable(paths: Paths, home: Path) -> None:
    """A hook without +x is a hook the harness silently never runs."""
    install(paths, "claude", home / "repo")
    for name in ("brain-context.sh", "brain-capture.sh"):
        mode = (home / ".claude" / "hooks" / name).stat().st_mode
        assert mode & stat.S_IXUSR, f"{name} is not executable"


def test_pi_gets_no_mcp_config(paths: Paths, home: Path) -> None:
    """pi ships no MCP client. Writing one produces a file nothing ever reads.

    This is the regression guard for the failure the five-target split exists to
    prevent: config that looks installed, parses cleanly, and does nothing.
    """
    written = install(paths, "pi", home / "repo")
    assert not [p for p in written if "mcp" in Path(p).name.lower()]
    assert (home / ".pi" / "agent" / "skills" / "brain" / "SKILL.md").is_file()

    plan = adapters.plan(paths, "pi", home / "repo", scope="user")
    assert any("no MCP" in note for note in plan.notes)


def test_codex_user_scope_uses_toml_not_json(paths: Paths, home: Path) -> None:
    """Codex reads ~/.codex/config.toml. An earlier version wrote .codex/config.json,
    which Codex does not read at all."""
    install(paths, "codex", home / "repo")
    config = tomllib.loads((home / ".codex" / "config.toml").read_text())
    assert config["mcp_servers"]["brain"]["args"] == ["-m", "brain.mcp_server"]
    assert "BRAIN_STATE_DIR" in config["mcp_servers"]["brain"]["env"]


def test_omp_user_scope_writes_its_own_agent_dir(paths: Paths, home: Path) -> None:
    install(paths, "omp", home / "repo")
    config = json.loads((home / ".omp" / "agent" / "mcp.json").read_text())
    assert "brain" in config["mcpServers"]
    assert (home / ".omp" / "agent" / "skills" / "brain" / "SKILL.md").is_file()


def test_opencode_user_scope_lands_in_xdg_config(paths: Paths, home: Path) -> None:
    install(paths, "opencode", home / "repo")
    config = json.loads((home / ".config" / "opencode" / "opencode.json").read_text())
    # OpenCode's schema, not the generic one: `mcp`, argv array, `environment`.
    assert isinstance(config["mcp"]["brain"]["command"], list)
    assert "environment" in config["mcp"]["brain"]


# -- merging into files that belong to other tools ---------------------------------


def test_codex_merge_preserves_unrelated_tables(paths: Paths, home: Path) -> None:
    """config.toml holds trust levels and hook hashes. Losing them is expensive and
    invisible until the next time Codex asks about a directory."""
    config = home / ".codex"
    config.mkdir()
    (config / "config.toml").write_text(
        '[projects."/somewhere"]\ntrust_level = "trusted"\n\n'
        "[features]\nhooks = true\n\n"
        '[mcp_servers.other]\ncommand = "other-server"\n'
    )
    install(paths, "codex", home / "repo")

    merged = tomllib.loads((config / "config.toml").read_text())
    assert merged["projects"]["/somewhere"]["trust_level"] == "trusted"
    assert merged["features"]["hooks"] is True
    assert merged["mcp_servers"]["other"]["command"] == "other-server"
    assert "brain" in merged["mcp_servers"]


def test_claude_merge_preserves_other_hooks_and_settings(paths: Paths, home: Path) -> None:
    claude = home / ".claude"
    claude.mkdir()
    (claude / "settings.json").write_text(
        json.dumps(
            {
                "theme": "dark",
                "hooks": {
                    "SessionStart": [
                        {"matcher": "*", "hooks": [{"type": "command", "command": "other.sh"}]}
                    ]
                },
            }
        )
    )
    install(paths, "claude", home / "repo")

    settings = json.loads((claude / "settings.json").read_text())
    assert settings["theme"] == "dark"
    assert settings["hooks"]["SessionStart"][0]["hooks"][0]["command"] == "other.sh"
    assert "UserPromptSubmit" in settings["hooks"]


def test_a_merged_file_is_backed_up_once_not_every_run(paths: Paths, home: Path) -> None:
    """Backing up on every run overwrites the pre-brain copy with a post-brain one,
    which is the same as having no backup."""
    claude = home / ".claude"
    claude.mkdir()
    original = json.dumps({"theme": "dark"})
    (claude / "settings.json").write_text(original)

    install(paths, "claude", home / "repo")
    install(paths, "claude", home / "repo")

    backup = claude / "settings.json.pre-brain.bak"
    assert backup.is_file()
    assert backup.read_text() == original


def test_a_file_brain_created_is_never_backed_up_as_pre_brain(paths: Paths, home: Path) -> None:
    """Nothing preceded us, so there is no pre-brain state to save.

    The tempting rule — "back up when no backup exists" — writes one here on the
    second run, containing brain's own output under a name claiming otherwise.
    """
    install(paths, "claude", home / "repo")
    install(paths, "claude", home / "repo")
    install(paths, "claude", home / "repo")

    assert not list((home / ".claude").glob("*.pre-brain.bak"))
    assert not list(home.glob("*.pre-brain.bak"))


# -- idempotency -------------------------------------------------------------------


@pytest.mark.parametrize("target", adapters.TARGETS)
def test_installing_twice_is_the_same_as_installing_once(
    paths: Paths, home: Path, target: str
) -> None:
    """The second run is the one you do at 1am. It must not double anything."""
    repo = home / "repo"
    install(paths, target, repo)
    first = {p: Path(p).read_text() for p in install(paths, target, repo)}
    second = {p: Path(p).read_text() for p in install(paths, target, repo)}
    assert first == second


def test_claude_hooks_are_replaced_not_appended(paths: Paths, home: Path) -> None:
    """Matched by basename, so a moved repository rewrites the entry instead of
    leaving a stale one shadowing the new one."""
    install(paths, "claude", home / "repo")
    install(paths, "claude", home / "repo")

    settings = json.loads((home / ".claude" / "settings.json").read_text())
    commands = [
        h["command"] for matcher in settings["hooks"]["UserPromptSubmit"] for h in matcher["hooks"]
    ]
    assert len(commands) == 1


# -- refusals ----------------------------------------------------------------------


def test_an_unknown_scope_is_refused(paths: Paths, home: Path) -> None:
    with pytest.raises(ValueError, match="unknown adapter scope"):
        adapters.generate(paths, "claude", home, scope="global")


def test_malformed_existing_config_is_never_silently_replaced(paths: Paths, home: Path) -> None:
    """Overwriting a broken config is the data loss the merge exists to prevent."""
    (home / ".omp" / "agent").mkdir(parents=True)
    (home / ".omp" / "agent" / "mcp.json").write_text("{not json")
    with pytest.raises(ValueError, match="not valid JSON"):
        adapters.generate(paths, "omp", home, scope="user")

    (home / ".codex").mkdir()
    (home / ".codex" / "config.toml").write_text("[unclosed\n")
    with pytest.raises(ValueError, match="not valid TOML"):
        adapters.generate(paths, "codex", home, scope="user")

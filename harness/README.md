# harness/

Files that get **copied** into a harness's configuration directory, verbatim.

Everything here is committed source. Nothing here is generated at install time, and
that distinction is the point.

`src/brain/adapters.py` *generates* instruction files, and generated content is held
to `assert_pointers_only`: a generator that can put retrieved or agent-proposed text
into `CLAUDE.md` has rebuilt the auto-injection path the whole design rejects. The
check exists because the generator is the thing that might change.

These files are the other half of that rule. They carry prose a generator has no
business inventing — how to use the store, when to consult it, what a `CONTESTED`
result means — so they reach an instruction file the only way such content is allowed
to: through a reviewed commit. The installer copies bytes; it does not compose them.

| File | Installed as | Read by |
|---|---|---|
| `SKILL.md` | `<harness>/skills/brain/SKILL.md` | Claude Code, omp, pi, Codex |
| `hooks/brain-context.sh` | `~/.claude/hooks/brain-context.sh` | Claude Code `UserPromptSubmit` |
| `hooks/brain-capture.sh` | `~/.claude/hooks/brain-capture.sh` | Claude Code `Stop` |

## The hooks resolve `brain` from `PATH`

Both scripts take `brain` from `command -v`, never from a project virtualenv. An
earlier version named `~/Projects/brain/.venv/bin/brain` directly, and recreating that
venv broke every hook at once — silently, because the hooks fail open by design. A
memory system that stops working quietly is worse than one that stops working loudly.

`BRAIN_BIN` overrides, for the case where `PATH` is not what the harness sees.

## They fail open, always

If `brain` is missing, slow, or broken, both hooks exit `0` and say nothing. This is
deliberate and it is not negotiable: a memory system that blocks your editor is one
you will turn off within a week, and a turned-off memory system remembers nothing.

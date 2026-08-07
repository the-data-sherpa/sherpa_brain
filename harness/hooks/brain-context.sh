#!/usr/bin/env bash
# UserPromptSubmit: surface POINTERS to related memories — never their content.
#
# Content stays out on purpose. The design this store is built on rejects
# auto-injection (agent pulls via a visible tool call; the prefix is not mutated
# per turn; memory used in an answer must be visible in that answer). So the hook
# guarantees you always LOOK; reading is still a brain.search / brain.get call.
#
# Silent when nothing matches. A hook that speaks every turn trains you to ignore it.
set -uo pipefail

# Resolved from PATH, not from a project venv. A hook that names
# ~/Projects/brain/.venv dies the moment that venv is recreated, and it dies
# silently — which is the worst way for a memory system to stop working.
BRAIN="${BRAIN_BIN:-$(command -v brain 2>/dev/null)}"
[ -n "$BRAIN" ] && [ -x "$BRAIN" ] || exit 0

prompt=$(jq -r '.prompt // empty' 2>/dev/null) || exit 0
[ -z "$prompt" ] && exit 0

# Never let memory retrieval block or break a session.
out=$(timeout 5 "$BRAIN" context "$prompt" --hook 2>/dev/null) || exit 0
[ -z "$out" ] && exit 0

jq -n --arg ctx "$out" \
  '{hookSpecificOutput:{hookEventName:"UserPromptSubmit",additionalContext:$ctx}}'

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

# Run a command under a time limit, on any of the systems this ships to.
#
# `timeout` is GNU coreutils and is NOT on a stock macOS. Calling it there fails
# with 127, the hook treats that as "brain is unavailable", and — because these
# hooks fail open by design — consultation silently never happens. It looks
# exactly like a store with nothing to say. That is how this was found: macOS CI
# installed cleanly, wrote and searched a memory, and the hook returned nothing.
#
# Homebrew coreutils installs it as `gtimeout`. Failing both, perl ships with
# macOS and `alarm` does the same job. Running unbounded is the last resort: a
# bound that does not exist is better than a prompt that hangs, but only just.
bounded() {
  local secs=$1; shift
  if command -v timeout >/dev/null 2>&1; then
    timeout "$secs" "$@"
  elif command -v gtimeout >/dev/null 2>&1; then
    gtimeout "$secs" "$@"
  elif command -v perl >/dev/null 2>&1; then
    perl -e 'my $s = shift; alarm $s; exec @ARGV or exit 127' "$secs" "$@"
  else
    "$@"
  fi
}

prompt=$(jq -r '.prompt // empty' 2>/dev/null) || exit 0
[ -z "$prompt" ] && exit 0

# Never let memory retrieval block or break a session.
out=$(bounded 5 "$BRAIN" context "$prompt" --hook 2>/dev/null) || exit 0
[ -z "$out" ] && exit 0

jq -n --arg ctx "$out" \
  '{hookSpecificOutput:{hookEventName:"UserPromptSubmit",additionalContext:$ctx}}'

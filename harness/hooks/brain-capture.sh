#!/usr/bin/env bash
# Stop: ask once per session whether anything learned is worth keeping.
#
# Fires only when the working tree changed AND nothing was written to the brain.
# Suppressed for the rest of the session after firing once, so it can never loop:
# "I have nothing to record" is a legitimate answer and must be able to end the turn.
set -uo pipefail

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

session=$(jq -r '.session_id // "unknown"' 2>/dev/null) || exit 0
marker="${TMPDIR:-/tmp}/brain-capture-${session}"
[ -f "$marker" ] && exit 0

out=$(bounded 10 "$BRAIN" capture-check --hook 2>/dev/null) || exit 0
[ -z "$out" ] && exit 0

: > "$marker"
jq -n --arg r "$out" '{decision:"block", reason:$r}'

#!/usr/bin/env bash
# Stop: ask once per session whether anything learned is worth keeping.
#
# Fires only when the working tree changed AND nothing was written to the brain.
# Suppressed for the rest of the session after firing once, so it can never loop:
# "I have nothing to record" is a legitimate answer and must be able to end the turn.
set -uo pipefail

BRAIN="${BRAIN_BIN:-$(command -v brain 2>/dev/null)}"
[ -n "$BRAIN" ] && [ -x "$BRAIN" ] || exit 0

session=$(jq -r '.session_id // "unknown"' 2>/dev/null) || exit 0
marker="${TMPDIR:-/tmp}/brain-capture-${session}"
[ -f "$marker" ] && exit 0

out=$(timeout 10 "$BRAIN" capture-check --hook 2>/dev/null) || exit 0
[ -z "$out" ] && exit 0

: > "$marker"
jq -n --arg r "$out" '{decision:"block", reason:$r}'

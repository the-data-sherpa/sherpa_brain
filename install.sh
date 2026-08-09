#!/usr/bin/env bash
# Bootstrap a brain store on a fresh system.
#
# Idempotent by construction: every step either checks first or is safe to repeat,
# because the second run is the one you do at 1am when something is wrong, and an
# installer you are afraid to re-run is an installer you will not run.
#
# What it deliberately does NOT do:
#
#   - create the off-device ledger repository. That is outward-facing and account-
#     specific; it prints the `gh` command and takes the URL you already own.
#   - enable the timers. Writing a unit file and scheduling it are different acts,
#     and the second one deserves your explicit consent.
#   - touch anything outside $HOME. No sudo, no system units, no package installs.
#
# Usage:  ./install.sh [--harness a,b,c] [--ledger-remote URL] [--scope user|repo]
#                      [--state DIR] [--dry-run] [--skip-timers]

set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HARNESSES=""
LEDGER_REMOTE=""
SCOPE="user"
STATE_DIR=""
DRY_RUN=0
SKIP_TIMERS=0

ALL_HARNESSES="claude codex opencode pi omp"

# -- output ------------------------------------------------------------------------

if [ -t 1 ]; then
  BOLD=$'\033[1m'; DIM=$'\033[2m'; RED=$'\033[31m'; YEL=$'\033[33m'; GRN=$'\033[32m'; OFF=$'\033[0m'
else
  BOLD=""; DIM=""; RED=""; YEL=""; GRN=""; OFF=""
fi

step() { printf '\n%s==>%s %s%s%s\n' "$GRN" "$OFF" "$BOLD" "$*" "$OFF"; }
info() { printf '    %s\n' "$*"; }
note() { printf '    %s%s%s\n' "$DIM" "$*" "$OFF"; }
warn() { printf '%swarning:%s %s\n' "$YEL" "$OFF" "$*" >&2; }
die()  { printf '%serror:%s %s\n' "$RED" "$OFF" "$*" >&2; exit 1; }

run() {
  if [ "$DRY_RUN" -eq 1 ]; then
    printf '    %s$ %s%s\n' "$DIM" "$*" "$OFF"
  else
    "$@"
  fi
}

# -- arguments ---------------------------------------------------------------------

usage() {
  sed -n '2,20p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
  exit "${1:-0}"
}

while [ $# -gt 0 ]; do
  case "$1" in
    --harness)       HARNESSES="${2:?--harness needs a value}"; shift 2 ;;
    --harness=*)     HARNESSES="${1#*=}"; shift ;;
    --ledger-remote) LEDGER_REMOTE="${2:?--ledger-remote needs a URL}"; shift 2 ;;
    --ledger-remote=*) LEDGER_REMOTE="${1#*=}"; shift ;;
    --scope)         SCOPE="${2:?--scope needs a value}"; shift 2 ;;
    --scope=*)       SCOPE="${1#*=}"; shift ;;
    --state)         STATE_DIR="${2:?--state needs a directory}"; shift 2 ;;
    --state=*)       STATE_DIR="${1#*=}"; shift ;;
    --dry-run)       DRY_RUN=1; shift ;;
    --skip-timers)   SKIP_TIMERS=1; shift ;;
    -h|--help)       usage 0 ;;
    *)               printf '%serror:%s unknown option %s\n' "$RED" "$OFF" "$1" >&2; usage 1 ;;
  esac
done

case "$SCOPE" in
  user|repo) ;;
  *) die "--scope must be 'user' or 'repo', not '$SCOPE'" ;;
esac

[ -n "$STATE_DIR" ] && export BRAIN_STATE_DIR="$STATE_DIR"

# -- 0. platform -------------------------------------------------------------------

step "Checking the platform"

OS="$(uname -s)"
case "$OS" in
  Linux)  info "Linux — systemd --user timers, renameat2(RENAME_EXCHANGE)" ;;
  Darwin) info "macOS — LaunchAgents, renamex_np(RENAME_SWAP), F_FULLFSYNC" ;;
  *) die "$OS is not supported. The write protocol needs an atomic path-exchange
       primitive, which brain implements for Linux and macOS only." ;;
esac

# -- 1. prerequisites --------------------------------------------------------------

step "Checking prerequisites"

missing=""
need() {
  if command -v "$1" >/dev/null 2>&1; then
    info "$1 ${DIM}$(command -v "$1")${OFF}"
  else
    missing="$missing $1"
    printf '    %s%s — missing: %s%s\n' "$RED" "$1" "$2" "$OFF"
  fi
}

need git "the ledger is a git repository"
need rg  "ripgrep is the rung-0 search backend"
need jq  "the Claude Code hooks parse their input with it"
need uv  "installs the CLI into an isolated tool environment"

if [ -n "$missing" ]; then
  printf '\n'
  case "$OS" in
    Darwin) die "install the missing tools, e.g.:  brew install$missing" ;;
    *)
      if command -v pacman >/dev/null 2>&1; then
        die "install the missing tools, e.g.:  sudo pacman -S$missing"
      elif command -v apt-get >/dev/null 2>&1; then
        die "install the missing tools, e.g.:  sudo apt-get install$missing"
      else
        die "install the missing tools:$missing"
      fi ;;
  esac
fi

# Python is checked for version, not just presence: 3.12 is a hard floor and the
# failure without it is an obscure syntax error deep in an import.
PY="$(command -v python3 || true)"
[ -n "$PY" ] || die "python3 not found. brain requires Python 3.12 or newer."
if ! "$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)'; then
  die "python3 is $("$PY" -V 2>&1 | cut -d' ' -f2); brain requires 3.12 or newer.
       uv can supply one:  uv python install 3.12"
fi
info "python3 ${DIM}$("$PY" -V 2>&1 | cut -d' ' -f2)${OFF}"

# -- 2. the CLI --------------------------------------------------------------------

step "Installing the brain CLI"

# A *tool* install, not a project venv. The distinction is the whole reason this step
# exists: every harness config generated below records an absolute interpreter path,
# and an earlier setup recorded the project venv's. Recreating that venv broke every
# harness at once, silently, because the hooks fail open. `uv tool` keeps its own
# environment, so `uv sync` in the checkout cannot break it.
#
# Not `--editable`, which this used to pass. Editable fixes the interpreter half and
# leaves the source half: the tool env still reads code from $REPO, so moving,
# renaming, or deleting the checkout takes out the CLI and every scheduler unit with
# it. For a store whose stated purpose is outliving things, the memory system should
# not depend on a working tree. A non-editable install copies the code in — including
# the harness assets, which is what `adapters.harness_dir()`'s wheel branch is for.
#
# The cost is real and worth stating: developing brain no longer updates the installed
# brain. Re-run this script (or `uv tool install . --force`) to pick up your changes.
run uv tool install "$REPO" --force

TOOL_BIN="${UV_TOOL_BIN_DIR:-$HOME/.local/bin}"
if [ "$DRY_RUN" -eq 0 ]; then
  command -v brain >/dev/null 2>&1 || {
    warn "brain is installed at $TOOL_BIN but not on your PATH."
    warn "The generated instruction files name \`brain\`, and hooks fail open, so"
    warn "this would look like nothing happening rather than an error. Add:"
    printf '\n      export PATH="%s:$PATH"\n\n' "$TOOL_BIN"
    die "add $TOOL_BIN to PATH and re-run"
  }
  info "brain ${DIM}$(command -v brain)${OFF}"
fi

BRAIN="${BRAIN:-brain}"

# -- 3. the store ------------------------------------------------------------------

step "Initializing the store"

# `brain init` probes the filesystem and refuses network filesystems and sync
# folders. Re-running it on an existing store is a no-op that re-verifies the probe,
# which is exactly what you want from an installer you ran twice.
run "$BRAIN" init

# -- 4. the off-device replica -----------------------------------------------------

step "Configuring the off-device replica"

if [ -n "$LEDGER_REMOTE" ]; then
  run "$BRAIN" ledger init --remote "$LEDGER_REMOTE"
  if [ "$DRY_RUN" -eq 0 ]; then
    "$BRAIN" ledger status >/dev/null 2>&1 || warn \
      "ledger status is not clean. It exits 3 when it cannot verify the remote
       rejects force-push and branch deletion — a rewritable history is not an
       anchor, so acks from it are refused at quorum time. Protect the branch."
  fi
else
  note "skipped — no --ledger-remote given."
  note ""
  note "Until a second replica exists, quorum is unreachable and every deletion"
  note "reports 'pending' forever. The content is unreachable; the deletion is"
  note "simply not *complete*. Before you delete anything:"
  note ""
  note "    gh repo create brain-ledger --private"
  note "    $BRAIN ledger init --remote git@github.com:<you>/brain-ledger.git"
  note ""
  note "The ledger holds tombstones only — sequence numbers, ids, hashes, times."
fi

# -- 5. scheduling -----------------------------------------------------------------

if [ "$SKIP_TIMERS" -eq 1 ]; then
  step "Scheduling"
  note "skipped — --skip-timers."
else
  step "Writing the scheduler units"
  run "$BRAIN" install-timers
  note "Written, not scheduled. The commands to activate them are printed above."
fi

# -- 6. harness wiring -------------------------------------------------------------

step "Wiring harnesses"

if [ -z "$HARNESSES" ]; then
  # Detect rather than install everything: writing config for a harness that is not
  # present leaves files nobody reads, which is indistinguishable from a bug the
  # next time someone looks.
  detected=""
  for h in $ALL_HARNESSES; do
    case "$h" in
      omp) probe="omp" ;;
      *)   probe="$h" ;;
    esac
    if command -v "$probe" >/dev/null 2>&1; then
      detected="$detected $h"
    fi
  done
  HARNESSES="$(echo "$detected" | tr -s ' ' ',' | sed 's/^,//;s/,$//')"
  if [ -z "$HARNESSES" ]; then
    note "no supported harness found on PATH ($ALL_HARNESSES)."
    note "Nothing to wire. Re-run with --harness <name> to force one."
  else
    info "detected:${DIM} $(echo "$HARNESSES" | tr ',' ' ')${OFF}"
  fi
fi

IFS=',' read -r -a wanted <<< "$HARNESSES"
for h in "${wanted[@]}"; do
  [ -n "$h" ] || continue
  case " $ALL_HARNESSES " in
    *" $h "*) ;;
    *) die "unknown harness '$h' (expected one of: $ALL_HARNESSES)" ;;
  esac
  info "$h (--scope $SCOPE)"
  if [ "$DRY_RUN" -eq 1 ]; then
    run "$BRAIN" adapter "$h" --scope "$SCOPE" --dry-run
  else
    "$BRAIN" adapter "$h" --scope "$SCOPE" >/dev/null
  fi
done

# -- 7. verify ---------------------------------------------------------------------

step "Verifying"

if [ "$DRY_RUN" -eq 1 ]; then
  note "skipped — dry run."
  exit 0
fi

set +e
"$BRAIN" doctor >/dev/null 2>&1
doctor_exit=$?
set -e

case "$doctor_exit" in
  0) info "${GRN}brain doctor: clean${OFF}" ;;
  3) warn "brain doctor exits 3 — warnings, not breakage. On a fresh install the"
     warn "expected ones are: no replica, no backup yet, empty store. Run"
     warn "\`brain doctor\` to read them." ;;
  *) warn "brain doctor exits $doctor_exit — a safety property is not holding."
     warn "Run \`brain doctor\` and see RUNBOOK section 3 before writing anything." ;;
esac

printf '\n%sInstalled.%s Next:\n\n' "$BOLD" "$OFF"
printf '    %s doctor          # every quiet failure mode in one place\n' "$BRAIN"
printf '    %s remember "..."  # nothing is written unless you write it\n' "$BRAIN"
printf '\n%sdocs/RUNBOOK.md%s covers operation, incidents, and the recovery drill.\n\n' "$DIM" "$OFF"

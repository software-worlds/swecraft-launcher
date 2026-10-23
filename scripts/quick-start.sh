#!/usr/bin/env bash
# Guided first run: the tutorial's path, one confirmed step at a time.
# `--yes` runs without prompts. Rerunning after a stop continues safely:
# every step is idempotent or refuses cleanly when already done.
set -euo pipefail
cd "$(dirname "$0")/.."

ASSUME_YES=0
for argument in "$@"; do
  case "$argument" in
    --yes | -y) ASSUME_YES=1 ;;
    --help | -h)
      echo "usage: just quick-start [--yes]"
      echo "Walks from an empty checkout to a running society, confirming"
      echo "each step. --yes skips the confirmations. The same path, by"
      echo "hand and explained: docs/tutorials/first-society.md"
      exit 0
      ;;
    *)
      echo "unknown argument: $argument (try --help)" >&2
      exit 2
      ;;
  esac
done

announce() { printf '\n\033[1m== %s\033[0m\n' "$1"; }

confirm() {
  [ "$ASSUME_YES" -eq 1 ] && return 0
  local reply
  read -r -p "   proceed? [Y/n] " reply
  case "$reply" in
    n* | N*)
      echo "   stopped — rerun 'just quick-start' to continue from here."
      exit 0
      ;;
  esac
}

step() { # step <description> <command...>
  local description=$1
  shift
  announce "$description"
  printf '   $ %s\n' "$*"
  confirm
  "$@"
}

announce "checking prerequisites"
missing=0
for tool in docker just uv; do
  if ! command -v "$tool" >/dev/null; then
    echo "   missing: $tool" >&2
    missing=1
  fi
done
[ "$missing" -eq 0 ] || exit 1
echo "   docker, just, uv: found"

step "start the forge (Forgejo on http://127.0.0.1:3400)" just forge-up

announce "create a world"
echo "   This creates the swecraft organization and an empty swecraft/demo repository."
echo "   For a seeded multi-repository world instead, stop here and follow"
echo "   docs/tutorials/first-society.md ('Reproducible' path)."
printf '   $ %s\n' "just forge-bootstrap demo"
confirm
just forge-bootstrap demo

step "build the task image the agents run in" just runner-image-build

announce "coordinator configuration"
if [ -f coordinator.yaml ]; then
  echo "   coordinator.yaml already exists; leaving it as is."
else
  printf '   $ %s\n' "cp examples/coordinator.yaml coordinator.yaml"
  confirm
  cp examples/coordinator.yaml coordinator.yaml
  echo "   Review coordinator.yaml: one agent per repository, and agent_env"
  echo "   must forward your model credential."
fi

announce "checking the model credentials coordinator.yaml expects"
unset_names=""
while IFS= read -r name; do
  if [ -z "${!name:-}" ]; then
    unset_names="$unset_names $name"
  fi
done < <(grep -o 'env:[A-Za-z_][A-Za-z0-9_]*' coordinator.yaml | cut -d: -f2 | sort -u)
if [ -n "$unset_names" ]; then
  echo "   these variables are referenced by coordinator.yaml but not set:" >&2
  echo "    $unset_names" >&2
  echo "   export them and rerun 'just quick-start'." >&2
  exit 1
fi
echo "   all env: references are set"

agent=$(grep -m1 -E '^[[:space:]]*-[[:space:]]*name:' coordinator.yaml | sed -E 's/.*name:[[:space:]]*//; s/[[:space:]]*#.*//')
if [ -z "$agent" ]; then
  echo "   could not find an agent name in coordinator.yaml" >&2
  exit 1
fi
announce "onboarding '$agent' (forge identity, scoped write access, memory branch)"
if [ -f charter.md ]; then
  printf '   $ %s\n' "uv run --project services/coordinator swecraft-coordinator onboard $agent --goal-file charter.md"
  confirm
  uv run --project services/coordinator swecraft-coordinator onboard "$agent" --goal-file charter.md
else
  echo "   (no charter.md found — GOAL.md gets a placeholder; edit it on the"
  echo "   agent/memory branch at any time)"
  printf '   $ %s\n' "uv run --project services/coordinator swecraft-coordinator onboard $agent"
  confirm
  uv run --project services/coordinator swecraft-coordinator onboard "$agent"
fi

announce "bootstrap: one sandboxed survey turn (costs one model turn)"
printf '   $ %s\n' "uv run --project services/coordinator swecraft-coordinator bootstrap $agent"
confirm
uv run --project services/coordinator swecraft-coordinator bootstrap "$agent"

if [ "$ASSUME_YES" -eq 0 ]; then
  announce "optional: one test wake before the loop (costs one model turn)"
  read -r -p "   run a single wake now? [y/N] " reply
  case "$reply" in
    y* | Y*) uv run --project services/coordinator swecraft-coordinator wake "$agent" ;;
  esac
fi

announce "done — the society is ready"
echo "   start the loop:    just coordinator-serve"
echo "   agent status:      uv run --project services/coordinator swecraft-coordinator agents"
echo "   watch the forge:   http://127.0.0.1:3400"
echo "   the full guide:    docs/tutorials/first-society.md"

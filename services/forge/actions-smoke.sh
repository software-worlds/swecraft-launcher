#!/usr/bin/env bash
# Verify Forgejo Actions end to end: push a trivial workflow and wait for a
# green run. Ported from the PR #3 spike, with two additions: the repository's
# Actions unit is enabled explicitly (repositories created before Actions was
# turned on keep the unit off and silently ignore workflows), and every
# invocation triggers a fresh run and waits for a task newer than the ones
# that existed before the push — a re-run must never pass on a previous run's
# green status.
set -euo pipefail

here=$(cd "$(dirname "$0")" && pwd)
state="$here/state"
port=${SWECRAFT_FORGE_PORT:-3400}
host=${SWECRAFT_FORGE_HOST:-127.0.0.1}
base="http://$host:$port"
org=${SWECRAFT_FORGE_ORG:-swecraft}
repository=${SWECRAFT_FORGE_REPOSITORY:-actions-demo}
admin_token=$(<"$state/admin_token")

api() {
  curl --fail-with-body --silent --show-error \
    -H "Authorization: token $admin_token" \
    "$base/api/v1$1"
}

newest_task_id() {
  api "/repos/$org/$repository/actions/tasks" | python3 -c '
import json, sys
tasks = json.load(sys.stdin).get("workflow_runs") or []
print(max((task.get("id", 0) for task in tasks), default=0))
'
}

curl --fail-with-body --silent --show-error -X PATCH \
  -H "Authorization: token $admin_token" \
  -H "Content-Type: application/json" \
  -d '{"has_actions": true}' \
  "$base/api/v1/repos/$org/$repository" >/dev/null

baseline=$(newest_task_id)

workdir=$(mktemp -d)
trap 'rm -rf "$workdir"' EXIT

git clone --quiet "http://swecraft-admin:$admin_token@$host:$port/$org/$repository.git" \
  "$workdir/repo"
mkdir -p "$workdir/repo/.forgejo/workflows"
cat > "$workdir/repo/.forgejo/workflows/hello.yml" <<'EOF'
name: hello
on: [push]
jobs:
  hello:
    runs-on: docker
    steps:
      - run: echo "hello from forgejo actions"
EOF
git -C "$workdir/repo" -c user.name="Hub Smoke" -c user.email="smoke@localhost" \
  add .forgejo/workflows/hello.yml
# Always create a commit: on re-runs the workflow file is unchanged, and
# without a fresh push there is no new run to observe.
git -C "$workdir/repo" -c user.name="Hub Smoke" -c user.email="smoke@localhost" \
  commit --quiet --allow-empty -m "ci: trigger actions smoke"
git -C "$workdir/repo" push --quiet origin HEAD:main

deadline=$(($(date +%s) + 180))
while :; do
  # Transient API hiccups poll again instead of killing the script.
  status=$(api "/repos/$org/$repository/actions/tasks" | python3 -c '
import json, sys
baseline = int(sys.argv[1])
tasks = json.load(sys.stdin).get("workflow_runs") or []
fresh = [task for task in tasks if task.get("id", 0) > baseline]
fresh.sort(key=lambda task: task.get("id", 0), reverse=True)
print(fresh[0].get("status", "unknown") if fresh else "pending")
' "$baseline" 2>/dev/null || echo pending)
  case "$status" in
    success)
      echo "Actions smoke passed: $base/$org/$repository"
      exit 0
      ;;
    failure|cancelled|skipped)
      echo "Actions smoke failed with status: $status" >&2
      echo "inspect: $base/$org/$repository/actions" >&2
      exit 1
      ;;
  esac
  if (($(date +%s) > deadline)); then
    echo "Actions smoke timed out; last status: $status" >&2
    echo "inspect: $base/$org/$repository/actions" >&2
    exit 1
  fi
  sleep 5
done

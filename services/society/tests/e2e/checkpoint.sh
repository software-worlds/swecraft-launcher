#!/usr/bin/env bash
# Live acceptance for checkpoint -> local tar -> pure load -> resume.
set -euo pipefail

hub=$(cd "$(dirname "$0")/../../../.." && pwd)
port=${SWECRAFT_SOCIETY_E2E_PORT:-3477}
copy_port=$((port + 1))
project=${SWECRAFT_SOCIETY_E2E_PROJECT:-swecraft-society-checkpoint-e2e}
copy_project="$project-copy"
org=e2e-world
agent=e2e-agent
work=$(mktemp -d "${TMPDIR:-/tmp}/swecraft-society-checkpoint-e2e.XXXXXX")
restored="$work/restored"

say() { printf '\n== %s\n' "$*"; }
fail() { echo "FAIL: $*" >&2; exit 1; }

cleanup() {
  say "cleaning up"
  docker compose -p "$project" -f "$work/forge/compose.yaml" down -v \
    --remove-orphans >/dev/null 2>&1 || true
  if [[ -d "$restored/state/forge/host" ]]; then
    SWECRAFT_FORGE_STATE_DIR="$restored/state/forge/host" \
      SWECRAFT_FORGE_PORT="$copy_port" \
      docker compose -p "$copy_project" -f "$hub/services/forge/compose.yaml" \
      down -v --remove-orphans >/dev/null 2>&1 || true
  fi
  docker volume rm "${copy_project}_forgejo-data" >/dev/null 2>&1 || true
  rm -rf "$work"
}
trap cleanup EXIT

git_quiet() {
  git -c user.name=e2e -c user.email=e2e@localhost "$@" >/dev/null
}

api_on() {
  local base=$1 token=$2 method=$3 path=$4 body=${5:-}
  local args=(
    --fail-with-body --silent --show-error -X "$method"
    -H "Authorization: token $token" -H "Content-Type: application/json"
  )
  [[ -n "$body" ]] && args+=(-d "$body")
  curl "${args[@]}" "$base/api/v1$path"
}

status_on() {
  local base=$1 token=$2 path=$3
  curl --silent --output /dev/null --write-out '%{http_code}' \
    -H "Authorization: token $token" "$base/api/v1$path"
}

export COMPOSE_PROJECT_NAME="$project"
export SWECRAFT_FORGE_PORT="$port"
base="http://127.0.0.1:$port"
copy_base="http://127.0.0.1:$copy_port"

say "1/7 starting an isolated Forge and seeding a one-repository world"
cp -R "$hub/services/forge" "$work/forge"
find "$work/forge/state" -type f ! -name .gitkeep -delete
"$work/forge/prepare.sh"
docker compose -p "$project" -f "$work/forge/compose.yaml" up -d --wait
SWECRAFT_FORGE_ORG=$org "$work/forge/bootstrap.sh" --no-repositories
admin_token=$(<"$work/forge/state/admin_token")
forge_image=$(awk '/^[[:space:]]+image: codeberg.org\/forgejo\/forgejo:/{print $2; exit}' "$work/forge/compose.yaml")
[[ -n "$forge_image" ]] || fail "could not read pinned Forge image"

git_quiet init -b main "$work/upstream"
echo seed > "$work/upstream/README.md"
mkdir -p "$work/upstream/.github/workflows"
echo 'name: ci' > "$work/upstream/.github/workflows/ci.yml"
git_quiet -C "$work/upstream" add -A
git_quiet -C "$work/upstream" commit -m seed
git_quiet -C "$work/upstream" tag v1

society="$work/society"
mkdir -p "$society"
cat > "$society/world.yaml" <<MANIFEST
schema_version: 1
world_id: e2e
org: $org
repos:
  - name: demo
    upstream: $work/upstream
    pin: {type: tag, value: v1}
MANIFEST
(
  cd "$society"
  uv run --project "$hub/services/world" --frozen swecraft-world \
    --manifest world.yaml --forge-url "$base" \
    --token-file "$work/forge/state/admin_token" seed
)
baseline=$(python3 -c "import json,sys;print(json.load(open(sys.argv[1]))['repos'][0]['baseline_commit'])" "$society/world.lock.json")

say "2/7 creating social, runtime, evaluator, and apparatus state"
api_on "$base" "$admin_token" POST /admin/users \
  "{\"username\":\"$agent\",\"email\":\"$agent@localhost\",\"password\":\"e2e-password-1\",\"must_change_password\":false}" >/dev/null
api_on "$base" "$admin_token" PUT "/repos/$org/demo/collaborators/$agent" \
  '{"permission":"write"}' >/dev/null
api_on "$base" "$admin_token" POST "/repos/$org/demo/issues" \
  '{"title":"e2e issue","body":"created before checkpoint"}' >/dev/null
old_runner_uuid=$(api_on "$base" "$admin_token" GET /admin/actions/runners \
  | python3 -c "import json,sys;d=json.load(sys.stdin);e=d.get('entries') if isinstance(d,dict) else d;print((e or [{}])[0].get('uuid',''))")
[[ -n "$old_runner_uuid" ]] || fail "source society has no runner"
old_agent_token=$(curl --fail --silent --show-error -X POST \
  -u "$agent:e2e-password-1" -H 'Content-Type: application/json' \
  -d '{"name":"swecraft-coordinator","scopes":["write:repository","write:issue","read:notification","write:notification","read:user"]}' \
  "$base/api/v1/users/$agent/tokens" \
  | python3 -c "import json,sys;print(json.load(sys.stdin)['sha1'])")

git_quiet clone "http://swecraft-admin:$admin_token@127.0.0.1:$port/$org/demo.git" "$work/clone" 2>/dev/null
echo evolved >> "$work/clone/README.md"
git_quiet -C "$work/clone" add -A
git_quiet -C "$work/clone" commit -m 'society work'
git_quiet -C "$work/clone" push origin main 2>/dev/null

mkdir -p "$society/.swecraft/runs/run-e2e/input" \
  "$society/.swecraft/runs/run-e2e/output" \
  "$society/.swecraft/runs/run-e2e/harbor" \
  "$society/.swecraft/coordinator/credentials" \
  "$society/.swecraft/evaluator/records/eval-e2e" \
  "$society/charters" "$society/images/agent"
echo '{"agent":"claude-code","model":"test/model"}' \
  > "$society/.swecraft/runs/run-e2e/input/request.json"
echo 'E2E wake prompt.' > "$society/.swecraft/runs/run-e2e/input/prompt.md"
echo '{"run_id":"run-e2e","status":"completed"}' \
  > "$society/.swecraft/runs/run-e2e/output/result.json"
echo 'E2E wake completed.' > "$society/.swecraft/runs/run-e2e/output/response.md"
echo '{}' > "$society/.swecraft/runs/run-e2e/harbor/result.json"
printf '%s\n' "$old_agent_token" > "$society/.swecraft/coordinator/credentials/$agent.token"
# A record directory containing record.json is a complete measurement;
# the loop-state cursors ride along in state.json.
echo '{"id":"eval-e2e","evaluation":"e2e"}' \
  > "$society/.swecraft/evaluator/records/eval-e2e/record.json"
echo '{"releases":{}}' > "$society/.swecraft/evaluator/state.json"
echo 'hub-read-api-token' > "$society/.swecraft/api_token"
SOCIETY_PATH="$society" AGENT_NAME="$agent" \
  uv run --project "$hub/services/coordinator" --frozen python - <<'PY'
import os
from pathlib import Path
from swecraft_coordinator.state import StateStore

root = Path(os.environ["SOCIETY_PATH"])
agent = os.environ["AGENT_NAME"]
state = StateStore(root / ".swecraft/coordinator.db")
state.ensure_agent(agent)
wake = state.claim(agent, stale_after_seconds=3600)
state.finish(
    agent,
    wake,
    run_id="run-e2e",
    outcome="worked",
    summary="captured before checkpoint",
    cost_usd=0.0,
    next_due_seconds=3600,
)
PY
cat > "$society/coordinator.yaml" <<CONFIG
forge_url: $base
admin_token: file:$work/forge/state/admin_token
api_token: file:.swecraft/api_token
model: e2e-model
image: $forge_image
agent_env:
  CLAUDE_CODE_OAUTH_TOKEN: env:CLAUDE_CODE_OAUTH_TOKEN
agents:
  - name: $agent
    repo: $org/demo
CONFIG
cat > "$society/evaluation.yaml" <<EVALUATION
world_lock: world.lock.json
subjects:
  demo:
    install:
      - argv: ["true"]
evaluators:
  consumer:
    image: "$forge_image"
    subjects: [demo]
    gate: {argv: ["true"]}
    tasks:
      request:
        command: {argv: ["true"]}
        metrics: [walltime]
evaluations:
  e2e:
    tasks: [consumer@request]
    metric: walltime
EVALUATION
echo '# Charter' > "$society/charters/$agent.md"
echo "FROM $forge_image" > "$society/images/agent/Dockerfile"
(
  cd "$society"
  uv run --project "$hub/services/evaluator" --frozen swecraft-evaluator validate
)
mkdir -p "$society/.swecraft"
cat > "$society/.swecraft/checkpoint.yaml" <<CHECKPOINT
schema_version: 1
checkpoint:
  compose_project: $project
  # The default omits the raw Harbor archive; this e2e keeps exercising
  # the full include path end-to-end (pack, verify, load, resume).
  harbor: include
  forge_compose_file: $work/forge/compose.yaml
  forge_state_dir: $work/forge/state
load:
  output_dir: .swecraft/loaded
resume:
  compose_project_prefix: hub-e2e-copy
CHECKPOINT

say "3/7 checkpointing, inspecting the local tar, and loading pure files"
(
  cd "$society"
  uv run --project "$hub/services/society" --frozen swecraft-society checkpoint \
    --output "$work/checkpoint" --archive "$work/checkpoint.society.tar.gz"
  uv run --project "$hub/services/society" --frozen swecraft-society inspect \
    "$work/checkpoint.society.tar.gz"
  uv run --project "$hub/services/society" --frozen swecraft-society load \
    "$work/checkpoint.society.tar.gz" "$restored"
)
old_admin_token=$(<"$restored/state/forge/host/admin_token")

say "4/7 verifying baseline/current identity and pure loaded state"
manifest_head=$(python3 -c "import json,sys;print(json.load(open(sys.argv[1]))['heads'][0]['current_head'])" "$restored/manifest.json")
manifest_baseline=$(python3 -c "import json,sys;print(json.load(open(sys.argv[1]))['heads'][0]['baseline_commit'])" "$restored/manifest.json")
[[ "$manifest_baseline" == "$baseline" ]] || fail "baseline drifted from world lock"
[[ "$manifest_head" != "$manifest_baseline" ]] || fail "society head never evolved"
[[ -s "$restored/apparatus/evaluation.yaml" ]] || fail "evaluation manifest missing"
[[ -s "$restored/state/coordinator/api_token" ]] || fail "Hub API token missing"
[[ -s "$restored/evaluations/eval-e2e/record.json" ]] || fail "evaluation record missing"
[[ -s "$restored/state/evaluator/state.json" ]] || fail "evaluator loop state missing"
[[ -s "$restored/runs/run-e2e/harbor.tar.gz" ]] || fail "Harbor archive missing"
[[ -s "$restored/runs/run-e2e/output/result.json" ]] || fail "run receipt missing"
[[ ! -e "$restored/runs/run-e2e/input" ]] || fail "successful run kept input staging"
[[ -d "$society/.swecraft/runs/run-e2e/harbor" ]] || fail "checkpoint changed live Harbor"
[[ ! -e "$society/.swecraft/runs/run-e2e/harbor.tar.gz" ]] \
  || fail "checkpoint wrote into the live run"
[[ -z "$(docker ps -q --filter "label=com.docker.compose.project=$copy_project")" ]] \
  || fail "load started restored services"

say "5/7 resuming the copy through standard Hub Compose"
(
  cd "$society"
  uv run --project "$hub/services/society" --frozen swecraft-society resume \
    "$restored" --project "$copy_project" --port "$copy_port"
)
resumed_admin_token=$(<"$restored/state/forge/host/admin_token")
resumed_agent_token=$(<"$restored/state/coordinator/credentials/$agent.token")

say "6/7 verifying social, runtime, and evaluator state on the copy"
restored_head=$(git ls-remote "$copy_base/$org/demo.git" refs/heads/main | cut -f1)
[[ "$restored_head" == "$manifest_head" ]] || fail "restored head mismatch"
issue_count=$(api_on "$copy_base" "$resumed_admin_token" GET "/repos/$org/demo/issues?state=all" \
  | python3 -c "import json,sys;print(len(json.load(sys.stdin)))")
[[ "$issue_count" -ge 1 ]] || fail "issue did not survive"
[[ "$(sqlite3 "$restored/state/coordinator/coordinator.db" 'SELECT COUNT(*) FROM wakes')" -ge 1 ]] \
  || fail "coordinator history did not survive"
[[ -s "$restored/runtime/coordinator.yaml" ]] || fail "runtime config missing"
[[ -s "$restored/runtime/checkpoint.yaml" ]] || fail "runtime checkpoint config missing"
[[ ! -e "$restored/runtime/api_token" ]] || fail "resume replaced the Hub API token"
[[ -s "$restored/state/coordinator/api_token" ]] || fail "copied Hub API token missing"
[[ -s "$restored/state/forge/host/runner/.runner" ]] || fail "runner not restored"
(
  # Run from the original experiment directory: absolute runtime paths must
  # still checkpoint the resumed society rather than this source checkout.
  cd "$society"
  uv run --project "$hub/services/society" --frozen swecraft-society checkpoint \
    --config "$restored/runtime/checkpoint.yaml" \
    --output "$work/resumed-checkpoint"
)
[[ -s "$work/resumed-checkpoint/manifest.json" ]] \
  || fail "resumed society checkpoint missing"
[[ -s "$work/resumed-checkpoint/runs/run-e2e/harbor.tar.gz" ]] \
  || fail "second-generation run projection lost its Harbor archive"

say "7/7 proving copied credentials and runner identity still work"
restored_runners=$(api_on "$copy_base" "$resumed_admin_token" GET /admin/actions/runners)
restored_runner_uuids=$(python3 -c "import json,sys;d=json.loads(sys.argv[1]);e=d.get('entries') if isinstance(d,dict) else d;print(' '.join(x.get('uuid','') for x in e or []))" "$restored_runners")
[[ -n "$restored_runner_uuids" ]] || fail "restored Forge lists no runner"
case " $restored_runner_uuids " in
  *" $old_runner_uuid "*) ;;
  *) fail "copied runner record changed" ;;
esac
restored_runner_status=$(python3 -c "import json,sys;d=json.loads(sys.argv[1]);e=d.get('entries') if isinstance(d,dict) else d;print(next((x.get('status','') for x in e or [] if x.get('uuid')==sys.argv[2]),''))" "$restored_runners" "$old_runner_uuid")
[[ "$restored_runner_status" == idle || "$restored_runner_status" == active ]] \
  || fail "copied runner is not connected"
[[ "$resumed_admin_token" == "$old_admin_token" ]] || fail "admin token changed"
[[ "$resumed_agent_token" == "$old_agent_token" ]] || fail "agent token changed"
[[ "$(status_on "$copy_base" "$old_admin_token" /user)" == 200 ]] || fail "copied admin token fails"
[[ "$(status_on "$copy_base" "$old_agent_token" /user)" == 200 ]] || fail "copied agent token fails"
comment_author=$(api_on "$copy_base" "$resumed_agent_token" POST "/repos/$org/demo/issues/1/comments" \
  '{"body":"posted after checkpoint resume"}' \
  | python3 -c "import json,sys;print(json.load(sys.stdin)['user']['login'])")
[[ "$comment_author" == "$agent" ]] || fail "copied agent cannot write"

say "PASS: directory checkpoint, local tar, pure load, and resume round-tripped"

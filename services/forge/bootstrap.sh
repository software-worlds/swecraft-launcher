#!/usr/bin/env bash
set -euo pipefail

here=$(cd "$(dirname "$0")" && pwd)
state="$here/state"
port=${SWECRAFT_FORGE_PORT:-3400}
host=${SWECRAFT_FORGE_HOST:-127.0.0.1}
base="http://$host:$port"
org=${SWECRAFT_FORGE_ORG:-swecraft}
project=${COMPOSE_PROJECT_NAME:-swecraft-forge}
# The admin account; a forge resumed from a checkpoint made before the rename
# has `hub-admin`, set SWECRAFT_FORGE_ADMIN to it.
admin_user=${SWECRAFT_FORGE_ADMIN:-swecraft-admin}

compose() {
  docker compose -p "$project" -f "$here/compose.yaml" "$@"
}

if [[ ! "$org" =~ ^[a-zA-Z0-9._-]+$ ]]; then
  echo "invalid organization name: $org" >&2
  exit 2
fi

if [[ ! -s "$state/admin_password" ]]; then
  (umask 077 && openssl rand -hex 16 > "$state/admin_password")
fi
admin_password=$(<"$state/admin_password")

if ! compose exec -T forgejo forgejo admin user list --admin \
  | grep -q "$admin_user"; then
  compose exec -T forgejo forgejo admin user create \
    --admin \
    --username "$admin_user" \
    --password "$admin_password" \
    --email "$admin_user@localhost" \
    --must-change-password=false >/dev/null
fi

generate_admin_token() {
  (umask 077 && compose exec -T forgejo forgejo admin user generate-access-token \
    --username "$admin_user" \
    --token-name "bootstrap-$(date +%s)" \
    --scopes all \
    --raw | tr -d '\r\n' > "$state/admin_token.tmp")
  mv "$state/admin_token.tmp" "$state/admin_token"
}

admin_token_valid() {
  [[ -s "$state/admin_token" ]] && curl --fail --silent --show-error \
    -H "Authorization: token $(<"$state/admin_token")" \
    "$base/api/v1/user" > /dev/null 2>&1
}

# A stale token survives volume resets that recreate the database; validate
# it against the API instead of trusting the file.
if ! admin_token_valid; then
  generate_admin_token
fi
admin_token=$(<"$state/admin_token")

api() {
  local method=$1
  local path=$2
  local body=${3:-}
  local args=(
    --fail-with-body --silent --show-error
    -X "$method"
    -H "Authorization: token $admin_token"
    -H "Content-Type: application/json"
  )
  if [[ -n "$body" ]]; then
    args+=(-d "$body")
  fi
  curl "${args[@]}" "$base/api/v1$path"
}

if ! api GET "/orgs/$org" >/dev/null 2>&1; then
  api POST /orgs "{\"username\":\"$org\",\"visibility\":\"public\"}" >/dev/null
fi

seed_default_repository=1
if [[ "${1:-}" == "--no-repositories" ]]; then
  seed_default_repository=0
  shift
fi
if (($# == 0 && seed_default_repository == 1)); then
  set -- demo
fi
for repository in "$@"; do
  if [[ ! "$repository" =~ ^[a-zA-Z0-9._-]+$ ]]; then
    echo "invalid repository name: $repository" >&2
    exit 2
  fi
  if ! api GET "/repos/$org/$repository" >/dev/null 2>&1; then
    api POST "/orgs/$org/repos" \
      "{\"name\":\"$repository\",\"private\":false,\"auto_init\":true,\"default_branch\":\"main\"}" \
      >/dev/null
  fi
  echo "repository: $base/$org/$repository"
done

# Actions runner registration (ported from PR #3). Labels map workflow
# `runs-on` values to job images; keep a Python-capable label available so
# agents can run their own test suites in CI. Changed labels re-register
# instead of trusting the .runner file forever; a recreated database is
# always paired with a state wipe by `just forge-reset`.
runner_labels=${SWECRAFT_FORGE_RUNNER_LABELS:-docker:docker://node:22-bookworm,ubuntu-latest:docker://ghcr.io/catthehacker/ubuntu:act-22.04,python:docker://python:3.12-bookworm}
runner_needs_registration() {
  [[ ! -s "$state/runner/.runner" ]] && return 0
  [[ "$(cat "$state/runner/labels" 2>/dev/null)" != "$runner_labels" ]] && return 0
  return 1
}
if runner_needs_registration; then
  rm -f "$state/runner/.runner"
  runner_token=$(compose exec -T forgejo \
    forgejo forgejo-cli actions generate-runner-token | tr -d '\r\n')
  compose run --rm --no-deps runner forgejo-runner register \
    --no-interactive \
    --instance http://forgejo:3000 \
    --token "$runner_token" \
    --name swecraft-runner \
    --labels "$runner_labels" >/dev/null
  printf '%s' "$runner_labels" > "$state/runner/labels"
  compose restart runner >/dev/null 2>&1 || true
fi
compose up -d --wait runner

echo "admin: $admin_user"
echo "password: $state/admin_password"
echo "token: $state/admin_token"

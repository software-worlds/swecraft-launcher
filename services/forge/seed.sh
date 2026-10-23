#!/usr/bin/env bash
# Mirror an upstream repository into the forge as a world for agents to
# maintain: full history, pinned to a tag, with upstream CI configuration
# removed and the result verified.
#
# Every step here exists because skipping it breaks a world in a way that is
# hard to see afterwards; see docs/reference/setup.md for the failure each one prevents.
#
#   seed.sh <upstream-url> <tag> [name]
set -euo pipefail

here=$(cd "$(dirname "$0")" && pwd)
state="$here/state"
port=${SWECRAFT_FORGE_PORT:-3400}
host=${SWECRAFT_FORGE_HOST:-127.0.0.1}
base="http://$host:$port"
org=${SWECRAFT_FORGE_ORG:-swecraft}
work=${SWECRAFT_FORGE_SEED_DIR:-${TMPDIR:-/tmp}/swecraft-seed}

if (($# < 2)); then
  echo "usage: seed.sh <upstream-url> <tag> [name]" >&2
  exit 2
fi

upstream=$1
tag=$2
name=${3:-$(basename "$upstream" .git)}

if [[ ! "$name" =~ ^[a-zA-Z0-9._-]+$ ]]; then
  echo "invalid repository name: $name" >&2
  exit 2
fi
if [[ ! -s "$state/admin_token" ]]; then
  echo "no admin token; run bootstrap.sh first" >&2
  exit 2
fi
admin_token=$(<"$state/admin_token")

api() {
  local method=$1 path=$2 body=${3:-}
  local args=(--fail-with-body --silent --show-error -X "$method"
              -H "Authorization: token $admin_token"
              -H "Content-Type: application/json")
  [[ -n $body ]] && args+=(-d "$body")
  curl "${args[@]}" "$base/api/v1$path"
}


# The token goes through the environment, not the URL: `/proc/<pid>/cmdline`
# is world-readable, so a token in the push URL is visible to every user on
# the host via `ps`, while `/proc/<pid>/environ` is owner-only.
push_main() {
  GIT_CONFIG_COUNT=1 \
  GIT_CONFIG_KEY_0="http.$base/.extraheader" \
  GIT_CONFIG_VALUE_0="Authorization: token $admin_token" \
  git -C "$checkout" push --quiet --force --no-follow-tags "$base/$org/$name.git" main
}

mkdir -p "$work"
checkout="$work/$name"
if [[ ! -d "$checkout/.git" ]]; then
  git clone --quiet "$upstream" "$checkout"
fi
git -C "$checkout" reset --quiet --hard
git -C "$checkout" fetch --quiet --tags origin
git -C "$checkout" checkout --quiet -B main "refs/tags/$tag"

# Forgejo Actions reads .github/workflows/ too, so a mirror push queues the
# entire upstream matrix — jobs that cannot work here (GitHub-hosted runners,
# marketplace actions, repository secrets) against a capacity-1 runner. The
# message doubles as guidance for the agent that will read this history.
if [[ -d "$checkout/.github/workflows" ]]; then
  git -C "$checkout" rm -r --quiet .github/workflows
  git -C "$checkout" \
    -c user.name=swecraft-admin -c user.email=swecraft-admin@localhost \
    commit --quiet -m "chore: remove upstream GitHub Actions workflows

These target github.com (hosted runners, marketplace actions, repository
secrets) and cannot work on this forge. Forgejo Actions IS enabled here:
write workflows under .forgejo/workflows/, with runs-on one of the labels
the runner registered. Jobs cannot reach github.com, so clone from
\$GITHUB_SERVER_URL instead of using actions/checkout."
fi

# auto_init would add a commit that orphans the mirrored history.
if existing=$(api GET "/repos/$org/$name" 2>/dev/null); then
  # Seeding force-pushes `main`. Against a repository an agent is already
  # maintaining that erases its merged work, and the verification below
  # would then report a clean seed. Refuse unless the repository is empty
  # or the operator asks for a replacement explicitly.
  if [[ "$(printf '%s' "$existing" | tr -d ' ' | grep -c '"empty":true')" -eq 0 ]]; then
    if [[ "${SWECRAFT_FORGE_SEED_REPLACE:-}" != "1" ]]; then
      echo "refusing to seed $org/$name: repository already has content." >&2
      echo "re-run with SWECRAFT_FORGE_SEED_REPLACE=1 to delete and recreate it." >&2
      exit 3
    fi
    # Delete and recreate rather than force-push over it: stale tags and
    # Actions runs cannot be removed through the API, so an in-place
    # replacement leaves history that looks clean and is not.
    api DELETE "/repos/$org/$name" >/dev/null
    api POST "/orgs/$org/repos" \
      "{\"name\":\"$name\",\"private\":false,\"auto_init\":false,\"default_branch\":\"main\"}" \
      >/dev/null
  fi
else
  api POST "/orgs/$org/repos" \
    "{\"name\":\"$name\",\"private\":false,\"auto_init\":false,\"default_branch\":\"main\"}" \
    >/dev/null
fi

# Deliberately no tags: the strip commit sits on top of the tag, so the
# tag's tree still carries .github/workflows/ and pushing it re-fires the
# upstream matrix even though main is clean. The pin is recorded below.
push_main

# Repositories created before Actions was enabled keep the unit off and
# silently ignore their workflows.
api PATCH "/repos/$org/$name" '{"has_actions":true,"default_branch":"main"}' >/dev/null

# A push can leave the repository flagged empty in Forgejo's database while
# its refs are already present, and anything reading the API — coordinator
# onboarding included — then sees an empty repository. The flag settles on
# its own, but not synchronously with the push, so wait for it rather than
# trusting that the push exited zero.
settled=0
for _ in $(seq 1 30); do
  if api GET "/repos/$org/$name" | tr -d ' ' | grep -q '"empty":false'; then
    settled=1
    break
  fi
  sleep 2
done
if ((settled == 0)); then
  echo "repository still flagged empty after 60s; pushing again" >&2
  push_main
  sleep 5
fi

head_sha=$(git -C "$checkout" rev-parse --short=9 HEAD)
# Peel to the commit: an annotated tag resolves to the tag *object*, which
# this script deliberately never pushes — so the recorded pin would dangle
# in the seeded world, corrupting the one reproducibility fact it prints.
pin_sha=$(git -C "$checkout" rev-parse --short=9 "refs/tags/$tag^{commit}")
# Each of these fails closed. `grep -c` prints 0 for empty input, so folding
# a failed API call into the count would turn "could not check" into
# "checked, found none" — the failure mode this whole script exists to
# prevent, reproduced inside its own verification.
count_in() { # <json> <needle>
  printf '%s' "$1" | tr -d ' \n' | grep -c "$2" || true
}
require_api() { # <path> — response on stdout, or abort
  local body
  if ! body=$(api GET "$1"); then
    echo "seed verification could not query $1" >&2
    exit 1
  fi
  printf '%s' "$body"
}

empty=$(count_in "$(require_api "/repos/$org/$name")" '"empty":false')
workflows=$(curl --silent --output /dev/null --write-out '%{http_code}' \
  -H "Authorization: token $admin_token" \
  "$base/api/v1/repos/$org/$name/contents/.github/workflows")
# A pushed tag is the trap this script exists to avoid, and a queued run is
# the damage it causes; neither is inferable from `main` looking correct,
# and neither can be cleaned up afterwards through the API.
tags=$(count_in "$(require_api "/repos/$org/$name/tags")" '"name":')
runs=$(count_in "$(require_api "/repos/$org/$name/actions/tasks")" '"run_number":')

echo "repository:      $base/$org/$name"
echo "pinned:          $tag ($pin_sha)"
echo "head:            $head_sha"
echo "non-empty:       $([[ $empty -gt 0 ]] && echo yes || echo NO)"
echo "upstream CI:     $([[ $workflows == 404 ]] && echo removed || echo "STILL PRESENT (http $workflows)")"
echo "tags pushed:     $([[ $tags -eq 0 ]] && echo none || echo "$tags (SHOULD BE NONE)")"
echo "actions queued:  $([[ $runs -eq 0 ]] && echo none || echo "$runs (SHOULD BE NONE)")"

if [[ $empty -eq 0 || $workflows != 404 || $tags -ne 0 || $runs -ne 0 ]]; then
  echo "seed verification failed" >&2
  exit 1
fi

#!/usr/bin/env bash
set -euo pipefail

port=${SWECRAFT_FORGE_PORT:-3400}
host=${SWECRAFT_FORGE_HOST:-127.0.0.1}
base="http://$host:$port"
org=${SWECRAFT_FORGE_ORG:-swecraft}
repository=${SWECRAFT_FORGE_REPOSITORY:-demo}

health=$(curl --fail --silent --show-error "$base/api/healthz")
python3 -c '
import json, sys
payload = json.load(sys.stdin)
if payload.get("status") != "pass":
    raise SystemExit(f"forge health failed: {payload}")
' <<<"$health"

git ls-remote "$base/$org/$repository.git" HEAD | grep -Eq '^[0-9a-f]{40}[[:space:]]+HEAD$'

echo "Forgejo smoke passed: $base/$org/$repository.git"

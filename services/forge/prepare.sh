#!/usr/bin/env bash
set -euo pipefail

here=$(cd "$(dirname "$0")" && pwd)
state="$here/state"
mkdir -p "$state"

# Give the rootless container (UID 1000) access to the files it must read or
# write, preferring an ACL that names only that user over a mode that opens
# them to everyone on the host. `setfacl` is not always available — it is
# absent on the host this was written for — so the fallback is a permissive
# mode. That fallback is the reason these are machine-local secrets for a
# forge published on loopback and nothing more: on a shared host, readable
# SECRET_KEY means forge admin by session forgery, and a writable runner
# config means code execution in CI.
grant_container_access() {
  local acl=$1 fallback=$2 path=$3
  # Order matters: `chmod` rewrites a POSIX ACL's mask, so tightening the
  # base mode *after* setting an ACL would leave the named-user entry
  # present but ineffective — the container would be denied by a file that
  # `getfacl` says grants it access. Tighten first, then grant, then verify
  # the entry survived; anything else falls back to the blunt mode rather
  # than shipping a silent denial.
  if command -v setfacl >/dev/null 2>&1; then
    chmod go-rwx "$path" 2>/dev/null || true
    if setfacl -m "u:1000:$acl" "$path" 2>/dev/null &&
       ! getfacl -p "$path" 2>/dev/null | grep -q '^user:1000:.*#effective:'; then
      return
    fi
  fi
  chmod "$fallback" "$path"
}

for secret in forgejo_internal_token forgejo_secret_key; do
  path="$state/$secret"
  if [[ ! -s "$path" ]]; then
    (umask 077 && openssl rand -hex 32 > "$path")
  fi
done

# Readable by the container that needs them, not just by whoever generated
# them: the rootless Forgejo image runs as UID 1000, and on a Linux host
# whose checkout belongs to any other user it cannot read a 0600 file —
# Forgejo then crash-loops on `Failed to read SECRET_KEY_URI`. `chown` is
# not an option on a checkout the operator does not own (NFS, for one), and
# this script re-applies the mode on every `forge-up`, so a manual fix does
# not survive. These are machine-local secrets for a forge published on
# loopback by default; being readable by the container is the property that
# matters.
grant_container_access r 644 "$state/forgejo_internal_token"
grant_container_access r 644 "$state/forgejo_secret_key"

# The runner container writes its registration here as UID 1000, while the
# directory is created by whoever runs this script. Without a permissive
# mode, registration fails on Linux with `open .runner: permission denied`
# and `forge-bootstrap` cannot complete.
mkdir -p "$state/runner"
grant_container_access rwx 777 "$state/runner"
# Only written when absent, so raising SWECRAFT_FORGE_RUNNER_CAPACITY on an
# existing state directory has no effect until config.yml is removed (or
# `just forge-reset` regenerates it). One slot is the safe default, but a
# society whose agents write CI will outrun it: each push expands its
# workflows into jobs faster than a single slot retires them, and stale
# Actions runs can be neither cancelled nor deleted through the API.
if [[ ! -s "$state/runner/config.yml" ]]; then
  # A world pinned at a date installs from an index capped at that date
  # (docs/reference/index-cap.md), and CI must see the same index as the
  # agents or a job log shows the releases the world withholds. Jobs run in
  # the DinD network namespace, where the Docker bridge address is DinD's
  # own, so SWECRAFT_FORGE_INDEX_URL has to name the host by an address that is
  # reachable from there (its LAN address), e.g.
  # http://10.0.0.5:5280/2026-08-16/simple/. Like the capacity, this is read
  # only when config.yml is first written.
  index_envs=""
  if [[ -n "${SWECRAFT_FORGE_INDEX_URL:-}" ]]; then
    index_host=$(printf '%s' "$SWECRAFT_FORGE_INDEX_URL" | sed -E 's#^[a-z]+://([^/:]+).*#\1#')
    index_envs="  envs:
    PIP_INDEX_URL: \"${SWECRAFT_FORGE_INDEX_URL}\"
    PIP_TRUSTED_HOST: \"${index_host}\"
    UV_DEFAULT_INDEX: \"${SWECRAFT_FORGE_INDEX_URL}\"
    UV_INDEX_URL: \"${SWECRAFT_FORGE_INDEX_URL}\""
    if [[ -n "${SWECRAFT_FORGE_EXCLUDE_NEWER:-}" ]]; then
      index_envs+="
    UV_EXCLUDE_NEWER: \"${SWECRAFT_FORGE_EXCLUDE_NEWER}\""
    fi
  fi
  # The heredoc is unquoted so the settings expand, which also means a backtick
  # in it would run as a command: keep them out of the comments below.
  cat > "$state/runner/config.yml" <<EOF
log:
  level: info
runner:
  file: /data/.runner
  capacity: ${SWECRAFT_FORGE_RUNNER_CAPACITY:-1}
  timeout: 3h
${index_envs}
cache:
  enabled: false
container:
  # Job containers share the DinD network namespace so that the compose
  # DNS name forgejo resolves inside them (a nested bridge would not).
  network: host
  # Resource limits on every CI job container: without them a
  # single job can take the whole host — four concurrent botocore
  # pytest --with-xdist jobs (auto = nproc workers inside DinD, where
  # no cpuset exists) reached ~957 GiB RSS and drove a 1 TiB campaign
  # host to MemAvailable: 0. --memory-swap equal to --memory disables
  # swap so a runaway job OOM-kills itself instead of thrashing the
  # host. Size capacity x memory well under host RAM.
  options: "--memory=${SWECRAFT_FORGE_JOB_MEMORY:-8g} --memory-swap=${SWECRAFT_FORGE_JOB_MEMORY:-8g} --cpus=${SWECRAFT_FORGE_JOB_CPUS:-4}"
EOF
fi

# Outside the `if`, so a file created under a restrictive umask — or by an
# earlier run of this script — is repaired rather than only newly written
# ones. The runner daemon reads it as UID 1000.
grant_container_access r 644 "$state/runner/config.yml"

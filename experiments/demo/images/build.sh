#!/bin/sh
# Build the demo's images: the judge from wsproto at its pinned commit with the
# benchmark overlay staged where the tasks expect it, and the agent image with
# the capped package index. Needs benchmark-python:1 and swecraft-runner-image:0.1
# (`just benchmark-base-image-build`, `just runner-image-build`).
set -eu
HERE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT
git clone --quiet https://github.com/python-hyper/wsproto "$WORK/src"
git -C "$WORK/src" checkout --quiet 820701baf101b8c364a879440e1215da2fb0973f
rm -rf "$WORK/src/.git" "$WORK/src/.dockerignore"
mkdir -p "$WORK/src/.swecraft-eval/overlay"
cp -R "$HERE/wsproto/overlay/." "$WORK/src/.swecraft-eval/overlay/"
docker build -f "$HERE/wsproto/Dockerfile" -t demo-eval-wsproto:1 "$WORK/src"
docker build -f "$HERE/agent/Dockerfile" \
  --build-arg BASE_IMAGE=swecraft-runner-image:0.1 \
  --build-arg INDEX_URL="${SWECRAFT_FORGE_INDEX_URL:-http://172.17.0.1:5280/2026-08-16/simple/}" \
  --build-arg EXCLUDE_NEWER="${SWECRAFT_FORGE_EXCLUDE_NEWER:-2026-08-16T00:00:00Z}" \
  -t demo-agent:1 "$HERE/agent"

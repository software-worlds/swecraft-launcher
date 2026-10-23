# The repository's small, discoverable interface. `just exp <command> <folder>`
# runs swecraft-launch; the rest builds, checks and tests the components.

forge_dir := justfile_directory() / "services/forge"
evaluator_dir := justfile_directory() / "services/evaluator"
society_dir := justfile_directory() / "services/society"
python_dirs := "packages/swecraft-common packages/swecraft-launch services/analyzer services/coordinator services/evaluator services/forge services/index-cap services/runner services/society services/world"
project := env_var_or_default("COMPOSE_PROJECT_NAME", "swecraft-forge")

# List available recipes.
[private]
default:
    @just --list

# Run one experiment step with swecraft-launch: check, up, seed, shakeout, onboard, run, finish, status, export.
exp COMMAND EXPERIMENT *ARGS:
    uv run --frozen --all-packages swecraft-launch {{COMMAND}} "{{EXPERIMENT}}" {{ARGS}}

# Sync the root uv workspace with every Python component and dependency group.
python-sync:
    uv sync --frozen --all-packages --all-groups

# Assert that the root uv.lock matches every Python workspace member.
python-lock-check:
    uv lock --check

# Lint and format-check every Python component (the exporter: lint only, see its ruff.toml).
python-check: python-sync
    uv run --frozen --all-packages --all-groups ruff check {{python_dirs}} services/export
    uv run --frozen --all-packages --all-groups ruff format --check {{python_dirs}}

# Run every Python component's tests.
python-test: python-sync
    uv run --frozen --all-packages --all-groups python -m unittest discover -s packages/swecraft-common/tests -v
    uv run --frozen --all-packages --all-groups python -m unittest discover -s services/forge/tests -v
    uv run --frozen --all-packages --all-groups python -m unittest discover -s services/runner/tests -v
    uv run --frozen --all-packages --all-groups python -m unittest discover -s services/world/tests -v
    uv run --frozen --all-packages --all-groups python -m unittest discover -s services/index-cap/tests -v
    uv run --frozen --all-packages --all-groups python -m unittest discover -s services/coordinator/tests -v
    uv run --frozen --all-packages --all-groups python -m unittest discover -s services/evaluator/tests -v
    uv run --frozen --all-packages --all-groups python -m unittest discover -s services/society/tests -v
    uv run --frozen --all-packages --all-groups python -m unittest discover -s services/analyzer/tests -v
    uv run --frozen --all-packages --all-groups python -m unittest discover -s services/export -v
    uv run --frozen --all-packages --all-groups python -m unittest discover -s packages/swecraft-launch/tests -v

# Run the coordinator loop (reads coordinator.yaml at the repo root).
coordinator-serve:
    uv run --frozen --package swecraft-coordinator swecraft-coordinator serve

# Lint and format-check the shared swecraft-common library.
common-check:
    uv run --frozen --package swecraft-common --all-groups ruff check packages/swecraft-common
    uv run --frozen --package swecraft-common --all-groups ruff format --check packages/swecraft-common

# Run the tests of the shared swecraft-common library.
common-test:
    uv run --frozen --package swecraft-common --all-groups python -m unittest discover -s packages/swecraft-common/tests -v

# Lint and format-check the Forge runtime adapter.
forge-runtime-check:
    uv run --frozen --package swecraft-forge --all-groups ruff check services/forge
    uv run --frozen --package swecraft-forge --all-groups ruff format --check services/forge

# Run the tests of the Forge runtime adapter.
forge-runtime-test:
    uv run --frozen --package swecraft-forge --all-groups python -m unittest discover -s services/forge/tests -v

# Lint and format-check the world protocol and seeder.
world-check:
    uv run --frozen --package swecraft-world --all-groups ruff check services/world
    uv run --frozen --package swecraft-world --all-groups ruff format --check services/world

# Run the tests of the world protocol and seeder.
world-test:
    uv run --frozen --package swecraft-world --all-groups python -m unittest discover -s services/world/tests -v

# Show the swecraft-world CLI.
world-help:
    uv run --frozen --package swecraft-world swecraft-world --help

# Lint and format-check the coordinator.
coordinator-check:
    uv run --frozen --package swecraft-coordinator --all-groups ruff check services/coordinator
    uv run --frozen --package swecraft-coordinator --all-groups ruff format --check services/coordinator

# Run the tests of the coordinator.
coordinator-test:
    uv run --frozen --package swecraft-coordinator --all-groups python -m unittest discover -s services/coordinator/tests -v

# Show the swecraft-coordinator CLI.
coordinator-help:
    uv run --frozen --package swecraft-coordinator swecraft-coordinator --help

# Lint and format-check the one-shot runner.
runner-check:
    uv run --frozen --package swecraft-runner --all-groups ruff check services/runner
    uv run --frozen --package swecraft-runner --all-groups ruff format --check services/runner

# Run the tests of the one-shot runner.
runner-test:
    uv run --frozen --package swecraft-runner --all-groups python -m unittest discover -s services/runner/tests -v

# Show the runner CLI.
runner-help:
    uv run --frozen --package swecraft-runner swecraft-runner --help

# Lint and format-check the evaluator.
evaluator-check:
    uv run --frozen --package swecraft-evaluator --all-groups ruff check services/evaluator
    uv run --frozen --package swecraft-evaluator --all-groups ruff format --check services/evaluator

# Run the tests of the evaluator.
evaluator-test:
    uv run --frozen --package swecraft-evaluator --all-groups python -m unittest discover -s services/evaluator/tests -v

# Show the swecraft-evaluator CLI.
evaluator-help:
    uv run --frozen --package swecraft-evaluator swecraft-evaluator --help

# Lint and format-check the society checkpoint protocol.
society-check:
    uv run --frozen --package swecraft-society --all-groups ruff check services/society
    uv run --frozen --package swecraft-society --all-groups ruff format --check services/society

# Run the tests of the society checkpoint protocol.
society-test:
    uv run --frozen --package swecraft-society --all-groups python -m unittest discover -s services/society/tests -v

# Show the swecraft-society CLI.
society-help:
    uv run --frozen --package swecraft-society swecraft-society --help

# Lint and format-check the checkpoint analyzer.
analyzer-check:
    uv run --frozen --package swecraft-analyzer --all-groups ruff check services/analyzer
    uv run --frozen --package swecraft-analyzer --all-groups ruff format --check services/analyzer

# Run the tests of the checkpoint analyzer.
analyzer-test:
    uv run --frozen --package swecraft-analyzer --all-groups python -m unittest discover -s services/analyzer/tests -v

# Show the swecraft-analyzer CLI.
analyzer-help:
    uv run --frozen --package swecraft-analyzer swecraft-analyzer --help

# Run the exporter's tests (standard library only).
export-test:
    uv run --frozen python -m unittest discover -s services/export -v

# Start Forgejo and wait for its health check.
forge-up:
    "{{forge_dir}}/prepare.sh"
    docker compose -p "{{project}}" -f "{{forge_dir}}/compose.yaml" up -d --wait

# Create the local admin, organization, and one or more public repositories.
forge-bootstrap *REPOSITORIES="demo": forge-up
    "{{forge_dir}}/bootstrap.sh" {{REPOSITORIES}}

# Mirror an upstream repository into the forge as a world to maintain.
forge-seed UPSTREAM TAG NAME="": forge-up
    "{{forge_dir}}/bootstrap.sh"
    "{{forge_dir}}/seed.sh" "{{UPSTREAM}}" "{{TAG}}" "{{NAME}}"

# Verify health and cloneability of a bootstrapped repository.
forge-smoke REPOSITORY="demo": forge-up
    "{{forge_dir}}/bootstrap.sh" "{{REPOSITORY}}"
    SWECRAFT_FORGE_REPOSITORY="{{REPOSITORY}}" "{{forge_dir}}/smoke.sh"

# Verify Forgejo Actions end to end: push a workflow, wait for a green run.
forge-actions-smoke REPOSITORY="actions-demo": forge-up
    "{{forge_dir}}/bootstrap.sh" "{{REPOSITORY}}"
    SWECRAFT_FORGE_REPOSITORY="{{REPOSITORY}}" "{{forge_dir}}/actions-smoke.sh"

# Show the Forgejo container and health state.
forge-status:
    docker compose -p "{{project}}" -f "{{forge_dir}}/compose.yaml" ps

# Follow Forgejo logs.
forge-logs:
    docker compose -p "{{project}}" -f "{{forge_dir}}/compose.yaml" logs -f forgejo

# Stop Forgejo while preserving its data.
forge-down:
    docker compose -p "{{project}}" -f "{{forge_dir}}/compose.yaml" down

# Destroy Forgejo data and locally generated credentials. Checkpoint first.
forge-reset:
    docker compose -p "{{project}}" -f "{{forge_dir}}/compose.yaml" down -v --remove-orphans
    find "{{forge_dir}}/state" -type f ! -name .gitkeep -delete

# Seed a manifest-defined world and write its lock beside the manifest.
world-seed MANIFEST="world.yaml" *ARGS="": forge-up
    "{{forge_dir}}/bootstrap.sh" --no-repositories
    uv run --frozen --package swecraft-world swecraft-world --manifest "{{MANIFEST}}" seed {{ARGS}}

# Adopt an existing world into a generated lock without mutating repositories.
world-adopt MANIFEST="world.yaml" *ARGS="": forge-up
    uv run --frozen --package swecraft-world swecraft-world --manifest "{{MANIFEST}}" adopt {{ARGS}}

# Verify a complete world lock against upstream and the live Forgejo world.
world-verify MANIFEST="world.yaml" *ARGS="": forge-up
    uv run --frozen --package swecraft-world swecraft-world --manifest "{{MANIFEST}}" check {{ARGS}}

# Serve PyPI's index capped at a date, for every world on this host (port 5280).
index-cap-serve *ARGS:
    uv run --frozen --package swecraft-index-cap swecraft-index-cap serve {{ARGS}}

# Build the shared Python/Callgrind measurement image the judges' images build on.
benchmark-base-image-build TAG="benchmark-python:1":
    docker build -t "{{TAG}}" "{{evaluator_dir}}/image"

# Build the runner task image with pinned fj and forge tooling.
runner-image-build TAG="swecraft-runner-image:0.1":
    docker build -t "{{TAG}}" "{{justfile_directory()}}/services/runner-image"

# Run runner image helper tests without building the Docker image.
runner-image-test:
    node --test "{{justfile_directory()}}/services/runner-image/tests"/*.test.mjs

# Verify JSON, fj, and forge tooling inside a built runner image.
runner-image-smoke TAG="swecraft-runner-image:0.1":
    docker run --rm "{{TAG}}" sh -c 'jq --version && fj version && command -v forge && (forge GET / 2>&1 | head -1 || true)'
    docker run --rm "{{TAG}}" sh -c 'command -v forge-react && command -v forge-login && test -f /usr/local/share/swecraft/forge-api.md'

# Live isolated checkpoint -> archive -> pure load acceptance.
society-e2e:
    "{{society_dir}}/tests/e2e/checkpoint.sh"

# Capture the idle local society into a verified private checkpoint.
society-checkpoint *ARGS="":
    uv run --frozen --package swecraft-society swecraft-society checkpoint --project "{{project}}" {{ARGS}}

# Describe and verify a checkpoint directory or tar.gz.
society-inspect BUNDLE *ARGS="":
    uv run --frozen --package swecraft-society swecraft-society inspect "{{BUNDLE}}" {{ARGS}}

# Materialize and verify a checkpoint without importing or starting services.
society-load SOURCE *ARGS="":
    uv run --frozen --package swecraft-society swecraft-society load "{{SOURCE}}" {{ARGS}}

# Resume a pristine loaded checkpoint as a mutable society.
society-resume *ARGS="":
    uv run --frozen --package swecraft-society swecraft-society resume {{ARGS}}

# Guided first run on a one-repository demo forge: forge, image, config, onboard, bootstrap.
quick-start *ARGS:
    "{{justfile_directory()}}/scripts/quick-start.sh" {{ARGS}}

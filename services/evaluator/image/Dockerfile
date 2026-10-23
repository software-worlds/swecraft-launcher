# Shared deterministic measurement tooling. Evaluator images layer their
# pinned downstream dependency environment on top; a baked tag is an epoch
# and is never rebuilt in place — a toolchain change means a new tag.
FROM python:3.12-bookworm

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential \
        curl \
        git \
        time \
        valgrind \
    && rm -rf /var/lib/apt/lists/*

RUN curl -sSf https://sh.rustup.rs | sh -s -- -y \
        --default-toolchain stable --profile minimal
ENV PATH="/root/.cargo/bin:${PATH}"

# Build backends and frontends. Measurement containers run with
# `--network none` and inject subjects with `--no-build-isolation`, so a
# build dependency that is not here cannot be fetched later — the build
# simply fails inside the container.
#
# Cython and expandvars are not optional extras: the aio-libs in-tree PEP
# 517 backend (yarl, propcache, frozenlist) declares Cython through the
# dynamic `get_requires_for_build_wheel` hook, which `--no-build-isolation`
# skips entirely. setuptools-rust is tiktoken's declared build backend.
# pytest-codspeed is what makes every CodSpeed-instrumented suite collect
# rather than skip.
#
# The frontends below were each established by a build that failed without
# them. flit-core is idna's backend and poetry-core is aiohappyeyeballs';
# both fail as a clean `BackendUnavailable`. hatch-vcs and
# hatch-fancy-pypi-readme are attrs' declared hatchling *plugins*, which
# fail late and much less legibly — inside `hatchling.metadata.core.
# validate_fields`, because the backend itself imported fine.
#
# editables is hatchling's editable-install helper. Every target whose
# `setup` runs `pip install -e /eval/target` against a hatchling project
# (uvicorn) dies in `build_editable_detection` without it — and it is not
# reachable by testing the gate alone, because running pytest against the
# checkout never exercises the install.
RUN python -m pip install --no-cache-dir \
        build \
        cython \
        editables \
        expandvars \
        flit-core \
        hatch-fancy-pypi-readme \
        hatch-vcs \
        hatchling \
        maturin \
        poetry-core \
        pytest \
        pytest-codspeed \
        setuptools \
        setuptools-rust \
        setuptools-scm \
        uv \
        wheel

# hatch-vcs and setuptools-scm shell out to git to derive a version, and the
# subject source is a host-owned bind mount read by a container running as
# root — which git refuses as `detected dubious ownership`. This has to be a
# real config file: git deliberately ignores safe.directory from `-c` and
# from GIT_CONFIG_* so an untrusted environment cannot grant itself trust.
RUN git config --global --add safe.directory '*'

ENV PYTHONHASHSEED=0

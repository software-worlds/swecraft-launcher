# The world's package index, capped at the world's date: any agent image plus the
# system-wide pip and uv configuration that points every install at
# swecraft-index-cap (docs/reference/index-cap.md). Without it an agent's
# `pip install` resolves against today's PyPI, and a release upstream published
# after the world was pinned lands in every environment of that world.
#
#   docker build -f images/index-cap/Dockerfile \
#     --build-arg BASE_IMAGE=<experiment>-agent:1 \
#     --build-arg INDEX_URL=http://172.17.0.1:5280/2026-08-16/simple/ \
#     --build-arg EXCLUDE_NEWER=2026-08-16T00:00:00Z \
#     -t <experiment>-agent-capped:1 .
#
# Files and environment, both on purpose. The files reach every process whatever
# a harness does to the environment of its tool shells, and they are what a venv's
# own pip reads. The environment outranks a project's own `[tool.uv]` settings,
# which a system-level uv.toml does not. uv also gets `exclude-newer`, its native
# form of the same rule, so it holds even if the index were unreachable.
ARG BASE_IMAGE=swecraft-runner-image:0.1
FROM ${BASE_IMAGE}

ARG INDEX_URL
ARG EXCLUDE_NEWER
RUN test -n "${INDEX_URL}" && test -n "${EXCLUDE_NEWER}" \
    && host="$(printf '%s' "${INDEX_URL}" | sed -E 's#^[a-z]+://([^/:]+).*#\1#')" \
    && mkdir -p /etc/uv \
    && printf '[global]\nindex-url = %s\ntrusted-host = %s\n' "${INDEX_URL}" "${host}" > /etc/pip.conf \
    && printf 'exclude-newer = "%s"\n\n[[index]]\nurl = "%s"\ndefault = true\n' "${EXCLUDE_NEWER}" "${INDEX_URL}" > /etc/uv/uv.toml \
    && printf '%s\n' "${host}" > /etc/swecraft-index-cap.host \
    && cat /etc/pip.conf /etc/uv/uv.toml
ENV PIP_INDEX_URL=${INDEX_URL} \
    UV_DEFAULT_INDEX=${INDEX_URL} \
    UV_INDEX_URL=${INDEX_URL} \
    UV_EXCLUDE_NEWER=${EXCLUDE_NEWER}

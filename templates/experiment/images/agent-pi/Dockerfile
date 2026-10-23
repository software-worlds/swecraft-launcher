# pi profile image: the world's agent image plus a pinned pi, for
# `agent_import_path: swecraft_runner.harbor_agents:SwecraftPi`
# (docs/reference/runner.md#pi). The pi extensions and settings are not baked
# here — the runner writes them into the sandbox per trial — so this image
# changes only when pi is bumped.
#   docker build --build-arg BASE_IMAGE=<the world's agent image> \
#     -f images/agent-pi/Dockerfile -t <experiment>-agent-pi:1 .
ARG BASE_IMAGE=swecraft-runner-image:0.1
FROM ${BASE_IMAGE}

# pi needs node 22+; swecraft-runner-image is node:22 based, and the version
# check fails the build on a shadowed older node.
ARG PI_VERSION=0.85.1
RUN npm install -g @earendil-works/pi-coding-agent@${PI_VERSION} && pi --version

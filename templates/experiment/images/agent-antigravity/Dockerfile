# Profile image for the Antigravity CLI: the world's agent image plus the
# `agy` binary, placed under ~/.local/bin by Google's installer. The installer
# takes no version and fetches the current release, and Harbor's stock agent
# would run it again at every trial; the runner's SwecraftAntigravityCli skips the
# installer when this binary is present, so building the image is the pin.
# Record `agy --version` from the built image in the experiment's run log.
# Credentials never enter the image: the Gemini API key, or the Vertex AI
# project, location and service-account file, travel in agent_env
# (docs/reference/runner.md#antigravity-cli).
ARG BASE_IMAGE=swecraft-runner-image:0.1
FROM ${BASE_IMAGE}
RUN curl -fsSL https://antigravity.google/cli/install.sh | bash \
    && $HOME/.local/bin/agy --version

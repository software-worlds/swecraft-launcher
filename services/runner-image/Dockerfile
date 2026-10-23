# Task image for agent runs: node on Debian trixie (fj needs glibc 2.39+)
# plus git and read-only forge tooling (pinned fj CLI and a curl wrapper).
#
# Deliberately agent-agnostic. Harbor selects its agent per run and installs
# the CLI itself when one is absent, so an agent baked in here is a cache —
# and a cache every lane inherits whether or not it uses that agent. Baking
# (and pinning) belongs in a profile image derived from this one; see
# docs/reference/runner-image.md.

FROM node:22-trixie-slim

ARG FJ_VERSION=0.6.0
# Pinned release-tarball digests; recompute both when bumping FJ_VERSION.
ARG FJ_SHA256_ARM64=f427e0f94e2bf6f4b44bff17020706915bccfe7abb7ad9b9cbb74cc1aa2dd7b5
ARG FJ_SHA256_AMD64=ea559da5449b6dd7e0dd9f7ea51c906b575696f782edd606b52adcf773602742
ARG TARGETARCH

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl git jq ripgrep \
    && rm -rf /var/lib/apt/lists/*

RUN case "$TARGETARCH" in \
        arm64) fj_arch=aarch64; fj_sha256="$FJ_SHA256_ARM64" ;; \
        amd64) fj_arch=x86_64; fj_sha256="$FJ_SHA256_AMD64" ;; \
        *) echo "unsupported architecture: $TARGETARCH" >&2; exit 1 ;; \
    esac \
    && curl -fsSL -o /tmp/fj.tar.gz \
        "https://codeberg.org/forgejo-contrib/forgejo-cli/releases/download/v${FJ_VERSION}/forgejo-cli-${fj_arch}-linux.tar.gz" \
    && echo "$fj_sha256  /tmp/fj.tar.gz" | sha256sum -c - \
    && tar -xzf /tmp/fj.tar.gz -C /tmp \
    && install -m 0755 "$(find /tmp -name fj -type f | head -1)" /usr/local/bin/fj-bin \
    && rm -rf /tmp/fj.tar.gz /tmp/forgejo-cli* \
    && fj-bin version

COPY fj-wrapper /usr/local/bin/fj
COPY forge /usr/local/bin/forge
COPY forge-react /usr/local/bin/forge-react
COPY forge-login /usr/local/bin/forge-login
COPY swecraft-memory /usr/local/bin/swecraft-memory
COPY forge-api.md /usr/local/share/swecraft/forge-api.md
RUN chmod 0755 /usr/local/bin/fj /usr/local/bin/forge \
    /usr/local/bin/forge-react /usr/local/bin/forge-login /usr/local/bin/swecraft-memory

# The forge as reachable from inside task containers; host-side checkout
# URLs use the host loopback and are not valid in here.
ENV SWECRAFT_FORGE_URL=http://host.docker.internal:3400

# Demo: one maintainer on h11

The smallest experiment that exercises the whole path: one library (h11 at
v0.16.0), one maintainer agent (Claude Code), and one judge (wsproto's
benchmarks, which go through h11's opening handshake). It stops after 20
completed wakes or $20, whichever comes first.

```bash
source .venv/bin/activate                  # after uv sync --all-packages --all-groups
just benchmark-base-image-build && just runner-image-build
experiments/demo/images/build.sh          # demo-eval-wsproto:1, demo-agent:1
swecraft-launch check    experiments/demo
swecraft-launch up       experiments/demo
swecraft-launch seed     experiments/demo
swecraft-launch shakeout experiments/demo        # no model spending up to here
export CLAUDE_CODE_OAUTH_TOKEN=...        # or change agent_env in coordinator.yaml
swecraft-launch onboard  experiments/demo
swecraft-launch run      experiments/demo
swecraft-launch export   experiments/demo
```

`experiment.env` assumes port 3400 and the capped index on this host at
port 5280 (`up` starts it when nothing answers).

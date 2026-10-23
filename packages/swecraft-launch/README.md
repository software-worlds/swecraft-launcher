# swecraft-launch

The launcher: one command per step of an experiment, run from a checkout of
the repository on an experiment folder (`experiments/<name>`).

```bash
swecraft-launch check    experiments/<name>
swecraft-launch up       experiments/<name>   # capped index + forge
swecraft-launch seed     experiments/<name>   # world at its pins, CI enabled
swecraft-launch shakeout experiments/<name>   # A/A run: every gate must pass
swecraft-launch onboard  experiments/<name>   # agents' identities + bootstrap
swecraft-launch run      experiments/<name>   # until the stop rule, then finish
swecraft-launch status   experiments/<name>
swecraft-launch export   experiments/<name>   # bundles/<name> for sim-browser
```

It calls the other components' commands (`swecraft-world`,
`swecraft-evaluator`, `swecraft-coordinator`, `swecraft-society`,
`swecraft-analyzer`) and the exporter in `services/export`. See
[run an experiment](../../docs/tutorials/run-an-experiment.md).

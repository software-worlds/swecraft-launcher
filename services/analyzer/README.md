# swecraft-analyzer

Reads one finished experiment, as its society checkpoint, into one SQLite
evidence store: every wake, tool call, forge action, pull request, diff,
comment, review and release. `collect` builds the store and unpacks the
frozen forge beside it; `query` runs read-only SQL over it. The exporter
(`services/export`) reads this directory to build a run's browsable bundle.

```bash
uv run --package swecraft-analyzer swecraft-analyzer collect <checkpoint-dir> <analysis-dir>
uv run --package swecraft-analyzer swecraft-analyzer query <analysis-dir> "SELECT ..." [--json]
```

Both commands use only the standard library, so they also run as
`python3 -m swecraft_analyzer` where the workspace is not installed. See
[docs/reference/analyzer.md](../../docs/reference/analyzer.md) for the input
it expects and the tables it writes.

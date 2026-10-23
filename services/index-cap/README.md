# swecraft-index-cap

PyPI's simple index as it stood on a date. A seeded world installs from it, so
an agent's `pip install` cannot pull in a release that upstream published after
the world was pinned. See [docs/reference/index-cap.md](../../docs/reference/index-cap.md).

```
uv run --package swecraft-index-cap swecraft-index-cap serve            # 0.0.0.0:5280
uv run --package swecraft-index-cap swecraft-index-cap show 2026-08-16 urllib3
```

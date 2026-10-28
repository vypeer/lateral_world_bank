# lateral_world_bank

Configuration-driven pipeline that pulls country indicators from the public World Bank API v2,
models them in DuckDB (raw → staging → core → marts), and produces reporting outputs.

> Work in progress — full setup, design walkthrough, production note and AI usage to follow.

## Quick start

```bash
make run    # requires uv; installs from the lockfile and runs the pipeline
make check  # lint, typecheck, tests (no live API calls)
```

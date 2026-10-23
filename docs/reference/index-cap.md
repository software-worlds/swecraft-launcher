# The capped package index

A world is seeded at pinned releases, but an agent's `pip install` resolves
against the package index of the day it runs. From the moment an upstream
publishes a later release, every environment in the world contains it. In
one of our runs a requests maintainer benchmarked against urllib3 2.8.0, which
upstream published three weeks after the world pinned urllib3 at 2.7.0, and CI
logs showed the same version to anyone who read them. The seeder now withholds
upstream's later tags from the forge (see [worlds](worlds.md)); `swecraft-index-cap`
withholds the same releases from the package index.

`services/index-cap` serves PyPI's simple index with every file uploaded after a
cutoff removed, using the upload time PyPI reports per file (PEP 700). The cutoff
is part of the URL, so one process serves every world and date:

```
http://<host>:5280/<cutoff>/simple/<project>/
```

`<cutoff>` is a date (`2026-08-16`, meaning the first instant of that day in
UTC) or an RFC 3339 timestamp. A client that accepts the JSON form (PEP 691)
gets it, with upload times intact so uv's own `exclude-newer` still works;
anything else gets PEP 503 HTML. Only the listing is filtered: file URLs still
point at the upstream file host, so installs download exactly what PyPI serves.
Pages are cached in memory for an hour, and a cached page is served if the
upstream is unreachable.

```bash
uv run --package swecraft-index-cap swecraft-index-cap serve                 # 0.0.0.0:5280
uv run --package swecraft-index-cap swecraft-index-cap show 2026-08-16 urllib3
# urllib3 before 2026-08-16T00:00:00+00:00: 190 files kept, 2 withheld; newest version 2.7.0
```

## Choosing the cutoff

Any instant after the newest pinned release of the world was uploaded and
before the first later release of any world package was uploaded. `show`
answers both: at a valid cutoff the newest version of every world package is
its pin. For society A of the combined Requests–urllib3 world that window is
2026-07-22 to 2026-08-26, and the runs use 2026-08-16.

## Pointing a world at it

Two places install packages, and they reach the host by different addresses.

**Agent containers** reach the host on the Docker bridge (`172.17.0.1`), as they
reach the forge. Layer `templates/experiment/images/index-cap/Dockerfile`
on the experiment's agent image:

```bash
docker build -f images/index-cap/Dockerfile \
  --build-arg BASE_IMAGE=<experiment>-agent:1 \
  --build-arg INDEX_URL=http://172.17.0.1:5280/2026-08-16/simple/ \
  --build-arg EXCLUDE_NEWER=2026-08-16T00:00:00Z \
  -t <experiment>-agent-capped:1 .
```

It writes `/etc/pip.conf` and `/etc/uv/uv.toml` and sets `PIP_INDEX_URL`,
`UV_DEFAULT_INDEX`, `UV_INDEX_URL` and `UV_EXCLUDE_NEWER`. The files reach every
process whatever a harness does to the environment of its tool shells, and they
are what a venv's own pip reads. The environment outranks a project's own
`[tool.uv]` settings, which a system-level `uv.toml` does not.

**CI jobs** run inside the forge's Docker-in-Docker network namespace, where
`172.17.0.1` is that daemon's own bridge, so they need the host's LAN address.
Set these when the forge is first prepared (like the runner capacity, they are
read only when `state/runner/config.yml` is written):

```bash
SWECRAFT_FORGE_INDEX_URL=http://<host LAN address>:5280/2026-08-16/simple/ \
SWECRAFT_FORGE_EXCLUDE_NEWER=2026-08-16T00:00:00Z just forge-up
```

The runner then passes the same five variables to every job container.

Check both before agents start: in the image, `pip index versions <package>`,
a fresh `python -m venv` and `uv pip install` must all stop at the pin, with the
environment stripped as well (`env -i`); in CI, a job that prints
`pip index versions <package>` must show the same.

## What it does not do

It sets the default, it is not a wall. An agent that names `https://pypi.org/simple`
or clones the upstream from GitHub still reaches the present. Closing that needs
the containers' egress restricted at the network. It also covers Python packages
only. Tell the agents: a line in the charter saying that packages come from a
snapshot of PyPI taken on the cutoff date keeps a maintainer from spending wakes
on why `pip install x==<newer>` finds nothing.

"""Typer CLI for manifest-driven world seeding and verification."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Annotated, NoReturn

import typer
from swecraft_common import HubError
from swecraft_common.git import hub_checkout_head

from swecraft_world.engine import WorldEngine
from swecraft_world.errors import WorldError
from swecraft_world.forge import WorldForgeClient
from swecraft_world.io import load_manifest

DEFAULT_MANIFEST = Path("world.yaml")
DEFAULT_CACHE = Path(".swecraft/world-cache")
DEFAULT_TOKEN_FILE = Path("services/forge/state/admin_token")

app = typer.Typer(
    name="swecraft-world",
    help="Seed, adopt, and verify a manifest-defined Forgejo world.",
    no_args_is_help=True,
    pretty_exceptions_enable=False,
)

_manifest_path = DEFAULT_MANIFEST
_lock_path: Path | None = None
_forge_url = ""
_token_file = DEFAULT_TOKEN_FILE
_cache_dir = DEFAULT_CACHE


@app.callback()
def main(
    manifest: Annotated[
        Path,
        typer.Option("--manifest", help="Human-authored world manifest."),
    ] = DEFAULT_MANIFEST,
    lock: Annotated[
        Path | None,
        typer.Option("--lock", help="Generated lock; defaults beside the manifest."),
    ] = None,
    forge_url: Annotated[
        str | None,
        typer.Option(help="Forgejo base URL; defaults from SWECRAFT_FORGE_HOST/PORT."),
    ] = None,
    token_file: Annotated[
        Path,
        typer.Option(
            help="File containing the Forge admin token; the token is never argv."
        ),
    ] = DEFAULT_TOKEN_FILE,
    cache_dir: Annotated[
        Path,
        typer.Option(help="Host cache for upstream bare repositories."),
    ] = DEFAULT_CACHE,
) -> None:
    global _manifest_path, _lock_path, _forge_url, _token_file, _cache_dir
    _manifest_path = manifest
    _lock_path = lock
    _forge_url = forge_url or _default_forge_url()
    _token_file = token_file
    _cache_dir = cache_dir


@app.command()
def seed(
    replace: Annotated[
        bool,
        typer.Option(
            "--replace",
            help="Delete and recreate non-matching live repositories.",
        ),
    ] = False,
) -> None:
    """Seed every manifest repository and write resumable provenance."""

    engine, lock_path = _engine()
    try:
        lock = engine.seed(replace=replace)
    except WorldError as error:
        _fail(error)
    typer.echo(f"world {lock.world_id}: {lock.status} lock at {lock_path}")


@app.command()
def adopt() -> None:
    """Write provenance for an already-seeded world without mutating it."""

    engine, lock_path = _engine()
    try:
        lock = engine.adopt()
    except WorldError as error:
        _fail(error)
    typer.echo(f"world {lock.world_id}: {lock.status} lock at {lock_path}")


@app.command(name="check")
def check_world() -> None:
    """Verify the complete lock against upstream pins and the live Forge."""

    engine, lock_path = _engine()
    try:
        lock = engine.check()
    except WorldError as error:
        _fail(error)
    typer.echo(f"world {lock.world_id}: verified {len(lock.repos)} repositories")
    typer.echo(f"lock: {lock_path}")


@app.command(name="enable-actions")
def enable_actions() -> None:
    """Turn Forgejo Actions on across a seeded world and re-lock.

    Run after `seed`, never as part of it: `git push` returns before Forgejo
    has evaluated the ref-update events, so enabling Actions inside the seed
    raced that evaluation and fired every workflow carried in every pushed
    tag tree. Each repository is proven quiet before it is enabled, so a
    contaminated world fails here rather than mid-campaign.
    """

    engine, lock_path = _engine()
    try:
        lock = engine.enable_actions()
    except WorldError as error:
        _fail(error)
    enabled = sum(1 for record in lock.repos if record.actions_enabled)
    typer.echo(f"world {lock.world_id}: Actions enabled on {enabled} repositories")
    typer.echo(f"lock: {lock_path}")


@app.command(name="actions-status")
def actions_status() -> None:
    """Report waiting/running/blocked Actions runs per repository."""

    engine, _ = _engine()
    try:
        counts = engine.settle_actions(expected_zero=False)
    except WorldError as error:
        _fail(error)
    total = sum(counts.values())
    for name, count in sorted(counts.items()):
        typer.echo(f"{name}: {count}")
    typer.echo(f"total queued/active Actions runs: {total}")


def _engine() -> tuple[WorldEngine, Path]:
    manifest_path = _manifest_path.expanduser().resolve()
    lock_path = (
        _lock_path.expanduser().resolve()
        if _lock_path is not None
        else manifest_path.with_name("world.lock.json")
    )
    token_path = _token_file.expanduser().resolve()
    try:
        token = token_path.read_text(encoding="utf-8").strip()
    except OSError as error:
        _fail(WorldError(f"could not read Forge token file {token_path}: {error}"))
    if not token:
        _fail(WorldError(f"Forge token file is empty: {token_path}"))
    try:
        document = load_manifest(manifest_path)
        forge = WorldForgeClient(_forge_url, token, document.manifest.org)
        commit = _hub_commit()
        engine = WorldEngine(
            document=document,
            lock_path=lock_path,
            forge=forge,
            cache_dir=_cache_dir.expanduser().resolve(),
            hub_commit=commit,
            report=typer.echo,
        )
    except WorldError as error:
        _fail(error)
    return engine, lock_path


def _hub_commit() -> str:
    try:
        return hub_checkout_head(Path(__file__))
    except HubError as error:
        raise WorldError(f"could not record the Hub revision: {error}") from error


def _default_forge_url() -> str:
    host = os.environ.get("SWECRAFT_FORGE_HOST", "127.0.0.1")
    port = os.environ.get("SWECRAFT_FORGE_PORT", "3400")
    return f"http://{host}:{port}"


def _fail(error: WorldError) -> NoReturn:
    typer.echo(f"error: {error}", err=True)
    raise typer.Exit(2) from error


if __name__ == "__main__":
    app()

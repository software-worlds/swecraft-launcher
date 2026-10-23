"""Typer CLI for the society bundle protocol."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, NoReturn

import typer

from swecraft_society import orchestrator
from swecraft_society import resume as resume_module
from swecraft_society.archive import opened_bundle
from swecraft_society.config import load_checkpoint_config
from swecraft_society.errors import SocietyError
from swecraft_society.huggingface import (
    HuggingFaceTransport,
    parse_hf_uri,
    record_load_provenance,
)
from swecraft_society.inventory import load_manifest, verify_bundle

app = typer.Typer(
    name="swecraft-society",
    help="Checkpoint, load, inspect, and resume private society datasets.",
    no_args_is_help=True,
    pretty_exceptions_enable=False,
)


@app.command()
def checkpoint(
    output: Annotated[
        Path | None,
        typer.Option(
            help="Checkpoint directory; defaults under .swecraft/checkpoints/."
        ),
    ] = None,
    archive: Annotated[
        Path | None,
        typer.Option(help="Also create one standalone local tar.gz transport."),
    ] = None,
    harbor: Annotated[
        str | None,
        typer.Option(help="Override raw Harbor archive policy: 'include' or 'omit'."),
    ] = None,
    config: Annotated[
        Path, typer.Option(help="Local checkpoint defaults file.")
    ] = Path(".swecraft/checkpoint.yaml"),
    project: Annotated[
        str | None, typer.Option(help="Override the source Forge Compose project.")
    ] = None,
    publish: Annotated[
        bool,
        typer.Option(help="Publish the directory to configured private HF."),
    ] = False,
) -> None:
    """Capture the quiescent local society into a verified private checkpoint."""

    if harbor not in (None, "include", "omit"):
        _fail("--harbor must be 'include' or 'omit'")
    try:
        result = orchestrator.checkpoint(
            orchestrator.CheckpointOptions(
                config_path=config,
                output=output,
                archive=archive,
                include_harbor=None if harbor is None else harbor == "include",
                compose_project=project,
            )
        )
    except SocietyError as error:
        _fail(str(error))
    typer.echo(f"checkpoint: {result}")
    if archive is not None:
        typer.echo(f"archive:    {archive}")
    if publish:
        try:
            published = _publish_checkpoint(result, config=config, repo_id=None)
        except SocietyError as error:
            _fail(f"checkpoint remains at {result}; publish failed: {error}")
        typer.echo(
            f"published:  hf://{published.location.repo_id}"
            f"@{published.resolved_revision}"
        )
    typer.echo("private: checkpoint contains society-local credentials")


@app.command("publish")
def publish_command(
    checkpoint_dir: Annotated[
        Path,
        typer.Argument(help="An existing verified checkpoint directory or tar.gz."),
    ],
    config: Annotated[
        Path, typer.Option(help="Local checkpoint defaults file.")
    ] = Path(".swecraft/checkpoint.yaml"),
    repo_id: Annotated[
        str | None,
        typer.Option(help="Override the configured huggingface.repo_id dataset."),
    ] = None,
) -> None:
    """Upload one already-captured checkpoint to the private HF dataset.

    A finalized checkpoint is immutable and self-verifying, so publishing
    needs no quiescence, no lifecycle lock, and no Forge interaction — it
    can run while the society is live, and a failed upload is simply
    retried by running this again.
    """

    try:
        published = _publish_checkpoint(checkpoint_dir, config=config, repo_id=repo_id)
    except SocietyError as error:
        _fail(str(error))
    typer.echo(
        f"published: hf://{published.location.repo_id}@{published.resolved_revision}"
    )


def _publish_checkpoint(checkpoint_dir: Path, *, config: Path, repo_id: str | None):
    hf = load_checkpoint_config(config).config.huggingface
    if repo_id is None:
        if hf is None:
            raise SocietyError(
                "publish requires huggingface.repo_id in the checkpoint config "
                "or an explicit --repo-id"
            )
        repo_id = hf.repo_id
    private = hf.private if hf is not None else True
    with opened_bundle(checkpoint_dir) as root:
        return HuggingFaceTransport().publish(root, repo_id=repo_id, private=private)


@app.command()
def load(
    source: Annotated[
        str,
        typer.Argument(
            help="Local directory/tar.gz or private hf://owner/dataset[@revision]."
        ),
    ],
    dest: Annotated[
        Path | None,
        typer.Argument(help="Empty destination; defaults under .swecraft/loaded/."),
    ] = None,
    config: Annotated[
        Path, typer.Option(help="Local checkpoint defaults file.")
    ] = Path(".swecraft/checkpoint.yaml"),
) -> None:
    """Materialize a verified checkpoint without starting or importing services."""

    downloaded = None
    try:
        if source.startswith("hf://"):
            downloaded = HuggingFaceTransport().fetch(
                parse_hf_uri(source), cache_dir=Path(".swecraft/huggingface")
            )
            source_path = downloaded.path
        else:
            source_path = Path(source)
        result = orchestrator.load(
            orchestrator.LoadOptions(
                source=source_path,
                config_path=config,
                dest=dest,
            )
        )
        provenance = (
            record_load_provenance(result.dest, downloaded)
            if downloaded is not None
            else None
        )
    except SocietyError as error:
        _fail(str(error))
    typer.echo(f"loaded:  {result.dest}")
    if downloaded is not None:
        typer.echo(f"revision: {downloaded.resolved_revision}")
        typer.echo(f"provenance: {provenance}")
    typer.echo("services: not imported or started")


@app.command()
def resume(
    dest: Annotated[
        Path | None,
        typer.Argument(
            help="Pristine loaded checkpoint; defaults under .swecraft/loaded/."
        ),
    ] = None,
    config: Annotated[
        Path, typer.Option(help="Local checkpoint defaults file.")
    ] = Path(".swecraft/checkpoint.yaml"),
    project: Annotated[
        str | None, typer.Option(help="Compose project for the resumed copy.")
    ] = None,
    port: Annotated[int, typer.Option(help="Host port for resumed Forge.")] = 3400,
    host: Annotated[
        str, typer.Option(help="Host address for resumed Forge.")
    ] = "127.0.0.1",
) -> None:
    """Turn one loaded checkpoint into a mutable running society."""

    try:
        report = resume_module.resume(
            resume_module.ResumeOptions(
                config_path=config,
                dest=dest,
                compose_project=project,
                port=port,
                host=host,
            ),
            warn=lambda message: typer.echo(f"warning: {message}", err=True),
        )
    except SocietyError as error:
        _fail(str(error))
    typer.echo(f"resumed: {report.dest}")
    typer.echo(f"forge:   {report.forge_url}")
    typer.echo(f"project: {report.compose_project}")
    typer.echo(f"agents:  {', '.join(report.agents)}")
    typer.echo(f"config:  {report.config_path}")


@app.command()
def inspect(
    bundle: Annotated[
        Path, typer.Argument(help="A bundle directory or its tar.gz transport.")
    ],
    verify: Annotated[
        bool,
        typer.Option(
            "--verify/--no-verify",
            help="Check every file against the manifest inventory.",
        ),
    ] = True,
) -> None:
    """Describe a bundle and prove its contents match the manifest."""

    try:
        with opened_bundle(bundle) as root:
            manifest = verify_bundle(root) if verify else load_manifest(root)
    except SocietyError as error:
        _fail(str(error))
    typer.echo(f"checkpoint: {manifest.checkpoint_id}")
    typer.echo(f"created:    {manifest.created_at.isoformat(timespec='seconds')}")
    typer.echo(f"world:      {manifest.world_id}")
    typer.echo(f"hub:        {manifest.hub_commit}")
    typer.echo(
        f"forge:      {manifest.forge.source_url} "
        f"(project {manifest.forge.compose_project})"
    )
    apparatus = manifest.apparatus.git_ref or "working tree"
    if manifest.apparatus.git_commit:
        apparatus += f" @ {manifest.apparatus.git_commit}"
    if manifest.apparatus.dirty:
        apparatus += " (dirty)"
    typer.echo(f"apparatus:  {apparatus}")
    typer.echo("repositories:")
    for head in manifest.heads:
        evolved = "evolved" if head.current_head != head.baseline_commit else "at seed"
        typer.echo(f"  {head.name}: {head.current_head} ({evolved})")
    typer.echo("contributors:")
    for contributor in manifest.contributors:
        total = sum(entry.size for entry in contributor.files)
        typer.echo(
            f"  {contributor.name}: {len(contributor.files)} files, {total} bytes"
        )
    if manifest.omissions:
        typer.echo("omissions (partial checkpoint):")
        for omitted in manifest.omissions:
            typer.echo(f"  {omitted}")
    if verify:
        typer.echo("inventory: verified")


def _fail(message: str) -> NoReturn:
    typer.echo(f"error: {message}", err=True)
    raise typer.Exit(code=1)

"""Typer command-line interface for the one-shot Hub runner."""

from __future__ import annotations

import shlex
from pathlib import Path
from typing import Annotated

import typer

from swecraft_runner.pricing import ModelPricing
from swecraft_runner.runner import (
    DEFAULT_HARBOR_COMMAND,
    DockerDaemonConfig,
    RunnerError,
    RunRequest,
    run_once,
)

app = typer.Typer(
    name="swecraft-runner",
    help="Run one coding agent against one Git repository.",
    no_args_is_help=True,
    pretty_exceptions_enable=False,
)


@app.callback()
def main() -> None:
    """Run one coding agent against one Git repository."""


@app.command("run")
def run_command(
    repository: Annotated[
        str,
        typer.Option("--repo", help="Public Git URL or local repository path."),
    ],
    model: Annotated[str, typer.Option(help="Model identifier passed to Harbor.")],
    revision: Annotated[
        str, typer.Option("--ref", help="Branch, tag, or exact commit to fetch.")
    ] = "HEAD",
    prompt: Annotated[str | None, typer.Option(help="Inline task prompt.")] = None,
    prompt_file: Annotated[
        Path | None,
        typer.Option(
            exists=True,
            dir_okay=False,
            readable=True,
            help="UTF-8 file containing the task prompt.",
        ),
    ] = None,
    agent: Annotated[
        str, typer.Option(help="Stock Harbor agent name.")
    ] = "claude-code",
    agent_import_path: Annotated[
        str | None,
        typer.Option(
            "--agent-import-path",
            help="Harbor agent class (module.path:ClassName) run in place of "
            "the stock agent; --agent then only labels the run.",
        ),
    ] = None,
    image: Annotated[
        str, typer.Option(help="Docker image for the task.")
    ] = "node:22-slim",
    tool_dir: Annotated[
        Path | None,
        typer.Option(
            exists=True,
            file_okay=False,
            readable=True,
            help="Trusted tools or reference files mounted at /app/tools.",
        ),
    ] = None,
    docker_daemon_image: Annotated[
        str | None,
        typer.Option(
            help=(
                "Digest-pinned DinD image for one disposable daemon exposed "
                "only to this turn; never mounts the host Docker socket."
            )
        ),
    ] = None,
    agent_env: Annotated[
        list[str] | None,
        typer.Option(
            "--agent-env",
            help="Host environment variable to expose only to the Harbor agent.",
        ),
    ] = None,
    agent_kwarg: Annotated[
        list[str] | None,
        typer.Option(
            "--agent-kwarg",
            help="Non-secret Harbor agent constructor option as NAME=VALUE.",
        ),
    ] = None,
    input_cost_per_million: Annotated[
        float | None,
        typer.Option(
            min=0,
            help="Fallback USD cost per million input tokens.",
        ),
    ] = None,
    cached_input_cost_per_million: Annotated[
        float | None,
        typer.Option(
            min=0,
            help="Fallback USD cost per million cached input tokens.",
        ),
    ] = None,
    output_cost_per_million: Annotated[
        float | None,
        typer.Option(
            min=0,
            help="Fallback USD cost per million output tokens.",
        ),
    ] = None,
    output_dir: Annotated[
        Path, typer.Option(help="Parent directory for immutable run outputs.")
    ] = Path(".swecraft/runs"),
    harbor_command: Annotated[
        str,
        typer.Option(
            help="Shell-like Harbor command; arguments are parsed with shlex. "
            "Defaults to the runner's swecraft-harbor launcher: the pinned Harbor "
            "with the in-process fixes applied."
        ),
    ] = shlex.join(DEFAULT_HARBOR_COMMAND),
    timeout_seconds: Annotated[
        int,
        typer.Option(
            min=1,
            help="Maximum agent execution time in seconds, enforced by Harbor "
            "inside the trial.",
        ),
    ] = 3_600,
    fetch_depth: Annotated[
        int,
        typer.Option(
            min=0,
            help="Git fetch depth; 0 fetches full history.",
        ),
    ] = 0,
    extra_ref: Annotated[
        list[str] | None,
        typer.Option(
            "--extra-ref",
            help="Additional branch fetched as origin/<name>. Repeatable.",
        ),
    ] = None,
) -> None:
    """Fetch REPO, execute one Harbor trial, and collect changed files."""

    if (prompt is None) == (prompt_file is None):
        raise typer.BadParameter("provide exactly one of --prompt or --prompt-file")
    if prompt_file is not None:
        try:
            task_prompt = prompt_file.read_text(encoding="utf-8")
        except UnicodeDecodeError as error:
            raise typer.BadParameter("--prompt-file must be UTF-8") from error
    else:
        task_prompt = prompt or ""
    agent_kwargs: dict[str, str] = {}
    for entry in agent_kwarg or ():
        name, separator, value = entry.partition("=")
        if not separator or not name or not value:
            raise typer.BadParameter("--agent-kwarg must be NAME=VALUE")
        if name in agent_kwargs:
            raise typer.BadParameter(f"duplicate --agent-kwarg: {name}")
        agent_kwargs[name] = value
    pricing_values = (
        input_cost_per_million,
        cached_input_cost_per_million,
        output_cost_per_million,
    )
    pricing = None
    if any(value is not None for value in pricing_values):
        if input_cost_per_million is None or output_cost_per_million is None:
            raise typer.BadParameter(
                "input and output costs are both required when fallback pricing is set"
            )
        try:
            pricing = ModelPricing(
                input_per_million=input_cost_per_million,
                cached_input_per_million=cached_input_cost_per_million,
                output_per_million=output_cost_per_million,
            )
        except ValueError as error:
            raise typer.BadParameter(str(error)) from error
    command = tuple(shlex.split(harbor_command))
    try:
        result = run_once(
            RunRequest(
                repository=repository,
                revision=revision,
                prompt=task_prompt,
                model=model,
                output_dir=output_dir,
                agent=agent,
                agent_import_path=agent_import_path,
                image=image,
                tool_directory=tool_dir,
                docker_daemon=(
                    DockerDaemonConfig(image=docker_daemon_image)
                    if docker_daemon_image
                    else None
                ),
                agent_env_names=tuple(agent_env or ()),
                agent_kwargs=agent_kwargs,
                model_pricing=pricing,
                harbor_command=command,
                timeout_seconds=timeout_seconds,
                fetch_depth=fetch_depth,
                extra_refs=tuple(extra_ref or ()),
            )
        )
    except RunnerError as error:
        typer.echo(f"error: {error}", err=True)
        raise typer.Exit(2) from error

    typer.echo(result.response_path.read_text(encoding="utf-8").rstrip())
    typer.echo()
    typer.echo(f"Run: {result.run_id}")
    typer.echo(f"Commit: {result.source_sha}")
    typer.echo(f"Changed files: {len(result.changed_files)}")
    typer.echo(f"Output: {result.run_dir}")


if __name__ == "__main__":
    app()

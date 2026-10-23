"""CLI: validate the suite, run one evaluation, score records, or loop."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Annotated, NoReturn

import typer

from swecraft_evaluator.bundle import (
    cell_id_from_array_index,
    collect_bundle_results,
    export_evaluation_bundle,
    run_bundle_cell,
)
from swecraft_evaluator.environment import (
    SEED,
    Forge,
    environment_digest,
    load_world,
    parse_environment,
    seed_environment,
)
from swecraft_evaluator.errors import EvaluatorError
from swecraft_evaluator.executor import (
    ApptainerExecutor,
    DockerExecutor,
    EvaluatorExecutor,
)
from swecraft_evaluator.loop import LoopSettings, loop, reconcile, run_scored
from swecraft_evaluator.manifest import load_manifest
from swecraft_evaluator.runner import RunnerSettings, run_evaluation
from swecraft_evaluator.score import latest_reference, load_records, score_record

app = typer.Typer(
    name="swecraft-evaluator",
    help="Run downstream performance evaluations of a Hub society.",
    no_args_is_help=True,
    pretty_exceptions_enable=False,
)

_manifest = Path("evaluation.yaml")
_records = Path(".swecraft/evaluator/records")
_forge_url = "http://127.0.0.1:3400"
_token_file = Path(".swecraft/forge/admin_token")
_executor_name = "docker"
_docker_command = "docker"
_apptainer_command = "apptainer"


@app.callback()
def main(
    manifest: Annotated[Path, typer.Option(help="Evaluation manifest.")] = _manifest,
    records: Annotated[Path, typer.Option(help="Run records directory.")] = _records,
    forge_url: Annotated[str, typer.Option(help="Forge base URL.")] = _forge_url,
    forge_token_file: Annotated[
        Path, typer.Option(help="Forge token file; the token is never argv.")
    ] = _token_file,
    executor: Annotated[
        str,
        typer.Option(help="Container executor for run/worker: docker or apptainer."),
    ] = _executor_name,
    docker_command: Annotated[
        str, typer.Option(help="Docker executable for the docker executor.")
    ] = _docker_command,
    apptainer_command: Annotated[
        str, typer.Option(help="Apptainer executable for the apptainer executor.")
    ] = _apptainer_command,
) -> None:
    global _manifest, _records, _forge_url, _token_file
    global _executor_name, _docker_command, _apptainer_command
    _manifest = manifest
    _records = records
    _forge_url = forge_url
    _token_file = forge_token_file
    _executor_name = executor
    _docker_command = docker_command
    _apptainer_command = apptainer_command


@app.command()
def validate() -> None:
    """Parse the manifest and its world lock; report what they define."""

    manifest, world = _load()
    known = {repo.name for repo in world.repos}
    unknown = sorted(set(manifest.subjects) - known)
    if unknown:
        _fail(
            EvaluatorError(f"subjects are not world repositories: {', '.join(unknown)}")
        )
    typer.echo(
        f"{len(manifest.subjects)} subject(s), "
        f"{len(manifest.evaluators)} evaluator(s), "
        f"evaluations: {', '.join(sorted(manifest.evaluations))}; "
        f"world: {len(world.repos)} repositories"
    )


@app.command()
def run(
    evaluation: Annotated[str, typer.Argument(help="Evaluation name.")],
    env: Annotated[
        str, typer.Option(help="world@seed, world@latest, or world@seed+repo:tag")
    ] = SEED,
) -> None:
    """Run one evaluation on one environment and score it against seed.

    Running ``world@seed`` explicitly always measures a fresh baseline record;
    other environments reuse the newest seed reference.
    """

    manifest, world = _load()
    try:
        forge = _forge()
        environment = parse_environment(env, world, forge)
        if env == SEED:
            record = run_evaluation(
                manifest, world, evaluation, environment, env, _runner()
            )
            typer.echo(f"seed reference refreshed: {record.payload['id']}")
            return
        score = run_scored(
            manifest, world, evaluation, environment, env, _runner(), typer.echo
        )
    except EvaluatorError as error:
        _fail(error)
    typer.echo(json.dumps(score, indent=2, sort_keys=True))


@app.command(name="export")
def export_command(
    evaluation: Annotated[str, typer.Argument(help="Evaluation name.")],
    bundle: Annotated[Path, typer.Option(help="Destination bundle directory.")],
    env: Annotated[
        str, typer.Option(help="world@seed, world@latest, or world@seed+repo:tag")
    ] = SEED,
) -> None:
    """Export a credential-free bundle for offline cell workers."""

    manifest, world = _load()
    try:
        environment = parse_environment(env, world, _forge())
        exported = export_evaluation_bundle(
            manifest,
            world,
            evaluation,
            environment,
            env,
            _runner(),
            bundle,
        )
    except EvaluatorError as error:
        _fail(error)
    typer.echo(
        f"exported {len(exported.payload['expected_cells'])} cell(s): {exported.path}"
    )


@app.command()
def worker(
    bundle: Annotated[Path, typer.Argument(help="Evaluation bundle directory.")],
    cell: Annotated[
        str | None, typer.Option(help="Cell id from bundle cells.txt.")
    ] = None,
    array_index: Annotated[
        int | None, typer.Option(help="1-based Slurm array index.")
    ] = None,
    results: Annotated[
        Path | None, typer.Option(help="Cell results directory.")
    ] = None,
) -> None:
    """Run one exported cell and write its terminal result bundle."""

    try:
        resolved_cell = cell or _array_cell(bundle, array_index)
        result = run_bundle_cell(bundle, resolved_cell, results, _executor())
    except EvaluatorError as error:
        _fail(error)
    typer.echo(f"cell result: {result['cell_id']}")


@app.command()
def collect(
    bundle: Annotated[Path, typer.Argument(help="Evaluation bundle directory.")],
    results: Annotated[
        Path | None, typer.Option(help="Cell results directory.")
    ] = None,
) -> None:
    """Collect terminal cell results into one aggregate record.json."""

    try:
        record = collect_bundle_results(bundle, _records, results)
    except EvaluatorError as error:
        _fail(error)
    typer.echo(f"record collected: {record.path}")


@app.command()
def score(
    evaluation: Annotated[
        str | None, typer.Argument(help="Evaluation name; omit for all.")
    ] = None,
) -> None:
    """Score the newest candidate record per evaluation against seed."""

    manifest, world = _load()
    seed_digest = environment_digest(seed_environment(world))
    records = load_records(_records)
    names = [evaluation] if evaluation else sorted(manifest.evaluations)
    scores = []
    try:
        for name in names:
            candidate = next(
                (
                    record
                    for record in reversed(records)
                    if record.get("evaluation") == name
                    and record.get("environment_digest") != seed_digest
                ),
                None,
            )
            reference = (
                latest_reference(
                    manifest, records, name, seed_digest, candidate=candidate
                )
                if candidate is not None
                else None
            )
            if reference is None or candidate is None:
                scores.append({"evaluation": name, "aggregates": None})
                continue
            scores.append(score_record(manifest, candidate, reference))
    except EvaluatorError as error:
        _fail(error)
    typer.echo(json.dumps(scores, indent=2, sort_keys=True))


@app.command(name="loop")
def loop_command(
    cadence_evaluation: Annotated[
        str, typer.Argument(help="Evaluation the cadence sweep runs.")
    ],
    state: Annotated[Path, typer.Option()] = Path(".swecraft/evaluator/state.json"),
    interval_seconds: Annotated[float, typer.Option(min=1)] = 10800.0,
    poll_seconds: Annotated[float, typer.Option(min=1)] = 300.0,
    once: Annotated[bool, typer.Option("--once", help="One pass, then exit.")] = False,
) -> None:
    """Cadence sweeps on world@latest plus release evaluations, forever."""

    manifest, world = _load()
    settings = LoopSettings(
        state_path=state,
        cadence_evaluation=cadence_evaluation,
        interval_seconds=interval_seconds,
    )
    try:
        forge = _forge()
        if once:
            produced = reconcile(
                manifest, world, forge, _runner(), settings, typer.echo
            )
            typer.echo(f"produced {len(produced)} score(s)")
            return
        loop(
            manifest,
            world,
            forge,
            _runner(),
            settings,
            poll_seconds,
            typer.echo,
        )
    except KeyboardInterrupt:
        typer.echo("stopped")
    except EvaluatorError as error:
        _fail(error)


def _load():
    try:
        manifest = load_manifest(_manifest.resolve())
        return manifest, load_world(manifest.world_lock)
    except EvaluatorError as error:
        _fail(error)


def _forge() -> Forge:
    return Forge(_forge_url, _token())


def _runner() -> RunnerSettings:
    return RunnerSettings(
        forge_url=_forge_url,
        forge_token=_token(),
        records_dir=_records.resolve(),
        executor=_executor(),
    )


def _executor() -> EvaluatorExecutor:
    if _executor_name == "docker":
        return DockerExecutor((_docker_command,))
    if _executor_name == "apptainer":
        return ApptainerExecutor((_apptainer_command,))
    raise EvaluatorError("executor must be docker or apptainer")


def _array_cell(bundle: Path, array_index: int | None) -> str:
    if array_index is None:
        value = os.environ.get("SLURM_ARRAY_TASK_ID")
        if value is None:
            raise EvaluatorError("provide --cell or --array-index")
        try:
            array_index = int(value)
        except ValueError:
            raise EvaluatorError(f"invalid SLURM_ARRAY_TASK_ID {value!r}") from None
    return cell_id_from_array_index(bundle, array_index)


def _token() -> str:
    try:
        return _token_file.read_text(encoding="utf-8").strip()
    except OSError as error:
        raise EvaluatorError(f"could not read token file: {error}") from None


def _fail(error: Exception) -> NoReturn:
    typer.echo(f"error: {error}", err=True)
    raise typer.Exit(code=1)


if __name__ == "__main__":
    app()

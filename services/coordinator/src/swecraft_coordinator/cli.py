"""Typer command-line interface for the coordinator."""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Annotated

import typer

from swecraft_coordinator import CoordinatorError
from swecraft_coordinator.bootstrap import bootstrap_agent
from swecraft_coordinator.cadence import checkpoint_request_path
from swecraft_coordinator.config import CoordinatorConfig
from swecraft_coordinator.onboard import onboard_agent
from swecraft_coordinator.review import plan_review_jobs, run_review_batch
from swecraft_coordinator.review_models import load_review_manifest
from swecraft_coordinator.scheduler import serve_forever
from swecraft_coordinator.state import StateStore
from swecraft_coordinator.wake import wake_agent

app = typer.Typer(
    name="swecraft-coordinator",
    help="Run autonomous per-repository maintainer agents on a fixed cadence.",
    no_args_is_help=True,
    pretty_exceptions_enable=False,
)

_config_path: Path | None = None


@app.callback()
def main(
    config: Annotated[
        Path | None,
        typer.Option("--config", help="Path to coordinator.yaml."),
    ] = None,
) -> None:
    """Run autonomous per-repository maintainer agents on a fixed cadence."""

    global _config_path
    _config_path = config
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s"
    )


def _load() -> CoordinatorConfig:
    try:
        return CoordinatorConfig.load(_config_path)
    except CoordinatorError as error:
        typer.echo(f"error: {error}", err=True)
        raise typer.Exit(2) from error


@app.command()
def agents() -> None:
    """List configured agents and their scheduler state."""

    config = _load()
    state = StateStore(config.database)
    for profile in config.agents:
        status = state.status(profile.name)
        wakes, cost = state.day_usage(profile.name)
        line = (
            f"{profile.name}  home {profile.home_repo}  maintains "
            f"{', '.join(profile.write_repos)}  every {profile.interval_seconds}s"
        )
        if status is None:
            typer.echo(f"{line}  (never woken)")
            continue
        active = "  ACTIVE" if status.active_started else ""
        budget = ""
        if config.budget.wakes:
            used = state.run_wakes(profile.name)
            budget = f"  budget {used}/{config.budget.wakes} wakes"
        typer.echo(
            f"{line}  due {status.next_due}  last {status.last_outcome or '-'}"
            f"  today {wakes} wake(s) / {cost:.4f} USD{budget}"
            f"  failures {status.consecutive_failures}{active}"
        )


@app.command()
def onboard(
    name: Annotated[str, typer.Argument(help="Agent name from coordinator.yaml.")],
    goal_file: Annotated[
        Path | None,
        typer.Option(
            exists=True,
            dir_okay=False,
            readable=True,
            help="Initial GOAL.md content (only used when GOAL.md is missing).",
        ),
    ] = None,
    adopt_existing: Annotated[
        bool,
        typer.Option(
            "--adopt-existing",
            help="Take over an existing forge user the coordinator does not "
            "manage yet. Resets that user's password — only for users that "
            "genuinely should become coordinator-managed agents.",
        ),
    ] = False,
) -> None:
    """Provision the agent's forge user, token, access, and memory files."""

    config = _load()
    goal = goal_file.read_text(encoding="utf-8") if goal_file else None
    state = StateStore(config.database)
    try:
        report = onboard_agent(
            config,
            config.agent(name),
            state=state,
            goal=goal,
            adopt_existing=adopt_existing,
        )
    except CoordinatorError as error:
        typer.echo(f"error: {error}", err=True)
        raise typer.Exit(2) from error
    state.ensure_agent(name)
    if report.user_created:
        token_note = "user created"
    elif report.token_rotated:
        token_note = "token rotated"
    else:
        token_note = "token reused"
    revoked = (
        f"; revoked: {', '.join(report.revoked_repos)}" if report.revoked_repos else ""
    )
    typer.echo(
        f"Onboarded {report.agent}: home {report.home_repo}"
        f"{' (created)' if report.home_created else ''}; maintains "
        f"{', '.join(report.write_repos)} ({token_note};"
        f" seeded: {', '.join(report.seeded_files) or 'nothing new'}{revoked})"
    )


@app.command()
def bootstrap(
    name: Annotated[str, typer.Argument(help="Agent name from coordinator.yaml.")],
) -> None:
    """Run the one-time orientation turn that writes the initial memory."""

    config = _load()
    state = StateStore(config.database)
    try:
        report = bootstrap_agent(config, config.agent(name), state)
    except CoordinatorError as error:
        typer.echo(f"error: {error}", err=True)
        raise typer.Exit(2) from error
    typer.echo(
        f"Bootstrapped {report.agent} at {report.memory_commit[:12]} "
        f"(run {report.run_id}; {', '.join(report.changed_files)})"
    )


@app.command()
def review(
    manifest: Annotated[
        Path, typer.Argument(help="Review manifest (reviews.yaml).")
    ] = Path("reviews.yaml"),
    check: Annotated[
        bool,
        typer.Option("--check", help="Validate and report planned jobs only."),
    ] = False,
    force: Annotated[
        bool,
        typer.Option("--force", help="Re-run completed jobs in this batch."),
    ] = False,
    parallel: Annotated[
        int,
        typer.Option("--parallel", min=1, max=32, help="Concurrent review turns."),
    ] = 1,
) -> None:
    """Run an end-of-run review batch: wake-shaped survey turns, no writes.

    Peer turns let each agent choose its own review targets and answer
    step by step through the staged CLI; the society review asks each
    agent about the society's coordination and infrastructure. Nothing is
    staged beyond the questions and answer schema — the reviewer works
    from its own memory, repository, and forge browsing. Answers are
    validated host-side; re-running dispatches only the jobs still
    missing a valid answer, and the batch seals once every job has one.
    """

    config = _load()
    try:
        review_manifest = load_review_manifest(manifest)
        if check:
            jobs = plan_review_jobs(review_manifest, config)
            peers = sum(1 for job in jobs if job.kind == "peer")
            typer.echo(
                f"{peers} peer review job(s), {len(jobs) - peers} society review job(s)"
            )
            return
        result = run_review_batch(
            review_manifest, config, force=force, parallel=parallel
        )
    except CoordinatorError as error:
        typer.echo(f"error: {error}", err=True)
        raise typer.Exit(2) from error
    typer.echo(
        f"batch: {result.batch_dir} "
        f"({result.peer_reviews} peer review(s) from {result.peer_turns} "
        f"evaluator turn(s), {result.society_reviews} society, "
        f"{result.failed_jobs} failed)"
    )
    if result.failed_jobs:
        raise typer.Exit(1)


@app.command()
def wake(
    name: Annotated[str, typer.Argument(help="Agent name from coordinator.yaml.")],
) -> None:
    """Wake one agent immediately (still one active wake at a time)."""

    config = _load()
    state = StateStore(config.database)
    state.ensure_agent(name)
    try:
        report = wake_agent(config, config.agent(name), state)
    except CoordinatorError as error:
        typer.echo(f"error: {error}", err=True)
        raise typer.Exit(2) from error
    typer.echo(
        f"Wake {report.run_id}: {report.outcome or 'no envelope'}"
        + (f" — {report.summary}" if report.summary else "")
    )
    if report.memory_commit:
        typer.echo(f"Memory: {report.memory_commit[:12]}")
    typer.echo(f"Cost: {report.cost_usd:.4f} USD")


@app.command()
def recover(
    name: Annotated[str, typer.Argument(help="Agent name from coordinator.yaml.")],
) -> None:
    """Release the claim left by a dead wake so the agent can run again.

    Use only after the wake's process and trial container are actually
    gone — recovering a live wake would allow a double claim. Recorded as
    an operator cancellation, not an agent failure: no backoff is charged,
    and the next wake is told to reconcile against live forge state.
    """

    config = _load()
    config.agent(name)
    if StateStore(config.database).recover(name):
        typer.echo(f"Released {name}'s claim; next wake is due in about a minute.")
    else:
        typer.echo(f"{name} has no active wake to recover.")


@app.command("checkpoint-now")
def checkpoint_now() -> None:
    """Request an off-cadence boundary checkpoint from the running serve loop.

    Control stays CLI-only: this writes a flag file the serve loop polls.
    The loop then runs its normal boundary path — stop dispatching, drain
    in-flight wakes, capture at the quiet boundary, resume — so no process
    needs stopping and no wake is ever cut mid-conversation. Without a
    running serve loop the request waits until one starts.
    """

    config = _load()
    path = checkpoint_request_path(config)
    if path.exists():
        typer.echo("checkpoint request already pending")
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("requested\n", encoding="utf-8")
    except OSError as error:
        typer.echo(f"error: could not write {path}: {error}", err=True)
        raise typer.Exit(2) from error
    typer.echo(
        "checkpoint requested; the serve loop will drain in-flight wakes and "
        "capture at the next quiet boundary"
    )


@app.command()
def budget(
    seed: Annotated[
        bool,
        typer.Option(
            "--seed",
            help="Commit the budget file to the board (budget.ledger) when it "
            "does not exist yet, using the admin token.",
        ),
    ] = False,
    protect: Annotated[
        bool,
        typer.Option(
            "--protect",
            help="With --seed: require one approval from another maintainer "
            "before a change to the board's default branch can merge, and refuse "
            "direct pushes to it, so nobody merges their own transfer.",
        ),
    ] = False,
) -> None:
    """Show each agent's run budget in wakes, used and left; seed the ledger."""

    from swecraft_coordinator.budget import agent_cap, render_ledger
    from swecraft_coordinator.forge_admin import AdminForgeClient

    config = _load()
    state = StateStore(config.database)
    ledger = config.budget.ledger
    if seed:
        if ledger is None:
            typer.echo("error: budget.ledger is not configured", err=True)
            raise typer.Exit(2)
        forge = AdminForgeClient(config.forge_url, config.resolved_admin_token())
        if forge.get_file(ledger.repo, ledger.path) is not None:
            typer.echo(
                f"error: {ledger} already exists; edit it by pull request", err=True
            )
            raise typer.Exit(2)
        sha = forge.create_file(
            ledger.repo,
            ledger.path,
            render_ledger(config),
            message="Seed the run budget in wakes",
        )
        typer.echo(f"seeded {ledger} @{sha[:8]}")
        if protect:
            repository = forge.get_repository(ledger.repo) or {}
            branch = repository.get("default_branch") or "main"
            forge.require_review(ledger.repo, branch)
            typer.echo(
                f"{ledger.repo}:{branch} now merges only with another maintainer's "
                "approval"
            )
        return
    if protect:
        typer.echo("error: --protect goes with --seed", err=True)
        raise typer.Exit(2)
    for profile in config.agents:
        cap = agent_cap(config, state, profile.name)
        used = state.run_wakes(profile.name)
        if cap is not None:
            typer.echo(
                f"{profile.name}  budget {cap} wakes  used {used}  "
                f"left {max(cap - used, 0)}"
            )
        else:
            typer.echo(f"{profile.name}  no budget  used {used} wakes")
    if ledger is not None:
        sha, _ = state.ledger_allocation()
        typer.echo(
            f"ledger {ledger}: " + (f"applied @{sha[:8]}" if sha else "not applied yet")
        )


@app.command()
def serve() -> None:
    """Wake every configured agent on its cadence until interrupted."""

    config = _load()
    try:
        # Every wake publishes memory as the operator; fail here, not mid-run.
        config.resolved_admin_token()
    except CoordinatorError as error:
        typer.echo(f"error: {error}", err=True)
        raise typer.Exit(2) from error
    state = StateStore(config.database)
    stop = threading.Event()
    # The loop runs in a worker thread so Ctrl-C (delivered to the main
    # thread) can set the stop event and let serve_forever's drain path run.
    # Raising KeyboardInterrupt inside serve_forever instead would unwind
    # past the drain into ThreadPoolExecutor.__exit__, which blocks silently
    # for up to a full wake with no indication of why.
    # daemon=True so a second Ctrl-C can actually abandon: the interpreter
    # then exits without waiting for the loop thread. The normal path always
    # joins explicitly, so nothing is abandoned unless asked twice.
    loop = threading.Thread(
        target=serve_forever, args=(config, state, stop), name="serve-loop", daemon=True
    )
    loop.start()
    try:
        while loop.is_alive():
            loop.join(timeout=0.5)
    except KeyboardInterrupt:
        stop.set()
        typer.echo(
            "stopping: waiting for in-flight wakes to finish "
            "(interrupting again abandons their claims until they go stale)"
        )
        try:
            loop.join()
        except KeyboardInterrupt:
            typer.echo("abandoned; in-flight claims release when they go stale")
            raise typer.Exit(130) from None
    typer.echo("stopped")


if __name__ == "__main__":
    app()

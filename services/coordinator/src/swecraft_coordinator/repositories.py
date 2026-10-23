"""The repositories a turn stages: the home, and the rest under /app/repos.

An agent's home (``AgentProfile.home_repo``) is the checkout the runner
stages at ``/app/repository`` and the repository whose memory branch is
mounted at ``/app/memory``. Every other repository the agent maintains
(``AgentProfile.work_repos``) is staged by the runner as a real checkout at
``/app/repos/<name>``, for bootstrap and wakes alike: the agent never clones
anything itself, so the orientation turn's no-fetch contract holds and a
wake's push remotes are set up by the runner, authenticated as the agent.
"""

from __future__ import annotations

from dataclasses import dataclass

from swecraft_common.forge import ForgeClient
from swecraft_runner.runner import WorkRepository

from swecraft_coordinator.config import (
    AgentProfile,
    CollaborationMode,
    CoordinatorConfig,
)


@dataclass(frozen=True)
class StagedRepository:
    """A maintained repository as the prompt describes it."""

    slug: str
    name: str
    default_branch: str

    def as_variables(self) -> dict[str, str]:
        return {
            "slug": self.slug,
            "name": self.name,
            "default_branch": self.default_branch,
        }


def default_branch(forge: ForgeClient, repo: str) -> str:
    repository = forge.get_repository(repo) or {}
    return repository.get("default_branch") or "main"


def work_repositories(
    config: CoordinatorConfig,
    forge: ForgeClient,
    profile: AgentProfile,
    *,
    push: bool,
) -> tuple[tuple[WorkRepository, ...], tuple[StagedRepository, ...]]:
    """What the runner stages under /app/repos for this agent, and how to name it.

    ``push`` gives each checkout a push remote on the container-reachable
    forge (a wake, direct action); without it the checkouts are read-only in
    effect (the orientation turn discards changes there).
    """

    staged: list[WorkRepository] = []
    described: list[StagedRepository] = []
    for repo in profile.work_repos:
        branch = default_branch(forge, repo)
        name = repo.split("/", 1)[1]
        staged.append(
            WorkRepository(
                name=name,
                repository=f"{config.forge_url}/{repo}.git",
                revision=branch,
                push_remote_url=f"{config.push_url}/{repo}.git" if push else None,
            )
        )
        described.append(StagedRepository(slug=repo, name=name, default_branch=branch))
    return tuple(staged), tuple(described)


def prompt_variables(
    profile: AgentProfile,
    home_default_branch: str,
    described: tuple[StagedRepository, ...],
    *,
    collaboration: CollaborationMode = "shared",
) -> dict[str, object]:
    """The repository variables every prompt template receives.

    Python renders the nouns and the templates keep the sentences: a
    template says ``the autonomous maintainer of {{ MAINTAINED }}`` and
    ``edit the checkout — {{ CHECKOUTS }} —`` instead of looping over the
    repositories inline, and its remaining ``{% if %}`` blocks only choose
    between pieces of prose.

    ``MAINTAINED``
        The repositories the agent maintains, backticked and comma-joined:
        ```swecraft/requests`, `swecraft/urllib3```.
    ``MAINTAINED_NOUN``
        ``repository`` or ``repositories``, for ``MAINTAINED``.
    ``CHECKOUTS``
        Where each maintained repository is staged:
        ```/app/repository` is `swecraft/demo`, and `/app/repos/lib` is
        `swecraft/lib```;
        for a dedicated home, only the ``/app/repos/<name>`` checkouts.
    ``SEVERAL``
        Whether more than one repository is maintained.
    ``ISOLATED``
        ``collaboration == "isolated"``: the wake prompt drops its
        sibling-facing sentences and the notifications step, and states
        the access boundary instead of naming the treatment.

    The raw values stay available for prompt overrides: ``REPO`` (the home,
    as in the one-repository layout), ``HOME_REPO``, ``DEDICATED_HOME``,
    ``WRITE_REPOS``, ``WORK_REPOS`` (slug/name/default_branch dicts),
    ``DEFAULT_BRANCH`` (the home's) and ``COLLABORATION``.
    """

    several = len(profile.write_repos) > 1
    return {
        "AGENT": profile.name,
        "REPO": profile.home_repo,
        "HOME_REPO": profile.home_repo,
        "DEDICATED_HOME": profile.dedicated_home,
        "WRITE_REPOS": list(profile.write_repos),
        "WORK_REPOS": [repository.as_variables() for repository in described],
        "DEFAULT_BRANCH": home_default_branch,
        "COLLABORATION": collaboration,
        "ISOLATED": collaboration == "isolated",
        "SEVERAL": several,
        "MAINTAINED": _backticked(profile.write_repos),
        "MAINTAINED_NOUN": "repositories" if several else "repository",
        "CHECKOUTS": _checkouts(profile, described),
    }


def _backticked(names: tuple[str, ...]) -> str:
    return ", ".join(f"`{name}`" for name in names)


def _checkouts(profile: AgentProfile, described: tuple[StagedRepository, ...]) -> str:
    staged = ", ".join(
        f"`/app/repos/{repository.name}` is `{repository.slug}`"
        for repository in described
    )
    if profile.dedicated_home:
        return staged
    home = f"`/app/repository` is `{profile.home_repo}`"
    return f"{home}, and {staged}" if staged else home

"""Provision one agent: forge identity, its home, scoped access, token, memory seed."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any

from swecraft_common.forge import ForgeClient, ForgeError

from swecraft_coordinator import CoordinatorError
from swecraft_coordinator.config import AgentProfile, CoordinatorConfig, resolve_secret
from swecraft_coordinator.forge_admin import (
    AdminForgeClient,
    AgentForgeClient,
    generate_password,
)
from swecraft_coordinator.memory import ensure_memory_files
from swecraft_coordinator.state import StateStore

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class OnboardReport:
    agent: str
    home_repo: str
    write_repos: tuple[str, ...]
    home_created: bool
    user_created: bool
    token_rotated: bool
    seeded_files: tuple[str, ...]
    # Repositories an earlier onboarding granted that this profile no longer
    # lists; their grant and subscription were revoked.
    revoked_repos: tuple[str, ...]


def onboard_agent(
    config: CoordinatorConfig,
    profile: AgentProfile,
    *,
    state: StateStore | None = None,
    goal: str | None = None,
    adopt_existing: bool = False,
) -> OnboardReport:
    """Idempotent provisioning with an ownership guard.

    An existing forge user is only touched when the coordinator can prove it
    manages it — the stored credentials file authenticates as that user — or
    when the operator explicitly passes ``adopt_existing``. Without that
    guard, onboarding a name that belongs to a human or another bot would be
    an account takeover (password reset plus token mint) handed to whoever
    holds the admin token.

    A stored, still-valid token is reused as-is, so re-running onboarding
    does not reset passwords or rotate credentials needlessly.

    Write isolation is permission-based: the bot user gets collaborator
    write on its home and on every repository it maintains
    (``profile.grant_repos``) and nothing anywhere else, so its token can
    merge its own pull requests but cannot write elsewhere. With ``state``,
    the grants are recorded and a later onboarding revokes what the profile
    no longer lists, so reassigning an agent is a configuration change.
    Adopted users may carry pre-existing permissions elsewhere; adopting
    makes the operator responsible for them.

    Order matters: every repository is inspected before anything is
    mutated, so a typo in ``write_repos`` leaves no half-provisioned identity
    behind; the identity comes next, then the home (created when missing and
    agent-owned, see ``ensure_agent_home``), then the grants.
    """

    operator_token = config.resolved_admin_token()
    admin = AdminForgeClient(config.forge_url, operator_token)
    for repo in profile.write_repos:
        _require_repository(admin, repo)
    home = _inspect_home(admin, profile)
    user_exists = admin.user_exists(profile.name)
    stored_token = _stored_token(config, profile.name)
    token = None
    user_created = False
    token_rotated = False
    if (
        user_exists
        and stored_token
        and _authenticates_as(config, stored_token, profile.name)
    ):
        token = stored_token
    if token is None:
        if user_exists and stored_token is None and not adopt_existing:
            raise CoordinatorError(
                f"forge user {profile.name} already exists and the "
                "coordinator has no credentials for it; refusing to take it "
                "over. If it really should become a coordinator-managed "
                "agent, re-run with --adopt-existing (this resets the "
                "user's password)."
            )
        password = generate_password()
        if user_exists:
            admin.set_password(profile.name, password)
        else:
            admin.create_user(profile.name, password)
            user_created = True
        token = admin.mint_agent_token(profile.name, password)
        _write_credentials(config, profile.name, token)
        token_rotated = not user_created
    agent_client = AgentForgeClient(config.forge_url, token)
    home_created, seeded = ensure_agent_home(
        admin,
        agent_client,
        profile,
        config,
        operator_token=operator_token,
        goal=goal,
        existing=home,
    )
    for repo in profile.work_repos:
        admin.ensure_collaborator(repo, profile.name, "write")
        # As the agent, not as the operator: a subscription belongs to whoever
        # authenticates the call, and the wake prompt tells the agent its
        # notifications carry operator issues and other agents' mentions.
        agent_client.watch_repository(repo)
    revoked = _reconcile_grants(state, admin, agent_client, profile)
    logger.info(
        "onboarded %s: home %s%s, maintains %s (user %s, token %s, %d file(s) "
        "seeded%s)",
        profile.name,
        profile.home_repo,
        " (created)" if home_created else "",
        ", ".join(profile.write_repos),
        "created" if user_created else "existing",
        "rotated" if token_rotated else ("new" if user_created else "reused"),
        len(seeded),
        f", revoked {', '.join(revoked)}" if revoked else "",
    )
    return OnboardReport(
        agent=profile.name,
        home_repo=profile.home_repo,
        write_repos=profile.write_repos,
        home_created=home_created,
        user_created=user_created,
        token_rotated=token_rotated,
        seeded_files=tuple(seeded),
        revoked_repos=revoked,
    )


def ensure_agent_home(
    admin: AdminForgeClient,
    agent_client: AgentForgeClient,
    profile: AgentProfile,
    config: CoordinatorConfig,
    *,
    operator_token: str,
    goal: str | None,
    existing: dict[str, Any] | None,
) -> tuple[bool, list[str]]:
    """Make the agent's home exist, writable by it, watched, seeded, and its
    memory branch the operator's to push.

    Idempotent. ``existing`` is what ``_inspect_home`` found: an existing
    home is verified there and never adopted, publicized, deleted or
    replaced; a missing dedicated home is created only when the agent itself
    owns it (``<agent>/<name>``): private, initialized so it has a default
    branch, with Actions off — a control repository runs no CI. The owner
    of a repository writes it without a grant; any other home gets the
    collaborator grant. The home's memory branch is then seeded through
    ``ensure_memory_files``, which never overwrites what is already there,
    and protected so that only the operator pushes it: the coordinator
    publishes the agent's notes after every wake, with the agent as author,
    and an agent that runs ``git push`` in its memory checkout is refused by
    the forge instead of racing the coordinator. The seed and every
    later publish therefore push with the operator's token.
    Returns whether the home was created and which memory files were added.
    """

    owner, name = profile.home_repo.split("/", 1)
    created = False
    if existing is None:
        admin.create_user_repository(owner, name, private=True)
        admin.disable_actions(profile.home_repo)
        created = True
    if owner != profile.name:
        admin.ensure_collaborator(profile.home_repo, profile.name, "write")
    # Operator issues and mentions on the home reach the agent through its
    # notifications only if it is subscribed there, as the agent.
    agent_client.watch_repository(profile.home_repo)
    seeded = ensure_memory_files(
        clone_url=f"{config.forge_url}/{profile.home_repo}.git",
        branch=config.memory_branch,
        branch_exists=admin.branch_exists(profile.home_repo, config.memory_branch),
        token=operator_token,
        author=profile.name,
        goal=goal,
    )
    admin.protect_branch(
        profile.home_repo,
        config.memory_branch,
        pushers=[admin.get_authenticated_username()],
    )
    return created, seeded


def _require_repository(admin: AdminForgeClient, repo: str) -> dict[str, Any]:
    repository = admin.repository(repo)
    if repository is None:
        raise CoordinatorError(f"repository does not exist: {repo}")
    return repository


def _inspect_home(
    admin: AdminForgeClient, profile: AgentProfile
) -> dict[str, Any] | None:
    """The home as the forge has it, or None when onboarding may create it.

    A maintained home (the one-repository layout) must simply exist. A
    dedicated home must be private, since it holds the agent's memory, and
    is created only under the agent's own name: onboarding never creates a
    repository for another owner, nor adopts, publicizes, deletes or
    replaces one it finds.
    """

    if not profile.dedicated_home:
        return _require_repository(admin, profile.home_repo)
    owner = profile.home_repo.split("/", 1)[0]
    repository = admin.repository(profile.home_repo)
    if repository is None:
        if owner != profile.name:
            raise CoordinatorError(
                f"home repository {profile.home_repo} does not exist; onboarding "
                f"creates a home only under the agent's own name "
                f"({profile.name}/<name>). Create it first, private, or use that "
                "form."
            )
        return None
    if not repository.get("private", False):
        raise CoordinatorError(
            f"home repository {profile.home_repo} must be private: it holds "
            f"{profile.name}'s memory. Make it private or choose another home."
        )
    return repository


def _reconcile_grants(
    state: StateStore | None,
    admin: AdminForgeClient,
    agent_client: AgentForgeClient,
    profile: AgentProfile,
) -> tuple[str, ...]:
    """Revoke what an earlier onboarding granted and this profile no longer lists.

    Only coordinator-made grants are touched: the record in ``state`` is
    what a previous onboarding of this agent granted. An agent-owned former
    home is left alone — ownership is not a grant.
    """

    if state is None:
        return ()
    current = profile.grant_repos
    stale = tuple(
        repo for repo in state.assigned_repos(profile.name) if repo not in current
    )
    for repo in stale:
        agent_client.unwatch_repository(repo)
        if repo.split("/", 1)[0] != profile.name:
            admin.remove_collaborator(repo, profile.name)
    state.record_assignment(profile.name, current)
    return stale


def _stored_token(config: CoordinatorConfig, agent: str) -> str | None:
    path = config.credentials_path(agent)
    if not path.is_file():
        return None
    try:
        return resolve_secret(f"file:{path}")
    except CoordinatorError:
        return None


def _authenticates_as(config: CoordinatorConfig, token: str, agent: str) -> bool:
    try:
        return (
            ForgeClient(config.forge_url, token).get_authenticated_username() == agent
        )
    except ForgeError:
        return False


def _write_credentials(config: CoordinatorConfig, agent: str, token: str) -> None:
    directory = config.credentials_dir
    directory.mkdir(parents=True, exist_ok=True)
    os.chmod(directory, 0o700)
    path = config.credentials_path(agent)
    path.touch(mode=0o600)
    os.chmod(path, 0o600)
    path.write_text(token + "\n", encoding="utf-8")

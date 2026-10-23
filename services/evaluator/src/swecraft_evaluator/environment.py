"""World environments: complete repo -> commit maps and the ``world@`` shortcuts.

An environment is the full configuration the evaluation runs on top of: every
world repository mapped to an exact commit. Three constructors cover every
use:

- ``world@seed``            — the locked initial state
- ``world@latest``          — current default-branch heads from the Forge
- ``world@seed+repo:tag``   — the seed with one repository at a release tag
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from swecraft_common.forge import ForgeClient, ForgeError

from swecraft_evaluator.errors import EvaluatorError

Environment = dict[str, str]

SEED = "world@seed"
LATEST = "world@latest"


@dataclass(frozen=True)
class WorldRepo:
    name: str
    baseline_commit: str
    default_branch: str


@dataclass(frozen=True)
class World:
    org: str
    repos: tuple[WorldRepo, ...]

    def repo(self, name: str) -> WorldRepo:
        for repo in self.repos:
            if repo.name == name:
                return repo
        raise EvaluatorError(f"unknown world repository {name}")


def load_world(path: Path) -> World:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise EvaluatorError(f"could not read world lock {path}: {error}") from None
    try:
        repos = tuple(
            WorldRepo(
                name=repo["name"],
                baseline_commit=repo["baseline_commit"],
                default_branch=repo["default_branch"],
            )
            for repo in payload["repos"]
        )
        return World(org=payload["org"], repos=repos)
    except (KeyError, TypeError) as error:
        raise EvaluatorError(f"world lock {path} is missing {error}") from None


class Forge(ForgeClient):
    """The evaluator's few Forgejo reads, built on the shared client."""

    def branch_head(self, org: str, repo: str, branch: str) -> str:
        payload = self.get_branch(f"{org}/{repo}", branch)
        commit = payload.get("commit") if isinstance(payload, dict) else None
        sha = commit.get("id") if isinstance(commit, dict) else None
        if not isinstance(sha, str) or not sha:
            raise EvaluatorError(f"{org}/{repo}@{branch} returned no commit")
        return sha

    def tag_commit(self, org: str, repo: str, tag: str) -> str:
        try:
            payload = self._request("GET", f"/repos/{org}/{repo}/tags/{tag}")
        except ForgeError as error:
            raise EvaluatorError(
                f"could not resolve tag {org}/{repo}:{tag}: {error}"
            ) from None
        commit = payload.get("commit") if isinstance(payload, dict) else None
        sha = commit.get("sha") if isinstance(commit, dict) else None
        if not isinstance(sha, str) or not sha:
            raise EvaluatorError(f"tag {org}/{repo}:{tag} returned no commit")
        return sha

    def releases(
        self, org: str, repo: str, *, limit: int = 10, page: int = 1
    ) -> list[str]:
        """Release tag names, newest first, one page at a time."""

        payload = self._request(
            "GET", f"/repos/{org}/{repo}/releases?limit={int(limit)}&page={int(page)}"
        )
        tags = []
        for release in payload if isinstance(payload, list) else []:
            tag = release.get("tag_name") if isinstance(release, dict) else None
            if isinstance(tag, str) and tag:
                tags.append(tag)
        return tags


def seed_environment(world: World) -> Environment:
    return {repo.name: repo.baseline_commit for repo in world.repos}


def latest_environment(world: World, forge: Forge) -> Environment:
    return {
        repo.name: forge.branch_head(world.org, repo.name, repo.default_branch)
        for repo in world.repos
    }


def release_environment(world: World, forge: Forge, repo: str, tag: str) -> Environment:
    environment = seed_environment(world)
    world.repo(repo)
    environment[repo] = forge.tag_commit(world.org, repo, tag)
    return environment


def parse_environment(spec: str, world: World, forge: Forge | None) -> Environment:
    """Resolve a ``world@`` shortcut; network shortcuts need a Forge."""

    if spec == SEED:
        return seed_environment(world)
    if spec == LATEST:
        if forge is None:
            raise EvaluatorError(f"{spec} requires a Forge connection")
        return latest_environment(world, forge)
    if spec.startswith("world@seed+"):
        modifier = spec.removeprefix("world@seed+")
        repo, _, tag = modifier.partition(":")
        if not repo or not tag:
            raise EvaluatorError(f"modifier {modifier!r} is not of the form repo:tag")
        if forge is None:
            raise EvaluatorError(f"{spec} requires a Forge connection")
        return release_environment(world, forge, repo, tag)
    raise EvaluatorError(
        f"unknown environment {spec!r}; expected {SEED}, {LATEST}, "
        "or world@seed+repo:tag"
    )


def environment_digest(environment: Environment) -> str:
    encoded = json.dumps(
        dict(sorted(environment.items())), separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:12]

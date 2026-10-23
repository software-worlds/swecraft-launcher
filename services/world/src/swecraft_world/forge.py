"""Forgejo operations owned by the world seeder."""

from __future__ import annotations

import time
from typing import Any
from urllib.parse import urlsplit

import httpx
from swecraft_common.forge import ForgeClient, ForgeError

from swecraft_world.errors import WorldError
from swecraft_world.models import RepositoryVisibility


class WorldForgeClient(ForgeClient):
    """Forgejo transport plus repository operations specific to world seeding."""

    def __init__(
        self,
        base_url: str,
        token: str,
        org: str,
        *,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        base_url = base_url.rstrip("/")
        parts = urlsplit(base_url)
        if parts.scheme not in ("http", "https") or not parts.netloc:
            raise WorldError("Forge URL must be absolute HTTP(S)")
        if parts.username or parts.password:
            raise WorldError("Forge URL must not contain credentials")
        super().__init__(base_url, token, transport=transport)
        self.base_url = base_url
        self.token = token
        self.org = org

    def clone_url(self, name: str) -> str:
        return f"{self.base_url}/{self.org}/{name}.git"

    def ensure_org(self) -> None:
        try:
            self._request("GET", f"/orgs/{self.org}")
            return
        except ForgeError as error:
            if error.status_code != 404:
                raise _world_error("inspect organization", error) from error
        try:
            self._request(
                "POST",
                "/orgs",
                {"username": self.org, "visibility": "public"},
            )
        except ForgeError as error:
            raise _world_error("create organization", error) from error

    def repository(self, name: str) -> dict[str, Any] | None:
        try:
            payload = self._request("GET", f"/repos/{self.org}/{name}")
        except ForgeError as error:
            if error.status_code == 404:
                return None
            raise _world_error(f"inspect {self.org}/{name}", error) from error
        if not isinstance(payload, dict):
            raise WorldError(f"Forgejo returned an invalid repository for {name}")
        return payload

    def create_repository(
        self, name: str, default_branch: str, visibility: RepositoryVisibility
    ) -> None:
        try:
            self._request(
                "POST",
                f"/orgs/{self.org}/repos",
                {
                    "name": name,
                    "private": visibility == "private",
                    "auto_init": False,
                    "default_branch": default_branch,
                },
            )
        except ForgeError as error:
            raise _world_error(f"create {self.org}/{name}", error) from error

    def delete_repository(self, name: str) -> None:
        try:
            self._request("DELETE", f"/repos/{self.org}/{name}")
        except ForgeError as error:
            raise _world_error(f"delete {self.org}/{name}", error) from error

    def prepare_empty_repository(
        self,
        name: str,
        default_branch: str,
        visibility: RepositoryVisibility,
        *,
        replace: bool,
    ) -> None:
        repository = self.repository(name)
        if repository is None:
            self.create_repository(name, default_branch, visibility)
            self.disable_actions(name)
            return
        if repository.get("empty") is True:
            self.set_repository_visibility(name, visibility)
            self.disable_actions(name)
            return
        if not replace:
            raise WorldError(
                f"refusing to replace evolved repository {self.org}/{name}; "
                "use --replace only when discarding it is intentional"
            )
        self.delete_repository(name)
        self.create_repository(name, default_branch, visibility)
        self.disable_actions(name)

    def disable_actions(self, name: str) -> None:
        """Hold Actions off for the seed push. Forgejo 15 creates
        repositories with Actions enabled by default (verified live),
        so the disabled window must be explicit, never assumed."""

        try:
            self._request("PATCH", f"/repos/{self.org}/{name}", {"has_actions": False})
        except ForgeError as error:
            raise _world_error(
                f"disable Actions on {self.org}/{name}", error
            ) from error

    def set_repository_visibility(
        self, name: str, visibility: RepositoryVisibility
    ) -> None:
        try:
            self._request(
                "PATCH",
                f"/repos/{self.org}/{name}",
                {"private": visibility == "private"},
            )
        except ForgeError as error:
            raise _world_error(f"set visibility on {self.org}/{name}", error) from error

    def configure_repository(
        self, name: str, default_branch: str, visibility: RepositoryVisibility
    ) -> None:
        """Finish repository configuration *without* enabling Actions.

        Enabling belongs to `swecraft-world enable-actions`, run once the whole
        world is seeded. `git push` returns when refs are accepted, but
        Forgejo evaluates the ref-update events asynchronously; flipping
        `has_actions` on here raced that evaluation, so every workflow in
        every pushed tag tree fired against a now-enabled repository. The
        immediately following no-tasks assertion could not see it either,
        because at that instant the queue is legitimately empty.
        """

        try:
            self._request(
                "PATCH",
                f"/repos/{self.org}/{name}",
                {
                    "default_branch": default_branch,
                    "private": visibility == "private",
                },
            )
        except ForgeError as error:
            raise _world_error(f"configure {self.org}/{name}", error) from error

    def enable_actions(self, name: str) -> None:
        try:
            self._request("PATCH", f"/repos/{self.org}/{name}", {"has_actions": True})
        except ForgeError as error:
            raise _world_error(f"enable Actions on {self.org}/{name}", error) from error

    def queued_action_tasks(self, name: str) -> int:
        """How many Actions runs the repository has, in any status.

        Deliberately reads `actions/runs`, not `actions/tasks`. The tasks
        endpoint only reports runs that a runner has already picked up, so a
        repository holding thousands of `waiting` runs behind a saturated
        runner reports **zero** there -- measured live: pydantic and orjson
        showed 0 tasks against 236 and 246 runs. Any check built on tasks is
        therefore blind to exactly the contamination it exists to catch.

        Counts every status, completed runs included: this is the
        contamination gate, and a repository seeded correctly has no run
        history at all. For the current queue use `active_action_runs`.

        A repository whose Actions unit has never been enabled may not expose
        the endpoint at all, which is itself proof of quiet.
        """

        return self._count_action_runs(name)

    def active_action_runs(self, name: str) -> int:
        """How many Actions runs are waiting for or occupying the runner.

        Sums the `waiting`, `running`, and `blocked` statuses, one request
        per status: Forgejo's `status` filter takes a single value.
        Completed runs are excluded, so this reports the live queue rather
        than the repository's run history.
        """

        return sum(
            self._count_action_runs(name, status=status)
            for status in ("waiting", "running", "blocked")
        )

    def _count_action_runs(self, name: str, *, status: str | None = None) -> int:
        params: dict[str, Any] = {"limit": 1}
        if status is not None:
            params["status"] = status
        try:
            payload = self._request(
                "GET", f"/repos/{self.org}/{name}/actions/runs", params=params
            )
        except ForgeError as error:
            if error.status_code == 404:
                return 0
            raise _world_error(
                f"inspect Actions history in {self.org}/{name}", error
            ) from error
        if isinstance(payload, dict):
            total = payload.get("total_count")
            if isinstance(total, int):
                return total
        return len(_action_tasks(payload))

    def wait_until_nonempty(
        self, name: str, *, attempts: int = 30, delay_seconds: float = 2.0
    ) -> None:
        for attempt in range(attempts):
            repository = self.repository(name)
            if repository is not None and repository.get("empty") is False:
                return
            if attempt + 1 < attempts:
                time.sleep(delay_seconds)
        raise WorldError(
            f"{self.org}/{name} is still marked empty after {attempts} checks"
        )

    def assert_seed_recovery_safe(
        self, name: str, *, expected_tags: dict[str, str] | None = None
    ) -> None:
        """Check an existing exact seed before repairing its configuration.

        Git has already proved that the expected branch is still exactly the
        seed baseline. These remaining checks must also pass before mutation:
        tags or Actions history mean the repository is no longer an untouched
        seed, even when its branch has not moved.
        """

        repository = self.repository(name)
        if repository is None or repository.get("empty") is not False:
            raise WorldError(f"{self.org}/{name} is absent or marked empty")
        tags = self._list_tags(name)
        unexpected = _unexpected_tags(tags, expected_tags or {})
        if unexpected:
            raise WorldError(
                f"{self.org}/{name} contains unexpected tags: "
                + ", ".join(sorted(unexpected))
            )
        # Counts runs, not dispatched tasks: a saturated runner leaves runs
        # `waiting` and the tasks endpoint reports none of them.
        if self.queued_action_tasks(name):
            raise WorldError(
                f"{self.org}/{name} has Actions history; refusing seed recovery"
            )

    def assert_repository_state(
        self,
        name: str,
        *,
        expected_tags: dict[str, str] | None = None,
        require_no_action_tasks: bool = False,
        require_actions: bool | None = True,
        expected_visibility: RepositoryVisibility | None = None,
    ) -> None:
        repository = self.repository(name)
        if repository is None or repository.get("empty") is not False:
            raise WorldError(f"{self.org}/{name} is absent or marked empty")
        if expected_visibility is not None:
            expected_private = expected_visibility == "private"
            if repository.get("private") is not expected_private:
                raise WorldError(
                    f"{self.org}/{name} visibility is "
                    f"{_visibility_name(repository.get('private'))}, "
                    f"expected {expected_visibility}"
                )
        # Tri-state on purpose. True: the world should be live. False:
        # seed-time, where Actions must still be off so pushed refs are not
        # evaluated against an enabled repository. None: adoption, which
        # observes an existing world rather than dictating its state.
        if require_actions is True and repository.get("has_actions") is not True:
            raise WorldError(f"{self.org}/{name} does not have Actions enabled")
        if require_actions is False and repository.get("has_actions") is True:
            raise WorldError(
                f"{self.org}/{name} has Actions enabled during seeding; "
                "pushed refs would be evaluated against an enabled repository"
            )
        if expected_tags is not None:
            # Seed-time exactness: the remote must carry exactly the tags
            # the lock says were seeded, at exactly the pinned commits.
            # Post-seed checks pass None — agents legitimately add release
            # tags once the world is theirs.
            tags = self._list_tags(name)
            found = _tag_commits(tags)
            if found != expected_tags:
                raise WorldError(
                    f"{self.org}/{name} tags {sorted(found)} do not match "
                    f"the seeded expectation {sorted(expected_tags)}"
                )
        try:
            self._request(
                "GET",
                f"/repos/{self.org}/{name}/contents/.github/workflows",
            )
        except ForgeError as error:
            if error.status_code != 404:
                raise _world_error(
                    f"inspect workflows in {self.org}/{name}", error
                ) from error
        else:
            raise WorldError(f"{self.org}/{name} still contains upstream workflows")
        if require_no_action_tasks and self.queued_action_tasks(name):
            raise WorldError(
                f"{self.org}/{name} queued Actions while it was being seeded"
            )

    def _list_tags(self, name: str) -> list[Any]:
        """Every tag, paginated: an upstream with hundreds of releases must
        not have its exact-set assertion satisfied by the first page."""

        tags: list[Any] = []
        page = 1
        while True:
            batch = self._json(
                "GET", f"/repos/{self.org}/{name}/tags?page={page}&limit=50"
            )
            if not isinstance(batch, list) or not batch:
                break
            tags.extend(batch)
            # Terminate only on an empty page: the server may clamp `limit`
            # below 50 (MAX_RESPONSE_ITEMS), and a short-page exit would
            # silently truncate the exact-set assertion this exists for.
            page += 1
        return tags

    def _json(self, method: str, path: str) -> Any:
        try:
            return self._request(method, path)
        except ForgeError as error:
            raise _world_error(f"Forgejo {method} {path}", error) from error


def _tag_commits(payload: Any) -> dict[str, str]:
    tags: dict[str, str] = {}
    if isinstance(payload, list):
        for item in payload:
            if not isinstance(item, dict):
                continue
            name = item.get("name")
            commit = item.get("commit")
            sha = commit.get("sha") if isinstance(commit, dict) else None
            if isinstance(name, str) and isinstance(sha, str):
                tags[name] = sha
    return tags


def _unexpected_tags(payload: Any, expected: dict[str, str]) -> set[str]:
    """Tags on the remote that the seed cannot account for. A subset is
    fine mid-recovery (the tag push may not have happened yet); a wrong
    commit under an expected name is tampering."""

    found = _tag_commits(payload)
    return {name for name, sha in found.items() if expected.get(name) != sha}


def _action_tasks(payload: Any) -> list[Any]:
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("workflow_runs", "tasks", "data"):
            value = payload.get(key)
            if isinstance(value, list):
                return value
    return []


def _visibility_name(value: object) -> str:
    if value is True:
        return "private"
    if value is False:
        return "public"
    return f"unknown ({value!r})"


def _world_error(action: str, error: ForgeError) -> WorldError:
    return WorldError(f"could not {action}: {error}")

"""Forgejo admin operations for onboarding agent identities."""

from __future__ import annotations

import secrets
from typing import Any

from swecraft_common.forge import ForgeClient, ForgeError
from swecraft_forge.admin import ForgeAdminClient

# Full-scope agent token: repo-level write isolation comes from collaborator
# permissions (write on the agent's own repository only), not token scopes.
AGENT_TOKEN_SCOPES = [
    "write:repository",
    "write:issue",
    "read:notification",
    "write:notification",
    "read:user",
]
TOKEN_NAME = "swecraft-coordinator"


class AgentForgeClient(ForgeClient):
    """A client acting as the agent itself rather than as the operator."""

    def watch_repository(self, repo: str) -> None:
        """Subscribe this token's own user to ``repo``.

        Forgejo only creates notifications for subscribed threads, and a
        collaborator grant subscribes nobody. Without this an agent's
        ``GET /notifications`` stays empty forever, so operator issues and
        mentions from other agents never reach the wake that is told to
        check them — and from inside the sandbox an empty feed is
        indistinguishable from a quiet forge.
        """

        repo = self._validated_repo(repo)
        self._request("PUT", f"/repos/{repo}/subscription")

    def unwatch_repository(self, repo: str) -> None:
        """Drop this token's own subscription to ``repo``; gone already is fine."""

        repo = self._validated_repo(repo)
        try:
            self._request("DELETE", f"/repos/{repo}/subscription")
        except ForgeError as error:
            if error.status_code != 404:
                raise


class AdminForgeClient(ForgeAdminClient):
    """ForgeClient plus the admin endpoints onboarding needs."""

    def user_exists(self, username: str) -> bool:
        username = self._validated_username(username)
        try:
            self._request("GET", f"/users/{username}")
        except ForgeError as error:
            if error.status_code == 404:
                return False
            raise
        return True

    def create_user(self, username: str, password: str) -> Any:
        username = self._validated_username(username)
        return self._request(
            "POST",
            "/admin/users",
            {
                "username": username,
                # Gitea's Email binding validator rejects a dotless domain
                # (`@localhost` -> 422 "[Email]: Email"); a TLD-bearing local
                # domain passes and the address is otherwise unused offline.
                "email": f"{username}@swecraft.local",
                "password": password,
                "must_change_password": False,
                "visibility": "public",
            },
        )

    def set_password(self, username: str, password: str) -> Any:
        return self.set_user_password(username, password)

    def ensure_collaborator(
        self, repo: str, username: str, permission: str = "write"
    ) -> None:
        repo = self._validated_repo(repo)
        username = self._validated_username(username)
        self._request(
            "PUT",
            f"/repos/{repo}/collaborators/{username}",
            {"permission": permission},
        )

    def remove_collaborator(self, repo: str, username: str) -> None:
        """Revoke a collaborator grant; a grant that is gone already is fine."""

        repo = self._validated_repo(repo)
        username = self._validated_username(username)
        try:
            self._request("DELETE", f"/repos/{repo}/collaborators/{username}")
        except ForgeError as error:
            if error.status_code != 404:
                raise

    def repository(self, repo: str) -> Any | None:
        """The repository as Forgejo describes it, or None when it does not exist."""

        repo = self._validated_repo(repo)
        try:
            return self._request("GET", f"/repos/{repo}")
        except ForgeError as error:
            if error.status_code == 404:
                return None
            raise

    def create_user_repository(
        self,
        username: str,
        name: str,
        *,
        private: bool = True,
        default_branch: str = "main",
    ) -> Any:
        """Create a repository owned by ``username``, initialized so it has a branch."""

        username = self._validated_username(username)
        self._validated_repo(f"{username}/{name}")
        return self._request(
            "POST",
            f"/admin/users/{username}/repos",
            {
                "name": name,
                "private": private,
                "auto_init": True,
                "default_branch": default_branch,
            },
        )

    def require_review(self, repo: str, branch: str) -> None:
        """Changes to ``branch`` merge only through a pull request approved by
        someone other than its author: no direct pushes, one approval, a
        rejection blocks, and an approval lapses when the change moves.
        Idempotent: an existing rule is updated.
        """

        self._protect(
            repo,
            branch,
            {
                "enable_push": False,
                "required_approvals": 1,
                "enable_approvals_whitelist": False,
                "block_on_rejected_reviews": True,
                "dismiss_stale_approvals": True,
            },
        )

    def protect_branch(self, repo: str, branch: str, *, pushers: list[str]) -> None:
        """Only ``pushers`` may push ``branch``; every other push is refused by
        the forge. The memory branch is protected this way with the operator
        as its one pusher: the coordinator publishes an agent's notes for it,
        and the agent's own push is rejected server-side rather than merely
        discouraged. Idempotent: an existing rule is updated.
        """

        self._protect(
            repo,
            branch,
            {
                "enable_push": True,
                "enable_push_whitelist": True,
                "push_whitelist_usernames": list(pushers),
                "push_whitelist_teams": [],
                "push_whitelist_deploy_keys": False,
                "required_approvals": 0,
                "enable_merge_whitelist": False,
                "block_on_rejected_reviews": False,
            },
        )

    def _protect(self, repo: str, branch: str, rule: dict[str, Any]) -> None:
        repo = self._validated_repo(repo)
        payload = {"rule_name": branch, "branch_name": branch, **rule}
        try:
            self._request("GET", f"/repos/{repo}/branch_protections/{branch}")
        except ForgeError as error:
            if error.status_code != 404:
                raise
            self._request("POST", f"/repos/{repo}/branch_protections", payload)
            return
        self._request("PATCH", f"/repos/{repo}/branch_protections/{branch}", payload)

    def disable_actions(self, repo: str) -> None:
        """A control repository runs no CI; Forgejo enables Actions by default."""

        repo = self._validated_repo(repo)
        self._request("PATCH", f"/repos/{repo}", {"has_actions": False})

    def mint_agent_token(self, username: str, password: str) -> str:
        return self.mint_token(username, password, TOKEN_NAME, AGENT_TOKEN_SCOPES)


def generate_password() -> str:
    return secrets.token_urlsafe(24)

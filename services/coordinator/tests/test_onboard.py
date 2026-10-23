from __future__ import annotations

import stat
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import MagicMock, patch

from swecraft_common.forge import ForgeError

from swecraft_coordinator import CoordinatorError
from swecraft_coordinator.config import AgentProfile, CoordinatorConfig
from swecraft_coordinator.onboard import onboard_agent
from swecraft_coordinator.state import StateStore


def make_config(root: Path, *profiles: AgentProfile) -> CoordinatorConfig:
    return CoordinatorConfig(
        forge_url="http://127.0.0.1:3400",
        push_url="http://forge.internal:3000",
        admin_token_source="admin-token",
        model="test/model",
        agents=profiles
        or (
            AgentProfile(
                name="demo-bot",
                home_repo="hub/demo",
                write_repos=("hub/demo",),
                image="image:1",
                model="test/model",
                interval_seconds=600,
                timeout_seconds=300,
            ),
        ),
        agent_env_sources={},
        runs_dir=root / "runs",
        database=root / "state.db",
        credentials_dir=root / "credentials",
    )


def dedicated_profile(*write_repos: str) -> AgentProfile:
    return AgentProfile(
        name="avery-bot",
        home_repo="avery-bot/home",
        write_repos=write_repos or ("hub/requests", "hub/urllib3"),
        image="image:1",
        model="test/model",
        interval_seconds=600,
        timeout_seconds=300,
    )


class OnboardTests(unittest.TestCase):
    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._directory.cleanup)
        self.root = Path(self._directory.name)
        self.config = make_config(self.root)
        patcher = patch("swecraft_coordinator.onboard.AgentForgeClient")
        self.agent_client = patcher.start()
        self.addCleanup(patcher.stop)

    def _admin(
        self, *, user_exists: bool, repositories: dict[str, dict | None] | None = None
    ) -> MagicMock:
        admin = MagicMock()
        known = repositories if repositories is not None else {}
        admin.repository.side_effect = lambda repo: known.get(
            repo, {"full_name": repo, "private": False}
        )
        admin.user_exists.return_value = user_exists
        admin.branch_exists.return_value = False
        admin.mint_agent_token.return_value = "minted-token"
        admin.get_authenticated_username.return_value = "hub-operator"
        return admin

    def _store_credentials(
        self, agent: str = "demo-bot", token: str = "stored-token"
    ) -> None:
        self.config.credentials_dir.mkdir(parents=True, exist_ok=True)
        self.config.credentials_path(agent).write_text(token + "\n", encoding="utf-8")

    def test_creates_user_scopes_access_and_seeds_memory(self) -> None:
        admin = self._admin(user_exists=False)
        with (
            patch("swecraft_coordinator.onboard.AdminForgeClient", return_value=admin),
            patch(
                "swecraft_coordinator.onboard.ensure_memory_files",
                return_value=["GOAL.md", "notebook.md"],
            ) as seed,
        ):
            report = onboard_agent(
                self.config, self.config.agent("demo-bot"), goal="# Charter\n"
            )

        self.assertTrue(report.user_created)
        self.assertFalse(report.home_created)
        self.assertEqual(report.home_repo, "hub/demo")
        self.assertEqual(report.write_repos, ("hub/demo",))
        self.assertEqual(report.seeded_files, ("GOAL.md", "notebook.md"))
        admin.create_user.assert_called_once()
        admin.set_password.assert_not_called()
        # The one-repository layout: the home is the maintained repository,
        # granted once, never created.
        admin.create_user_repository.assert_not_called()
        admin.ensure_collaborator.assert_called_once_with(
            "hub/demo", "demo-bot", "write"
        )
        # Subscribed as the agent, with the agent's own token: a collaborator
        # grant creates no subscription, and an unsubscribed agent's
        # notification feed is permanently empty.
        self.agent_client.assert_called_once_with(self.config.forge_url, "minted-token")
        self.agent_client.return_value.watch_repository.assert_called_once_with(
            "hub/demo"
        )
        credentials = self.config.credentials_path("demo-bot")
        self.assertEqual(
            credentials.read_text(encoding="utf-8").strip(), "minted-token"
        )
        mode = stat.S_IMODE(credentials.stat().st_mode)
        self.assertEqual(mode, 0o600)
        self.assertEqual(seed.call_args.kwargs["goal"], "# Charter\n")
        # The memory branch is the operator's to push: seeded with the admin
        # token, then protected with the operator as its only pusher.
        self.assertEqual(seed.call_args.kwargs["token"], "admin-token")
        self.assertEqual(seed.call_args.kwargs["author"], "demo-bot")
        admin.protect_branch.assert_called_once_with(
            "hub/demo", "agent/memory", pushers=["hub-operator"]
        )
        self.assertEqual(
            seed.call_args.kwargs["clone_url"], "http://127.0.0.1:3400/hub/demo.git"
        )

    def test_refuses_existing_user_without_stored_credentials(self) -> None:
        admin = self._admin(user_exists=True)
        with patch("swecraft_coordinator.onboard.AdminForgeClient", return_value=admin):
            with self.assertRaisesRegex(CoordinatorError, "refusing to take"):
                onboard_agent(self.config, self.config.agent("demo-bot"))
        admin.set_password.assert_not_called()
        admin.mint_agent_token.assert_not_called()

    def test_adopt_existing_resets_password_and_mints(self) -> None:
        admin = self._admin(user_exists=True)
        with (
            patch("swecraft_coordinator.onboard.AdminForgeClient", return_value=admin),
            patch("swecraft_coordinator.onboard.ensure_memory_files", return_value=[]),
        ):
            report = onboard_agent(
                self.config, self.config.agent("demo-bot"), adopt_existing=True
            )

        self.assertFalse(report.user_created)
        self.assertTrue(report.token_rotated)
        admin.create_user.assert_not_called()
        admin.set_password.assert_called_once()
        admin.mint_agent_token.assert_called_once()

    def test_valid_stored_token_is_reused_without_password_reset(self) -> None:
        self._store_credentials()
        admin = self._admin(user_exists=True)
        forge = MagicMock()
        forge.get_authenticated_username.return_value = "demo-bot"
        with (
            patch("swecraft_coordinator.onboard.AdminForgeClient", return_value=admin),
            patch("swecraft_coordinator.onboard.ForgeClient", return_value=forge),
            patch(
                "swecraft_coordinator.onboard.ensure_memory_files", return_value=[]
            ) as seed,
        ):
            report = onboard_agent(self.config, self.config.agent("demo-bot"))

        self.assertFalse(report.user_created)
        self.assertFalse(report.token_rotated)
        admin.set_password.assert_not_called()
        admin.mint_agent_token.assert_not_called()
        # The stored token authenticates the agent; the seed pushes as the operator.
        self.assertEqual(seed.call_args.kwargs["token"], "admin-token")

    def test_invalid_stored_token_rotates_managed_user(self) -> None:
        self._store_credentials()
        admin = self._admin(user_exists=True)
        forge = MagicMock()
        forge.get_authenticated_username.return_value = "someone-else"
        with (
            patch("swecraft_coordinator.onboard.AdminForgeClient", return_value=admin),
            patch("swecraft_coordinator.onboard.ForgeClient", return_value=forge),
            patch("swecraft_coordinator.onboard.ensure_memory_files", return_value=[]),
        ):
            report = onboard_agent(self.config, self.config.agent("demo-bot"))

        self.assertTrue(report.token_rotated)
        admin.set_password.assert_called_once()
        admin.mint_agent_token.assert_called_once()

    def test_missing_repository_refuses_before_any_mutation(self) -> None:
        admin = self._admin(user_exists=False, repositories={"hub/demo": None})
        with patch("swecraft_coordinator.onboard.AdminForgeClient", return_value=admin):
            with self.assertRaisesRegex(CoordinatorError, "does not exist"):
                onboard_agent(self.config, self.config.agent("demo-bot"))
        admin.create_user.assert_not_called()
        admin.create_user_repository.assert_not_called()

    def test_other_forge_errors_propagate(self) -> None:
        admin = self._admin(user_exists=False)
        admin.repository.side_effect = ForgeError(
            "Forgejo GET /repos/hub/demo failed with 500: boom", status_code=500
        )
        with patch("swecraft_coordinator.onboard.AdminForgeClient", return_value=admin):
            with self.assertRaises(ForgeError):
                onboard_agent(self.config, self.config.agent("demo-bot"))


class DedicatedHomeTests(unittest.TestCase):
    """A private home the agent owns, plus the repositories it maintains."""

    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._directory.cleanup)
        self.root = Path(self._directory.name)
        self.config = make_config(self.root, dedicated_profile())
        patcher = patch("swecraft_coordinator.onboard.AgentForgeClient")
        self.agent_client = patcher.start()
        self.addCleanup(patcher.stop)
        self.state = StateStore(self.config.database)

    def _admin(self, repositories: dict[str, dict | None]) -> MagicMock:
        admin = MagicMock()
        admin.repository.side_effect = lambda repo: repositories.get(repo)
        admin.user_exists.return_value = False
        admin.branch_exists.return_value = False
        admin.mint_agent_token.return_value = "minted-token"
        admin.get_authenticated_username.return_value = "hub-operator"
        return admin

    def _onboard(self, admin: MagicMock, *, profile: AgentProfile | None = None):
        with (
            patch("swecraft_coordinator.onboard.AdminForgeClient", return_value=admin),
            patch(
                "swecraft_coordinator.onboard.ensure_memory_files",
                return_value=["GOAL.md"],
            ),
        ):
            return onboard_agent(
                self.config, profile or self.config.agent("avery-bot"), state=self.state
            )

    def test_creates_the_agent_owned_home_private_and_grants_the_work_repos(
        self,
    ) -> None:
        public = {"private": False}
        admin = self._admin({"hub/requests": public, "hub/urllib3": public})

        report = self._onboard(admin)

        self.assertTrue(report.home_created)
        admin.create_user_repository.assert_called_once_with(
            "avery-bot", "home", private=True
        )
        admin.disable_actions.assert_called_once_with("avery-bot/home")
        # The owner writes its own home: the grants are the work repositories.
        self.assertEqual(
            [call.args for call in admin.ensure_collaborator.call_args_list],
            [
                ("hub/requests", "avery-bot", "write"),
                ("hub/urllib3", "avery-bot", "write"),
            ],
        )
        watched = [
            call.args[0]
            for call in self.agent_client.return_value.watch_repository.call_args_list
        ]
        self.assertEqual(watched, ["avery-bot/home", "hub/requests", "hub/urllib3"])
        self.assertEqual(
            self.state.assigned_repos("avery-bot"),
            ("avery-bot/home", "hub/requests", "hub/urllib3"),
        )
        self.assertEqual(report.revoked_repos, ())

    def test_existing_private_home_is_kept_and_not_recreated(self) -> None:
        admin = self._admin(
            {
                "avery-bot/home": {"private": True},
                "hub/requests": {"private": False},
                "hub/urllib3": {"private": False},
            }
        )

        report = self._onboard(admin)

        self.assertFalse(report.home_created)
        admin.create_user_repository.assert_not_called()
        admin.disable_actions.assert_not_called()

    def test_refuses_a_public_dedicated_home(self) -> None:
        admin = self._admin(
            {
                "avery-bot/home": {"private": False},
                "hub/requests": {"private": False},
                "hub/urllib3": {"private": False},
            }
        )
        with self.assertRaisesRegex(CoordinatorError, "must be private"):
            self._onboard(admin)
        admin.create_user.assert_not_called()

    def test_never_creates_a_home_under_another_owner(self) -> None:
        profile = replace(self.config.agent("avery-bot"), home_repo="hub/avery-home")
        self.config = replace(self.config, agents=(profile,))
        admin = self._admin({"hub/requests": {}, "hub/urllib3": {}})
        with self.assertRaisesRegex(CoordinatorError, "creates a home only"):
            self._onboard(admin, profile=profile)
        admin.create_user_repository.assert_not_called()
        admin.create_user.assert_not_called()

    def test_an_operator_created_private_home_gets_the_grant(self) -> None:
        profile = replace(self.config.agent("avery-bot"), home_repo="hub/avery-home")
        self.config = replace(self.config, agents=(profile,))
        admin = self._admin(
            {"hub/avery-home": {"private": True}, "hub/requests": {}, "hub/urllib3": {}}
        )

        report = self._onboard(admin, profile=profile)

        self.assertFalse(report.home_created)
        self.assertEqual(
            admin.ensure_collaborator.call_args_list[0].args,
            ("hub/avery-home", "avery-bot", "write"),
        )

    def test_missing_work_repo_refuses_before_creating_anything(self) -> None:
        admin = self._admin({"hub/requests": {}})
        with self.assertRaisesRegex(CoordinatorError, "does not exist: hub/urllib3"):
            self._onboard(admin)
        admin.create_user_repository.assert_not_called()
        admin.create_user.assert_not_called()

    def test_reassignment_revokes_what_the_profile_dropped(self) -> None:
        admin = self._admin(
            {
                "avery-bot/home": {"private": True},
                "hub/requests": {},
                "hub/urllib3": {},
                "hub/nilearn": {},
            }
        )
        self.state.record_assignment(
            "avery-bot", ("avery-bot/home", "hub/requests", "hub/nilearn")
        )

        report = self._onboard(admin)

        self.assertEqual(report.revoked_repos, ("hub/nilearn",))
        admin.remove_collaborator.assert_called_once_with("hub/nilearn", "avery-bot")
        self.agent_client.return_value.unwatch_repository.assert_called_once_with(
            "hub/nilearn"
        )
        self.assertEqual(
            self.state.assigned_repos("avery-bot"),
            ("avery-bot/home", "hub/requests", "hub/urllib3"),
        )

    def test_a_former_agent_owned_home_is_unwatched_but_stays_owned(self) -> None:
        admin = self._admin(
            {
                "avery-bot/home": {"private": True},
                "hub/requests": {},
                "hub/urllib3": {},
            }
        )
        self.state.record_assignment(
            "avery-bot", ("avery-bot/old-home", "hub/requests")
        )

        report = self._onboard(admin)

        self.assertEqual(report.revoked_repos, ("avery-bot/old-home",))
        admin.remove_collaborator.assert_not_called()
        self.agent_client.return_value.unwatch_repository.assert_called_once_with(
            "avery-bot/old-home"
        )

    def test_without_state_nothing_is_reconciled(self) -> None:
        admin = self._admin(
            {"avery-bot/home": {"private": True}, "hub/requests": {}, "hub/urllib3": {}}
        )
        with (
            patch("swecraft_coordinator.onboard.AdminForgeClient", return_value=admin),
            patch("swecraft_coordinator.onboard.ensure_memory_files", return_value=[]),
        ):
            report = onboard_agent(self.config, self.config.agent("avery-bot"))
        self.assertEqual(report.revoked_repos, ())
        self.assertEqual(self.state.assigned_repos("avery-bot"), ())


if __name__ == "__main__":
    unittest.main()

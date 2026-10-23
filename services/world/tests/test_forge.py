from __future__ import annotations

import json
import unittest

import httpx

from swecraft_world import WorldError
from swecraft_world.forge import WorldForgeClient


class FakeApi:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict | None]] = []
        self.repositories: dict[str, dict] = {}
        self.org_exists = False
        self.tags: list[dict] = []
        self.tasks: list[dict] = []
        self.workflows_present = False
        self.transport = httpx.MockTransport(self.handle)

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/api/v1")
        body = json.loads(request.content) if request.content else None
        self.calls.append((request.method, path, body))
        if path == "/orgs/hub":
            return httpx.Response(200 if self.org_exists else 404, json={})
        if path == "/orgs" and request.method == "POST":
            self.org_exists = True
            return httpx.Response(201, json={"username": "hub"})
        if path == "/orgs/hub/repos" and request.method == "POST":
            assert body is not None
            self.repositories[body["name"]] = {
                "empty": True,
                "has_actions": False,
                "private": bool(body["private"]),
            }
            return httpx.Response(201, json={})
        prefix = "/repos/hub/"
        if path.startswith(prefix):
            rest = path[len(prefix) :]
            name, _, suffix = rest.partition("/")
            if not suffix:
                if request.method == "GET":
                    repository = self.repositories.get(name)
                    return httpx.Response(
                        200 if repository is not None else 404,
                        json=repository or {},
                    )
                if request.method == "DELETE":
                    self.repositories.pop(name, None)
                    return httpx.Response(204)
                if request.method == "PATCH":
                    assert body is not None
                    self.repositories[name].update(body)
                    return httpx.Response(200, json=self.repositories[name])
            if suffix == "tags":
                page = int(request.url.params.get("page", "1"))
                limit = int(request.url.params.get("limit", "50"))
                start = (page - 1) * limit
                return httpx.Response(200, json=self.tags[start : start + limit])
            if suffix == "actions/tasks":
                return httpx.Response(200, json=self.tasks)
            if suffix == "actions/runs":
                # Forgejo reports runs here whether or not a runner has picked
                # them up; `actions/tasks` above only lists dispatched work and
                # returns nothing while runs sit `waiting`. The `status`
                # query parameter filters on a single status value.
                status = request.url.params.get("status")
                runs = [
                    run
                    for run in self.tasks
                    if status is None or run.get("status") == status
                ]
                return httpx.Response(
                    200,
                    json={"total_count": len(runs), "workflow_runs": runs},
                )
            if suffix == "contents/.github/workflows":
                return httpx.Response(
                    200 if self.workflows_present else 404,
                    json={} if self.workflows_present else {"message": "missing"},
                )
        return httpx.Response(404, json={"message": "unknown"})


class WorldForgeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.api = FakeApi()
        self.forge = WorldForgeClient(
            "http://forge.test",
            "token",
            "hub",
            transport=self.api.transport,
        )

    def test_forge_url_cannot_embed_credentials(self) -> None:
        with self.assertRaisesRegex(WorldError, "must not contain credentials"):
            WorldForgeClient("https://user:secret@forge.test", "token", "hub")

    def test_ensure_org_and_create_empty_repository(self) -> None:
        self.forge.ensure_org()
        self.forge.prepare_empty_repository("demo", "main", "public", replace=False)
        self.forge.configure_repository("demo", "main", "public")

        self.assertTrue(self.api.org_exists)
        self.assertEqual(
            self.api.repositories["demo"],
            # configure_repository settles the branch only; Actions stays
            # off until enable_actions runs.
            {
                "empty": True,
                "has_actions": False,
                "private": False,
                "default_branch": "main",
            },
        )
        self.assertEqual(self.forge.clone_url("demo"), "http://forge.test/hub/demo.git")

    def test_private_repository_creation_and_verification(self) -> None:
        self.forge.prepare_empty_repository("demo", "main", "private", replace=False)
        self.forge.configure_repository("demo", "main", "private")
        self.api.repositories["demo"]["empty"] = False

        self.forge.assert_repository_state(
            "demo", require_actions=False, expected_visibility="private"
        )
        with self.assertRaisesRegex(WorldError, "visibility"):
            self.forge.assert_repository_state(
                "demo", require_actions=False, expected_visibility="public"
            )

    def test_nonempty_repository_requires_explicit_replace(self) -> None:
        self.api.repositories["demo"] = {
            "empty": False,
            "has_actions": True,
            "private": False,
        }
        with self.assertRaisesRegex(WorldError, "refusing to replace"):
            self.forge.prepare_empty_repository("demo", "main", "public", replace=False)

        self.forge.prepare_empty_repository("demo", "main", "public", replace=True)
        self.assertTrue(self.api.repositories["demo"]["empty"])

    def test_waiting_runs_are_detected_even_when_no_task_was_dispatched(self) -> None:
        """The contamination gate must not read `actions/tasks`.

        That endpoint lists only work a runner has picked up. Behind a
        saturated runner every run stays `waiting` and it reports zero --
        measured live on one of our worlds, where pydantic and orjson showed 0 tasks
        against 236 and 246 runs. A gate built on it would pass exactly the
        contaminated world it exists to reject.
        """

        self.api.repositories["demo"] = {
            "empty": False,
            "has_actions": False,
            "private": False,
        }
        self.api.tasks = [{"id": 1, "status": "waiting"}]

        self.assertEqual(self.forge.queued_action_tasks("demo"), 1)

    def test_active_runs_exclude_completed_history(self) -> None:
        """`actions-status` reports the queue; the gate reports all history.

        On a live world completed runs accumulate, so the status report must
        filter to waiting/running/blocked while the contamination gate keeps
        counting every run.
        """

        self.api.repositories["demo"] = {
            "empty": False,
            "has_actions": True,
            "private": False,
        }
        self.api.tasks = [
            {"id": 1, "status": "waiting"},
            {"id": 2, "status": "running"},
            {"id": 3, "status": "success"},
            {"id": 4, "status": "failure"},
        ]

        self.assertEqual(self.forge.active_action_runs("demo"), 2)
        self.assertEqual(self.forge.queued_action_tasks("demo"), 4)

    def test_repository_state_rejects_tags_workflows_and_seed_tasks(self) -> None:
        self.api.repositories["demo"] = {
            "empty": False,
            "has_actions": True,
            "private": False,
        }
        self.forge.assert_repository_state(
            "demo", expected_tags={}, require_no_action_tasks=True
        )

        for attribute, value, message in (
            ("tags", [{"name": "v1", "commit": {"sha": "a" * 40}}], "tags"),
            ("workflows_present", True, "workflows"),
            ("tasks", [{"id": 1}], "queued Actions"),
        ):
            setattr(self.api, attribute, value)
            with self.subTest(attribute=attribute):
                with self.assertRaisesRegex(WorldError, message):
                    self.forge.assert_repository_state(
                        "demo",
                        expected_tags={},
                        require_no_action_tasks=True,
                    )
            setattr(self.api, attribute, [] if isinstance(value, list) else False)

    def test_tag_listing_paginates_past_the_first_page(self) -> None:
        """An upstream with hundreds of releases must not satisfy the
        exact-set assertion with only the API's first page."""

        self.api.repositories["demo"] = {
            "empty": False,
            "has_actions": True,
            "private": False,
        }
        expected = {f"v0.{n}": "a" * 40 for n in range(120)}
        self.api.tags = [
            {"name": name, "commit": {"sha": sha}} for name, sha in expected.items()
        ]
        self.forge.assert_repository_state("demo", expected_tags=expected)
        with self.assertRaisesRegex(WorldError, "do not match"):
            self.forge.assert_repository_state(
                "demo", expected_tags=dict(list(expected.items())[:60])
            )

    def test_repository_state_requires_exactly_the_seeded_tag(self) -> None:
        self.api.repositories["demo"] = {
            "empty": False,
            "has_actions": True,
            "private": False,
        }
        expected = {"v1": "a" * 40}
        with self.assertRaisesRegex(WorldError, "do not match"):
            self.forge.assert_repository_state("demo", expected_tags=expected)
        self.api.tags = [{"name": "v1", "commit": {"sha": "a" * 40}}]
        self.forge.assert_repository_state("demo", expected_tags=expected)
        # Same name at a different commit is tampering, not a match.
        self.api.tags = [{"name": "v1", "commit": {"sha": "b" * 40}}]
        with self.assertRaisesRegex(WorldError, "do not match"):
            self.forge.assert_repository_state("demo", expected_tags=expected)

    def test_exact_seed_recovery_can_repair_disabled_actions(self) -> None:
        self.api.repositories["demo"] = {
            "empty": False,
            "has_actions": False,
            "private": False,
        }
        self.forge.assert_seed_recovery_safe("demo")

        for attribute, value, message in (
            ("tags", [{"name": "v1", "commit": {"sha": "a" * 40}}], "tags"),
            ("tasks", [{"id": 1}], "Actions history"),
        ):
            setattr(self.api, attribute, value)
            with self.subTest(attribute=attribute):
                with self.assertRaisesRegex(WorldError, message):
                    self.forge.assert_seed_recovery_safe("demo")
            setattr(self.api, attribute, [])

    def test_recovery_accepts_the_expected_seed_tag_as_a_subset(self) -> None:
        """Mid-recovery the tag push may or may not have happened: absent
        is fine, present-and-exact is fine, anything else refuses."""

        self.api.repositories["demo"] = {
            "empty": False,
            "has_actions": False,
            "private": False,
        }
        expected = {"v1": "a" * 40}
        self.forge.assert_seed_recovery_safe("demo", expected_tags=expected)
        self.api.tags = [{"name": "v1", "commit": {"sha": "a" * 40}}]
        self.forge.assert_seed_recovery_safe("demo", expected_tags=expected)
        self.api.tags = [{"name": "v1", "commit": {"sha": "b" * 40}}]
        with self.assertRaisesRegex(WorldError, "unexpected tags"):
            self.forge.assert_seed_recovery_safe("demo", expected_tags=expected)

    def test_live_verification_allows_agent_release_tags(self) -> None:
        self.api.repositories["demo"] = {
            "empty": False,
            "has_actions": True,
            "private": False,
        }
        self.api.tags = [{"name": "agent-release-v1"}]
        self.forge.assert_repository_state("demo")


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import base64
import json
import threading
import unittest
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest.mock import patch

import httpx
from swecraft_common.forge import ForgeError

from swecraft_coordinator.forge_admin import (
    AGENT_TOKEN_SCOPES,
    TOKEN_NAME,
    AdminForgeClient,
    AgentForgeClient,
)


class _RecordingHandler(BaseHTTPRequestHandler):
    requests: list[tuple[str, str, str]] = []
    bodies: list[dict] = []

    def _record(self) -> None:
        type(self).requests.append(
            (self.command, self.path, self.headers.get("Authorization") or "")
        )
        length = int(self.headers.get("Content-Length") or 0)
        type(self).bodies.append(json.loads(self.rfile.read(length)) if length else {})
        body = b"{}"
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    do_GET = do_PUT = do_POST = do_PATCH = _record

    def log_message(self, *arguments: object) -> None:
        pass


class WatchRepositoryTests(unittest.TestCase):
    """The subscription call itself, against a live HTTP listener.

    The onboarding tests mock the client, so a typo in the verb or path
    would pass them — the exact silent-failure class this call exists to
    fix.
    """

    def setUp(self) -> None:
        _RecordingHandler.requests = []
        _RecordingHandler.bodies = []
        self.server = HTTPServer(("127.0.0.1", 0), _RecordingHandler)
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self.server.shutdown)
        self.base_url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def test_watch_repository_puts_the_subscription_as_the_agent(self) -> None:
        AgentForgeClient(self.base_url, "agent-token").watch_repository("hub/demo")
        self.assertEqual(
            _RecordingHandler.requests,
            [("PUT", "/api/v1/repos/hub/demo/subscription", "token agent-token")],
        )


class RequireReviewTests(unittest.TestCase):
    """The board protection posts a rule that lets no one merge their own transfer."""

    def setUp(self) -> None:
        _RecordingHandler.requests = []
        _RecordingHandler.bodies = []
        self.server = HTTPServer(("127.0.0.1", 0), _RecordingHandler)
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self.server.shutdown)
        self.base_url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def test_require_review_updates_the_existing_rule(self) -> None:
        AdminForgeClient(self.base_url, "admin-token").require_review(
            "hub/society", "main"
        )
        self.assertEqual(
            [(method, path) for method, path, _ in _RecordingHandler.requests],
            [
                ("GET", "/api/v1/repos/hub/society/branch_protections/main"),
                ("PATCH", "/api/v1/repos/hub/society/branch_protections/main"),
            ],
        )
        rule = _RecordingHandler.bodies[-1]
        self.assertEqual(rule["required_approvals"], 1)
        self.assertFalse(rule["enable_push"])
        self.assertTrue(rule["dismiss_stale_approvals"])


class ProtectBranchTests(unittest.TestCase):
    """The memory-branch rule names the operator as the only pusher."""

    def setUp(self) -> None:
        _RecordingHandler.requests = []
        _RecordingHandler.bodies = []
        self.server = HTTPServer(("127.0.0.1", 0), _RecordingHandler)
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self.server.shutdown)
        self.base_url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def test_protect_branch_whitelists_the_pushers(self) -> None:
        AdminForgeClient(self.base_url, "admin-token").protect_branch(
            "hub/demo", "agent/memory", pushers=["hub-operator"]
        )
        self.assertEqual(
            [(method, path) for method, path, _ in _RecordingHandler.requests],
            [
                ("GET", "/api/v1/repos/hub/demo/branch_protections/agent/memory"),
                ("PATCH", "/api/v1/repos/hub/demo/branch_protections/agent/memory"),
            ],
        )
        rule = _RecordingHandler.bodies[-1]
        self.assertEqual(rule["branch_name"], "agent/memory")
        self.assertTrue(rule["enable_push"])
        self.assertTrue(rule["enable_push_whitelist"])
        self.assertEqual(rule["push_whitelist_usernames"], ["hub-operator"])
        self.assertEqual(rule["required_approvals"], 0)


class CreateUserTests(unittest.TestCase):
    """The user record onboarding posts, against a live HTTP listener."""

    def setUp(self) -> None:
        _RecordingHandler.requests = []
        _RecordingHandler.bodies = []
        self.server = HTTPServer(("127.0.0.1", 0), _RecordingHandler)
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self.server.shutdown)
        self.base_url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def test_the_agent_email_carries_a_dotted_domain(self) -> None:
        # Gitea's email validator rejects a dotless domain such as
        # `@localhost` (422 on the user record); Forgejo accepts either. The
        # address is never delivered to, so a dotted local domain serves both.
        AdminForgeClient(self.base_url, "admin-token").create_user("demo-bot", "pw")
        self.assertEqual(
            _RecordingHandler.requests,
            [("POST", "/api/v1/admin/users", "token admin-token")],
        )
        self.assertEqual(
            _RecordingHandler.bodies[0]["email"], "demo-bot@swecraft.local"
        )
        self.assertEqual(_RecordingHandler.bodies[0]["username"], "demo-bot")


class FakeForgeApi:
    """Records the mint exchange and can misbehave on the POST."""

    def __init__(
        self,
        *,
        post_response: httpx.Response | None = None,
        post_times_out: bool = False,
    ) -> None:
        self.requests: list[dict] = []
        self.post_response = post_response
        self.post_times_out = post_times_out
        self.transport = httpx.MockTransport(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(
            {
                "method": request.method,
                "url": str(request.url),
                "host": urllib.parse.urlsplit(str(request.url)).netloc,
                "auth": request.headers.get("Authorization"),
                "body": json.loads(request.content) if request.content else None,
            }
        )
        if request.method == "DELETE":
            return httpx.Response(404, json={"message": "not found"})
        if request.method == "POST":
            if self.post_times_out:
                raise httpx.ReadTimeout("slow forge", request=request)
            if self.post_response is not None:
                return self.post_response
        return httpx.Response(200, json={"sha1": "minted-token"})


class MintAgentTokenTests(unittest.TestCase):
    """Token creation is the one call that carries the agent's password.

    It cannot present a token, so it presents basic auth — the strongest
    credential the coordinator holds, on the one endpoint that is exempt
    from the token boundary. These tests pin it to the same bounded,
    non-redirecting, timed transport as every other Forgejo call, so the
    exception cannot silently drift back out of the hardening.
    """

    password = "correct-horse-battery-staple"
    host = "forge.test"

    def client(self, api: FakeForgeApi) -> AdminForgeClient:
        return AdminForgeClient(
            f"http://{self.host}", "admin-token", transport=api.transport
        )

    def expected_credential(self) -> str:
        raw = base64.b64encode(f"demo-agent:{self.password}".encode()).decode()
        return f"Basic {raw}"

    def assert_password_stayed_on_the_forge(self, api: FakeForgeApi) -> None:
        credential = self.expected_credential()
        elsewhere = [
            request
            for request in api.requests
            if request["auth"] == credential and request["host"] != self.host
        ]
        self.assertEqual(elsewhere, [], "basic credentials left the forge origin")

    def test_mint_replaces_the_prior_token_over_basic_auth(self) -> None:
        api = FakeForgeApi()

        token = self.client(api).mint_agent_token("demo-agent", self.password)

        self.assertEqual(token, "minted-token")
        credential = self.expected_credential()
        self.assertEqual(
            [
                (request["method"], request["url"], request["auth"])
                for request in api.requests
            ],
            [
                (
                    "DELETE",
                    f"http://{self.host}/api/v1/users/demo-agent/tokens/{TOKEN_NAME}",
                    credential,
                ),
                (
                    "POST",
                    f"http://{self.host}/api/v1/users/demo-agent/tokens",
                    credential,
                ),
            ],
        )
        self.assertEqual(
            api.requests[-1]["body"],
            {"name": TOKEN_NAME, "scopes": AGENT_TOKEN_SCOPES},
        )

    def test_redirects_are_not_followed_with_the_password(self) -> None:
        api = FakeForgeApi(
            post_response=httpx.Response(
                302, headers={"Location": "https://other.test/stolen"}
            )
        )

        with self.assertRaisesRegex(ForgeError, "302"):
            self.client(api).mint_agent_token("demo-agent", self.password)

        # The 404 DELETE, then the redirected POST — and nothing after it.
        # urllib's redirect handler copies every header except content-length
        # and content-type onto the new request, across a host change, and
        # redirects POST on 301/302/303; this call used to run through it.
        self.assertEqual(
            [request["method"] for request in api.requests], ["DELETE", "POST"]
        )
        self.assert_password_stayed_on_the_forge(api)

    def test_the_mint_response_is_bounded(self) -> None:
        api = FakeForgeApi()

        with patch("swecraft_common.forge.MAX_RESPONSE_BYTES", 1):
            with self.assertRaisesRegex(ForgeError, "response limit"):
                self.client(api).mint_agent_token("demo-agent", self.password)

    def test_a_hanging_forge_times_out_rather_than_blocking_onboarding(self) -> None:
        api = FakeForgeApi(post_times_out=True)

        with self.assertRaisesRegex(ForgeError, "timed out"):
            self.client(api).mint_agent_token("demo-agent", self.password)


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import base64
import json
import unittest
from unittest.mock import patch

import httpx

from swecraft_common.forge import ForgeClient, ForgeError


class FakeForgeApi:
    def __init__(self) -> None:
        self.requests: list[dict] = []
        self.releases_payload: object = [{"tag_name": "v1.0.0"}]
        self.file_text = "demo-bot: 100\n"
        self.created: list[dict] = []
        self.transport = httpx.MockTransport(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        body = request.content
        self.requests.append(
            {
                "method": request.method,
                "path": request.url.raw_path.decode(),
                "auth": request.headers.get("Authorization"),
                "body": json.loads(body) if body else None,
            }
        )
        if request.url.path.endswith("/missing"):
            return httpx.Response(404, json={"message": "not found"})
        if request.url.path.endswith("/redirect"):
            return httpx.Response(302, headers={"Location": "https://other.test/"})
        if request.url.path.endswith("/invalid-json"):
            return httpx.Response(200, content=b"not-json")
        if request.url.path.endswith("/timeout"):
            raise httpx.ReadTimeout("slow forge", request=request)
        if request.url.path.endswith("/user"):
            return httpx.Response(200, json={"login": "demo-bot"})
        if "/branches/" in request.url.path:
            return httpx.Response(200, json={"name": "b", "commit": {"id": "a" * 40}})
        if "/contents/" in request.url.path:
            if request.method == "POST":
                self.created.append(json.loads(body))
                return httpx.Response(201, json={"content": {"sha": "c" * 40}})
            if request.url.path.endswith("/dir"):
                return httpx.Response(200, json=[{"type": "file"}])
            if request.url.path.endswith("/binary"):
                return httpx.Response(
                    200,
                    json={
                        "type": "file",
                        "sha": "d" * 40,
                        "content": "/w==",
                        "encoding": "base64",
                    },
                )
            return httpx.Response(
                200,
                json={
                    "type": "file",
                    "sha": "b" * 40,
                    "encoding": "base64",
                    "content": base64.b64encode(self.file_text.encode()).decode(),
                },
            )
        if request.url.path.endswith("/releases"):
            return httpx.Response(200, json=self.releases_payload)
        return httpx.Response(200, json=[])


class ForgeClientTests(unittest.TestCase):
    def setUp(self) -> None:
        self.api = FakeForgeApi()
        self.client = ForgeClient(
            "http://forge.test", "tok-123", transport=self.api.transport
        )

    def test_authenticated_request_sends_the_token(self) -> None:
        self.assertEqual(self.client.get_authenticated_username(), "demo-bot")
        self.assertEqual(self.api.requests[-1]["auth"], "token tok-123")

    def test_branch_exists_treats_only_404_as_absent(self) -> None:
        self.assertFalse(self.client.branch_exists("hub/demo", "missing"))
        self.assertTrue(self.client.branch_exists("hub/demo", "agent/memory"))

        missing, existing = self.api.requests[-2:]
        self.assertEqual(missing["path"], "/api/v1/repos/hub/demo/branches/missing")
        self.assertEqual(
            existing["path"], "/api/v1/repos/hub/demo/branches/agent%2Fmemory"
        )

    def test_get_branch_quotes_the_ref_and_returns_the_payload(self) -> None:
        payload = self.client.get_branch("hub/demo", "agent/memory")
        self.assertEqual(payload["commit"]["id"], "a" * 40)
        self.assertTrue(
            self.api.requests[-1]["path"].endswith("/branches/agent%2Fmemory")
        )

    def test_get_releases_passes_the_limit_and_returns_the_payload(self) -> None:
        releases = self.client.get_releases("hub/demo", limit=1)
        self.assertEqual(releases[0]["tag_name"], "v1.0.0")
        self.assertIn("limit=1", self.api.requests[-1]["path"])

    def test_get_releases_normalizes_a_non_list_payload_to_empty(self) -> None:
        self.api.releases_payload = {"message": "unexpected shape"}
        self.assertEqual(self.client.get_releases("hub/demo"), [])

    def test_http_errors_become_forge_errors(self) -> None:
        with self.assertRaisesRegex(ForgeError, "404") as raised:
            self.client._request("GET", "/missing")
        self.assertEqual(raised.exception.status_code, 404)

    def test_redirects_are_not_followed_with_the_token(self) -> None:
        with self.assertRaisesRegex(ForgeError, "302"):
            self.client._request("GET", "/redirect")
        self.assertEqual(len(self.api.requests), 1)

    def _timeout_transport(self, calls: dict, succeed_after: int = 0):
        def handle(request: httpx.Request) -> httpx.Response:
            calls["n"] = calls.get("n", 0) + 1
            if succeed_after and calls["n"] >= succeed_after:
                return httpx.Response(200, json={"login": "demo-bot"})
            raise httpx.ReadTimeout("busy repository", request=request)

        return httpx.MockTransport(handle)

    def test_idempotent_reads_retry_then_succeed(self) -> None:
        """A read queued behind a push must not fail the caller."""

        calls: dict = {}
        client = ForgeClient(
            "http://forge.test", "tok", transport=self._timeout_transport(calls, 3)
        )
        with patch("swecraft_common.forge._backoff"):
            self.assertEqual(client.get_authenticated_username(), "demo-bot")
        self.assertEqual(calls["n"], 3)

    def test_mutating_requests_are_attempted_exactly_once(self) -> None:
        """A timeout is indistinguishable from a slow success, so a retried
        POST could open a second pull request."""

        calls: dict = {}
        client = ForgeClient(
            "http://forge.test", "tok", transport=self._timeout_transport(calls)
        )
        with patch("swecraft_common.forge._backoff"):
            with self.assertRaises(ForgeError):
                client._request("POST", "/repos/hub/demo/pulls", {"title": "x"})
        self.assertEqual(calls["n"], 1)

    def test_retries_are_bounded(self) -> None:
        calls: dict = {}
        client = ForgeClient(
            "http://forge.test", "tok", transport=self._timeout_transport(calls)
        )
        with patch("swecraft_common.forge._backoff"):
            with self.assertRaisesRegex(ForgeError, "attempt"):
                client.get_authenticated_username()
        self.assertEqual(calls["n"], 3)

    def test_gateway_errors_retry_but_answers_do_not(self) -> None:
        calls: dict = {}

        def handle(request: httpx.Request) -> httpx.Response:
            calls["n"] = calls.get("n", 0) + 1
            return httpx.Response(503 if calls["n"] < 2 else 200, json={"ok": True})

        client = ForgeClient(
            "http://forge.test", "tok", transport=httpx.MockTransport(handle)
        )
        with patch("swecraft_common.forge._backoff"):
            self.assertEqual(client._request("GET", "/x"), {"ok": True})
        self.assertEqual(calls["n"], 2)

        # A 404 is an answer, not a hiccup: exactly one attempt.
        before = len(self.api.requests)
        with self.assertRaises(ForgeError):
            self.client._request("GET", "/missing")
        self.assertEqual(len(self.api.requests) - before, 1)

    def test_timeout_and_invalid_json_are_normalized(self) -> None:
        # Patched so the suite does not pay the real retry backoff.
        with patch("swecraft_common.forge._backoff"):
            with self.assertRaisesRegex(ForgeError, "timed out"):
                self.client._request("GET", "/timeout")
        with self.assertRaisesRegex(ForgeError, "invalid JSON"):
            self.client._request("GET", "/invalid-json")

    def test_response_size_is_bounded(self) -> None:
        with patch("swecraft_common.forge.MAX_RESPONSE_BYTES", 1):
            with self.assertRaisesRegex(ForgeError, "response limit"):
                self.client._request("GET", "/large-enough")

    def test_a_basic_credential_can_replace_the_token_header(self) -> None:
        """The override the coordinator's token-minting call rides on.

        Forgejo's token-creation endpoint cannot accept a token, so it is the
        one caller that must present a password. It gets this parameter
        rather than its own client so that it inherits the bounds, the
        timeout, and the refusal to follow redirects proven above.
        """

        self.client._request("GET", "/user", authorization="Basic Zm9vOmJhcg==")

        self.assertEqual(self.api.requests[-1]["auth"], "Basic Zm9vOmJhcg==")

    def test_an_overridden_credential_is_not_carried_through_a_redirect(self) -> None:
        with self.assertRaisesRegex(ForgeError, "302"):
            self.client._request("GET", "/redirect", authorization="Basic Zm9vOmJhcg==")

        self.assertEqual(len(self.api.requests), 1)


class FileTests(unittest.TestCase):
    def setUp(self) -> None:
        self.api = FakeForgeApi()
        self.client = ForgeClient(
            "http://forge.test", "tok-123", transport=self.api.transport
        )

    def test_get_file_returns_sha_and_text_or_none(self) -> None:
        self.assertEqual(
            self.client.get_file("hub/society", "budget.yaml"),
            ("b" * 40, "demo-bot: 100\n"),
        )
        self.assertEqual(
            self.api.requests[-1]["path"],
            "/api/v1/repos/hub/society/contents/budget.yaml",
        )
        self.client.get_file("hub/society", "a dir/b.yaml", ref="main")
        self.assertEqual(
            self.api.requests[-1]["path"],
            "/api/v1/repos/hub/society/contents/a%20dir/b.yaml?ref=main",
        )
        self.assertIsNone(self.client.get_file("hub/society", "missing"))
        with self.assertRaisesRegex(ForgeError, "not a file"):
            self.client.get_file("hub/society", "dir")
        with self.assertRaisesRegex(ForgeError, "not UTF-8"):
            self.client.get_file("hub/society", "binary")
        with self.assertRaisesRegex(ForgeError, "not a repository file path"):
            self.client.get_file("hub/society", "../secrets")

    def test_create_file_commits_base64_content(self) -> None:
        sha = self.client.create_file(
            "hub/society",
            "budget.yaml",
            "demo-bot: 100\n",
            message="seed",
            branch="main",
        )
        self.assertEqual(sha, "c" * 40)
        self.assertEqual(self.api.requests[-1]["method"], "POST")
        created = self.api.created[-1]
        self.assertEqual(
            base64.b64decode(created["content"]).decode(), "demo-bot: 100\n"
        )
        self.assertEqual(created["message"], "seed")
        self.assertEqual(created["branch"], "main")


if __name__ == "__main__":
    unittest.main()

"""Minimal authenticated Forgejo transport shared by Hub services."""

from __future__ import annotations

import base64
import json
import re
import time
import urllib.parse
from typing import Any

import httpx

from swecraft_common import HubError

_REPO_SLUG = re.compile(r"[A-Za-z0-9._-]+/[A-Za-z0-9._-]+")
_USERNAME = re.compile(r"[A-Za-z0-9._-]+")
# Forgejo serializes work per repository, so a read can queue behind a long
# push to the same repository and take far longer than the endpoint's own
# cost. Measured on a twelve-agent campaign: the same call answered in 48 ms
# while the repository was idle and blocked past 60 s while its agent pushed.
REQUEST_TIMEOUT_SECONDS = 90
REQUEST_ATTEMPTS = 3
RETRY_BACKOFF_SECONDS = 2.0
# A retry must not be able to open a second pull request, post a second
# comment, or merge twice. A timeout is indistinguishable from a slow success
# at the client — and `POST /pulls` was observed taking 81 s — so anything
# that mutates state is attempted exactly once.
_RETRY_METHODS = frozenset({"GET", "HEAD"})
_RETRY_STATUS = frozenset({502, 503, 504})
MAX_RESPONSE_BYTES = 10 * 1024 * 1024


class ForgeError(HubError):
    """Raised when a Forgejo API call fails."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


def _quoted_path(path: str) -> str:
    parts = [part for part in path.strip("/").split("/") if part]
    if not parts or any(part == ".." for part in parts):
        raise ForgeError(f"not a repository file path: {path!r}")
    return "/".join(urllib.parse.quote(part, safe="") for part in parts)


def _backoff(attempt: int) -> None:
    time.sleep(RETRY_BACKOFF_SECONDS * attempt)


def _read_response(
    response: httpx.Response, *, limit: int, reject_over: bool = True
) -> bytes:
    payload = bytearray()
    for chunk in response.iter_bytes():
        remaining = limit - len(payload)
        if len(chunk) > remaining:
            if reject_over:
                raise ForgeError(
                    f"Forgejo response exceeded the {limit}-byte response limit"
                )
            payload.extend(chunk[:remaining])
            break
        payload.extend(chunk)
    return bytes(payload)


class ForgeClient:
    """Small Forgejo JSON client with bounded, non-redirecting requests.

    Only operations shared by current services belong here. Service-specific
    admin or workflow endpoints should build on ``_request`` in the service
    that owns their policy rather than turning this class into a Forgejo SDK.
    """

    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._transport = transport

    def _request(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
        *,
        params: dict[str, Any] | None = None,
        authorization: str | None = None,
    ) -> Any:
        """Issue one JSON request; protected for service-specific clients.

        ``authorization`` replaces this client's token header for the rare
        call that cannot present a token — Forgejo's token-creation endpoint
        requires basic auth. It is a parameter rather than a second transport
        so that every credential, including a password, travels this one
        bounded, non-redirecting, timed path.
        """

        url = f"{self._base_url}/api/v1{path}"
        attempts = REQUEST_ATTEMPTS if method.upper() in _RETRY_METHODS else 1
        for attempt in range(1, attempts + 1):
            last = attempt == attempts
            try:
                with (
                    httpx.Client(
                        headers={
                            "Authorization": authorization or f"token {self._token}",
                            "Accept": "application/json",
                        },
                        timeout=REQUEST_TIMEOUT_SECONDS,
                        follow_redirects=False,
                        transport=self._transport,
                    ) as client,
                    client.stream(method, url, json=body, params=params) as response,
                ):
                    if not 200 <= response.status_code < 300:
                        detail = _read_response(
                            response, limit=200, reject_over=False
                        ).decode("utf-8", errors="replace")
                        if not last and response.status_code in _RETRY_STATUS:
                            _backoff(attempt)
                            continue
                        raise ForgeError(
                            f"Forgejo {method} {path} failed with "
                            f"{response.status_code}: {detail}",
                            status_code=response.status_code,
                        )
                    payload = _read_response(response, limit=MAX_RESPONSE_BYTES)
                break
            except httpx.TimeoutException:
                if not last:
                    _backoff(attempt)
                    continue
                raise ForgeError(
                    f"Forgejo {method} {path} timed out after "
                    f"{REQUEST_TIMEOUT_SECONDS}s ({attempts} attempt(s))"
                ) from None
            except httpx.RequestError as error:
                if not last:
                    _backoff(attempt)
                    continue
                raise ForgeError(f"Forgejo {method} {path} failed: {error}") from None
        if not payload:
            return None
        try:
            return json.loads(payload)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ForgeError(
                f"Forgejo {method} {path} returned invalid JSON"
            ) from error

    @staticmethod
    def _validated_repo(repo: str) -> str:
        if not _REPO_SLUG.fullmatch(repo):
            raise ForgeError(f"invalid repository slug: {repo!r}")
        return repo

    @staticmethod
    def _validated_username(username: str) -> str:
        if not _USERNAME.fullmatch(username):
            raise ForgeError(f"invalid username: {username!r}")
        return username

    def get_authenticated_username(self) -> str:
        user = self._request("GET", "/user")
        login = (user or {}).get("login")
        if not isinstance(login, str) or not login:
            raise ForgeError("could not determine the token's user")
        return login

    def branch_exists(self, repo: str, branch: str) -> bool:
        repo = self._validated_repo(repo)
        quoted = urllib.parse.quote(branch, safe="")
        try:
            self._request("GET", f"/repos/{repo}/branches/{quoted}")
        except ForgeError as error:
            if error.status_code == 404:
                return False
            raise
        return True

    def get_repository(self, repo: str) -> Any:
        repo = self._validated_repo(repo)
        return self._request("GET", f"/repos/{repo}")

    def get_branch(self, repo: str, branch: str) -> Any:
        repo = self._validated_repo(repo)
        quoted = urllib.parse.quote(branch, safe="")
        return self._request("GET", f"/repos/{repo}/branches/{quoted}")

    def get_file(
        self, repo: str, path: str, *, ref: str | None = None
    ) -> tuple[str, str] | None:
        """One text file from a repository: its blob sha and content, or None.

        ``ref`` is a branch, tag or commit; the default branch when omitted.
        Only files come back: a directory or a submodule at ``path`` is an
        error, and so is content that is not UTF-8 text.
        """

        repo = self._validated_repo(repo)
        try:
            payload = self._request(
                "GET",
                f"/repos/{repo}/contents/{_quoted_path(path)}",
                params={"ref": ref} if ref else None,
            )
        except ForgeError as error:
            if error.status_code == 404:
                return None
            raise
        if not isinstance(payload, dict) or payload.get("type") != "file":
            raise ForgeError(f"{repo}:{path} is not a file")
        sha = str(payload.get("sha") or "")
        if not sha or payload.get("encoding", "base64") != "base64":
            raise ForgeError(f"{repo}:{path} came back in an unexpected shape")
        try:
            text = base64.b64decode(payload.get("content") or "", validate=True)
            return sha, text.decode("utf-8")
        except (ValueError, UnicodeDecodeError) as error:
            raise ForgeError(f"{repo}:{path} is not UTF-8 text: {error}") from None

    def create_file(
        self,
        repo: str,
        path: str,
        text: str,
        *,
        message: str,
        branch: str | None = None,
    ) -> str:
        """Commit a new text file and return its blob sha.

        Creation only: Forgejo refuses the call when the file exists, and so
        does this method, rather than silently overwriting.
        """

        repo = self._validated_repo(repo)
        body: dict[str, Any] = {
            "content": base64.b64encode(text.encode("utf-8")).decode("ascii"),
            "message": message,
        }
        if branch:
            body["branch"] = branch
        payload = self._request(
            "POST", f"/repos/{repo}/contents/{_quoted_path(path)}", body
        )
        sha = (
            payload.get("content", {}).get("sha") if isinstance(payload, dict) else None
        )
        if not sha:
            raise ForgeError(f"creating {repo}:{path} returned no blob sha")
        return str(sha)

    def get_releases(self, repo: str, *, limit: int = 50) -> list[Any]:
        repo = self._validated_repo(repo)
        payload = self._request("GET", f"/repos/{repo}/releases?limit={int(limit)}")
        return payload if isinstance(payload, list) else []

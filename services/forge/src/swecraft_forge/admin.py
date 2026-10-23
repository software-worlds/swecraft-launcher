"""Forge-owned administrative API primitives used during resume."""

from __future__ import annotations

import base64
from collections.abc import Sequence
from typing import Any

from swecraft_common.forge import ForgeClient, ForgeError


class ForgeAdminClient(ForgeClient):
    def set_user_password(self, username: str, password: str) -> Any:
        username = self._validated_username(username)
        return self._request(
            "PATCH",
            f"/admin/users/{username}",
            {
                "login_name": username,
                "source_id": 0,
                "password": password,
                "must_change_password": False,
            },
        )

    def list_tokens(self, username: str, password: str) -> list[Any]:
        username = self._validated_username(username)
        listed = self._basic_request(
            "GET", f"/users/{username}/tokens", username, password
        )
        return list(listed or [])

    def delete_token(self, username: str, password: str, token: int | str) -> None:
        username = self._validated_username(username)
        try:
            self._basic_request(
                "DELETE", f"/users/{username}/tokens/{token}", username, password
            )
        except ForgeError as error:
            if error.status_code != 404:
                raise

    def mint_token(
        self, username: str, password: str, name: str, scopes: Sequence[str]
    ) -> str:
        username = self._validated_username(username)
        self.delete_token(username, password, name)
        created = self._basic_request(
            "POST",
            f"/users/{username}/tokens",
            username,
            password,
            {"name": name, "scopes": list(scopes)},
        )
        token = (created or {}).get("sha1")
        if not isinstance(token, str) or not token:
            raise ForgeError("token creation returned no token value")
        return token

    def _basic_request(
        self,
        method: str,
        path: str,
        username: str,
        password: str,
        body: dict[str, Any] | None = None,
    ) -> Any:
        credentials = base64.b64encode(f"{username}:{password}".encode()).decode(
            "ascii"
        )
        return self._request(method, path, body, authorization=f"Basic {credentials}")

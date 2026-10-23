from __future__ import annotations

import base64
import json
import os
import re
import shlex
import unittest
from unittest.mock import patch

from swecraft_runner import pi


class PiAgentTests(unittest.TestCase):
    def test_run_command_loads_exactly_the_shipped_extensions(self) -> None:
        command = pi.run_command(
            provider="anthropic",
            model="claude-opus-5",
            thinking="medium",
            instruction="Maintain it's repository",
        )
        self.assertIn("pi --print --mode json --no-extensions", command)
        self.assertEqual(command.count(" -e "), len(pi.EXTENSIONS))
        for path in pi.extension_paths():
            self.assertIn(f"-e {path}", command)
        self.assertIn(f"--session-dir {pi.SESSION_DIR}", command)
        self.assertIn("--provider anthropic --model claude-opus-5", command)
        self.assertIn("--thinking medium", command)
        self.assertIn(shlex.quote("Maintain it's repository"), command)
        self.assertIn(f"tee /logs/agent/{pi.OUTPUT_FILENAME}", command)
        with self.assertRaisesRegex(ValueError, "thinking must be one of"):
            pi.run_command(
                provider="anthropic", model="m", thinking="lots", instruction="go"
            )

    def test_install_command_ships_the_extensions_and_settings(self) -> None:
        policy = pi.LongWakePolicy()
        command = pi.install_command(context_window=1_000_000, policy=policy)
        self.assertTrue(command.startswith("command -v pi"))
        payloads = {
            target: base64.b64decode(encoded)
            for encoded, target in re.findall(
                r"printf '%s' ([A-Za-z0-9+/=]+) \| base64 -d > (\S+)", command
            )
        }
        shipped = pi.shipped_files()
        self.assertEqual(set(shipped), set(pi.EXTENSIONS))
        for name, data in shipped.items():
            self.assertEqual(payloads[f"{pi.HARNESS_DIR}/{name}"], data)
        settings = json.loads(payloads[f"{pi.HARNESS_DIR}/settings.json"])
        self.assertEqual(
            settings, pi.pi_settings(context_window=1_000_000, policy=policy)
        )
        self.assertEqual(settings["defaultProjectTrust"], "never")
        self.assertIn('"$HOME/.pi/agent/settings.json"', command)

    def test_native_summary_is_the_only_context_mode(self) -> None:
        policy = pi.LongWakePolicy()
        native = pi.pi_settings(context_window=1_000_000, policy=policy)
        self.assertTrue(native["compaction"]["enabled"])
        self.assertEqual(
            pi.extension_paths("native-summary"),
            tuple(f"{pi.HARNESS_DIR}/{n}" for n in pi.EXTENSIONS),
        )
        with self.assertRaisesRegex(ValueError, "context must be one of"):
            pi.extension_paths("mirror")
        with self.assertRaisesRegex(ValueError, "context must be one of"):
            pi.pi_settings(context_window=1, policy=policy, context="mirror")

    def test_defaults_use_the_whole_window(self) -> None:
        policy = pi.LongWakePolicy()
        self.assertEqual(policy.context_soft_tokens, 0)
        self.assertEqual(policy.context_hard_tokens, 0)
        # pi compacts above window - reserve: the edge of the window, less
        # the room a thinking model needs for the summary.
        settings = pi.pi_settings(context_window=1_000_000, policy=policy)
        self.assertEqual(
            settings["compaction"]["reserveTokens"], pi.MIN_COMPACTION_RESERVE
        )
        resolved = policy.resolved(1_000_000)
        self.assertEqual(
            resolved.context_hard_tokens, 1_000_000 - pi.HARD_STOP_HEADROOM
        )
        self.assertGreater(
            resolved.context_hard_tokens, 1_000_000 - pi.MIN_COMPACTION_RESERVE
        )
        self.assertEqual(
            resolved.environment()["SWECRAFT_CONTEXT_HARD_TOKENS"], "967232"
        )
        explicit = pi.LongWakePolicy(context_hard_tokens=300_000).resolved(1_000_000)
        self.assertEqual(explicit.context_hard_tokens, 300_000)
        self.assertEqual(policy.resolved(20_000).context_hard_tokens, 0)

    def test_compaction_reserve_puts_the_threshold_at_the_soft_budget(self) -> None:
        policy = pi.LongWakePolicy(context_soft_tokens=200_000)
        compaction = pi.pi_settings(context_window=1_000_000, policy=policy)[
            "compaction"
        ]
        self.assertTrue(compaction["enabled"])
        # pi compacts above window - reserve: 1M - 800k = the 200k soft budget.
        self.assertEqual(compaction["reserveTokens"], 800_000)
        small = pi.pi_settings(context_window=200_000, policy=policy)["compaction"]
        self.assertEqual(small["reserveTokens"], pi.MIN_COMPACTION_RESERVE)
        off = pi.pi_settings(
            context_window=1_000_000, policy=pi.LongWakePolicy(context_soft_tokens=0)
        )["compaction"]
        self.assertEqual(off["reserveTokens"], pi.MIN_COMPACTION_RESERVE)

    def test_bash_timeout_extension_reads_its_variable(self) -> None:
        source = pi.shipped_files()["bash-timeout.ts"].decode("utf-8")
        self.assertIn(pi.BASH_TIMEOUT_ENV, source)
        self.assertIn('isToolCallEventType("bash", event)', source)
        self.assertIn("SWECRAFT_BASH_TIMEOUT_MAX", source)

    def test_credentials_come_from_the_declared_env_only(self) -> None:
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "ambient"}):
            with self.assertRaisesRegex(RuntimeError, "no anthropic credential"):
                pi.credentials("anthropic", {})
            self.assertEqual(
                pi.credentials("anthropic", {"ANTHROPIC_API_KEY": "k"}),
                {"ANTHROPIC_API_KEY": "k"},
            )
        self.assertEqual(
            pi.credentials(
                "anthropic", {"ANTHROPIC_OAUTH_TOKEN": "t", "SWECRAFT_FORGE_TOKEN": "f"}
            ),
            {"ANTHROPIC_OAUTH_TOKEN": "t"},
        )
        with self.assertRaisesRegex(ValueError, "not wired"):
            pi.credentials("bedrock", {"AWS_ACCESS_KEY_ID": "a"})

    def test_vertex_credentials_take_a_key_or_a_file_and_the_project(self) -> None:
        # The CLI's name for the Vertex key is accepted and forwarded under pi's.
        self.assertEqual(
            pi.credentials(
                "google-vertex",
                {
                    "GOOGLE_API_KEY": "k",
                    "GOOGLE_CLOUD_PROJECT": "proj",
                    "GOOGLE_CLOUD_LOCATION": "global",
                    "SWECRAFT_FORGE_TOKEN": "f",
                },
            ),
            {
                "GOOGLE_CLOUD_API_KEY": "k",
                "GOOGLE_CLOUD_PROJECT": "proj",
                "GOOGLE_CLOUD_LOCATION": "global",
            },
        )
        # pi's own name wins when both are declared.
        self.assertEqual(
            pi.credentials(
                "google-vertex",
                {
                    "GOOGLE_CLOUD_API_KEY": "pi",
                    "GOOGLE_API_KEY": "cli",
                    "GOOGLE_CLOUD_PROJECT": "proj",
                    "GOOGLE_CLOUD_LOCATION": "us-central1",
                },
            )["GOOGLE_CLOUD_API_KEY"],
            "pi",
        )
        # A service-account file is a credential too; the host path is
        # forwarded as declared, the agent swaps in the sandbox copy.
        declared = {
            "GOOGLE_APPLICATION_CREDENTIALS": "/host/sa.json",
            "GOOGLE_CLOUD_PROJECT": "proj",
            "GOOGLE_CLOUD_LOCATION": "us-central1",
        }
        self.assertEqual(pi.credentials("google-vertex", declared), declared)
        self.assertEqual(pi.google_credentials_path(declared), "/host/sa.json")
        self.assertIsNone(pi.google_credentials_path({"GEMINI_API_KEY": "g"}))
        with self.assertRaisesRegex(RuntimeError, "no google-vertex credential"):
            pi.credentials(
                "google-vertex",
                {"GOOGLE_CLOUD_PROJECT": "proj", "GOOGLE_CLOUD_LOCATION": "global"},
            )
        with self.assertRaisesRegex(RuntimeError, "GOOGLE_CLOUD_PROJECT and"):
            pi.credentials("google-vertex", {"GOOGLE_CLOUD_API_KEY": "k"})
        with self.assertRaisesRegex(RuntimeError, "GOOGLE_CLOUD_LOCATION"):
            pi.credentials(
                "google-vertex",
                {"GOOGLE_CLOUD_API_KEY": "k", "GOOGLE_CLOUD_PROJECT": "proj"},
            )
        # The Gemini API stays a one-key provider.
        self.assertEqual(
            pi.credentials("google", {"GEMINI_API_KEY": "g", "GOOGLE_API_KEY": "x"}),
            {"GEMINI_API_KEY": "g"},
        )

    def test_vertex_routes_through_a_gateway_with_its_own_wire_api(self) -> None:
        models = pi.models_json("google-vertex", "https://gateway.example/")
        self.assertEqual(
            models["providers"]["google-vertex"],
            {
                "baseUrl": "https://gateway.example",
                "api": "google-vertex",
                "apiKey": "$GOOGLE_CLOUD_API_KEY",
            },
        )

    def test_a_vertex_gateway_names_no_key_for_a_service_account_run(self) -> None:
        # A configured apiKey short-circuits pi's own Vertex resolver before it
        # can fall back to the credentials file, so an ADC-only run must not
        # name one; a declared key is named under pi's name for it.
        project = {"GOOGLE_CLOUD_PROJECT": "proj", "GOOGLE_CLOUD_LOCATION": "global"}
        adc = pi.credentials(
            "google-vertex",
            {"GOOGLE_APPLICATION_CREDENTIALS": "/host/sa.json", **project},
        )
        models = pi.models_json("google-vertex", "https://gateway.example", adc)
        self.assertEqual(
            models["providers"]["google-vertex"],
            {"baseUrl": "https://gateway.example", "api": "google-vertex"},
        )
        keyed = pi.credentials("google-vertex", {"GOOGLE_API_KEY": "k", **project})
        models = pi.models_json("google-vertex", "https://gateway.example", keyed)
        self.assertEqual(
            models["providers"]["google-vertex"]["apiKey"], "$GOOGLE_CLOUD_API_KEY"
        )
        # The install step writes what models_json decided.
        routed = pi.install_command(
            context_window=1_048_576,
            policy=pi.LongWakePolicy(),
            provider="google-vertex",
            base_url="https://gateway.example",
            credentials=adc,
        )
        self.assertIn("models.json", routed)
        # Other providers keep naming their first key when no credentials are given.
        self.assertEqual(
            pi.models_json("anthropic", "https://gw.example")["providers"]["anthropic"][
                "apiKey"
            ],
            "$ANTHROPIC_API_KEY",
        )

    def test_agent_environment(self) -> None:
        environment = pi.agent_environment(
            credentials={"ANTHROPIC_API_KEY": "k"},
            bash_timeout=600,
            policy=pi.LongWakePolicy(final_turns=2),
            wake={
                "SWECRAFT_WAKE_MODE": "long-wake",
                "SWECRAFT_WAKE_TURNS": "1000",
                "SWECRAFT_FORGE_TOKEN": "never forwarded",
            },
        )
        self.assertEqual(environment["ANTHROPIC_API_KEY"], "k")
        self.assertEqual(environment["SWECRAFT_BASH_TIMEOUT"], "600")
        self.assertEqual(environment["SWECRAFT_BASH_TIMEOUT_MAX"], "0")
        self.assertEqual(environment["SWECRAFT_WAKE_MODE"], "long-wake")
        self.assertEqual(environment["SWECRAFT_WAKE_TURNS"], "1000")
        self.assertEqual(environment["SWECRAFT_FINAL_TURNS"], "2")
        self.assertEqual(environment["SWECRAFT_RESULT_PATH"], "/app/result.json")
        self.assertEqual(environment["PI_OFFLINE"], "1")
        self.assertNotIn("SWECRAFT_FORGE_TOKEN", environment)
        self.assertEqual(pi.PI_AGENT_IMPORT, "swecraft_runner.harbor_agents:SwecraftPi")

    def test_long_wake_policy(self) -> None:
        self.assertEqual(len(pi.LongWakePolicy().environment()), 11)
        giant = pi.LongWakePolicy(
            hang_aborts=6, force_continues=3, limit_wait_seconds=21_600
        ).environment()
        self.assertEqual(giant["SWECRAFT_HANG_ABORTS"], "6")
        self.assertEqual(giant["SWECRAFT_FORCE_CONTINUES"], "3")
        self.assertEqual(giant["SWECRAFT_LIMIT_WAIT_S"], "21600")
        with self.assertRaisesRegex(ValueError, "limit_wait_seconds"):
            pi.LongWakePolicy(limit_wait_seconds=-1)
        with self.assertRaisesRegex(ValueError, "context_hard_tokens"):
            pi.LongWakePolicy(context_soft_tokens=300_000, context_hard_tokens=200_000)
        with self.assertRaisesRegex(ValueError, "final_turns"):
            pi.LongWakePolicy(final_turns=0)
        kwargs: dict[str, object] = {"error_runs": "5", "thinking": "high"}
        policy = pi.policy_from_kwargs(kwargs)
        self.assertEqual(policy.error_runs, 5)
        self.assertEqual(kwargs, {"thinking": "high"})
        with self.assertRaisesRegex(ValueError, "must be an integer"):
            pi.policy_from_kwargs({"error_runs": "many"})

    def test_models_json_for_a_gateway(self) -> None:
        models = pi.models_json("anthropic", "https://gateway.example/v1/")
        self.assertEqual(
            models,
            {
                "providers": {
                    "anthropic": {
                        "baseUrl": "https://gateway.example/v1",
                        "api": "anthropic-messages",
                        "apiKey": "$ANTHROPIC_API_KEY",
                    }
                }
            },
        )
        with self.assertRaisesRegex(ValueError, "not wired"):
            pi.models_json("bedrock", "https://gateway.example")
        with self.assertRaisesRegex(ValueError, "http"):
            pi.models_json("anthropic", "gateway.example")
        plain = pi.install_command(context_window=1_000_000, policy=pi.LongWakePolicy())
        self.assertNotIn("models.json", plain)
        routed = pi.install_command(
            context_window=1_000_000,
            policy=pi.LongWakePolicy(),
            provider="anthropic",
            base_url="https://gateway.example",
        )
        self.assertIn('models.json "$HOME/.pi/agent/models.json"', routed)
        with self.assertRaisesRegex(ValueError, "no single wire API"):
            pi.models_json("openrouter", "https://gateway.example")
        for name in pi.PROVIDER_API:
            self.assertIn(name, pi.PROVIDER_KEYS, name)

    def test_long_wake_extension_reads_every_variable(self) -> None:
        source = pi.shipped_files()["long-wake.ts"].decode("utf-8")
        for name in pi.LongWakePolicy().environment():
            if name == "SWECRAFT_CONTEXT_SOFT_TOKENS":
                continue  # consumed by the agent class, not the extension
            self.assertIn(f'"{name}"', source, name)
        for name in (
            "SWECRAFT_WAKE_MODE",
            "SWECRAFT_WAKE_TURNS",
            "SWECRAFT_WAKE_SECONDS",
            "SWECRAFT_WAKE_USD",
            "SWECRAFT_WAKE_FINALIZE_AT",
            "SWECRAFT_WAKE_HARD_STOP_AT",
            "SWECRAFT_WAKE_DEADLINE_SECONDS",
            "SWECRAFT_RESULT_PATH",
        ):
            self.assertIn(name, source, name)
        self.assertIn('=== "long-wake"', source)
        self.assertNotIn("compact()", source)


def _jwt(payload: dict) -> str:
    body = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    return f"eyJhbGciOiJSUzI1NiJ9.{body}.signature"


class CodexLoginTests(unittest.TestCase):
    def snapshot(self, **claims) -> dict:
        payload = {
            "exp": 1_800_000_000,
            "https://api.openai.com/auth": {"chatgpt_account_id": "acct-1"},
        }
        payload.update(claims)
        return {
            "auth_mode": "chatgpt",
            "OPENAI_API_KEY": None,
            "tokens": {
                "id_token": "id",
                "access_token": _jwt(payload),
                "refresh_token": "refresh-1",
                "account_id": "acct-1",
            },
            "last_refresh": "2026-09-01T04:06:17Z",
        }

    def test_snapshot_becomes_pi_credential(self) -> None:
        credential = pi.codex_credential(self.snapshot())
        self.assertEqual(credential["type"], "oauth")
        self.assertEqual(credential["refresh"], "refresh-1")
        self.assertEqual(credential["expires"], 1_800_000_000_000)
        self.assertEqual(credential["accountId"], "acct-1")
        self.assertFalse(pi.codex_credential_expired(credential))
        self.assertTrue(pi.codex_credential_expired({**credential, "expires": 1}))
        auth = json.loads(pi.pi_auth_file(credential))
        self.assertEqual(list(auth), ["openai-codex"])
        self.assertEqual(auth["openai-codex"]["access"], credential["access"])

    def test_account_id_falls_back_to_the_snapshot(self) -> None:
        snapshot = self.snapshot()
        snapshot["tokens"]["access_token"] = _jwt({"exp": 1_800_000_000})
        self.assertEqual(pi.codex_credential(snapshot)["accountId"], "acct-1")

    def test_malformed_snapshots_are_refused(self) -> None:
        with self.assertRaisesRegex(ValueError, "no tokens"):
            pi.codex_credential({"OPENAI_API_KEY": "sk"})
        snapshot = self.snapshot()
        snapshot["tokens"].pop("refresh_token")
        with self.assertRaisesRegex(ValueError, "no refresh_token"):
            pi.codex_credential(snapshot)
        snapshot = self.snapshot()
        snapshot["tokens"]["access_token"] = "not-a-jwt"
        with self.assertRaisesRegex(ValueError, "not a JWT"):
            pi.codex_credential(snapshot)
        snapshot = self.snapshot()
        snapshot["tokens"]["access_token"] = _jwt({"sub": "x"})
        with self.assertRaisesRegex(ValueError, "no exp claim"):
            pi.codex_credential(snapshot)

    def test_snapshot_path_comes_from_the_declared_env_only(self) -> None:
        with patch.dict(os.environ, {"CODEX_AUTH_JSON_PATH": "/ambient/auth.json"}):
            with self.assertRaisesRegex(RuntimeError, "no Codex login"):
                pi.codex_snapshot_path({})
            self.assertEqual(
                pi.codex_snapshot_path({"CODEX_AUTH_JSON_PATH": "/declared/auth.json"}),
                "/declared/auth.json",
            )

    def test_install_copies_the_uploaded_credential_without_embedding_it(self) -> None:
        policy = pi.LongWakePolicy()
        command = pi.install_command(
            context_window=272_000, policy=policy, auth_file=pi.AUTH_UPLOAD_PATH
        )
        self.assertIn(f'cp {pi.AUTH_UPLOAD_PATH} "$HOME/.pi/agent/auth.json"', command)
        self.assertIn('chmod 600 "$HOME/.pi/agent/auth.json"', command)
        self.assertNotIn("refresh", command)
        self.assertNotIn(
            "auth.json", pi.install_command(context_window=1, policy=policy)
        )

    def test_known_context_windows(self) -> None:
        self.assertEqual(pi.context_window_for("openai-codex/gpt-5.6-sol"), 272_000)
        self.assertEqual(pi.context_window_for("anthropic/claude-opus-5"), 1_000_000)
        self.assertIsNone(pi.context_window_for("anthropic/claude-sonnet-5"))
        self.assertEqual(pi.context_window_for("google/gemini-3.8-flash"), 1_048_576)
        self.assertEqual(
            pi.context_window_for("google-vertex/gemini-3.8-flash"), 1_048_576
        )


if __name__ == "__main__":
    unittest.main()

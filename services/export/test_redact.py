"""Tests for the secret patterns: what is replaced, what is kept, and how it is counted."""

import tempfile
import unittest
from pathlib import Path

from redact import MARK, Redactor, known_secrets

SHA = "0123456789abcdef0123456789abcdef01234567"


class FormatsTest(unittest.TestCase):
    """Token formats, recognised by their shape alone."""

    cases = {
        "github": ["ghp_" + "a1B2" * 9, "gho_" + "Zz9" * 12, "ghs_" + "q" * 36, "github_pat_11ABCDEFG0" + "x" * 60],
        "huggingface": ["hf_" + "AbCdEfGhIj" * 3 + "Klmn"],
        "anthropic": ["sk-ant-api03-" + "Q9w8" * 10, "sk-ant-oat01-" + "x9_-" * 10],
        "openai": ["sk-" + "A1b2C3d4" * 4, "sk-proj-" + "Zy9" * 10],
        "aws": ["AKIAIOSFODNN7EXAMPLE", "ASIA" + "ABCDEFGHIJKLMNOP"],
        "jwt": ["eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U"],
    }

    def test_each_format_is_replaced_and_counted(self):
        for name, tokens in self.cases.items():
            for token in tokens:
                with self.subTest(name=name, token=token[:12]):
                    redactor = Redactor()
                    text, n = redactor.scrub(f"before {token} after")
                    self.assertEqual(text, f"before {MARK} after")
                    self.assertEqual(n, 1)
                    self.assertEqual(redactor.counts[name], 1)

    def test_after_an_escaped_newline(self):
        token = "ghp_" + "a1B2" * 9
        self.assertEqual(Redactor().scrub(f'{{"out": "line one\\n{token}\\tnext"}}'), (f'{{"out": "line one\\n{MARK}\\tnext"}}', 1))
        self.assertEqual(Redactor().scrub(f"?q=a%3D{token}"), (f"?q=a%3D{MARK}", 1))

    def test_not_inside_a_longer_word(self):
        for value in ("risk-assessment-for-the-long-running-jobs", "task-" + "a" * 30, "xghp_" + "a" * 36):
            with self.subTest(value=value):
                self.assertEqual(Redactor().scrub(value), (value, 0))

    def test_private_key_block(self):
        block = "-----BEGIN OPENSSH PRIVATE KEY-----\nb3BlbnNzaC1rZXktdjEAAAAA\n-----END OPENSSH PRIVATE KEY-----"
        text, n = Redactor().scrub(f"key:\n{block}\ndone")
        self.assertEqual(text, f"key:\n{MARK}\ndone")
        self.assertEqual(n, 1)

    def test_a_token_inside_a_header_is_counted_once(self):
        redactor = Redactor()
        text, n = redactor.scrub("Authorization: Bearer sk-ant-api03-" + "Q9w8" * 10)
        self.assertEqual(text, f"Authorization: Bearer {MARK}")
        self.assertEqual(n, 1)
        self.assertEqual(dict(redactor.counts), {"anthropic": 1})


class ContextTest(unittest.TestCase):
    """Credentials without a shape of their own, recognised by what surrounds them."""

    def scrub(self, value: str) -> str:
        return Redactor().scrub(value)[0]

    def test_authorization_headers(self):
        self.assertEqual(self.scrub(f'curl -H "Authorization: token {SHA}" http://x'), f'curl -H "Authorization: token {MARK}" http://x')
        self.assertEqual(self.scrub("authorization: Basic dXNlcjpwYXNz"), f"authorization: Basic {MARK}")
        self.assertEqual(self.scrub(f"{{'Authorization': 'Bearer {SHA}'}}"), f"{{'Authorization': 'Bearer {MARK}'}}")

    def test_a_url_password(self):
        url = f"http://pytest-bot:{SHA}@172.17.0.1:3500/hub/pytest.git"
        self.assertEqual(self.scrub(url), f"http://pytest-bot:{MARK}@172.17.0.1:3500/hub/pytest.git")

    def test_a_token_as_a_urls_user(self):
        url = f"http://{SHA}@172.17.0.1:3400/hub/attrs.git"
        self.assertEqual(self.scrub(url), f"http://{MARK}@172.17.0.1:3400/hub/attrs.git")
        token = "Zq3vK9xT2mLp8RwN4bYc6HdJ"
        self.assertEqual(self.scrub(f"https://{token}:x-oauth-basic@github.com/o/r.git"), f"https://{MARK}:x-oauth-basic@github.com/o/r.git")
        # A token as both user and password is two replacements.
        self.assertEqual(Redactor().scrub(f"https://{SHA}:{SHA}@forge/x"), (f"https://{MARK}:{MARK}@forge/x", 2))

    def test_an_ordinary_urls_user_is_kept(self):
        for value in (
            "git@github.com:o/r.git",
            "ssh://git@github.com/o/r.git",
            "https://deploy@example.com/x",
            "postgres://admin:pass@db:5432/app",
            "https://build-agent-2024-01-runner@ci.example.com/",
            f"https://example.com/{SHA}@v1",
        ):
            with self.subTest(value=value):
                self.assertEqual(self.scrub(value), value)

    def test_variables_keys_flags_and_queries(self):
        self.assertEqual(self.scrub(f"HUB_FORGE_TOKEN={SHA}"), f"HUB_FORGE_TOKEN={MARK}")
        self.assertEqual(self.scrub(f"export SECRET_KEY='{SHA}'"), f"export SECRET_KEY='{MARK}'")
        self.assertEqual(self.scrub(f'{{"token": "{SHA}"}}'), f'{{"token": "{MARK}"}}')
        self.assertEqual(self.scrub(f'{{"id": 3, "sha1": "{SHA}"}}'), f'{{"id": 3, "sha1": "{MARK}"}}')
        self.assertEqual(self.scrub(f"fj auth login --token {SHA}"), f"fj auth login --token {MARK}")
        self.assertEqual(self.scrub(f"GET /api/v1/repos?token={SHA}&page=2"), f"GET /api/v1/repos?token={MARK}&page=2")

    def test_a_quoted_variable_name(self):
        self.assertEqual(self.scrub(f'{{"HUB_FORGE_TOKEN": "{SHA}"}}'), f'{{"HUB_FORGE_TOKEN": "{MARK}"}}')
        self.assertEqual(
            self.scrub(f"environ({{'HUB_FORGE_TOKEN': '{SHA}', 'HOME': '/root'}})"),
            f"environ({{'HUB_FORGE_TOKEN': '{MARK}', 'HOME': '/root'}})",
        )
        self.assertEqual(self.scrub('{"MAX_TOKENS": 4096, "TOKEN_LIMIT": "TEXT_LIMIT"}'), '{"MAX_TOKENS": 4096, "TOKEN_LIMIT": "TEXT_LIMIT"}')

    def test_a_key_name_with_a_prefix(self):
        for key in ("refresh_token", "client_secret", "OPENAI_API_KEY", "db_password", "github_token"):
            with self.subTest(key=key):
                self.assertEqual(self.scrub(f'{{"{key}": "{SHA}"}}'), f'{{"{key}": "{MARK}"}}')
                self.assertEqual(self.scrub(f"{key}={SHA}"), f"{key}={MARK}")
        # A prefixed sha1 names a commit, and other keys ending in a credential word only in part are kept.
        for value in (f'{{"head_sha1": "{SHA}"}}', f'{{"commit_sha": "{SHA}"}}', '"max_tokens": 4096', '{"token_type": "bearer"}'):
            with self.subTest(value=value):
                self.assertEqual(self.scrub(value), value)

    def test_an_environment_list_entry(self):
        yaml = f"env:\n  - name: HUB_FORGE_TOKEN\n    value: {SHA}\n  - name: HOME\n    value: /root\n"
        self.assertEqual(self.scrub(yaml), f"env:\n  - name: HUB_FORGE_TOKEN\n    value: {MARK}\n  - name: HOME\n    value: /root\n")
        self.assertEqual(self.scrub(f'- name: "API_TOKEN"\n  value: "{SHA}"'), f'- name: "API_TOKEN"\n  value: "{MARK}"')
        self.assertEqual(
            self.scrub(f'[{{"name": "HUB_FORGE_TOKEN", "value": "{SHA}"}}, {{"name": "HOME", "value": "/root"}}]'),
            f'[{{"name": "HUB_FORGE_TOKEN", "value": "{MARK}"}}, {{"name": "HOME", "value": "/root"}}]',
        )
        # A reference to a stored secret holds no value.
        reference = "- name: HUB_FORGE_TOKEN\n  valueFrom:\n    secretKeyRef: {name: forge, key: token}\n"
        self.assertEqual(self.scrub(reference), reference)

    def test_a_cloudflare_account_id(self):
        account = "0123456789abcdef0123456789abcdef"
        redactor = Redactor()
        url = f'"base_url": "https://api.cloudflare.com/client/v4/accounts/{account}/ai/v1"'
        self.assertEqual(redactor.scrub(url), (f'"base_url": "https://api.cloudflare.com/client/v4/accounts/{MARK}/ai/v1"', 1))
        self.assertEqual(redactor.counts["cloudflare-account"], 1)
        # The same 32 hex digits elsewhere are an identifier like any other.
        for value in (f"md5 {account}", f"https://example.com/accounts/{account}/x", f"api.cloudflare.com/client/v4/accounts/{account}0"):
            with self.subTest(value=value):
                self.assertEqual(Redactor().scrub(value), (value, 0))

    def test_a_signed_url(self):
        url = "https://objects.example.com/x.whl?sv=2025-01-05&se=2026-08-05T10%3A00Z&sr=b&sp=r&sig=vFSveABrvbDoqn%2Fq5oZs%3D&rscd=a"
        self.assertEqual(
            self.scrub(url),
            f"https://objects.example.com/x.whl?sv=2025-01-05&se=2026-08-05T10%3A00Z&sr=b&sp=r&sig={MARK}&rscd=a",
        )
        aws = "https://b.s3.amazonaws.com/k?X-Amz-Credential=AKIDEXAMPLE%2F20260805&X-Amz-Signature=0123456789abcdef0123"
        self.assertEqual(self.scrub(aws), f"https://b.s3.amazonaws.com/k?X-Amz-Credential={MARK}&X-Amz-Signature={MARK}")

    def test_variable_references_are_kept(self):
        for value in (
            'curl -H "Authorization: token $HUB_FORGE_TOKEN" "$HUB_FORGE_URL/x"',
            'curl -H \\"Authorization: token \\$HUB_FORGE_TOKEN\\"',
            "Authorization: Bearer ${TOKEN}",
            "http://bot:${HUB_FORGE_TOKEN}@forge/hub/x.git",
            "GH_TOKEN: ${{ secrets.GITHUB_TOKEN }}",
            "fj auth login --token $TOKEN",
        ):
            with self.subTest(value=value):
                self.assertEqual(self.scrub(value), value)

    def test_identifiers_counts_and_short_values_are_kept(self):
        for value in (
            "MAX_TOKENS=100000000",
            "OUTPUT_TOKEN_LIMIT=TEXT_LIMIT",
            "token = Token.Name.Builtin",
            "https://user:pass@example.com/path",
            'URL = "http://user:password@www.domain.tld"',
            "Authorization: Bearer\nnext line",
        ):
            with self.subTest(value=value):
                self.assertEqual(self.scrub(value), value)


class KnownSecretsTest(unittest.TestCase):
    """Values the operator lists, replaced wherever they occur exactly."""

    # Synthetic values; nothing here was ever a credential.
    FORGE = "5e4d3c2b1a0f9e8d7c6b5a4f3e2d1c0b9a8f7e6d"
    PASSWORD = "correct-horse-battery"

    def test_a_bare_value(self):
        redactor = Redactor([self.FORGE, self.PASSWORD])
        output = f"$ printenv HUB_FORGE_TOKEN\n{self.FORGE}\n$ echo $DB\n{self.PASSWORD}\n"
        self.assertEqual(redactor.scrub(output), (f"$ printenv HUB_FORGE_TOKEN\n{MARK}\n$ echo $DB\n{MARK}\n", 2))
        self.assertEqual(dict(redactor.counts), {"known": 2})

    def test_counted_once_where_a_pattern_would_also_match(self):
        redactor = Redactor([self.FORGE])
        self.assertEqual(redactor.scrub(f"HUB_FORGE_TOKEN={self.FORGE}"), (f"HUB_FORGE_TOKEN={MARK}", 1))
        self.assertEqual(dict(redactor.counts), {"known": 1})

    def test_the_longest_value_first(self):
        short, long = "abcdefgh12345678", "abcdefgh12345678XYZW"
        self.assertEqual(Redactor([short, long]).scrub(f"{long} {short}"), (f"{MARK} {MARK}", 2))

    def test_other_text_and_hex_are_kept(self):
        value = f"commit {SHA}\nno secret here"
        self.assertEqual(Redactor([self.FORGE]).scrub(value), (value, 0))
        self.assertEqual(Redactor([]).scrub(value), (value, 0))

    def test_the_file(self):
        with tempfile.TemporaryDirectory() as scratch:
            path = Path(scratch) / "known"
            path.write_text(f"{self.FORGE}\n\n  {self.PASSWORD}  \n")
            self.assertEqual(known_secrets(path), [self.FORGE, self.PASSWORD])
            path.write_text(f"{self.FORGE}\nmain\n")
            with self.assertRaises(SystemExit) as refused:
                known_secrets(path)
            # The error names the line, never the value.
            self.assertIn("line 2", str(refused.exception))
            self.assertNotIn("main", str(refused.exception).replace(str(path), ""))


class BinaryTest(unittest.TestCase):
    """A binary content is searched for what a text would have redacted, and nothing is counted."""

    def test_a_known_value_as_exact_bytes(self):
        redactor = Redactor(["k9-Synthetic-Value-42"])
        self.assertEqual(redactor.secret_in(b"\x89PNG\0\0k9-Synthetic-Value-42\xff"), "known")
        self.assertIsNone(redactor.secret_in(b"\x89PNG\0\0k9-Synthetic-Value-4\xff"))
        self.assertEqual(redactor.counts, {})

    def test_a_pattern_over_the_bytes_read_as_latin_1(self):
        redactor = Redactor()
        self.assertEqual(redactor.secret_in(b"\0\xfe" + FormatsTest.cases["github"][0].encode() + b"\0"), "github")
        self.assertEqual(redactor.secret_in(b"\0\xe9 password = 'k9-synthetic-value'\n\0"), "key")
        self.assertIsNone(redactor.secret_in(bytes(range(256)) * 4))
        self.assertEqual(redactor.counts, {})


class HexTest(unittest.TestCase):
    def test_shas_checksums_and_uuids_survive(self):
        value = (
            f"commit {SHA}\nmerge base {SHA[:12]}\n"
            "sha256 e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855\n"
            '<a href="/hub/x/commit/' + SHA + '">fix</a>\n'
            "uuid 123e4567-e89b-12d3-a456-426614174000\n"
            '"object_format_name":"sha1"'
        )
        self.assertEqual(Redactor().scrub(value), (value, 0))


class TextTest(unittest.TestCase):
    def test_none_and_counts(self):
        redactor = Redactor()
        self.assertIsNone(redactor.text(None, 100))
        text = redactor.text(f"one ghp_{'a' * 36} two HUB_TOKEN={SHA}", 100)
        self.assertEqual(text, {"text": f"one {MARK} two HUB_TOKEN={MARK}", "redacted": 2, "truncated": False})
        self.assertEqual(sum(redactor.counts.values()), 2)

    def test_cut_after_scrubbing(self):
        # Scrubbed before the cut, so a token across the limit never shows in part.
        text = Redactor().text("x" * 7 + " ghp_" + "b" * 36, 10)
        self.assertTrue(text["truncated"])
        self.assertTrue(text["text"].startswith("x" * 7 + " " + MARK[:2]))
        self.assertNotIn("ghp_", text["text"])
        self.assertIn("characters cut", text["text"])

    def test_whole_without_a_limit(self):
        # The exporter names no limit, so a long text is kept whole.
        text = Redactor().text("y" * 300_000)
        self.assertEqual((len(text["text"]), text["truncated"]), (300_000, False))

    def test_trimmed_by_the_recorder(self):
        self.assertEqual(Redactor().text("short", 100, trimmed=True), {"text": "short", "redacted": 0, "truncated": True})

    def test_line(self):
        redactor = Redactor()
        self.assertIsNone(redactor.line(None))
        self.assertIsNone(redactor.line(""))
        self.assertEqual(redactor.line(f"title with ghp_{'c' * 36}"), f"title with {MARK}")


if __name__ == "__main__":
    unittest.main()

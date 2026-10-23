"""
Secret patterns, replaced in every text the exporter writes.

Two kinds of pattern. Token formats are recognised by their shape alone:
GitHub, Hugging Face, Anthropic, OpenAI and AWS keys, JWTs and private key
blocks. Credentials without a shape of their own (a forge token is 40 hex
digits, as a commit SHA is) are recognised by their context: an
Authorization header, the password in a URL or a user there that is shaped
like a token, a variable, key, flag or environment list entry named as a
token, secret or password. Plain hex identifiers are never matched on their
own, so commit SHAs and checksums survive; the one hex identifier replaced
is the account id in a Cloudflare API URL, which ties a run to an account.

A value that is a shell variable (`$TOKEN`, `${TOKEN}`) names a secret
without holding one and is kept.

A secret printed with no context at all (the output of `printenv
HUB_FORGE_TOKEN`) can only be recognised by its value. The operator may list
known values in a file, one per line (`known_secrets`); every exact
occurrence is then replaced too, and the values are never written anywhere.
"""

import re
from collections import Counter
from collections.abc import Iterable
from pathlib import Path

MARK = "[redacted]"
# A known secret shorter than this is refused: a token or password is
# longer, and a short value would be replaced inside ordinary words.
KNOWN_MIN_LENGTH = 8

# A contextual value: long, and mixing letters with digits, as random
# credentials do; an identifier such as TEXT_LIMIT or a count such as 4096 is
# left alone. It ends at punctuation that cannot be part of a token, so a
# value already replaced is not matched again.
def _value(char: str) -> str:
    return rf"(?={char}*\d)(?={char}*[A-Za-z]){char}{{16,}}"


_VALUE = _value(r"[^\s\"'`$\\<>,;&()\[\]{}]")
# In a URL's user information, the password ends at the `@` or a `/`.
_URL_VALUE = _value(r"[^\s\"'`$\\<>@/\[\]{}]")
# A URL's user that is a token: long hex, or a long URL-safe or base64 string
# mixing digits with upper and lower case letters. An ordinary user name
# (`git`, `deploy`, `pytest-bot`) is neither.
_URL_TOKEN_USER = (
    r"(?:[0-9a-fA-F]{32,}"
    r"|(?=[A-Za-z0-9_+=~.-]*\d)(?=[A-Za-z0-9_+=~.-]*[a-z])(?=[A-Za-z0-9_+=~.-]*[A-Z])[A-Za-z0-9_+=~.-]{20,})"
)

# Where a token format may start: not inside a longer word, but right after
# an escaped newline, tab or carriage return, as JSON quoted in an output
# shows them (`\nghp_…`), or after a percent-encoded character (`%3Dghp_…`).
_START = r"(?:(?<![A-Za-z0-9_-])|(?<=\\[ntr])|(?<=%[0-9A-Fa-f]{2}))"

# (name, pattern, group replaced). Group 0 is the whole match. Earlier
# patterns run first, so a token inside a header is counted once, by format.
PATTERNS: tuple[tuple[str, re.Pattern[str], int], ...] = (
    (
        "private-key",
        re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?(?:-----END [A-Z ]*PRIVATE KEY-----|\Z)"),
        0,
    ),
    ("github", re.compile(_START + r"(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{20,})"), 0),
    ("huggingface", re.compile(_START + r"hf_[A-Za-z0-9]{30,}"), 0),
    ("anthropic", re.compile(_START + r"sk-ant-[A-Za-z0-9_-]{20,}"), 0),
    ("openai", re.compile(_START + r"sk-(?!ant-)[A-Za-z0-9_-]{20,}"), 0),
    ("aws", re.compile(_START + r"(?:AKIA|ASIA)[0-9A-Z]{16}(?![A-Za-z0-9])"), 0),
    ("jwt", re.compile(_START + r"eyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"), 0),
    (
        "authorization",
        re.compile(
            r"(?i)(\bauthorization[\"']?[ \t]*[:=][ \t]*[\"']?[ \t]*(?:bearer|token|basic)[ \t]+)"
            r"([^\s\"'$\\<>{}\[][^\s\"'<>\\]*)"
        ),
        2,
    ),
    ("bearer", re.compile(r"(?i)(\bbearer[ \t]+)([A-Za-z0-9._~+/-]{20,}=*)"), 2),
    ("url-password", re.compile(r"(?i)(\b[a-z][a-z0-9+.-]*://[^\s/:@\"'<>]+:)(" + _URL_VALUE + ")(?=@)"), 2),
    # A token as the user, with or without a password after it: `http://<token>@host/`.
    ("url-user", re.compile(r"(\b[A-Za-z][A-Za-z0-9+.-]*://)(" + _URL_TOKEN_USER + r")(?=(?::[^\s/@\"'<>]*)?@)"), 2),
    ("cloudflare-account", re.compile(r"(?i)(api\.cloudflare\.com/client/v4/accounts/)([0-9a-f]{32})(?![0-9a-f])"), 2),
    (
        # Tokens in a query string, including a signed URL's signature.
        "query-token",
        re.compile(
            r"(?i)([?&](?:access_token|private_token|token|api_key|apikey|jwt|sig|signature"
            r"|x-amz-signature|x-amz-security-token|x-amz-credential|x-goog-signature|x-goog-credential)=)"
            r"([^&\s\"'#$<>\[]{8,})"
        ),
        2,
    ),
    (
        "variable",
        re.compile(
            # The name may be quoted, as a JSON key or a Python dict key is.
            r"\b([A-Z0-9_]*(?:TOKEN|SECRET|PASSWORD|PASSWD|API_KEY|APIKEY|ACCESS_KEY|PRIVATE_KEY)"
            r"[A-Z0-9_]*[\"']?[ \t]*[=:][ \t]*[\"']?)(" + _VALUE + ")"
        ),
        2,
    ),
    (
        # An entry of an environment list, as Kubernetes and CI files write
        # one: a name like a secret variable's, then its value on the next
        # line (YAML) or after a comma (JSON).
        "env-entry",
        re.compile(
            r"(?i)(\bname[\"']?[ \t]*:[ \t]*[\"']?"
            r"[A-Z0-9_]*(?:TOKEN|SECRET|PASSWORD|PASSWD|API_KEY|APIKEY|ACCESS_KEY|PRIVATE_KEY)[A-Z0-9_]*"
            r"[\"']?[ \t]*,?[ \t]*\r?\n?[ \t]*[\"']?value[\"']?[ \t]*:[ \t]*[\"']?)(" + _VALUE + ")"
        ),
        2,
    ),
    (
        # A key named as a credential, with or without a prefix (`refresh_token`,
        # `client_secret`, `OPENAI_API_KEY`). `sha1` counts only as itself, the
        # field the forge's token API answers with; a prefixed `head_sha1`
        # names a commit.
        "key",
        re.compile(
            r"(?i)([\"']?\b(?:\w*?(?:token|secret|secret_?key|api_?key|apikey|password|passwd)|sha1)"
            r"[\"']?\s*[:=]\s*[\"']?)(" + _VALUE + ")"
        ),
        2,
    ),
    (
        "flag",
        re.compile(r"(--(?:token|password|api-key|access-token|secret)(?:=|\s+)[\"']?)(" + _VALUE + ")"),
        2,
    ),
)


def known_secrets(path: Path) -> list[str]:
    """The values in an operator's file of known secrets: one per line, blank lines skipped.

    A value shorter than KNOWN_MIN_LENGTH is refused by its line number, so
    the error shows no value.
    """

    values = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        value = line.strip()
        if not value:
            continue
        if len(value) < KNOWN_MIN_LENGTH:
            raise SystemExit(f"{path}: line {number} holds a value shorter than {KNOWN_MIN_LENGTH} characters")
        values.append(value)
    return values


class Redactor:
    """Replaces secrets and counts them, per pattern, across everything it sees."""

    def __init__(self, known: Iterable[str] = ()) -> None:
        self.counts: Counter[str] = Counter()
        # Longest first, so a known value that holds another is replaced whole.
        values = sorted({value for value in known if value}, key=len, reverse=True)
        self._known = re.compile("|".join(map(re.escape, values))) if values else None
        # The same values as bytes, for a binary content, which is searched and never rewritten.
        self._known_bytes = re.compile(b"|".join(re.escape(value.encode()) for value in values)) if values else None

    def scrub(self, value: str) -> tuple[str, int]:
        """(the text with every secret replaced, how many were)."""

        total = 0
        # Known values first, so one inside a header is counted once, as known.
        if self._known is not None:
            value, n = self._known.subn(MARK, value)
            if n:
                self.counts["known"] += n
                total += n
        for name, pattern, group in PATTERNS:

            def replace(match: re.Match[str]) -> str:
                if group == 0:
                    return MARK
                start, end = match.span(group)
                return match.string[match.start() : start] + MARK + match.string[end : match.end()]

            value, n = pattern.subn(replace, value)
            if n:
                self.counts[name] += n
                total += n
        return value, total

    def secret_in(self, content: bytes) -> str | None:
        """What a binary content holds that a text would have redacted: `known`, a pattern's name, or None.

        A binary content cannot be rewritten, so it is only searched: for the
        known values as exact bytes, then for each pattern over the content
        read as latin-1, one character per byte. Nothing is counted.
        """

        if self._known_bytes is not None and self._known_bytes.search(content):
            return "known"
        text = content.decode("latin-1")
        return next((name for name, pattern, _ in PATTERNS if pattern.search(text)), None)

    def line(self, value: str | None) -> str | None:
        """A short field (a title, a summary): scrubbed, or None when empty."""

        if value is None:
            return None
        text, _ = self.scrub(value)
        return text or None

    def text(self, value: str | None, limit: int | None = None, *, trimmed: bool = False) -> dict | None:
        """The protocol's `Text`: scrubbed, and kept whole unless a caller names a `limit`.

        `trimmed` says the value was already shortened before it reached the
        exporter; the text is then marked truncated.
        """

        if value is None:
            return None
        text, redacted = self.scrub(value)
        truncated = trimmed
        if limit is not None and len(text) > limit:
            text = text[:limit] + f"\n[… {len(text) - limit} characters cut]"
            truncated = True
        return {"text": text, "redacted": redacted, "truncated": truncated}

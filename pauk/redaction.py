from __future__ import annotations

import os
import re
from collections.abc import Iterable, Mapping
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

REDACTED = "[REDACTED]"

SENSITIVE_ENV_NAMES = (
    "OPENALEX_API_KEY",
    "OPENROUTER_API_KEY",
    "GITHUB_TOKEN",
    "OPENREVIEW_PASSWORD",
    "NEO4J_PASSWORD",
)

_SENSITIVE_QUERY_NAMES = frozenset({
    "api_key",
    "apikey",
    "access_token",
    "auth_token",
    "password",
    "secret",
    "signature",
    "token",
    "x-amz-credential",
    "x-amz-security-token",
    "x-amz-signature",
})
_URL_RE = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)
_QUERY_VALUE_RE = re.compile(
    r"(?i)\b(api[_-]?key|access[_-]?token|auth[_-]?token|password|secret|signature|token|"
    r"x-amz-credential|x-amz-security-token|x-amz-signature)(\s*=\s*)([^&\s,;]+)"
)
_BEARER_RE = re.compile(r"(?i)\b(Bearer\s+)[A-Za-z0-9._~+/=-]+")


def configured_secret_values(environ: Mapping[str, str] | None = None) -> tuple[str, ...]:
    """Return configured credentials long enough for safe literal replacement."""
    source = os.environ if environ is None else environ
    values = {source.get(name, "") for name in SENSITIVE_ENV_NAMES}
    return tuple(sorted((value for value in values if len(value) >= 4), key=len, reverse=True))


def _sensitive_query_name(name: str) -> bool:
    normalized = name.casefold()
    return normalized in _SENSITIVE_QUERY_NAMES or normalized.endswith(
        ("_api_key", "_password", "_secret", "_signature", "_token")
    )


def redact_url(url: str) -> str:
    """Remove credentials and sensitive query values from a URL used for diagnostics."""
    try:
        parsed = urlsplit(url)
        netloc = parsed.netloc.rsplit("@", 1)[-1]
        query = urlencode([
            (name, REDACTED if _sensitive_query_name(name) else value)
            for name, value in parse_qsl(parsed.query, keep_blank_values=True)
        ], doseq=True)
        # Fragments are never sent in HTTP requests and can themselves carry secrets.
        return urlunsplit((parsed.scheme, netloc, parsed.path, query, ""))
    except (TypeError, ValueError):
        return "<invalid URL>"


def redact_text(value: object, secret_values: Iterable[str] | None = None) -> str:
    """Redact known credentials and common HTTP credential forms from diagnostic text."""
    text = str(value)
    if secret_values is None:
        secret_values = configured_secret_values()
    for secret in sorted({secret for secret in secret_values if len(secret) >= 4}, key=len, reverse=True):
        text = text.replace(secret, REDACTED)
    text = _URL_RE.sub(lambda match: redact_url(match.group(0)), text)
    text = _QUERY_VALUE_RE.sub(lambda match: f"{match.group(1)}{match.group(2)}{REDACTED}", text)
    return _BEARER_RE.sub(lambda match: f"{match.group(1)}{REDACTED}", text)

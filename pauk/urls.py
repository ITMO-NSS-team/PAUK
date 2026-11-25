"""URL comparison keys shared by the pipeline and the graph layer."""

from __future__ import annotations

from collections.abc import Iterable
from urllib.parse import urlparse, urlsplit, urlunparse

APPROVED_REPO_HOSTS = frozenset(
    {
        "github.com",
        "gitlab.com",
        "bitbucket.org",
        "codeberg.org",
        "sourceforge.net",
        "huggingface.co",
        "zenodo.org",
        "doi.org",
    }
)


def _split_approved_url(value: str) -> tuple[str, str] | None:
    """Validate an approved public repository URL and return (canonical_host, path)."""
    if (
        not value
        or len(value) > 2000
        or any(ord(c) < 33 or 0x7F <= ord(c) <= 0x9F for c in value)
        or any(ch in value for ch in ("@", "?", "#"))
    ):
        return None
    try:
        parsed = urlsplit(value)
    except ValueError:
        return None
    if (
        parsed.scheme not in ("https", "http")
        or parsed.username is not None
        or parsed.password is not None
        or not parsed.netloc.isascii()
    ):
        return None
    default_port = ":443" if parsed.scheme == "https" else ":80"
    canonical_host = parsed.netloc.lower().removesuffix(default_port).removeprefix("www.")
    if canonical_host not in APPROVED_REPO_HOSTS:
        return None
    return canonical_host, parsed.path


def safe_url(value: object) -> str | None:
    """Withhold local, credential-bearing, signed or malformed URLs without rewriting them."""
    if not isinstance(value, str):
        return None
    return value if _split_approved_url(value) is not None else None


def normalize_repo_url_query(value: object) -> str | None:
    """Normalize an approved repository URL lookup query without trusting unsafe URLs."""
    if not isinstance(value, str):
        return None
    parts = _split_approved_url(value.strip(" \t\n\r"))
    if parts is None:
        return None
    canonical_host, raw_path = parts
    path = raw_path.rstrip("/").lower().removesuffix(".git").rstrip("/")
    if not path:
        return None
    return f"https://{canonical_host}{path}"


def normalize_repo_url(url: str) -> str:
    """Comparison key for repository URLs.

    GitHub treats owner/name case-insensitively and the canonical html_url
    returned by its API may differ in case from the URL found in an abstract;
    HTTP/HTTPS, a "www." host prefix, a trailing slash or a ".git" suffix are also
    cosmetic. Without this normalization the same repository would split
    into a Repository node and a LinkCandidate node.
    """
    normalized = url.strip().rstrip("/").lower().removesuffix(".git")
    parsed = urlparse(normalized)
    if parsed.scheme in {"http", "https"}:
        parsed = parsed._replace(scheme="https")
    if parsed.netloc == "www.github.com":
        parsed = parsed._replace(netloc="github.com")
    return urlunparse(parsed)


def deduplicate_repo_urls(urls: Iterable[str]) -> list[str]:
    """Keep the first spelling of each repository URL identity."""
    unique: dict[str, str] = {}
    for url in urls:
        unique.setdefault(normalize_repo_url(url), url)
    return list(unique.values())

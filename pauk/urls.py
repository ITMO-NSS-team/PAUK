"""URL comparison keys shared by the pipeline and the graph layer."""

from __future__ import annotations

from urllib.parse import urlparse, urlunparse


def normalize_repo_url(url: str) -> str:
    """Comparison key for repository URLs.

    GitHub treats owner/name case-insensitively and the canonical html_url
    returned by its API may differ in case from the URL found in an abstract;
    a "www." host prefix, a trailing slash or a ".git" suffix are also
    cosmetic. Without this normalization the same repository would split
    into a Repository node and a LinkCandidate node.
    """
    normalized = url.strip().rstrip("/").lower().removesuffix(".git")
    parsed = urlparse(normalized)
    if parsed.netloc == "www.github.com":
        normalized = urlunparse(parsed._replace(netloc="github.com"))
    return normalized


GITHUB_HOSTS = {"github.com", "www.github.com"}


def github_owner_name(url: str | None) -> tuple[str, str] | None:
    """(owner, name) for a github.com URL of exactly two path segments.

    Anything else — a gist, a subdirectory link, another host — is not a
    repository the GitHub API can be asked about.
    """
    parsed = urlparse((url or "").rstrip("/"))
    parts = parsed.path.strip("/").split("/")
    if parsed.netloc.lower() not in GITHUB_HOSTS or len(parts) != 2:
        return None
    return parts[0], parts[1]

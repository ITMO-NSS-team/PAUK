"""Repository READMEs: fetched once from GitHub, cleaned before embedding.

The graph keeps only `has_readme`; the text is downloaded here and cached
under `<data_dir>/rag/readmes`, so building an index - which an ablation
does many times - never goes to the network. A README as served is mostly
badges, install commands and code; `clean_readme` keeps the prose that
says what the project is.
"""

from __future__ import annotations

import logging
import re
import shutil
from pathlib import Path

from pauk.settings import Settings
from pauk.sources.base import HttpRequestError
from pauk.sources.github import GitHubClient
from pauk.urls import github_owner_name

logger = logging.getLogger(__name__)

_CODE_BLOCK = re.compile(r"```.*?```|~~~.*?~~~", re.DOTALL)
_INDENTED_CODE = re.compile(r"(?m)^(?: {4}|\t).*$")
_HTML_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
_IMAGE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_LINKED_IMAGE = re.compile(r"\[\s*!\[[^\]]*\]\([^)]*\)\s*\]\([^)]*\)")
_LINK = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_HTML_TAG = re.compile(r"<[^>]+>")
_URL = re.compile(r"https?://\S+")
_TABLE_RULE = re.compile(r"(?m)^\s*\|?\s*:?-{3,}.*$")
_SHELL_LINE = re.compile(r"(?m)^\s*(?:\$|>>>|pip |conda |npm |git clone|docker |python -m|cd ).*$")
_MARKUP = re.compile(r"[#*_`>|=]+")
_SPACES = re.compile(r"[ \t]+")
_BLANK_LINES = re.compile(r"\n\s*\n+")


def clean_readme(text: str) -> str:
    """The prose of a README: no code, badges, images, HTML, links or shell lines."""
    text = _HTML_COMMENT.sub(" ", text or "")
    text = _CODE_BLOCK.sub(" ", text)
    text = _LINKED_IMAGE.sub(" ", text)
    text = _IMAGE.sub(" ", text)
    text = _LINK.sub(r"\1", text)  # a link's words stay, its target goes
    text = _HTML_TAG.sub(" ", text)
    text = _URL.sub(" ", text)
    text = _INDENTED_CODE.sub(" ", text)
    text = _TABLE_RULE.sub(" ", text)
    text = _SHELL_LINE.sub(" ", text)
    text = _MARKUP.sub(" ", text)
    text = _SPACES.sub(" ", text)
    return _BLANK_LINES.sub("\n", text).strip()


def readme_dir(config: Settings) -> Path:
    return config.data_dir / "rag" / "readmes"


def readme_path(config: Settings, repository_id: str) -> Path:
    return readme_dir(config) / f"{repository_id}.md"


def load_readme(config: Settings, repository_id: str) -> str | None:
    path = readme_path(config, repository_id)
    return path.read_text(encoding="utf-8", errors="replace") if path.is_file() else None


def fetch(config: Settings, repositories: list[dict], *, force: bool = False,
          seed_dirs: tuple[Path, ...] = ()) -> dict[str, int]:
    """Put a README for each repository (`id`, `url`) into the cache.

    `seed_dirs` are earlier caches (the prototype's `data/search/readmes`)
    copied in before anything is downloaded.
    """
    target = readme_dir(config)
    target.mkdir(parents=True, exist_ok=True)
    client = GitHubClient(config.request_timeout, config.github_token)
    stats = {"cached": 0, "seeded": 0, "fetched": 0, "missing": 0, "failed": 0}
    for row in repositories:
        path = readme_path(config, row["id"])
        if path.exists() and not force:
            stats["cached"] += 1
            continue
        seed = next((d / path.name for d in seed_dirs if (d / path.name).is_file()), None)
        if seed is not None and not force:
            shutil.copyfile(seed, path)
            stats["seeded"] += 1
            continue
        parsed = github_owner_name(row.get("url"))
        if parsed is None:
            stats["failed"] += 1
            continue
        try:
            text = client.get_readme(*parsed)
        except HttpRequestError as exc:
            logger.warning("README of %s: %s", row.get("url"), exc)
            stats["failed"] += 1
            continue
        if text is None:
            stats["missing"] += 1
            continue
        tmp = path.with_suffix(".tmp")
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(path)
        stats["fetched"] += 1
    return stats

"""Latest fetched README per canonical GitHub URL, shared by enrichment and corpus builds."""

from hashlib import sha256
from pathlib import Path

from pydantic import ValidationError

from pauk.sources.github import Readme
from pauk.storage.atomic import AtomicWriter
from pauk.urls import normalize_repo_url


def cache_path(root: Path, url: str) -> Path:
    key = sha256(normalize_repo_url(url).encode()).hexdigest()
    return root / f"{key}.json"


def store_readme(root: Path, url: str, readme: Readme) -> None:
    with AtomicWriter(cache_path(root, url)) as fh:
        fh.write(readme.model_dump_json())


def cached_readme(root: Path, url: str) -> Readme | None:
    try:
        return Readme.model_validate_json(cache_path(root, url).read_bytes())
    except (FileNotFoundError, ValidationError):
        return None

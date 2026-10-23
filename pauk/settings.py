from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

from pauk import institution as institutions
from pauk.person_resolution_defaults import MERGE_FROM, SEPARATE_BELOW

ROOT_DIR = Path(__file__).resolve().parent.parent
load_dotenv(ROOT_DIR / ".env")


def _path_setting(name: str, default: Path) -> Path:
    configured = os.getenv(name)
    if not configured:
        return default
    path = Path(configured)
    return path if path.is_absolute() else ROOT_DIR / path


@dataclass(frozen=True)
class Settings:
    # Which university this deployment collects (pauk/institution.py).
    institution_key: str = os.getenv("PAUK_INSTITUTION") or institutions.ITMO.key
    data_dir: Path = Path(os.getenv("PAUK_DATA_DIR") or ROOT_DIR / "data")
    openalex_api_key: str = os.getenv("OPENALEX_API_KEY", "")
    github_token: str = os.getenv("GITHUB_TOKEN", "")
    # A registered ORCID public API client: 100,000 reads a day instead of
    # 25,000 per IP address.
    orcid_client_id: str = os.getenv("ORCID_CLIENT_ID", "")
    orcid_client_secret: str = os.getenv("ORCID_CLIENT_SECRET", "")
    openrouter_api_key: str = os.getenv("OPENROUTER_API_KEY", "")
    llm_model: str = os.getenv("PAUK_LLM_MODEL", "anthropic/claude-haiku-5.5")
    person_resolution_enabled: bool = os.getenv(
        "PAUK_PERSON_RESOLUTION_ENABLED", "true"
    ).lower() in ("1", "true", "yes")
    person_resolution_model: str = os.getenv(
        "PAUK_PERSON_RESOLUTION_MODEL", "qwen/qwen3-next-80b-a3b-instruct"
    )
    person_resolution_logreg_model_path: Path = _path_setting(
        "PAUK_PERSON_RESOLUTION_LOGREG_MODEL_PATH",
        ROOT_DIR / "pauk" / "graph" / "artifacts" / "person_resolution_logreg.pkl",
    )
    person_resolution_concurrency: int = int(
        os.getenv("PAUK_PERSON_RESOLUTION_CONCURRENCY", "8")
    )
    # Above this many pairs the model is not asked at all and they stay
    # unmerged: East Asian names (Kim J., Lee S.) share surname and initial so
    # widely that POSTECH reached 238k pairs (~12 h, ~$60, 89% "review").
    # A Western university of the same size has 1-8k.
    person_resolution_max_pairs: int = int(os.getenv("PAUK_PERSON_RESOLUTION_MAX_PAIRS", "20000"))
    author_names_concurrency: int = int(os.getenv("PAUK_AUTHOR_NAMES_CONCURRENCY", "8"))
    # Threads for the persons and code_links stages; the shared per-host rate
    # limiter (pauk/sources/base.py) keeps them inside each API's limits.
    persons_concurrency: int = int(os.getenv("PAUK_PERSONS_CONCURRENCY", "8"))
    code_links_concurrency: int = int(os.getenv("PAUK_CODE_LINKS_CONCURRENCY", "8"))
    link_relevance_concurrency: int = int(os.getenv("PAUK_LINK_RELEVANCE_CONCURRENCY", "8"))
    # Crossref only backfilled ORCIDs by surname: 0.4% of people on UFJF, at
    # the cost of a namesake's ORCID now and then. Off unless asked for.
    crossref_enabled: bool = os.getenv("PAUK_CROSSREF_ENABLED", "").lower() in ("1", "true", "yes")
    person_resolution_separate_below: float = float(
        os.getenv("PAUK_PERSON_RESOLUTION_SEPARATE_BELOW", str(SEPARATE_BELOW))
    )
    person_resolution_merge_from: float = float(
        os.getenv("PAUK_PERSON_RESOLUTION_MERGE_FROM", str(MERGE_FROM))
    )
    openrouter_proxy_url: str = os.getenv("OPENROUTER_PROXY_URL", "")
    neo4j_uri: str = os.getenv("NEO4J_URI", "bolt://localhost:7687")
    neo4j_user: str = os.getenv("NEO4J_USER", "neo4j")
    neo4j_password: str = os.getenv("NEO4J_PASSWORD", "")
    mongo_uri: str = os.getenv("MONGO_URI", "mongodb://localhost:27017")
    # Empty means the university's own database (Institution.mongo_db):
    # entities are global inside one, so two universities sharing it would
    # merge each other's people.
    mongo_db: str = os.getenv("MONGO_DB", "")
    request_timeout: int = int(os.getenv("PAUK_REQUEST_TIMEOUT", "30"))
    # The admin panel's session cookie. Off by default so the panel works
    # over plain HTTP inside the VPN; turn it on wherever it is served
    # over TLS, and the browser stops sending the cookie unencrypted.
    admin_secure_cookie: bool = os.getenv("PAUK_ADMIN_SECURE_COOKIE", "").lower() in ("1", "true", "yes")
    pdf_crawler_url: str = os.getenv("PAUK_PDF_CRAWLER_URL", "")
    # Official staff records (personal data, never committed). None means
    # <static_dir>/<institution.staff_catalog>.
    staff_catalog_file: str | None = os.getenv("PAUK_STAFF_CATALOG_FILE") or os.getenv("PAUK_RUSSIAN_NAMES_FILE") or None

    def __post_init__(self) -> None:
        if not self.mongo_db:
            object.__setattr__(self, "mongo_db", self.institution.mongo_db)

    @property
    def institution(self) -> institutions.Institution:
        return institutions.get(self.institution_key)

    @property
    def static_dir(self) -> Path:
        # Per university: the department catalogue and the GitHub organizations
        # list describe one university each.
        return self.data_dir / "static" / self.institution.key

    @property
    def raw_dir(self) -> Path:
        return self.data_dir / "raw"

    @property
    def prepared_dir(self) -> Path:
        return self.data_dir / "prepared"

    # Snapshots, site data and audit journals describe one university's graph,
    # so each gets its own folder; PDFs are keyed by work and are shared.
    @property
    def cache_dir(self) -> Path:
        return self.data_dir / "cache" / self.institution.key

    @property
    def pdf_dir(self) -> Path:
        return self.data_dir / "pdf"

    @property
    def audit_dir(self) -> Path:
        return self.data_dir / "audit" / self.institution.key

    @property
    def gui_dir(self) -> Path:
        return self.data_dir / "gui" / self.institution.key


settings = Settings()

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

from pauk.person_resolution_defaults import MERGE_FROM, SEPARATE_BELOW

ROOT_DIR = Path(__file__).resolve().parent.parent
load_dotenv(ROOT_DIR / ".env")

OPENALEX_RPS = 30
OPENALEX_PUBLIC_RPS = 10
ORCID_RPS = 12
CROSSREF_RPS = 10
CROSSREF_CONCURRENCY = 3
CROSSREF_PUBLIC_RPS = 5
CROSSREF_PUBLIC_CONCURRENCY = 1


def _path_setting(name: str, default: Path) -> Path:
    configured = os.getenv(name)
    if not configured:
        return default
    path = Path(configured)
    return path if path.is_absolute() else ROOT_DIR / path


@dataclass(frozen=True)
class Settings:
    data_dir: Path = Path(os.getenv("PAUK_DATA_DIR") or ROOT_DIR / "data")
    openalex_api_key: str = os.getenv("OPENALEX_API_KEY", "")
    openalex_rps: float = float(os.getenv("PAUK_OPENALEX_RPS") or OPENALEX_RPS)
    orcid_rps: float = float(os.getenv("PAUK_ORCID_RPS") or ORCID_RPS)
    crossref_rps: float = float(os.getenv("PAUK_CROSSREF_RPS") or CROSSREF_RPS)
    crossref_concurrency: int = int(os.getenv("PAUK_CROSSREF_CONCURRENCY") or CROSSREF_CONCURRENCY)
    crossref_mailto: str = os.getenv("CROSSREF_MAILTO", "")
    github_token: str = os.getenv("GITHUB_TOKEN", "")
    openrouter_api_key: str = os.getenv("OPENROUTER_API_KEY", "")
    llm_model: str = os.getenv("PAUK_LLM_MODEL") or "qwen/qwen-2.5-72b-instruct"
    person_resolution_model: str = (
        os.getenv("PAUK_PERSON_RESOLUTION_MODEL") or "qwen/qwen3-next-80b-a3b-instruct"
    )
    reports_model: str = os.getenv("PAUK_REPORTS_MODEL") or "anthropic/claude-haiku-5.5"
    claude_cli_model: str = os.getenv("PAUK_CLAUDE_CLI_MODEL") or "claude-haiku-5-5"
    codex_cli_model: str = os.getenv("PAUK_CODEX_CLI_MODEL", "")
    llm_provider: str = os.getenv("PAUK_LLM_PROVIDER", "openrouter")
    link_relevance_provider: str = os.getenv("PAUK_LINK_RELEVANCE_PROVIDER", "")
    author_names_provider: str = os.getenv("PAUK_AUTHOR_NAMES_PROVIDER", "")
    person_resolution_provider: str = os.getenv("PAUK_PERSON_RESOLUTION_PROVIDER", "")
    person_resolution_enabled: bool = os.getenv(
        "PAUK_PERSON_RESOLUTION_ENABLED", "true"
    ).lower() in ("1", "true", "yes")
    person_resolution_logreg_model_path: Path = _path_setting(
        "PAUK_PERSON_RESOLUTION_LOGREG_MODEL_PATH",
        ROOT_DIR / "pauk" / "graph" / "artifacts" / "person_resolution_logreg.pkl",
    )
    person_resolution_concurrency: int = int(
        os.getenv("PAUK_PERSON_RESOLUTION_CONCURRENCY", "8")
    )
    person_resolution_batch_size: int = int(os.getenv("PAUK_PERSON_RESOLUTION_BATCH_SIZE", "1"))
    author_names_concurrency: int = int(os.getenv("PAUK_AUTHOR_NAMES_CONCURRENCY", "8"))
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
    mongo_db: str = os.getenv("MONGO_DB", "pauk")
    request_timeout: int = int(os.getenv("PAUK_REQUEST_TIMEOUT", "30"))
    # The admin panel's session cookie. Off by default so the panel works
    # over plain HTTP inside the VPN; turn it on wherever it is served
    # over TLS, and the browser stops sending the cookie unencrypted.
    admin_secure_cookie: bool = os.getenv("PAUK_ADMIN_SECURE_COOKIE", "").lower() in ("1", "true", "yes")
    pdf_crawler_url: str = os.getenv("PAUK_PDF_CRAWLER_URL", "")
    # Official ITMO staff records (personal data, never committed).
    # None means <static_dir>/russian_names.csv.
    russian_names_file: str | None = os.getenv("PAUK_RUSSIAN_NAMES_FILE") or None

    def provider_for(self, stage: str) -> str:
        """LLM provider for a stage: its own setting, else PAUK_LLM_PROVIDER; dedup only by its own.

        A bare CLI name gets its model from claude_cli_model or codex_cli_model. Codex has no default: the CLI's
        own changes between versions, so a bare "codex" with no model set stays bare and fails check_providers.
        """
        own = getattr(self, f"{stage}_provider")
        provider = (own or "openrouter") if stage == "person_resolution" else (own or self.llm_provider)
        model = {"claude": self.claude_cli_model, "codex": self.codex_cli_model}.get(provider.strip())
        return f"{provider.strip()}:{model}" if model else provider

    @property
    def static_dir(self) -> Path:
        return self.data_dir / "static"

    @property
    def raw_dir(self) -> Path:
        return self.data_dir / "raw"

    @property
    def prepared_dir(self) -> Path:
        return self.data_dir / "prepared"

    @property
    def cache_dir(self) -> Path:
        return self.data_dir / "cache"

    @property
    def pdf_dir(self) -> Path:
        return self.data_dir / "pdf"

    @property
    def audit_dir(self) -> Path:
        return self.data_dir / "audit"

    @property
    def gui_dir(self) -> Path:
        return self.data_dir / "gui"

    @property
    def osa_dir(self) -> Path:
        return self.data_dir / "osa"


settings = Settings()

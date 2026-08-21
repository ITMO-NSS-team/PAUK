from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

ROOT_DIR = Path(__file__).resolve().parent.parent
load_dotenv(ROOT_DIR / ".env")


@dataclass(frozen=True)
class Settings:
    data_dir: Path = Path(os.getenv("PAUK_DATA_DIR") or ROOT_DIR / "data")
    openalex_api_key: str = os.getenv("OPENALEX_API_KEY", "")
    github_token: str = os.getenv("GITHUB_TOKEN", "")
    openreview_username: str = os.getenv("OPENREVIEW_USERNAME", "")
    openreview_password: str = os.getenv("OPENREVIEW_PASSWORD", "")
    openrouter_api_key: str = os.getenv("OPENROUTER_API_KEY", "")
    llm_model: str = os.getenv("PAUK_LLM_MODEL", "qwen/qwen-2.5-72b-instruct")
    openrouter_proxy_url: str = os.getenv("OPENROUTER_PROXY_URL", "")
    neo4j_uri: str = os.getenv("NEO4J_URI", "bolt://localhost:7687")
    neo4j_user: str = os.getenv("NEO4J_USER", "neo4j")
    neo4j_password: str = os.getenv("NEO4J_PASSWORD", "")
    mongo_uri: str = os.getenv("MONGO_URI", "mongodb://localhost:27017")
    mongo_db: str = os.getenv("MONGO_DB", "pauk")
    request_timeout: int = int(os.getenv("PAUK_REQUEST_TIMEOUT", "30"))
    openreview_priority_fields: str = os.getenv("PAUK_OPENREVIEW_PRIORITY_FIELDS", "Computer Science")
    pdf_crawler_url: str = os.getenv("PAUK_PDF_CRAWLER_URL", "")
    # Official ITMO staff records (personal data — never committed).
    # None means <static_dir>/russian_names.csv.
    russian_names_file: str | None = os.getenv("PAUK_RUSSIAN_NAMES_FILE") or None

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
    def openreview_priority_field_set(self) -> frozenset[str]:
        return frozenset(field.strip().casefold() for field in self.openreview_priority_fields.split(",") if field.strip())


settings = Settings()

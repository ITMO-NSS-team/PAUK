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
    llm_model: str = os.getenv("PAUK_LLM_MODEL", "anthropic/claude-haiku-4.5")
    neo4j_uri: str = os.getenv("NEO4J_URI", "bolt://localhost:7687")
    neo4j_user: str = os.getenv("NEO4J_USER", "neo4j")
    neo4j_password: str = os.getenv("NEO4J_PASSWORD", "")
    request_timeout: int = int(os.getenv("PAUK_REQUEST_TIMEOUT", "30"))
    pdf_crawler_url: str = os.getenv("PAUK_PDF_CRAWLER_URL", "")

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


settings = Settings()

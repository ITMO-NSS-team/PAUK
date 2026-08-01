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
    neo4j_uri: str = os.getenv("NEO4J_URI", "bolt://localhost:7687")
    neo4j_user: str = os.getenv("NEO4J_USER", "neo4j")
    neo4j_password: str = os.getenv("NEO4J_PASSWORD", "")
    request_timeout: int = int(os.getenv("PAUK_REQUEST_TIMEOUT", "30"))

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


settings = Settings()

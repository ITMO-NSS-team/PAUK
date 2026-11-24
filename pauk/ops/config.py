"""Paths shared by the host and Docker daemon."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Config:
    home: Path
    essdata: Path
    stand: str
    env_file: Path
    state: Path
    assets: Path
    pages_script: Path | None = None
    project: str = "deploy"

    @classmethod
    def environment(cls):
        home = Path(os.environ.get("PAUK_HOME", "/home/asteb/pauk"))
        stand = os.environ.get("PAUK_STAND", "itmo")
        if not re.fullmatch(r"[a-zA-Z0-9_-]+", stand):
            raise ValueError("Invalid PAUK_STAND")
        return cls(
            home=home,
            essdata=Path(os.environ.get("PAUK_ESSDATA", "/var/essdata/pauk")),
            stand=stand,
            env_file=Path(os.environ.get("PAUK_ENV_FILE", str(home / "secrets/pauk.env"))),
            state=Path(os.environ.get("PAUK_OPS_STATE", str(home / "ops"))),
            assets=Path(os.environ.get("PAUK_OPS_ASSETS", "/app/ops-web")),
            pages_script=Path(os.environ.get("PAUK_PAGES_SCRIPT") or str(home / "code/scripts/publish_pages.sh")),
            project=os.environ.get("PAUK_COMPOSE_PROJECT", "deploy"),
        )

    @property
    def code(self):
        return self.home / "code"

    @property
    def dumps(self):
        return self.essdata / "dumps" / self.stand

    @property
    def releases(self):
        return self.essdata / "releases" / self.stand

    @property
    def site(self):
        return self.home / "site" / self.stand

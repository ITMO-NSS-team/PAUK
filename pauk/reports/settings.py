"""Settings of `pauk reports build`, apart from the project-wide `pauk.settings`."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from pauk.settings import ROOT_DIR, _path_setting


@dataclass(frozen=True)
class ReportsSettings:
    osa_package: str = "osa_tool==0.3.2"
    model: str = "anthropic/claude-haiku-5.5"
    context_window: int = 80_000
    languages: dict[str, str] = field(default_factory=lambda: {"en": "English", "ru": "Russian"})
    # On the lab server the clones sit on NFS: fewer at once keeps the peak space
    # down, and each one takes several times longer than on a local disk.
    workers: int = 6
    run_timeout: int = 30 * 60
    stop_grace: int = 30
    degraded_run_markers: tuple[str, ...] = ("Falling back to model", "Parsing failed, fallback applied")
    no_lfs_git_config: dict[str, str] = field(
        default_factory=lambda: {"filter.lfs.process": "", "filter.lfs.smudge": "cat", "filter.lfs.required": "false"}
    )
    proxy_bypass: str = "github.com,githubusercontent.com"
    out_dir: Path | None = _path_setting("PAUK_REPORTS_DIR", ROOT_DIR) if os.getenv("PAUK_REPORTS_DIR") else None


reports_settings = ReportsSettings()

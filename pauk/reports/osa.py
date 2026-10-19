"""One OSA run: a GitHub repository in, one PDF report out.

OSA (https://github.com/aimclub/OSA) is run as an external tool, through
uvx, so its dependency tree never enters the project's environment.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

# Pinned so a rerun reproduces the reports.
OSA_PACKAGE = "osa_tool==0.3.2"
# Cheap enough for every repository of the map, and its probe report was
# clearly more specific than the gpt-4o ones it replaces.
MODEL = "anthropic/claude-haiku-5.5"
# OSA counts tokens with an OpenAI tokenizer, so this stays well under
# Haiku's 100K-token prompt price step.
CONTEXT_WINDOW = 80_000
# The slowest of 399 real runs took 13 minutes, the median 25 seconds.
RUN_TIMEOUT = 15 * 60
# OSA exits 0 with a PDF in both cases: after switching to one of its
# fallback models, and after an unparsable reply, with an "Unknown" report.
DEGRADED_RUN_MARKERS = ("Falling back to model", "Parsing failed, fallback applied")
# The report reads the repository's structure, not its large files: skip Git
# LFS, which also fails the clone wherever the filter is configured but
# git-lfs is not installed.
NO_LFS_GIT_CONFIG = {"filter.lfs.process": "", "filter.lfs.smudge": "cat", "filter.lfs.required": "false"}


def git_config_env(config: dict[str, str]) -> dict[str, str]:
    """Git config as environment variables, honoured by every git subprocess.

    Leaves the user's gitconfig untouched, unlike `git config --global`.

    Example:
        >>> git_config_env({"filter.lfs.smudge": "cat"})
        {'GIT_CONFIG_COUNT': '1', 'GIT_CONFIG_KEY_0': 'filter.lfs.smudge', 'GIT_CONFIG_VALUE_0': 'cat'}
    """
    env = {"GIT_CONFIG_COUNT": str(len(config))}
    for i, (key, value) in enumerate(config.items()):
        env[f"GIT_CONFIG_KEY_{i}"] = key
        env[f"GIT_CONFIG_VALUE_{i}"] = value
    return env


class OsaRunner:
    """Runs OSA for one repository; the PDF lands at its target whole or not at all."""

    def __init__(self, api_key: str, osa_dir: Path) -> None:
        """Prepares the environment every run shares.

        Args:
            api_key: OpenRouter key, handed to OSA as `LLM_SERVICE_KEY`, the
                name its `protollm` client reads for OpenRouter.
            osa_dir: Working copies go to `<osa_dir>/tmp`, logs to `<osa_dir>/logs`.
        """
        self.osa_dir = osa_dir
        # The GitHub token is already in os.environ (settings loads .env).
        # A wide console keeps OSA's rich log lines unwrapped, so the markers stay greppable.
        self.env = {**os.environ, "LLM_SERVICE_KEY": api_key, "COLUMNS": "400", **git_config_env(NO_LFS_GIT_CONFIG)}

    @staticmethod
    def command(url: str, osa_language: str) -> list[str]:
        """The OSA command line for one repository.

        Report only: the basic mode also writes a README, docs and docstrings,
        paying for LLM calls whose output is deleted with the clone. `--api` is
        explicit because later OSA versions default to a subscription CLI.

        Args:
            url: GitHub URL OSA clones.
            osa_language: OSA's `--artefacts-language`, the `language` of one of its locale files.
        """
        return [
            "uvx", "--from", OSA_PACKAGE, "osa-tool", "-r", url,
            "--mode", "advanced", "--report", "--web-mode",
            "--no-fork", "--no-pull-request", "--delete-dir",
            "--api", "openai", "--model", MODEL, "--context-window", str(CONTEXT_WINDOW),
            "--artefacts-language", osa_language,
        ]

    def run(self, key: str, url: str, lang: str, osa_language: str, target: Path) -> bool:
        """Generates the report of one repository into `target`.

        Args:
            key: Repository id, names the working directory and the log.
            url: GitHub URL OSA clones.
            lang: Site language, a path segment of the working directory and the log.
            osa_language: See `command`.
            target: Where the PDF goes; untouched unless the run succeeds.

        Returns:
            True if OSA exited 0, left exactly one PDF report and logged no
            degraded run (`DEGRADED_RUN_MARKERS`); the reason is logged otherwise.
        """
        work = self.osa_dir / "tmp" / lang / key
        log_path = self.osa_dir / "logs" / lang / f"{key}.log"
        shutil.rmtree(work, ignore_errors=True)
        work.mkdir(parents=True)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with log_path.open("w", encoding="utf-8") as log, subprocess.Popen(
                self.command(url, osa_language), cwd=work, env=self.env, stdout=log, stderr=subprocess.STDOUT,
            ) as proc:
                try:
                    code = proc.wait(timeout=RUN_TIMEOUT)
                except subprocess.TimeoutExpired:
                    # SIGTERM, not SIGKILL: uvx forwards it to OSA, a kill would orphan it.
                    proc.terminate()
                    proc.wait()
                    logger.warning("reports %s/%s: OSA timed out after %ss, see %s", lang, key, RUN_TIMEOUT, log_path)
                    return False
            pdfs = list(work.glob("*_report.pdf"))
            if code != 0 or len(pdfs) != 1 or not pdfs[0].read_bytes().startswith(b"%PDF-"):
                logger.warning(
                    "reports %s/%s: OSA exited %s with %d report(s), see %s", lang, key, code, len(pdfs), log_path
                )
                return False
            log_text = log_path.read_text(encoding="utf-8", errors="replace")
            if marker := next((m for m in DEGRADED_RUN_MARKERS if m in log_text), None):
                logger.warning("reports %s/%s: OSA logged %r, report discarded, see %s", lang, key, marker, log_path)
                return False
            target.parent.mkdir(parents=True, exist_ok=True)
            os.replace(pdfs[0], target)
            return True
        finally:
            shutil.rmtree(work, ignore_errors=True)

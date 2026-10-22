"""One OSA run: a GitHub repository in, one PDF report out.

OSA (https://github.com/aimclub/OSA) is run as an external tool, through
uvx, so its dependency tree never enters the project's environment.
"""

from __future__ import annotations

import logging
import os
import shutil
import signal
import subprocess
import threading
from contextlib import suppress
from pathlib import Path

from .settings import ReportsSettings, reports_settings

logger = logging.getLogger(__name__)


def proxy_env(proxy_url: str, bypass: str) -> dict[str, str]:
    """Proxy variables for every HTTP client OSA starts, `bypass` hosts excepted; empty without a proxy.

    Both spellings, since curl (under git) reads only the lowercase `http_proxy`.
    """
    if not proxy_url:
        return {}
    names = ("HTTPS_PROXY", "HTTP_PROXY")
    return {
        **{name: proxy_url for name in names},
        **{name.lower(): proxy_url for name in names},
        "NO_PROXY": bypass,
        "no_proxy": bypass,
    }


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

    def __init__(
        self, api_key: str, osa_dir: Path, proxy_url: str = "", settings: ReportsSettings = reports_settings
    ) -> None:
        """Prepares the environment every run shares.

        Args:
            api_key: OpenRouter key, handed to OSA as `LLM_SERVICE_KEY`, the
                name its `protollm` client reads for OpenRouter.
            osa_dir: Working copies go to `<osa_dir>/tmp`, logs to `<osa_dir>/logs`.
            proxy_url: `OPENROUTER_PROXY_URL`, see `proxy_env`.
            settings: The OSA version, model and context window to run with.
        """
        self.osa_dir = osa_dir
        self.settings = settings
        self._running: set[subprocess.Popen] = set()
        self._lock = threading.Lock()
        self._stopped = threading.Event()
        # A wide console keeps OSA's log lines unwrapped, so the markers stay greppable.
        self.env = {
            **os.environ, "LLM_SERVICE_KEY": api_key, "COLUMNS": "400",
            **git_config_env(settings.no_lfs_git_config), **proxy_env(proxy_url, settings.proxy_bypass),
        }

    def command(self, url: str, osa_language: str) -> list[str]:
        """The OSA command line for one repository.

        Report only: the basic mode also writes a README, docs and docstrings,
        paying for LLM calls whose output is deleted with the clone. `--api` is
        explicit because later OSA versions default to a subscription CLI.

        Args:
            url: GitHub URL OSA clones.
            osa_language: OSA's `--artefacts-language`, the `language` of one of its locale files.
        """
        return [
            "uvx", "--from", self.settings.osa_package, "osa-tool", "-r", url,
            "--mode", "advanced", "--report", "--web-mode",
            "--no-fork", "--no-pull-request", "--delete-dir",
            "--api", "openai", "--model", self.settings.model, "--context-window", str(self.settings.context_window),
            "--artefacts-language", osa_language,
        ]

    def stop(self) -> None:
        """Stops every run in flight and refuses new ones; for an interrupted build."""
        with self._lock:
            self._stopped.set()
            running = list(self._running)
        for proc in running:
            self._terminate(proc)

    def _terminate(self, proc: subprocess.Popen) -> None:
        """SIGTERM the run's process group, SIGKILL whatever is left after `stop_grace`.

        The whole group, so the git and Python children of uvx go too.
        """
        with suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGTERM)
        try:
            proc.wait(timeout=self.settings.stop_grace)
        except subprocess.TimeoutExpired:
            with suppress(ProcessLookupError):
                os.killpg(proc.pid, signal.SIGKILL)
            proc.wait()

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
            degraded run (`degraded_run_markers`), and the runner was not
            stopped; the reason is logged otherwise.
        """
        if self._stopped.is_set():
            return False
        work = self.osa_dir / "tmp" / lang / key
        log_path = self.osa_dir / "logs" / lang / f"{key}.log"
        shutil.rmtree(work, ignore_errors=True)
        work.mkdir(parents=True)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with log_path.open("w", encoding="utf-8") as log:
                # Checked, started and registered under one lock, or a stop could
                # land in between and miss the process.
                with self._lock:
                    if self._stopped.is_set():
                        return False
                    # Own process group: a stop kills the whole tree, Ctrl+C reaches only pauk.
                    proc = subprocess.Popen(
                        self.command(url, osa_language), cwd=work, env=self.env, stdout=log,
                        stderr=subprocess.STDOUT, start_new_session=True,
                    )
                    self._running.add(proc)
                try:
                    code = proc.wait(timeout=self.settings.run_timeout)
                except subprocess.TimeoutExpired:
                    logger.warning(
                        "reports %s/%s: OSA timed out after %ss, see %s", lang, key, self.settings.run_timeout, log_path
                    )
                    self._terminate(proc)
                    return False
                finally:
                    with self._lock:
                        self._running.discard(proc)
            if self._stopped.is_set():
                return False
            pdfs = list(work.glob("*_report.pdf"))
            if code != 0 or len(pdfs) != 1 or not pdfs[0].read_bytes().startswith(b"%PDF-"):
                logger.warning(
                    "reports %s/%s: OSA exited %s with %d report(s), see %s", lang, key, code, len(pdfs), log_path
                )
                return False
            log_text = log_path.read_text(encoding="utf-8", errors="replace")
            if marker := next((m for m in self.settings.degraded_run_markers if m in log_text), None):
                logger.warning("reports %s/%s: OSA logged %r, report discarded, see %s", lang, key, marker, log_path)
                return False
            target.parent.mkdir(parents=True, exist_ok=True)
            os.replace(pdfs[0], target)
            return True
        finally:
            shutil.rmtree(work, ignore_errors=True)

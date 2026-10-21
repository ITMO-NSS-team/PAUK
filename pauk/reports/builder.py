"""`pauk reports build`: OSA reports on every repository of a graph snapshot.

OSA needs an LLM, which the lab server can't reach, so this runs locally,
outside the lab VPN, from the same snapshot `pauk gui build` reads.
"""

from __future__ import annotations

import json
import logging
import shutil
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from tqdm import tqdm

from pauk.cache.graph_snapshot import read_snapshot
from pauk.settings import Settings
from pauk.sources.base import HttpRequestError
from pauk.sources.llm import OpenRouterClient
from pauk.storage import AtomicWriter

from .osa import MODEL, OsaRunner

logger = logging.getLogger(__name__)

# Site language -> OSA's --artefacts-language, the languages the site has.
LANGUAGES = {"en": "English", "ru": "Russian"}
# 8 like the project's other LLM stages; the clones, not the LLM, are the
# bottleneck, and 8 parallel runs stayed within GitHub's limits.
WORKERS = 8


def report_path(out_dir: Path, lang: str, key: str) -> Path:
    """`<out_dir>/<lang>/<key>.pdf`, the URL path the site links to."""
    return out_dir / lang / f"{key}.pdf"


def write_index(out_dir: Path) -> dict[str, list[str]]:
    """Writes `<out_dir>/index.json` from what is on disk.

    Built from the files, not from the run, so the site never links a PDF
    that isn't there, whatever an interrupted run left behind.

    Returns:
        `{lang: [repository ids]}`, exactly what was written.
    """
    index = {
        lang_dir.name: sorted(path.stem for path in lang_dir.glob("*.pdf"))
        for lang_dir in sorted(out_dir.iterdir())
        if lang_dir.is_dir()
    }
    with AtomicWriter(out_dir / "index.json") as fh:
        json.dump(index, fh, ensure_ascii=False)
    return index


class ReportsBuilder:
    """Entry point for `pauk reports build`: snapshot -> one OSA report per repository and language."""

    def __init__(self, config: Settings) -> None:
        """Stores the config; nothing is checked or run until `build()`.

        Args:
            config: Project settings, in particular `openrouter_api_key` and `osa_dir`.
        """
        self.config = config
        self.llm = OpenRouterClient(config.request_timeout, config.openrouter_api_key, MODEL)

    def build(
        self, snapshot: Path, out_dir: Path | None = None, *,
        keys: frozenset[str] = frozenset(), force: bool = False, workers: int = WORKERS,
    ) -> dict[str, int | float | None]:
        """Generates the missing reports and rewrites the index.

        An existing PDF counts as done, a failed one is retried by the next
        run: the files are the only state.

        Args:
            snapshot: Graph snapshot taken by `pauk cache export`.
            out_dir: Site folder for the reports, `<out_dir>/<lang>/<id>.pdf`;
                defaults to `reports/` in the site data the map is built from.
            keys: Limit the run to these repository ids; empty means all of them.
            force: Regenerate reports that already exist.
            workers: OSA runs in parallel.

        Returns:
            Counts of generated, skipped and failed reports, plus the dollars
            OpenRouter charged for the run (None if it could not be read).

        Raises:
            ValueError: an id is not in the snapshot, or the key is not set.
            RuntimeError: uvx is missing, or OpenRouter is unreachable with the key.
        """
        out_dir = out_dir or self.config.gui_dir / "private" / "reports"
        repos = {row["id"]: row["url"] for row in read_snapshot(snapshot)["repositories"] if row.get("url")}
        if unknown := keys - repos.keys():
            raise ValueError(f"not in the snapshot: {', '.join(sorted(unknown))}")
        selected = sorted(keys or repos)
        jobs = [
            (key, lang) for lang in LANGUAGES for key in selected
            if force or not report_path(out_dir, lang, key).exists()
        ]
        # Nothing to generate needs no key or network: the index is rebuilt offline.
        spent_before = self._preflight() if jobs else None
        generated = failed = 0
        try:
            if jobs:
                generated, failed = self._run_all(jobs, repos, out_dir, workers)
        finally:
            out_dir.mkdir(parents=True, exist_ok=True)
            write_index(out_dir)
        if failed:
            logger.warning("reports: %d failed, see %s", failed, self.config.osa_dir / "logs")
        return {
            "generated": generated,
            "skipped": len(selected) * len(LANGUAGES) - len(jobs),
            "failed": failed,
            "spent_usd": 0.0 if spent_before is None else self._spent_since(spent_before),
        }

    def _run_all(
        self, jobs: list[tuple[str, str]], repos: dict[str, str], out_dir: Path, workers: int
    ) -> tuple[int, int]:
        """Runs OSA for every `(key, lang)` job in parallel; returns (generated, failed)."""
        runner = OsaRunner(self.llm.api_key, self.config.osa_dir)
        generated = failed = 0
        with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
            futures = [
                pool.submit(runner.run, key, repos[key], lang, LANGUAGES[lang], report_path(out_dir, lang, key))
                for key, lang in jobs
            ]
            try:
                for future in tqdm(as_completed(futures), total=len(futures), desc="OSA reports",
                                   unit="report", dynamic_ncols=True, disable=None):
                    if future.result():
                        generated += 1
                    else:
                        failed += 1
            except BaseException:
                # Cancelling only drops queued runs; the running ones are stopped
                # here, or leaving the pool would wait for them to finish.
                pool.shutdown(wait=False, cancel_futures=True)
                runner.stop()
                raise
        return generated, failed

    def _preflight(self) -> float:
        """Checks what OSA would otherwise fail on slowly; returns the key's spend so far.

        Without a working key OSA does not fail, it retries through its
        fallback models for minutes; a wrong network (the lab VPN) looks the same.
        """
        if not self.llm.api_key:
            raise ValueError("OPENROUTER_API_KEY is not set in .env")
        if shutil.which("uvx") is None:
            raise RuntimeError("uvx not found: OSA runs through uv, install it first")
        try:
            return self.llm.usage()
        except HttpRequestError as error:
            raise RuntimeError(f"OpenRouter is not reachable with this key: {error}") from None

    def _spent_since(self, before: float) -> float | None:
        # Read after hours of work: a failure here must not cost the summary.
        try:
            return round(self.llm.usage() - before, 4)
        except HttpRequestError as error:
            logger.warning("reports: could not read the spend from OpenRouter: %s", error)
            return None

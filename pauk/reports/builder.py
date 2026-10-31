"""`pauk reports build`: OSA reports on every repository of a graph snapshot.

Reports use the same graph snapshot as `pauk gui build` and an LLM via
OpenRouter, optionally through a configured proxy.
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

from .osa import OsaRunner
from .settings import ReportsSettings, reports_settings

logger = logging.getLogger(__name__)



def report_path(out_dir: Path, lang: str, key: str) -> Path:
    """`<out_dir>/<lang>/<key>.pdf`, the URL path the site links to."""
    return out_dir / lang / f"{key}.pdf"


def third_party_repos(graph: dict[str, list]) -> set[str]:
    """Repositories the map only cites as someone else's code.

    Never `IMPLEMENTS`, every `MENTIONS_LINK` judged `is_relevant=false`, no
    ITMO contributor: a report on them rates code nobody here wrote. An
    undecided mention (`null`) or an ITMO contributor keeps the repository,
    since the classifier can be wrong about the authors' own code.

    Args:
        graph: Snapshot tables `repo_pubs`, `mentions_repos` and `repo_persons`.

    Returns:
        Their repository ids.
    """
    ours = {edge["rid"] for edge in graph.get("repo_pubs", []) + graph.get("repo_persons", [])}
    mentions = graph.get("mentions_repos", [])
    undecided = {edge["rid"] for edge in mentions if edge.get("is_relevant") is not False}
    return {edge["rid"] for edge in mentions} - ours - undecided


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

    def __init__(self, config: Settings, settings: ReportsSettings = reports_settings) -> None:
        """Stores the config; nothing is checked or run until `build()`.

        Args:
            config: Project settings, in particular `openrouter_api_key`,
                `reports_model`, `openrouter_proxy_url` and `osa_dir`.
            settings: OSA version, context window and the reports folder.
        """
        self.config = config
        self.settings = settings
        self.llm = OpenRouterClient(
            config.request_timeout, config.openrouter_api_key, config.reports_model, config.openrouter_proxy_url
        )

    def build(
        self, snapshot: Path, out_dir: Path | None = None, *,
        keys: frozenset[str] = frozenset(), force: bool = False, workers: int | None = None,
    ) -> dict[str, int | float | None]:
        """Generates the missing reports and rewrites the index.

        An existing PDF counts as done, a failed one is retried by the next
        run: the files are the only state. With nothing to generate the index
        is rebuilt offline, without the key, uvx or network.

        Args:
            snapshot: Graph snapshot taken by `pauk cache export`.
            out_dir: Site folder for the reports, `<out_dir>/<lang>/<id>.pdf`;
                defaults to `PAUK_REPORTS_DIR`, else `reports/` in the site data.
            keys: Limit the run to these repository ids, third-party or not; empty
                means all of them but the third-party ones (`third_party_repos`).
            force: Regenerate reports that already exist.
            workers: OSA runs in parallel; None is `settings.workers`.

        Returns:
            Counts of generated, skipped (already there) and failed reports,
            of third-party repositories left out, plus the dollars OpenRouter
            charged for the run (None if it could not be read).

        Raises:
            ValueError: an id is not in the snapshot, or the key is not set.
            RuntimeError: uvx is missing, or OpenRouter is unreachable with the key.
        """
        out_dir = out_dir or self.settings.out_dir or self.config.gui_dir / "private" / "reports"
        graph = read_snapshot(snapshot)
        repos = {row["id"]: row["url"] for row in graph["repositories"] if row.get("url")}
        if unknown := keys - repos.keys():
            raise ValueError(f"not in the snapshot: {', '.join(sorted(unknown))}")
        third_party = set() if keys else third_party_repos(graph) & repos.keys()
        selected = sorted(keys or repos.keys() - third_party)
        jobs = [
            (key, lang) for lang in self.settings.languages for key in selected
            if force or not report_path(out_dir, lang, key).exists()
        ]
        spent_before = self._preflight() if jobs else None
        generated = failed = 0
        try:
            if jobs:
                generated, failed = self._run_all(jobs, repos, out_dir, workers or self.settings.workers)
        finally:
            out_dir.mkdir(parents=True, exist_ok=True)
            write_index(out_dir)
        if failed:
            logger.warning("reports: %d failed, see %s", failed, self.config.osa_dir / "logs")
        return {
            "generated": generated,
            "skipped": len(selected) * len(self.settings.languages) - len(jobs),
            "failed": failed,
            "third_party": len(third_party),
            "spent_usd": 0.0 if spent_before is None else self._spent_since(spent_before),
        }

    def _run_all(
        self, jobs: list[tuple[str, str]], repos: dict[str, str], out_dir: Path, workers: int
    ) -> tuple[int, int]:
        """Runs OSA for every `(key, lang)` job in parallel; returns (generated, failed)."""
        runner = OsaRunner(
            self.llm.api_key, self.config.osa_dir, self.config.openrouter_proxy_url, self.settings,
            model=self.llm.model,
        )
        generated = failed = 0
        with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
            futures = [
                pool.submit(
                    runner.run, key, repos[key], lang, self.settings.languages[lang], report_path(out_dir, lang, key)
                )
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
                # cancel_futures drops only queued runs; leaving the pool would wait for the rest.
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
        """Dollars spent since `before`; None rather than losing the summary after hours of work."""
        try:
            return round(self.llm.usage() - before, 4)
        except HttpRequestError as error:
            logger.warning("reports: could not read the spend from OpenRouter: %s", error)
            return None

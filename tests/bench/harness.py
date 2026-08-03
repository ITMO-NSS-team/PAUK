"""Running the real pipeline over a synthetic universe, with no network.

Both bench modules drive the same conveyor — collect, normalize, enrich,
publish — so the plumbing that swaps every external client for a mock lives
here and the test modules only describe what should come out.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

from pauk.settings import Settings

from .mocks import (
    MockCrossrefClient,
    MockGitHubClient,
    MockOpenAlexClient,
    MockOrcidClient,
    UnexpectedNetworkClient,
)


def bench_settings(data_dir: Path) -> Settings:
    """Settings pinned to the bench data directory and to no credentials.

    OpenReview is deliberately left unconfigured: the persons stage skips it
    without credentials, and the patched client asserts on any call, so a
    developer's own .env can never make the bench talk to a real service.
    """
    return Settings(data_dir=data_dir, openreview_username="", openreview_password="",
                    github_token="", openalex_api_key="")


def write_static_catalog(data_dir: Path, catalog: list[dict]) -> None:
    static_dir = data_dir / "static"
    static_dir.mkdir(parents=True, exist_ok=True)
    (static_dir / "departments_catalog.json").write_text(
        json.dumps(catalog, ensure_ascii=False), encoding="utf-8")


@contextmanager
def external_services(universe: dict, github: MockGitHubClient | None = None,
                      orcid: MockOrcidClient | None = None):
    """Point every enrichment stage at this universe instead of the network.

    The same client objects can be handed to several runs in a row, which is
    what makes transient failures ("fails once, then answers") observable
    across enrichment runs.
    """
    github = github if github is not None else MockGitHubClient(universe)
    orcid = orcid if orcid is not None else MockOrcidClient(universe)
    patches = (
        mock.patch("pauk.pipeline.stages.repositories.GitHubClient", lambda *a, **k: github),
        mock.patch("pauk.pipeline.stages.persons.OpenAlexClient",
                   lambda *a, **k: MockOpenAlexClient(universe)),
        mock.patch("pauk.pipeline.stages.persons.CrossrefClient",
                   lambda *a, **k: MockCrossrefClient(universe)),
        mock.patch("pauk.pipeline.stages.persons.OrcidClient", lambda *a, **k: orcid),
        mock.patch("pauk.pipeline.stages.persons.OpenReviewClient",
                   lambda *a, **k: UnexpectedNetworkClient()),
    )
    for patch in patches:
        patch.start()
    try:
        yield github, orcid
    finally:
        for patch in patches:
            patch.stop()


def works_file(tmp_dir: Path, name: str, work_ids: tuple[str, ...]) -> Path:
    """A --works-file for the collect step, one OpenAlex work id per line."""
    path = tmp_dir / name
    path.write_text("\n".join(work_ids) + "\n", encoding="utf-8")
    return path

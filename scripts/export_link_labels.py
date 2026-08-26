"""Export code links (or whole papers) to CSV for manual annotation.

The only harness in this repository that measures how well link extraction
works, so the numbers it produces are what any claim about extractor quality
rests on. Two modes, because precision and recall need different inputs and
only one of them can be measured from the links the pipeline already found:

  links  (default)  One row per extracted link, with two independent label
                    columns because the pipeline makes two independent
                    decisions: `url_ok` scores the extractor (PRECISION), and
                    `authors_own` scores the model, which predicts exactly that
                    one binary. Whether the repository exists at all is a third
                    decision, already answered by the GitHub lookup and carried
                    in `resolved_on_github` rather than labelled by hand.

  papers            One row per sampled publication. The annotator opens the
                    PDF and lists every repository it actually references.
                    Answers "what did the extractor miss" - i.e. extraction
                    RECALL, which is invisible from the extracted set alone.

In papers mode the URLs PAUK found are withheld by default: showing them first
turns an independent search into a confirmation task and inflates recall. Pass
--show-extracted only when reviewing the extractor, never when producing the
ground truth it will be scored against.

Usage:
    uv run python scripts/export_link_labels.py --group <group> [--mode links]
        [--limit 150] [--seed 42] [--out path.csv]
"""

from __future__ import annotations

import argparse
import csv
import random
import sys
from pathlib import Path

from pauk.models import Publication, RepoLink, Repository
from pauk.models.processing import ProcessingStatus
from pauk.pipeline.stages.code_links import ARCHIVED_DEPOSIT_REASON
from pauk.settings import settings
from pauk.storage import PreparedStore
from pauk.storage.mongo import get_mongo_client
from pauk.urls import normalize_repo_url

LINK_COLUMNS = [
    # Annotator fills these three. They are deliberately separate: the pipeline
    # makes three independent decisions about a link, and collapsing them into
    # one label would score the regex, the GitHub lookup and the model with a
    # single prediction.
    "authors_own",  # yes | no | unclear - the ONLY column the model predicts
    "url_ok",  # ok | mangled - scores the extractor, not the model
    "notes",
    "publication_id",
    "title",
    "doi",
    "url",
    # Whether code_links decided this one itself (archived deposit, no LLM call)
    # or the model did. Deterministic rows must be excluded from model metrics.
    "verdict_source",
    "model_verdict",
    "model_confidence",
    "model_reason",
    # Exactly what went into the prompt: link_relevance sends occurrences[0]
    # only. Scoring the model against evidence it never saw would be unfair.
    "model_context",
    "model_page",
    # The richest occurrence, for the human establishing ground truth.
    "full_context",
    "full_page",
    "occurrences",
    # Did the repositories stage resolve this on GitHub? Answers "is this a
    # real repository" deterministically, so no one needs to label it.
    "resolved_on_github",
]

PAPER_COLUMNS = [
    "ground_truth_urls",  # annotator fills: repo URLs found in the PDF, space-separated
    "notes",
    "publication_id",
    "title",
    "doi",
    "pdf_url",
    "local_pdf",
    "has_abstract",
]

LABEL_GUIDE = """\
# Manual annotation guide

## Mode `links` - one row per extracted link

PAUK makes three independent decisions about a link, so there are two label
columns rather than one combined class. Fill both on every row.

### `authors_own` - yes | no | unclear

The only judgement the model also makes, and the only column it is scored
against.

  yes      The artifact was released by the authors of THIS paper as supporting
           material for it. "our code is available at", "we release", "code:
           github.com/...". The owner or org in the URL often matches an author
           or their affiliation.

  no       A repository the paper only uses or cites - PyTorch, a baseline
           implementation, anything in the reference list.

  unclear  You genuinely cannot tell. Say why in `notes`. These rows are
           EXCLUDED from the metric, not counted as a third class - the model
           has no "unclear" output to compare against.

### `url_ok` - ok | mangled

Scores the extractor, not the model.

  ok       `url` is a well-formed owner/repo reference.

  mangled  PDF text extraction corrupted it and the defenses missed: a repo
           name truncated at a line break with no hyphen (`github.com/org/re`),
           or a footnote digit glued onto the name.

Note what CANNOT appear here, so you do not go looking for it: gist URLs and
hosts like `mygithub.com` are rejected by the pattern outright, and links to an
issue, a blob or a deep path are truncated to `owner/repo` before you see them.
A URL that is well-formed but points at a repository that no longer exists is
also NOT `mangled` - that is what `resolved_on_github` records, decided by the
GitHub API rather than by you.

### While labelling

Ignore `model_verdict`, `model_confidence` and `model_reason`. They are the
prediction being scored, and reading them first will pull your judgement toward
them.

Use `full_context` to decide. `model_context` is shown separately because
link_relevance sends only the first recorded occurrence to the model; when the
two differ, the metrics script can separate "the model was wrong" from "the
model never saw the sentence that settles it".

Rows whose `verdict_source` is `deterministic` were decided by code_links
without any model call (a Zenodo deposit archiving its own repository). Label
them anyway - they still measure the extractor - but they are excluded from the
model's precision.

## Mode `papers` - one row per publication

Open the PDF and list EVERY repository URL the paper references, in the
`ground_truth_urls` column, separated by spaces. Include the paper's own code
and third-party tools alike; note in `notes` which are the authors' own if it
is not obvious. An empty cell means "this paper references no repository" -
leave the row in, do not delete it. That is a real and useful observation.
"""


def _occurrence_cells(occurrence) -> tuple[str, str]:
    """Context and page of one occurrence, as CSV cells."""
    if occurrence is None:
        return "", ""
    page = "abstract" if occurrence.page_number is None else str(occurrence.page_number)
    return (occurrence.context or "").strip(), page


def _richest_occurrence(link):
    """The occurrence that best settles the own/third-party call for a human:
    a PDF page over the abstract, then the longest context. The abstract
    context is usually one truncated sentence."""
    if not link.occurrences:
        return None
    return max(
        link.occurrences,
        key=lambda o: (o.page_number is not None, len(o.context or "")),
    )


def _verdict_source(link) -> str:
    if link.is_relevant is None:
        return "none"
    return "deterministic" if link.llm_reason == ARCHIVED_DEPOSIT_REASON else "model"


def _resolution_index(store: PreparedStore) -> dict[str, str]:
    """Map every URL a repository is known by to whether GitHub resolved it.

    The repositories stage answers "is this a real repository" with an API
    call, so it is not something a human should be labelling.
    """
    index: dict[str, str] = {}
    for repo in store.read_models("repositories", Repository):
        state = repo.processing.get("repositories")
        if state is None or state.status == ProcessingStatus.NOT_STARTED:
            resolved = "not_checked"
        elif state.status == ProcessingStatus.FAILED:
            resolved = "no"
        else:
            resolved = "yes"
        for url in [repo.url, *repo.cited_urls]:
            if url:
                index[normalize_repo_url(url)] = resolved
    return index


def export_links(store: PreparedStore, limit: int | None, seed: int) -> list[dict]:
    publications = {p.id: p for p in store.read_models("publications", Publication)}
    resolution = _resolution_index(store)

    rows: list[dict] = []
    for repo_link in store.read_models("repo_links", RepoLink):
        pub = publications.get(repo_link.publication_id)
        for link in repo_link.links:
            # occurrences[0] is what link_relevance puts in the prompt; the
            # richest one is what the human gets. Keeping both lets the
            # metrics separate a wrong model from an under-informed one.
            model_context, model_page = _occurrence_cells(
                link.occurrences[0] if link.occurrences else None
            )
            full_context, full_page = _occurrence_cells(_richest_occurrence(link))
            rows.append({
                "authors_own": "",
                "url_ok": "",
                "notes": "",
                "publication_id": repo_link.publication_id,
                "title": (pub.title if pub else "") or "",
                "doi": (pub.doi if pub else "") or "",
                "url": link.url,
                "verdict_source": _verdict_source(link),
                "model_verdict": "" if link.is_relevant is None else str(link.is_relevant),
                "model_confidence": "" if link.llm_confidence is None else link.llm_confidence,
                "model_reason": link.llm_reason or "",
                "model_context": model_context,
                "model_page": model_page,
                "full_context": full_context,
                "full_page": full_page,
                "occurrences": len(link.occurrences),
                "resolved_on_github": resolution.get(normalize_repo_url(link.url), "not_checked"),
            })

    return _sample(rows, limit, seed)


def export_papers(
    store: PreparedStore, limit: int | None, seed: int, show_extracted: bool
) -> list[dict]:
    extracted: dict[str, list[str]] = {}
    for repo_link in store.read_models("repo_links", RepoLink):
        extracted[repo_link.publication_id] = [link.url for link in repo_link.links]

    rows: list[dict] = []
    for pub in store.read_models("publications", Publication):
        # Only papers whose PDF the annotator can actually open - a recall
        # sample over papers with no retrievable full text measures nothing.
        if not (pub.pdf_url or pub.full_text):
            continue
        local_pdf = settings.pdf_dir / store.group / f"{pub.id}.pdf"
        row = {
            "ground_truth_urls": "",
            "notes": "",
            "publication_id": pub.id,
            "title": pub.title or "",
            "doi": pub.doi or "",
            "pdf_url": pub.pdf_url or "",
            "local_pdf": str(local_pdf) if local_pdf.exists() else "",
            "has_abstract": "yes" if pub.abstract else "no",
        }
        if show_extracted:
            row["extracted_urls"] = " ".join(extracted.get(pub.id, []))
        rows.append(row)

    return _sample(rows, limit, seed)


def _sample(rows: list[dict], limit: int | None, seed: int) -> list[dict]:
    """Seeded sample so the same --limit/--seed always yields the same sheet -
    a re-export must not silently reshuffle work already annotated."""
    if limit is None or limit >= len(rows):
        return rows
    return random.Random(seed).sample(rows, limit)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n")[0])
    parser.add_argument("--group", required=True, help="pipeline group to export")
    parser.add_argument("--mode", choices=("links", "papers"), default="links")
    parser.add_argument("--limit", type=int, default=None, help="sample size")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument(
        "--show-extracted",
        action="store_true",
        help="papers mode: reveal the URLs PAUK found (biases recall - see module docstring)",
    )
    args = parser.parse_args(argv)

    out = args.out or Path("data/labeling") / f"{args.mode}__{args.group}.csv"
    out.parent.mkdir(parents=True, exist_ok=True)

    mongo = get_mongo_client(settings)
    try:
        store = PreparedStore(mongo[settings.mongo_db], args.group)
        if args.mode == "links":
            rows = export_links(store, args.limit, args.seed)
            columns = LINK_COLUMNS
        else:
            rows = export_papers(store, args.limit, args.seed, args.show_extracted)
            columns = PAPER_COLUMNS + (["extracted_urls"] if args.show_extracted else [])
    finally:
        mongo.close()

    if not rows:
        print(f"nothing to export for group {args.group!r} in mode {args.mode}", file=sys.stderr)
        return 1

    with out.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)

    guide = out.parent / "ANNOTATION_GUIDE.md"
    guide.write_text(LABEL_GUIDE, encoding="utf-8")

    print(f"wrote {len(rows)} rows to {out}")
    print(f"annotation guide: {guide}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
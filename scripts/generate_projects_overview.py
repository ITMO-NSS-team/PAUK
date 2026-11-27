"""Generate an offline Markdown catalog of ITMO projects from a graph snapshot.

Run from the repository root with ``uv run python -m scripts.generate_projects_overview``.
By default only the catalog is printed. ``--output`` writes a catalog file;
``--template`` updates the marked block in an existing Markdown page in place.
"""

from __future__ import annotations

import argparse
import html
import json
import re
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from urllib.parse import quote, urlsplit

from pauk.cache.graph_snapshot import latest_snapshot, read_snapshot
from pauk.models.identity import normalize_external_value
from pauk.settings import settings
from pauk.storage.atomic import AtomicWriter
from pauk.urls import normalize_repo_url

START_MARKER = "<!-- PAUK:PROJECTS:START -->"
END_MARKER = "<!-- PAUK:PROJECTS:END -->"
_TABLE_KEYS = {
    "persons": ("id",),
    "publications": ("id",),
    "repositories": ("id",),
    "departments": ("id",),
    "authorship": ("pid", "per"),
    "repo_pubs": ("rid", "pid"),
    "repo_persons": ("rid", "per"),
    "repo_depts": ("rid", "did"),
}


@dataclass(frozen=True)
class Project:
    """Public catalog fields only; no person records or internal identifiers."""

    name: str
    url: str
    year: int | None
    title: str = ""
    description: str = ""
    paper_url: str | None = None


def _text(value: object) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join(re.sub(r"[\x00-\x1f\x7f]", " ", value).split())


def _markdown(value: str) -> str:
    return re.sub(r"([\\`*_{}\[\]()#+.!|~\-])", r"\\\1", html.escape(value, quote=False))


def _safe_url(value: object) -> str | None:
    """Accept HTTP(S) destinations without credentials or Markdown delimiters."""
    if not isinstance(value, str):
        return None
    value = value.strip()
    if re.search(r"[\x00-\x1f\x7f]", value):
        return None
    try:
        parsed = urlsplit(value)
        if (
            parsed.scheme.lower() not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or not re.fullmatch(r"[\w.\-:\[\]]+", parsed.netloc)
        ):
            return None
        _ = parsed.port
    except ValueError:
        return None
    return quote(value, safe=":/?#[]@!$&'*+,;=%~-._")


def _records(value: object) -> list[dict]:
    """Read optional JSON-text metadata without exposing it in output/errors."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return []
    if not isinstance(value, list):
        return []
    return [row for row in value if isinstance(row, dict)]


def _doi_url(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    doi = normalize_external_value("doi", value)
    if not re.fullmatch(r"10\.\d{4,9}/\S+", doi):
        return None
    return _safe_url("https://doi.org/" + quote(doi, safe="/:;._-"))


def _paper_url(publication: dict) -> str | None:
    if doi := _doi_url(publication.get("doi")):
        return doi
    identifiers = _records(publication.get("identifiers"))
    dois = {url for row in identifiers if row.get("scheme") == "doi" and (url := _doi_url(row.get("value")))}
    if dois:
        return min(dois)
    urls = {url for row in _records(publication.get("source_records")) if (url := _safe_url(row.get("source_url")))}
    if urls:
        return min(urls)
    urls = {url for row in identifiers if (url := _safe_url(row.get("url")))}
    return min(urls) if urls else _safe_url(publication.get("openalex_url"))


def _year(publication: dict) -> int | None:
    value = publication.get("year")
    if isinstance(value, str) and re.fullmatch(r"\d{1,4}", value.strip()):
        value = int(value)
    if type(value) is int and 1 <= value <= 9999:
        return value
    try:
        return date.fromisoformat(publication.get("publication_date")).year
    except (TypeError, ValueError):
        return None


def _validate(snapshot: dict) -> None:
    for table, keys in _TABLE_KEYS.items():
        rows = snapshot.get(table)
        if not isinstance(rows, list):
            raise ValueError(f"snapshot requires table '{table}' as a list; use a complete graph snapshot")
        ids = set()
        for index, row in enumerate(rows):
            if not isinstance(row, dict):
                raise ValueError(f"snapshot table '{table}' row {index} must be an object")
            for key in keys:
                if not isinstance(row.get(key), str) or not row[key].strip():
                    raise ValueError(f"snapshot table '{table}' row {index} requires a non-empty string '{key}'")
            if table in {"persons", "publications", "departments"}:
                if row["id"] in ids:
                    raise ValueError(f"snapshot table '{table}' row {index} repeats an entity ID")
                ids.add(row["id"])


def collect_projects(snapshot: dict) -> tuple[list[Project], int]:
    """Return positively identified ITMO projects and the count of rejected URLs."""
    _validate(snapshot)
    itmo_persons = {row["id"] for row in snapshot["persons"] if row.get("is_itmo") is True}
    publications = {row["id"]: row for row in snapshot["publications"]}
    itmo_papers = {
        row["pid"] for row in snapshot["authorship"] if row["per"] in itmo_persons and row["pid"] in publications
    }
    repo_papers: dict[str, set[str]] = defaultdict(set)
    for row in snapshot["repo_pubs"]:
        if row["pid"] in publications:
            repo_papers[row["rid"]].add(row["pid"])
    eligible = {rid for rid, pids in repo_papers.items() if pids & itmo_papers}
    eligible.update(
        row["rid"] for row in snapshot["repo_persons"] if row["per"] in itmo_persons and row.get("role") == "owner"
    )
    departments = {row["id"] for row in snapshot["departments"]}
    eligible.update(row["rid"] for row in snapshot["repo_depts"] if row["did"] in departments)

    groups: dict[str, list[dict]] = defaultdict(list)
    skipped_urls = 0
    for row in snapshot["repositories"]:
        url = _safe_url(row.get("url"))
        if url is None:
            skipped_urls += row["id"] in eligible
            continue
        key = normalize_repo_url(url) if urlsplit(url).hostname.lower() in {"github.com", "www.github.com"} else url
        groups[key].append(row)

    projects = []
    for url, rows in sorted(groups.items()):
        if not any(row["id"] in eligible for row in rows):
            continue
        pids = {pid for row in rows for pid in repo_papers[row["id"]]}
        papers = [publications[pid] for pid in pids]
        years = [year for paper in papers if (year := _year(paper)) is not None]
        paper = min(
            papers,
            key=lambda pub: (
                not bool(_text(pub.get("title"))),
                _year(pub) or 10000,
                _text(pub.get("title")).casefold(),
                _text(pub.get("title")),
                _paper_url(pub) or "",
                pub["id"],
            ),
            default={},
        )
        repo = min(
            rows,
            key=lambda row: (
                not bool(_text(row.get("name"))),
                not bool(_text(row.get("description"))),
                _text(row.get("name")),
                _text(row.get("description")),
                row["id"],
            ),
        )
        name = _text(repo.get("name")) or urlsplit(url).path.rstrip("/").rsplit("/", 1)[-1] or "Repository"
        description = _text(repo.get("description"))
        if len(description) > 160:
            description = description[:159].rstrip() + "…"
        projects.append(
            Project(
                name=name,
                url=url,
                year=min(years) if years else None,
                title=_text(paper.get("title")),
                description=description,
                paper_url=_paper_url(paper),
            )
        )
    return projects, skipped_urls


def render_overview(projects: list[Project]) -> str:
    """Render a deterministic Markdown list, newest years first and undated last."""
    if not projects:
        return "Проекты не найдены.\n"
    groups: dict[int | None, list[Project]] = defaultdict(list)
    for project in projects:
        groups[project.year].append(project)
    sections = []
    for year in sorted(groups, key=lambda value: -(value or 0)):
        lines = [f"### {year if year is not None else 'Без года'}", ""]
        for project in sorted(
            groups[year],
            key=lambda item: (
                (item.title or item.name).casefold(),
                item.title or item.name,
                item.url,
            ),
        ):
            line = f"- [{_markdown(project.title or project.name)}]({project.url})"
            if project.title:
                line += f" — **{_markdown(project.name)}**"
            elif project.description:
                line += f" — {_markdown(project.description)}"
            if project.paper_url:
                line += f", [статья]({project.paper_url})"
            lines.append(line)
        sections.append("\n".join(lines))
    return "\n\n".join(sections) + "\n"


def replace_projects_block(template_text: str, overview: str) -> str:
    """Replace exactly one marked block, preserving all text outside its body."""
    matches = []
    for marker in (START_MARKER, END_MARKER):
        match = re.search(rf"(?m)^{re.escape(marker)}(?=\r?$)", template_text)
        if template_text.count(marker) != 1 or match is None:
            raise ValueError(f"template requires exactly one '{marker}' on its own line")
        matches.append(match)
    start, end = matches
    if start.end() >= end.start():
        raise ValueError("template project markers are in the wrong order")
    newline = "\r\n" if template_text.startswith("\r\n", start.end()) else "\n"
    body = overview.rstrip("\r\n").replace("\r\n", "\n").replace("\n", newline)
    return template_text[: start.end()] + newline * 2 + body + newline * 2 + template_text[end.start() :]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache", type=Path, help="Graph snapshot JSON (default: latest in settings.cache_dir)")
    output = parser.add_mutually_exclusive_group()
    output.add_argument("--output", type=Path, help="Write only the Markdown catalog to this file")
    output.add_argument(
        "--template", type=Path, help="Update the marked catalog in this existing Markdown file in place"
    )
    args = parser.parse_args(argv)
    try:
        cache = (args.cache or latest_snapshot(settings.cache_dir)).resolve()
        target = args.template or args.output
        if target is not None:
            target = target.resolve()
            if target == cache or (target.exists() and target.samefile(cache)):
                raise ValueError("output/template must not be the input snapshot")
        projects, skipped_urls = collect_projects(read_snapshot(cache))
        result = render_overview(projects)
        original = None
        if args.template:
            with target.open(encoding="utf-8", newline="") as handle:
                original = handle.read()
            result = replace_projects_block(original, result)
        if target is None:
            sys.stdout.write(result)
        elif result != original:
            with AtomicWriter(target) as handle:
                handle.write(result)
        print(f"Snapshot: {cache}; projects: {len(projects)}; skipped invalid URLs: {skipped_urls}", file=sys.stderr)
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Resolve curated titles that the graph has never heard of to OpenAlex IDs.

`import_curated_repos.py` refuses to invent a Publication from a title alone,
so a curated row whose paper is absent lands in its report and nothing more
happens. This script closes that gap from the other end: it asks OpenAlex
which work the title names, and writes the ids out in the one format the
pipeline already accepts — `pauk collect --works-file`, one id per line.

Nothing here writes to a database. The output is a list to be read by a human
and then fed to the ordinary pipeline, which is what actually creates the
publication, its authors and its departments.

Matching a title to a work is the whole risk, so a candidate is accepted only
on three-part evidence: the normalized titles agree almost exactly, *and*
either an author surname from the curated note appears among the authorships
or the publication year matches the note. A single strong signal is not
enough — "Fast gene set enrichment analysis" and "An algorithm for fast
preranked gene set enrichment analysis" are different papers by the same
people in the same year.

The ITMO affiliation is recorded but never required. Its absence is the
answer to "why is this paper missing", not a reason to skip: the pipeline
collects by `authorships.institutions.ror`, so a work OpenAlex does not
attribute to ITMO was never in scope. Such a work can still be collected by
explicit id — but every one of its authors will normalize to
`Person:External` and it will attach to no department, so it is worth
deciding about separately. `--include` does that filtering.
"""

from __future__ import annotations

import argparse
import difflib
import json
import logging
import re
import sys
import time
from collections import Counter
from datetime import date
from pathlib import Path

from pymongo.database import Database

from pauk.settings import Settings, settings
from pauk.sources import OpenAlexClient
from pauk.storage.mongo import get_mongo_client

sys.path.insert(0, str(Path(__file__).resolve().parent))
from import_curated_repos import (  # noqa: E402  - sibling script, path set above
    classify,
    load_publications,
    normalize_title,
    read_csv,
)

logger = logging.getLogger("resolve_missing_works")

ITMO_ROR = "04txgxn49"

# The graph is collected from this date onward; anything earlier is absent by
# construction, not by accident.
COLLECTION_START_YEAR = 2020

# Below this the titles are different papers, not one paper written twice.
TITLE_CUTOFF = 0.95

# The curated note leads with author surnames ("Borisova, Nikitin; IJCNN 2025").
SURNAME = re.compile(r"\b([A-ZА-ЯЁ][a-zа-яё]{2,})\b")
YEAR = re.compile(r"\b(19[5-9]\d|20[0-4]\d)\b")

# OpenAlex filter values are comma-separated, so a comma inside the title ends
# the filter early; the other punctuation confuses the search tokenizer.
FILTER_PUNCT = re.compile(r'[,:;"()\[\]]')


def _note_evidence(note: str) -> tuple[set[str], int | None]:
    """Surnames and year the curated note offers to corroborate a match."""
    head = note.split(";")[0]
    years = YEAR.findall(note)
    return {s.lower() for s in SURNAME.findall(head)}, int(years[0]) if years else None


def _is_itmo(work: dict) -> bool:
    for authorship in work.get("authorships") or []:
        for institution in authorship.get("institutions") or []:
            if ITMO_ROR in f"{institution.get('ror') or ''}{institution.get('id') or ''}":
                return True
    return False


def _candidates(client: OpenAlexClient, title: str) -> list[dict]:
    """Works OpenAlex offers for this title, precise filter first.

    `title.search` matches the title field alone and is the right question to
    ask; the general `search` also reads abstract and fulltext, which is why
    it is only a fallback — it answers for almost anything.
    """
    query = FILTER_PUNCT.sub(" ", title).strip()
    params = {"per_page": 5}
    if settings.openalex_api_key:
        params["api_key"] = settings.openalex_api_key
    page = client.get_json(client.WORKS_URL, params={**params, "filter": f"title.search:{query}"})
    results = page.get("results") or []
    if results:
        return results
    page = client.get_json(client.WORKS_URL, params={**params, "search": query})
    return page.get("results") or []


def _best_match(client: OpenAlexClient, row: dict) -> dict:
    """The strongest candidate for one curated row, with the evidence for it."""
    surnames, want_year = _note_evidence(row.get("note") or "")
    title = row["title"]
    try:
        candidates = _candidates(client, title)
    except Exception as exc:  # noqa: BLE001 - recorded per row, never fatal
        return {"verdict": "api_error", "error": f"{type(exc).__name__}: {exc}"}

    best: dict | None = None
    for work in candidates:
        ratio = difflib.SequenceMatcher(
            None, normalize_title(title), normalize_title(work.get("title") or "")).ratio()
        authors = " ".join(
            (a.get("author") or {}).get("display_name") or "" for a in work.get("authorships") or []
        ).lower()
        surname_hit = any(s in authors for s in surnames)
        year_hit = want_year is not None and work.get("publication_year") == want_year
        rank = (ratio, surname_hit, year_hit)
        if best is None or rank > best["_rank"]:
            best = {
                "_rank": rank,
                "openalex_id": (work.get("id") or "").rsplit("/", 1)[-1],
                "openalex_title": work.get("title"),
                "year": work.get("publication_year"),
                "doi": work.get("doi"),
                "ratio": round(ratio, 4),
                "surname_hit": surname_hit,
                "year_hit": year_hit,
                "itmo_affiliation": _is_itmo(work),
            }

    if best is None:
        return {"verdict": "no_candidate"}
    best.pop("_rank")
    if best["ratio"] >= TITLE_CUTOFF and (best["surname_hit"] or best["year_hit"]):
        best["verdict"] = "resolved"
    elif best["ratio"] >= TITLE_CUTOFF:
        best["verdict"] = "title_only"
    else:
        best["verdict"] = "no_match"
    return best


def _selected_ids(resolved: list[dict], include: str) -> list[dict]:
    """The subset to hand the pipeline, by the caller's inclusion rule."""
    rows = [r for r in resolved if r["verdict"] == "resolved"]
    if include == "itmo":
        return [r for r in rows if r["itmo_affiliation"]]
    if include == "since-2020":
        return [r for r in rows if (r.get("year") or 0) >= COLLECTION_START_YEAR]
    return rows


def _cell(text) -> str:
    return str(text or "").replace("|", "\\|").replace("\n", " ")


def write_report(path: Path, resolved: list[dict], selected: list[dict], include: str) -> None:
    counts = Counter(r["verdict"] for r in resolved)
    chosen = {r["csv_title"] for r in selected}
    lines = [
        "# Статьи с кодом, которых нет в графе — резолв в OpenAlex",
        "",
        f"Сформировано {date.today().isoformat()}. Правило отбора: `--include {include}`.",
        "",
        f"- строк без пары в графе: **{len(resolved)}**",
        f"- уверенно сопоставлено с OpenAlex: **{counts['resolved']}**",
        f"- отобрано для сбора: **{len(selected)}**",
        "",
        "Сопоставление принимается только при совпадении заголовка "
        f"не ниже {TITLE_CUTOFF} И подтверждении фамилией автора из примечания "
        "или годом. Одного сильного признака мало.",
        "",
        "## Отобрано для сбора",
        "",
        "| OpenAlex | Год | ИТМО | Заголовок | Совпадение |",
        "|---|---|---|---|---|",
    ]
    for r in sorted(selected, key=lambda x: (x.get("year") or 0)):
        lines.append(
            f"| `{r['openalex_id']}` | {r.get('year') or '—'} "
            f"| {'да' if r['itmo_affiliation'] else 'нет'} | {_cell(r['csv_title'])[:80]} "
            f"| {r['ratio']} |")
    lines.append("")

    rest = [r for r in resolved if r["csv_title"] not in chosen]
    titles = {
        "resolved": "Найдено, но не отобрано текущим правилом",
        "title_only": "Заголовок совпал, но подтверждения нет",
        "no_match": "Лучший кандидат — другая работа",
        "no_candidate": "OpenAlex не вернул ничего",
        "api_error": "Ошибка обращения к OpenAlex",
    }
    for verdict in ("resolved", "title_only", "no_match", "no_candidate", "api_error"):
        group = [r for r in rest if r["verdict"] == verdict]
        if not group:
            continue
        lines += [f"## {titles[verdict]} — {len(group)}", ""]
        if verdict == "resolved":
            lines += ["Работа в OpenAlex есть, но она не проходит по правилу отбора — "
                      "у неё нет аффилиации ИТМО (значит, пайплайн её никогда и не "
                      "собирал бы) либо она вне окна по годам.", ""]
        if verdict == "no_candidate":
            lines += ["Как правило это диссертации, тезисы конгрессов и русскоязычные "
                      "сборники — OpenAlex их не индексирует.", ""]
        lines += ["| Заголовок в CSV | Что предложил OpenAlex | Год | ИТМО | Совпадение |",
                  "|---|---|---|---|---|"]
        for r in sorted(group, key=lambda x: -(x.get("ratio") or 0)):
            lines.append(
                f"| {_cell(r['csv_title'])[:70]} | {_cell(r.get('openalex_title'))[:60]} "
                f"| {r.get('year') or '—'} | {'да' if r.get('itmo_affiliation') else 'нет'} "
                f"| {r.get('ratio', '—')} |")
        lines.append("")

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")
    logger.info("report written: %s", path)


def command_resolve(args, config: Settings, db: Database) -> None:
    rows = read_csv(args.csv)
    by_title = load_publications(db)
    # classify() checks the publication before it checks confidence, so this
    # picks up every row with a repository whose paper is absent, whatever
    # trust the curator assigned it.
    _selected, rejected = classify(rows, by_title, {"high"})
    missing = [r for r in rejected if r["reason"] == "publication_not_in_graph"]
    logger.info("rows with a repository but no publication in the graph: %d", len(missing))

    client = OpenAlexClient(config.request_timeout, config.openalex_api_key)
    resolved = []
    for index, row in enumerate(missing, 1):
        match = _best_match(client, row)
        resolved.append({"csv_title": row["title"], "note": row["note"],
                         "repo_url": row["repo_url"], "confidence": row["confidence"], **match})
        logger.info("%d/%d %-12s %s", index, len(missing), match["verdict"], row["title"][:60])
        time.sleep(args.delay)

    selected = _selected_ids(resolved, args.include)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text("".join(f"{r['openalex_id']}\n" for r in selected), encoding="utf-8")
    args.json.write_text(json.dumps(
        {"created_at": date.today().isoformat(), "include": args.include, "rows": resolved},
        ensure_ascii=False, indent=2), encoding="utf-8")
    write_report(args.report, resolved, selected, args.include)

    counts = Counter(r["verdict"] for r in resolved)
    print(f"\n{len(missing)} row(s) examined: " + ", ".join(f"{k}={v}" for k, v in counts.most_common()))
    print(f"selected for collection ({args.include}): {len(selected)} -> {args.out}")
    print(f"  with ITMO affiliation: {sum(1 for r in selected if r['itmo_affiliation'])}")
    print(f"  before {COLLECTION_START_YEAR}: {sum(1 for r in selected if (r.get('year') or 0) < COLLECTION_START_YEAR)}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--csv", type=Path, default=Path("itmo-github-repos.csv"))
    parser.add_argument("--out", type=Path, default=Path("data/reports/missing-works.txt"),
                        help="id list for `pauk collect --works-file`")
    parser.add_argument("--json", type=Path, default=Path("data/reports/missing-works-resolved.json"))
    parser.add_argument("--report", type=Path, default=Path("data/reports/missing-works.md"))
    parser.add_argument("--include", choices=("itmo", "since-2020", "all"), default="itmo",
                        help="which resolved works go into the id list (default: itmo)")
    parser.add_argument("--delay", type=float, default=0.12, help="seconds between OpenAlex calls")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    client = get_mongo_client(settings)
    try:
        command_resolve(args, settings, client[settings.mongo_db])
    finally:
        client.close()


if __name__ == "__main__":
    sys.exit(main())

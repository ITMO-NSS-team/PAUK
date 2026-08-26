"""Import a hand-curated `title,repo_url` list into the prepared repositories.

The list pairs a paper with the repository holding its code — a judgement a
human made, not something the pipeline can find in the text. It therefore
becomes `Repository.publication_ids`, which the graph loader turns into
`(:Repository)-[:IMPLEMENTS]->(:Publication)`. It deliberately does *not*
become `MENTIONS_LINK`: that edge claims the URL was found inside the
publication, and these were not.

Two steps, on purpose. `plan` only reads — it matches titles against the
publications already in Mongo, asks GitHub for the repository metadata, and
writes the result to a JSON file plus a report of everything it refused to
touch. `apply` writes that reviewed file into Mongo and nothing else. What
gets written is therefore exactly what was reviewed, and a re-run of `apply`
cannot silently pick up a different match.

A paper the graph has never heard of is never created here: the CSV carries a
title and nothing else, and a Publication invented from a title alone would
have no OpenAlex id to ever reconcile with. Those rows go to the report.
"""

from __future__ import annotations

import argparse
import csv
import difflib
import json
import logging
import re
import sys
import unicodedata
from collections import defaultdict
from datetime import UTC, date, datetime
from pathlib import Path

from pymongo.database import Database

from pauk.models import Repository
from pauk.models.processing import ProcessingState, ProcessingStatus
from pauk.settings import Settings, settings
from pauk.sources.github import GitHubClient
from pauk.storage import PreparedStore
from pauk.storage.mongo import get_mongo_client

logger = logging.getLogger("import_curated_repos")

# Only `owner/name` is a repository. The list also holds organisation pages
# and GitHub Pages sites, which address no repository and are reported instead.
REPO_URL = re.compile(r"^https?://github\.com/([^/\s]+)/([^/\s]+?)(?:\.git)?/?$", re.I)

# Above this ratio two titles are the same paper with a typo on one side
# ("Bayesain", "ofquantum" are both real cases in the current data). Below it
# they start being different papers that share a topic, so the row is reported
# rather than guessed at. Every fuzzy match is listed in the plan for review.
FUZZY_CUTOFF = 0.90

STAGE = "repositories"


def normalize_title(title: str) -> str:
    """Fold a title to what two records of the same paper always share.

    Case, punctuation and whitespace differ freely between a curated list and
    OpenAlex; letters and digits do not.
    """
    folded = unicodedata.normalize("NFKD", title or "").lower()
    return " ".join(re.sub(r"[^0-9a-zа-яё]+", " ", folded).split())


def repo_id_for(owner: str, name: str) -> str:
    return f"github_{owner.lower()}_{name.lower()}"


def read_csv(path: Path) -> list[dict]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def load_publications(db: Database) -> dict[str, list[dict]]:
    """Normalized title -> the publications carrying it.

    A title matching more than one publication is not resolved here: the
    curated row says nothing that could pick between them, so it is reported.
    """
    by_title: dict[str, list[dict]] = defaultdict(list)
    for doc in db.publications.find({}, {"_id": 0, "id": 1, "title": 1, "year": 1}):
        key = normalize_title(doc.get("title", ""))
        if key:
            by_title[key].append(doc)
    return by_title


def classify(rows: list[dict], by_title: dict[str, list[dict]],
             confidences: set[str]) -> tuple[list[dict], list[dict]]:
    """Split the CSV into rows to import and rows to report.

    Returns (selected, rejected); every input row lands in exactly one, so the
    report accounts for the whole file.
    """
    keys = list(by_title)
    selected: list[dict] = []
    rejected: list[dict] = []
    for row in rows:
        url = (row.get("repo_url") or "").strip()
        title = (row.get("title") or "").strip()
        confidence = (row.get("confidence") or "").strip()
        record = {"title": title, "repo_url": url, "confidence": confidence,
                  "note": (row.get("note") or "").strip()}

        match = REPO_URL.match(url)
        if not url or url.lower() == "none":
            rejected.append(record | {"reason": "no_repo_url"})
            continue
        if not match:
            rejected.append(record | {"reason": "not_a_repository_url"})
            continue

        key = normalize_title(title)
        hits = by_title.get(key, [])
        match_kind = "exact"
        if len(hits) > 1:
            rejected.append(record | {"reason": "ambiguous_title",
                                      "publication_ids": [h["id"] for h in hits]})
            continue
        if not hits:
            close = difflib.get_close_matches(key, keys, n=1, cutoff=FUZZY_CUTOFF)
            if not close:
                rejected.append(record | {"reason": "publication_not_in_graph"})
                continue
            hits = by_title[close[0]]
            match_kind = "fuzzy"
            if len(hits) > 1:
                rejected.append(record | {"reason": "ambiguous_title",
                                          "publication_ids": [h["id"] for h in hits]})
                continue

        # Confidence is checked last so the report can say *why* a low-trust
        # row was skipped even when everything else about it was fine.
        if confidence not in confidences:
            rejected.append(record | {"reason": f"confidence_{confidence or 'missing'}",
                                      "publication_id": hits[0]["id"]})
            continue

        owner, name = match.group(1), match.group(2)
        selected.append(record | {
            "owner": owner,
            "name": name,
            "repo_id": repo_id_for(owner, name),
            "publication_id": hits[0]["id"],
            "publication_title": hits[0].get("title"),
            "match": match_kind,
            "ratio": round(difflib.SequenceMatcher(None, key, normalize_title(hits[0]["title"])).ratio(), 4),
        })
    return selected, rejected


def _license_of(payload: dict) -> str | None:
    """SPDX id of the repository licence, or None when GitHub found none.

    GitHub answers an explicit `"license": null` for unlicensed repositories
    and `"spdx_id": "NOASSERTION"` for one it cannot identify; neither is a
    licence name worth storing.
    """
    licence = payload.get("license") or {}
    spdx = licence.get("spdx_id")
    return spdx if spdx and spdx != "NOASSERTION" else None


def _pushed_date(payload: dict) -> date | None:
    stamp = payload.get("pushed_at") or payload.get("updated_at")
    if not stamp:
        return None
    return datetime.fromisoformat(stamp.replace("Z", "+00:00")).date()


def fetch_metadata(client: GitHubClient, selected: list[dict]) -> dict[str, dict]:
    """One GitHub call per distinct repository, keyed by the CSV-derived id.

    A repository is cited by several papers in this list; the API must be paid
    for once. A failure is recorded rather than raised so one dead repository
    does not cost the other sixty.
    """
    payloads: dict[str, dict] = {}
    repos = {row["repo_id"]: (row["owner"], row["name"]) for row in selected}
    for index, (repo_id, (owner, name)) in enumerate(sorted(repos.items()), 1):
        logger.info("github %d/%d %s/%s", index, len(repos), owner, name)
        try:
            payload = client.get_repository(owner, name)
            payloads[repo_id] = {"ok": True, "payload": payload,
                                 "has_readme": client.has_readme(owner, name)}
        except Exception as exc:  # noqa: BLE001 - recorded, reported, not fatal
            logger.warning("github %s/%s failed: %s", owner, name, exc)
            payloads[repo_id] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    return payloads


def build_documents(db: Database, selected: list[dict],
                    payloads: dict[str, dict]) -> tuple[list[dict], list[dict]]:
    """Merge the curated pairs and the GitHub metadata onto existing rows.

    Keyed by the *canonical* id built from the payload, not from the cited URL:
    GitHub redirects a renamed repository, so `nccr-itmo/FEDOT` answers as
    `aimclub/FEDOT` and must land on the row that already holds it — otherwise
    two rows would claim one URL and the graph's uniqueness constraint would
    reject the publish.

    Existing rows are extended, never replaced: publication ids and cited URLs
    are unioned into what is already there.
    """
    existing = {doc["id"]: doc for doc in db.repositories.find({}, {"_id": 0})}
    built: dict[str, Repository] = {}
    failures: list[dict] = []

    for row in selected:
        result = payloads.get(row["repo_id"], {})
        if not result.get("ok"):
            failures.append({**row, "reason": "github_fetch_failed",
                             "error": result.get("error", "not fetched")})
            continue
        payload = result["payload"]
        owner_login = (payload.get("owner") or {}).get("login") or row["owner"]
        canonical_id = repo_id_for(owner_login, payload.get("name") or row["name"])
        cited_url = f"https://github.com/{row['owner']}/{row['name']}"

        repo = built.get(canonical_id)
        if repo is None:
            source = existing.get(canonical_id) or existing.get(row["repo_id"])
            repo = Repository.model_validate(source) if source else Repository(
                id=canonical_id, name=payload.get("name") or row["name"],
                url=payload.get("html_url") or cited_url)
            repo.id = canonical_id
            built[canonical_id] = repo

        # Metadata from the payload wins over whatever an earlier run stored:
        # this call is the fresher fact about the repository.
        repo.name = payload.get("name") or repo.name
        repo.url = payload.get("html_url") or repo.url
        repo.github_id = payload.get("id") or repo.github_id
        repo.description = payload.get("description") or repo.description
        repo.stars_num = payload.get("stargazers_count")
        repo.has_readme = bool(result.get("has_readme"))
        repo.license = _license_of(payload) or repo.license
        repo.last_updated = _pushed_date(payload) or repo.last_updated
        repo.owner_login = owner_login
        repo.access_date = date.today()

        if row["publication_id"] not in repo.publication_ids:
            repo.publication_ids.append(row["publication_id"])
        for url in (cited_url, repo.url):
            if url and url not in repo.cited_urls:
                repo.cited_urls.append(url)

        # The graph loader skips a repository row whose `repositories` stage is
        # recorded as failed. These rows were just fetched successfully, so the
        # state is written to say so.
        state = repo.processing.get(STAGE)
        repo.processing[STAGE] = ProcessingState(
            status=ProcessingStatus.COMPLETED,
            attempts=(state.attempts if state else 0) + 1,
            finished_at=datetime.now(UTC),
            result_count=1,
        )

    documents = []
    for repo in built.values():
        before = existing.get(repo.id)
        documents.append({
            "id": repo.id,
            "is_new": before is None,
            "added_publication_ids": sorted(
                set(repo.publication_ids) - set((before or {}).get("publication_ids") or [])),
            "document": repo.model_dump(mode="json", by_alias=True),
        })
    return sorted(documents, key=lambda d: d["id"]), failures


def write_report(path: Path, rejected: list[dict], failures: list[dict],
                 selected: list[dict], documents: list[dict]) -> None:
    """Everything the import refused to do, and why."""
    by_reason: dict[str, list[dict]] = defaultdict(list)
    for row in rejected:
        by_reason[row["reason"]].append(row)
    for row in failures:
        by_reason[row["reason"]].append(row)

    titles = {
        "publication_not_in_graph": "Статьи нет в графе",
        "ambiguous_title": "Заголовок совпал с несколькими публикациями",
        "no_repo_url": "В строке нет ссылки на репозиторий",
        "not_a_repository_url": "Ссылка не адресует репозиторий",
        "github_fetch_failed": "GitHub не отдал репозиторий",
    }
    lines = [
        "# Строки, не попавшие в граф",
        "",
        f"Сформировано {date.today().isoformat()} из `itmo-github-repos.csv`.",
        "",
        f"- строк в файле: **{len(selected) + len(rejected)}**",
        f"- импортировано пар «статья → репозиторий»: **{len(selected) - len(failures)}**",
        f"- затронуто репозиториев: **{len(documents)}** "
        f"(новых {sum(1 for d in documents if d['is_new'])})",
        f"- не импортировано строк: **{len(rejected) + len(failures)}**",
        "",
        "Ни одна из перечисленных ниже строк граф не меняла.",
        "",
    ]
    for reason, rows in sorted(by_reason.items(), key=lambda item: -len(item[1])):
        heading = titles.get(reason) or (
            f"Уровень доверия `{reason.removeprefix('confidence_')}`"
            if reason.startswith("confidence_") else reason)
        lines += [f"## {heading} — {len(rows)}", ""]
        if reason == "publication_not_in_graph":
            lines += ["Репозиторий известен, но публикации с таким заголовком в базе нет. "
                      "Чтобы связать, статью сначала нужно собрать пайплайном.", ""]
        lines += ["| Заголовок | Репозиторий | Доверие | Примечание |",
                  "|---|---|---|---|"]
        for row in sorted(rows, key=lambda r: r["title"]):
            note = row.get("error") or row.get("note") or ""
            url = row.get("repo_url") or "—"
            lines.append(
                f"| {_cell(row['title'])} | {_cell(url)} | {row.get('confidence', '')} "
                f"| {_cell(note)} |")
        lines.append("")

    fuzzy = [row for row in selected if row["match"] == "fuzzy"]
    if fuzzy:
        lines += ["## Импортировано по неточному совпадению заголовка", "",
                  "Заголовок в CSV и в базе различаются — совпадение выше "
                  f"{FUZZY_CUTOFF}. Стоит проглядеть глазами.", "",
                  "| Заголовок в CSV | Заголовок в базе | ID | Совпадение |",
                  "|---|---|---|---|"]
        for row in sorted(fuzzy, key=lambda r: r["ratio"]):
            lines.append(f"| {_cell(row['title'])} | {_cell(row['publication_title'])} "
                         f"| {row['publication_id']} | {row['ratio']} |")
        lines.append("")

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")
    logger.info("report written: %s", path)


def _cell(text: str) -> str:
    return (text or "").replace("|", "\\|").replace("\n", " ")


def command_plan(args, config: Settings, db: Database) -> None:
    rows = read_csv(args.csv)
    logger.info("csv rows: %d", len(rows))
    by_title = load_publications(db)
    logger.info("publications in mongo: %d distinct titles", len(by_title))

    selected, rejected = classify(rows, by_title, set(args.confidence))
    logger.info("selected %d pair(s), rejected %d row(s)", len(selected), len(rejected))

    client = GitHubClient(config.request_timeout, config.github_token)
    payloads = fetch_metadata(client, selected)
    documents, failures = build_documents(db, selected, payloads)

    plan = {
        "created_at": datetime.now(UTC).isoformat(),
        "csv": str(args.csv),
        "group": args.group,
        "confidence": sorted(args.confidence),
        "selected": selected,
        "failures": failures,
        "documents": documents,
    }
    args.plan.parent.mkdir(parents=True, exist_ok=True)
    args.plan.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info("plan written: %s", args.plan)
    write_report(args.report, rejected, failures, selected, documents)

    new = sum(1 for doc in documents if doc["is_new"])
    print(f"\nplan: {len(documents)} repository row(s) — {new} new, {len(documents)-new} updated")
    print(f"      {sum(len(d['added_publication_ids']) for d in documents)} new IMPLEMENTS pair(s)")
    print(f"      {len(rejected) + len(failures)} row(s) reported, nothing written")


def command_apply(args, config: Settings, db: Database) -> None:
    plan = json.loads(args.plan.read_text(encoding="utf-8"))
    documents = plan["documents"]
    group = args.group or plan["group"]
    if not documents:
        print("plan holds no documents; nothing to do")
        return

    print(f"about to write {len(documents)} repository row(s) into "
          f"{db.name}.repositories, group {group!r}")
    if not args.yes and input("type 'write' to continue: ").strip() != "write":
        print("aborted")
        return

    # upsert_models, not write_models: this run is not the authority on which
    # repositories the group contains, only on the rows it touched. Every
    # replaced document is snapshotted into `revisions` by the store itself.
    store = PreparedStore(db, group)
    store.upsert_models("repositories", [Repository.model_validate(doc["document"])
                                         for doc in documents])
    logger.info("wrote %d repository row(s) to group %s", len(documents), group)
    print(f"done. next: uv run python -m pauk.cli publish graph --group {group}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--csv", type=Path, default=Path("itmo-github-repos.csv"))
    parser.add_argument("--plan", type=Path, default=Path("data/reports/curated-repos-plan.json"))
    parser.add_argument("--report", type=Path,
                        default=Path("data/reports/curated-repos-unmatched.md"))
    parser.add_argument("--group", default=f"curated-repos-{date.today().isoformat()}")
    parser.add_argument("--confidence", nargs="+", default=["high"],
                        help="which CSV confidence levels to import (default: high)")
    parser.add_argument("--yes", action="store_true", help="skip the apply confirmation")
    parser.add_argument("command", choices=("plan", "apply"))
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    client = get_mongo_client(settings)
    try:
        db = client[settings.mongo_db]
        if args.command == "plan":
            command_plan(args, settings, db)
        else:
            command_apply(args, settings, db)
    finally:
        client.close()


if __name__ == "__main__":
    sys.exit(main())

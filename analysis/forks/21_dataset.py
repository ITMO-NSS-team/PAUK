"""Build the predictive analytics dataset from the fork analysis outputs."""

from __future__ import annotations

import csv
import json
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
OUT = ROOT / "out"

PANEL_PATH = OUT / "panel.json"
FORKS_PATH = OUT / "forks_classified.json"
OWNERSHIP_PATH = DATA / "repo_ownership.json"
PUBLICATIONS_PATH = DATA / "publications.jsonl"
IMPLEMENTS_PATH = DATA / "implements.jsonl"
AUTHORED_PATH = DATA / "authored.jsonl"
PERSON_DEPARTMENTS_PATH = DATA / "person_departments.jsonl"
MENTIONS_PATH = DATA / "mentions.jsonl"
REPO_LINKS_PATH = DATA / "repo_links.jsonl"
GRAPH_PERSONS_GITHUB_PATH = DATA / "graph_persons_github.jsonl"
FORK_OWNERS_PATH = DATA / "fork_owners.json"
FORK_COMPARE_PATH = DATA / "fork_compare.json"
FORKS_BY_REPO_PATH = DATA / "forks_by_repo.json"
GRAPH_PROFILES_PATH = DATA / "graph_profiles.jsonl"
GITHUB_PROFILES_PATH = DATA / "github_profiles.jsonl"
CSV_PATH = OUT / "analytics_dataset.csv"
README_PATH = OUT / "analytics_dataset.md"

CHANGED = frozenset({"derivative", "modified", "contribution"})
INDEPENDENT = frozenset({"external", "itmo_other_team", "unknown"})
AMBIGUOUS_REASON = "ambiguous_url_extraction"
SNAPSHOT_DATE = date(2026, 11, 20)

PANEL_FEATURES = (
    "created",
    "forks",
    "changed_forks",
    "derivative_forks",
    "independent_derivative_forks",
)
REPO_FEATURES = (
    "created_at",
    "last_updated",
    "stars_num",
    "forks_num",
    "is_fork",
    "archived",
    "has_readme",
    "language",
    "license",
    "owner_login",
    "owner_type",
    "implements_count",
    "contributors_count",
    "cited_urls_count",
    "itmo_source_count",
    "owner_profile_company_present",
    "owner_profile_location_present",
    "owner_profile_name_present",
    "owner_github_profile_fetched",
    "owner_github_repo_count",
    "owner_github_email_count",
    "owner_github_commit_name_count",
    "owner_github_group_count",
    "upstream_github_forks_count",
    "upstream_github_pages",
    "upstream_github_api_ok",
    "upstream_pushed_lag_days",
    "repo_is_itmo",
    "repo_access_lag_days",
    "repo_last_update_lag_days",
)
GRAPH_FEATURES = (
    "publication_count_to_year",
    "publication_first_year",
    "publication_last_year",
    "publication_year_span",
    "publication_has_code_share",
    "author_count_to_year",
    "author_department_count_to_year",
    "author_with_department_share",
    "mention_count_to_year",
    "relevant_mention_count_to_year",
    "relevant_mention_share",
    "false_mention_share",
    "null_mention_share",
    "ambiguous_mention_count_to_year",
    "llm_confidence_mean",
    "publication_link_count_to_year",
    "relevant_publication_link_share",
    "github_publication_link_share",
    "author_github_match_share",
    "publication_doi_share",
    "mention_first_year",
    "mention_last_year",
    "mention_candidate_urls_mean",
)
FORK_FEATURES = (
    "fork_ahead_by_sum",
    "fork_behind_by_sum",
    "fork_own_commits_sum",
    "fork_files_total_sum",
    "fork_code_additions_sum",
    "fork_functional_additions_sum",
    "fork_added_files_sum",
    "fork_pr_count_sum",
    "fork_pr_merged_count",
    "fork_pr_merge_rate",
    "fork_pr_days_to_merge_mean",
    "fork_independent_count",
    "fork_bot_count",
    "fork_owner_followers_mean",
    "fork_owner_public_repos_mean",
    "fork_owner_account_age_days_mean",
    "fork_owner_is_org_count",
    "fork_size_kb_mean",
    "fork_stars_num_mean",
    "fork_language_diversity",
    "fork_age_days_mean",
    "fork_days_to_last_push_mean",
    "fork_total_commits_sum",
    "fork_commit_authors_mean",
    "fork_compare_status_ok_count",
    "fork_pr_open_count_sum",
    "fork_pr_closed_unmerged_count_sum",
    "fork_owner_bio_present_mean",
    "fork_owner_blog_present_mean",
    "fork_owner_email_present_mean",
    "fork_owner_api_ok_count",
    "fork_first_own_commit_lag_days_mean",
    "fork_commit_recency_days_mean",
)
FORK_CUMULATIVE_FEATURES = (
    "forks_to_year",
    "changed_forks_to_year",
    "derivative_forks_to_year",
    "independent_derivative_forks_to_year",
)
TARGETS = (
    "target_derivative_next_year",
    "target_any_changed_next_year",
    "target_contribution_next_year",
    "target_pr_merged_next_year",
)
FIELDNAMES = (
    "repo_id",
    "repo",
    "year",
    *PANEL_FEATURES,
    *REPO_FEATURES,
    *GRAPH_FEATURES,
    *FORK_FEATURES,
    *FORK_CUMULATIVE_FEATURES,
    *TARGETS,
)
MEAN_FORK_FEATURES = frozenset({
    "fork_pr_days_to_merge_mean",
    "fork_owner_followers_mean",
    "fork_owner_public_repos_mean",
    "fork_owner_account_age_days_mean",
    "fork_size_kb_mean",
    "fork_stars_num_mean",
    "fork_age_days_mean",
    "fork_days_to_last_push_mean",
    "fork_commit_authors_mean",
    "fork_owner_bio_present_mean",
    "fork_owner_blog_present_mean",
    "fork_owner_email_present_mean",
    "fork_first_own_commit_lag_days_mean",
    "fork_commit_recency_days_mean",
})


def load_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def load_lines(path: Path) -> list[dict]:
    with path.open(encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def as_int(value) -> int:
    if value is None or value == "":
        return 0
    return int(value)


def as_bool(value) -> int:
    return int(bool(value))


def as_text(value) -> str:
    return "" if value is None else str(value)


def mean(values: list[float]) -> float:
    return round(sum(values) / len(values), 3) if values else 0.0


def ratio(part: int, whole: int) -> float:
    return round(part / whole, 3) if whole else 0.0


def parse_day(value) -> date | None:
    text = str(value or "")
    if len(text) < 10 or text[4] != "-" or text[7] != "-":
        return None
    try:
        return date(int(text[:4]), int(text[5:7]), int(text[8:10]))
    except ValueError:
        return None


def age_days(born, at: date) -> int | None:
    day = parse_day(born)
    return None if day is None else max(0, (at - day).days)


def year_end(year: int) -> date:
    return min(date(year, 12, 31), SNAPSHOT_DATE)


def fork_year(row: dict) -> int | None:
    value = row.get("fork_year")
    return None if value is None else int(value)


def build_publication_index(pubs: list[dict]) -> tuple[dict, dict, dict]:
    years = {row["id"]: as_int(row.get("year")) for row in pubs}
    code = {row["id"]: as_bool(row.get("has_code")) for row in pubs}
    doi = {row["id"]: as_bool(row.get("doi")) for row in pubs}
    return years, code, doi


def build_repository_publications(implements: list[dict], pub_years: dict) -> dict:
    result = defaultdict(list)
    for row in implements:
        pub_id = row["publication_id"]
        year = pub_years.get(pub_id) or as_int(row.get("year"))
        result[row["repo_id"]].append((year, pub_id))
    return result


def build_mentions(mentions: list[dict], pub_years: dict) -> dict:
    result = defaultdict(list)
    for row in mentions:
        if row.get("target_label") != "Repository":
            continue
        year = pub_years.get(row["publication_id"], 9999)
        result[row["target_id"]].append((year, row))
    return result


def build_publication_links(links: list[dict]) -> dict:
    result = defaultdict(list)
    for row in links:
        result[row["publication_id"]].append(row)
    return result


def build_person_github(rows: list[dict]) -> set:
    return {row["id"] for row in rows if row.get("github")}


def build_fork_compares(rows: list[dict]) -> dict:
    result = {}
    for row in rows:
        key = (
            row.get("repo_id"),
            as_text(row.get("fork_owner")).casefold(),
            as_text(row.get("fork_name")).casefold(),
        )
        result[key] = row
    return result


def build_upstreams(rows: list[dict]) -> dict:
    return {row["repo_id"]: row for row in rows}


def graph_features(
    repo_id: str,
    year: int,
    publications: dict,
    mentions: dict,
    authors: dict,
    departments: dict,
    pub_code: dict,
    pub_doi: dict,
    pub_links: dict,
    github_ids: set,
) -> dict:
    pubs = [pub_id for pub_year, pub_id in publications.get(repo_id, []) if pub_year <= year]
    pub_years = [pub_year for pub_year, pub_id in publications.get(repo_id, []) if pub_year <= year]
    authors_set = set()
    for pub_id in pubs:
        authors_set.update(authors.get(pub_id, ()))
    author_departments = set()
    for person_id in authors_set:
        author_departments.update(departments.get(person_id, ()))

    items = [row for mention_year, row in mentions.get(repo_id, []) if mention_year <= year]
    mention_years = [mention_year for mention_year, _ in mentions.get(repo_id, []) if mention_year <= year]
    relevant = sum(1 for row in items if row.get("is_relevant") is True)
    false_mentions = sum(1 for row in items if row.get("is_relevant") is False)
    null_mentions = sum(1 for row in items if row.get("is_relevant") is None)
    ambiguous = sum(
        1
        for row in items
        if row.get("llm_reason") == AMBIGUOUS_REASON or len(row.get("candidate_urls") or []) > 1
    )
    confidences = [float(row["llm_confidence"]) for row in items if row.get("llm_confidence") is not None]
    links = [link for pub_id in pubs for link in pub_links.get(pub_id, ())]
    github_links = sum(1 for link in links if as_text(link.get("host")).casefold() == "github.com")
    github_authors = sum(1 for person_id in authors_set if person_id in github_ids)

    first_year = min(pub_years) if pub_years else 0
    last_year = max(pub_years) if pub_years else 0
    first_mention = min(mention_years) if mention_years else 0
    last_mention = max(mention_years) if mention_years else 0
    return {
        "publication_count_to_year": len(pubs),
        "publication_first_year": first_year,
        "publication_last_year": last_year,
        "publication_year_span": last_year - first_year,
        "publication_has_code_share": ratio(sum(pub_code.get(pub_id, 0) for pub_id in pubs), len(pubs)),
        "author_count_to_year": len(authors_set),
        "author_department_count_to_year": len(author_departments),
        "author_with_department_share": ratio(
            sum(1 for person_id in authors_set if departments.get(person_id)), len(authors_set)
        ),
        "mention_count_to_year": len(items),
        "relevant_mention_count_to_year": relevant,
        "relevant_mention_share": ratio(relevant, len(items)),
        "false_mention_share": ratio(false_mentions, len(items)),
        "null_mention_share": ratio(null_mentions, len(items)),
        "ambiguous_mention_count_to_year": ambiguous,
        "llm_confidence_mean": mean(confidences),
        "publication_link_count_to_year": len(links),
        "relevant_publication_link_share": ratio(
            sum(1 for link in links if link.get("is_relevant") is True), len(links)
        ),
        "github_publication_link_share": ratio(github_links, len(links)),
        "author_github_match_share": ratio(github_authors, len(authors_set)),
        "publication_doi_share": ratio(sum(pub_doi.get(pub_id, 0) for pub_id in pubs), len(pubs)),
        "mention_first_year": first_mention,
        "mention_last_year": last_mention,
        "mention_candidate_urls_mean": mean([len(row.get("candidate_urls") or []) for row in items]),
    }


def build_fork_details(rows: list[dict]) -> dict:
    result = {}
    for upstream in rows:
        for item in upstream.get("forks") or []:
            key = (
                upstream.get("repo_id"),
                as_text(item.get("owner")).casefold(),
                as_text(item.get("name")).casefold(),
            )
            result[key] = item
    return result


def fork_aggregates(forks: list[dict], owners: dict, details: dict, compares: dict) -> dict:
    result = defaultdict(lambda: defaultdict(list))
    for row in forks:
        year = fork_year(row)
        if year is None:
            continue
        key = (row["repo_id"], year)
        values = result[key]
        values["fork_ahead_by_sum"].append(as_int(row.get("ahead_by")))
        values["fork_behind_by_sum"].append(as_int(row.get("behind_by")))
        values["fork_own_commits_sum"].append(as_int(row.get("own_commits")))
        values["fork_files_total_sum"].append(as_int(row.get("files_total")))
        values["fork_code_additions_sum"].append(as_int(row.get("code_additions")))
        values["fork_functional_additions_sum"].append(as_int(row.get("functional_additions")))
        values["fork_added_files_sum"].append(as_int(row.get("added_files")))
        pulls = row.get("prs") or []
        values["fork_pr_count_sum"].append(len(pulls))
        values["fork_pr_merged_count"].append(as_bool(row.get("pr_merged")))
        values["fork_pr_open_count_sum"].append(sum(1 for pull in pulls if pull.get("state") == "open"))
        values["fork_pr_closed_unmerged_count_sum"].append(
            sum(1 for pull in pulls if pull.get("state") == "closed" and not pull.get("merged_at"))
        )
        values["fork_independent_count"].append(int(row.get("independence") in INDEPENDENT))
        values["fork_bot_count"].append(as_bool(row.get("is_bot")))
        for pull in pulls:
            created = parse_day(pull.get("created_at"))
            merged = parse_day(pull.get("merged_at"))
            if created is not None and merged is not None:
                values["fork_pr_days_to_merge_mean"].append(max(0, (merged - created).days))

        owner_row = owners.get(as_text(row.get("fork_owner")).casefold()) or {}
        owner = owner_row.get("user") or {}
        values["fork_owner_api_ok_count"].append(as_int(owner_row.get("status")) == 200)
        values["fork_owner_followers_mean"].append(as_int(owner.get("followers")))
        values["fork_owner_public_repos_mean"].append(as_int(owner.get("public_repos")))
        values["fork_owner_bio_present_mean"].append(as_bool(owner.get("bio")))
        values["fork_owner_blog_present_mean"].append(as_bool(owner.get("blog")))
        values["fork_owner_email_present_mean"].append(as_bool(owner.get("email")))
        age = age_days(owner.get("created_at"), year_end(year))
        if age is not None:
            values["fork_owner_account_age_days_mean"].append(age)
        values["fork_owner_is_org_count"].append(int(as_text(owner.get("type")).casefold() == "organization"))

        detail = details.get((row["repo_id"], as_text(row.get("fork_owner")).casefold(), as_text(row.get("fork_name")).casefold()), {})
        values["fork_size_kb_mean"].append(as_int(detail.get("size")))
        values["fork_stars_num_mean"].append(as_int(detail.get("stars")))
        language = as_text(detail.get("language")).casefold()
        if language:
            values["fork_language_diversity"].append(language)
        fork_age = age_days(detail.get("created_at"), year_end(year))
        if fork_age is not None:
            values["fork_age_days_mean"].append(fork_age)
        push_lag = age_days(detail.get("pushed_at"), year_end(year))
        if push_lag is not None:
            values["fork_days_to_last_push_mean"].append(push_lag)

        compare = compares.get(
            (row["repo_id"], as_text(row.get("fork_owner")).casefold(), as_text(row.get("fork_name")).casefold()),
            {},
        )
        values["fork_total_commits_sum"].append(as_int(compare.get("total_commits")))
        commit_authors = {
            as_text(commit.get("login")).casefold()
            for commit in compare.get("commits") or []
            if commit.get("login")
        }
        values["fork_commit_authors_mean"].append(len(commit_authors))
        values["fork_compare_status_ok_count"].append(as_int(compare.get("status")) == 200)
        for commit in compare.get("commits") or []:
            commit_day = parse_day(commit.get("date"))
            if commit_day is not None:
                values["fork_commit_recency_days_mean"].append(age_days(commit_day, year_end(year)))
        first_own = parse_day(row.get("first_own_commit"))
        fork_created = parse_day(row.get("fork_created_at"))
        if first_own is not None and fork_created is not None:
            values["fork_first_own_commit_lag_days_mean"].append(max(0, (first_own - fork_created).days))

    result = {
        key: {
            name: (
                mean(parts[name])
                if name in MEAN_FORK_FEATURES
                else len(set(parts[name]))
                if name == "fork_language_diversity"
                else sum(parts[name])
            )
            for name in FORK_FEATURES
            if name in parts
        }
        for key, parts in result.items()
    }
    for values in result.values():
        prs = values.get("fork_pr_count_sum", 0)
        merged = values.get("fork_pr_merged_count", 0)
        values["fork_pr_merge_rate"] = ratio(merged, prs)
        values.setdefault("fork_pr_merge_rate", 0.0)
        assert merged <= prs
    return result


def cumulative_fork_counts(forks: list[dict]) -> dict:
    result = defaultdict(lambda: defaultdict(int))
    for row in forks:
        year = fork_year(row)
        if year is None:
            continue
        values = result[(row["repo_id"], year)]
        values["forks_to_year"] += 1
        values["changed_forks_to_year"] += int(row.get("category") in CHANGED)
        values["derivative_forks_to_year"] += int(row.get("category") == "derivative")
        values["independent_derivative_forks_to_year"] += int(
            row.get("category") == "derivative" and row.get("independence") in INDEPENDENT
        )
    return result


def target_counts(forks: list[dict]) -> dict:
    result = defaultdict(lambda: defaultdict(int))
    for row in forks:
        year = fork_year(row)
        if year is None:
            continue
        values = result[(row["repo_id"], year)]
        category = row.get("category")
        values["derivative"] += int(category == "derivative")
        values["any_changed"] += int(category in CHANGED)
        values["contribution"] += int(category == "contribution")
        values["pr_merged"] += as_bool(row.get("pr_merged"))
    return result


def repo_features(meta: dict, graph_profiles: dict, github_profiles: dict, upstreams: dict) -> dict:
    owner = as_text(meta.get("owner_login")).casefold()
    profile = graph_profiles.get(owner, {})
    github = github_profiles.get(owner, {})
    upstream = upstreams.get(meta.get("id"), {})
    return {
        "created_at": as_text(meta.get("created_at")),
        "last_updated": as_text(meta.get("last_updated")),
        "stars_num": as_int(meta.get("stars_num")),
        "forks_num": as_int(meta.get("forks_num")),
        "is_fork": as_bool(meta.get("is_fork")),
        "archived": as_bool(meta.get("archived")),
        "has_readme": as_bool(meta.get("has_readme")),
        "language": as_text(meta.get("language")),
        "license": as_text(meta.get("license")),
        "owner_login": as_text(meta.get("owner_login")),
        "owner_type": as_text(meta.get("owner_type")),
        "implements_count": as_int(meta.get("implements_count")),
        "contributors_count": len(meta.get("contributors") or []),
        "cited_urls_count": len(meta.get("cited_urls") or []),
        "itmo_source_count": len(meta.get("itmo_sources") or []),
        "owner_profile_company_present": as_bool(profile.get("company")),
        "owner_profile_location_present": as_bool(profile.get("location")),
        "owner_profile_name_present": as_bool(profile.get("name")),
        "owner_github_profile_fetched": as_bool(github.get("profile_fetched")),
        "owner_github_repo_count": len(github.get("repos") or []),
        "owner_github_email_count": len(github.get("emails") or []),
        "owner_github_commit_name_count": len(github.get("commit_names") or []),
        "owner_github_group_count": len(github.get("groups") or []),
        "upstream_github_forks_count": as_int(upstream.get("api_forks")),
        "upstream_github_pages": as_int(upstream.get("pages")),
        "upstream_github_api_ok": int(as_int(upstream.get("api_status")) == 200),
        "upstream_pushed_lag_days": as_int(age_days(upstream.get("pushed_at"), SNAPSHOT_DATE) or 0),
        "repo_is_itmo": as_bool(meta.get("is_itmo")),
        "repo_access_lag_days": as_int(age_days(meta.get("created_at"), SNAPSHOT_DATE) or 0),
        "repo_last_update_lag_days": as_int(age_days(meta.get("last_updated"), SNAPSHOT_DATE) or 0),
    }


def build_rows(
    panel: list[dict],
    forks: list[dict],
    ownership: list[dict],
    pubs: list[dict],
    implements: list[dict],
    authors: list[dict],
    person_departments: list[dict],
    mentions: list[dict],
    repo_links: list[dict],
    graph_persons_github: list[dict],
    fork_owners: list[dict],
    fork_compares: list[dict],
    forks_by_repo: list[dict],
    graph_profiles: list[dict],
    github_profiles: list[dict],
) -> list[dict]:
    pub_years, pub_code, pub_doi = build_publication_index(pubs)
    publications = build_repository_publications(implements, pub_years)
    mention_index = build_mentions(mentions, pub_years)
    authors_by_pub = defaultdict(set)
    for row in authors:
        authors_by_pub[row["publication_id"]].add(row["person_id"])
    departments_by_person = defaultdict(set)
    for row in person_departments:
        departments_by_person[row["person_id"]].add(row["department_id"])
    owners = {as_text(row.get("login")).casefold(): row for row in fork_owners}
    profiles = {as_text(row.get("login")).casefold(): row for row in graph_profiles}
    gh_profiles = {as_text(row.get("login")).casefold(): row for row in github_profiles}
    upstreams = build_upstreams(forks_by_repo)
    pub_links = build_publication_links(repo_links)
    github_ids = build_person_github(graph_persons_github)

    aggregates = fork_aggregates(
        forks,
        owners,
        build_fork_details(forks_by_repo),
        build_fork_compares(fork_compares),
    )
    yearly = cumulative_fork_counts(forks)
    targets = target_counts(forks)
    meta = {row["id"]: row for row in ownership}
    running = defaultdict(lambda: defaultdict(int))
    rows = []

    for item in sorted(panel, key=lambda row: (row["repo_id"], as_int(row["year"]))):
        repo_id = item["repo_id"]
        year = as_int(item["year"])
        next_targets = targets[(repo_id, year + 1)]
        row = {
            "repo_id": repo_id,
            "repo": item["repo"],
            "year": year,
        }
        row.update({name: as_int(item.get(name)) for name in PANEL_FEATURES})
        row.update(repo_features(meta.get(repo_id, {}), profiles, gh_profiles, upstreams))
        row.update(
            graph_features(
                repo_id,
                year,
                publications,
                mention_index,
                authors_by_pub,
                departments_by_person,
                pub_code,
                pub_doi,
                pub_links,
                github_ids,
            )
        )
        row.update(aggregates.get((repo_id, year), {}))
        for name in FORK_FEATURES:
            row.setdefault(name, 0)
        for name, count in yearly[(repo_id, year)].items():
            running[repo_id][name] += count
        row.update({name: running[repo_id][name] for name in FORK_CUMULATIVE_FEATURES})
        row.update({
            "target_derivative_next_year": int(next_targets["derivative"] > 0),
            "target_any_changed_next_year": int(next_targets["any_changed"] > 0),
            "target_contribution_next_year": int(next_targets["contribution"] > 0),
            "target_pr_merged_next_year": int(next_targets["pr_merged"] > 0),
        })
        assert row["target_derivative_next_year"] == as_int(item.get("target_next_year"))
        assert row["relevant_mention_count_to_year"] <= row["mention_count_to_year"]
        assert row["publication_count_to_year"] <= as_int(meta.get(repo_id, {}).get("implements_count")) or not meta.get(repo_id)
        rows.append(row)

    return rows


def write_csv(rows: list[dict]) -> None:
    with CSV_PATH.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDNAMES)
        writer.writeheader()
        writer.writerows(rows)


def write_readme(rows: list[dict]) -> None:
    positives = {name: sum(row[name] for row in rows) for name in TARGETS}
    lines = [
        "# Набор данных для предиктивной аналитики",
        "",
        f"Файл: `analytics_dataset.csv`, строк: {len(rows)}, столбцов: {len(FIELDNAMES)}.",
        "Сборка: `analysis/forks/21_dataset.py`.",
        "",
        "## Источники",
        "",
        "- `out/panel.json`: строка «репозиторий — год», исходные признаки года и исходный target `target_next_year`.",
        "- `out/forks_classified.json`: категории форков, даты, сравнение с исходной веткой, пул-реквесты.",
        "- `data/repo_ownership.json`: статические признаки репозиториев и результат отбора.",
        "- `data/publications.jsonl`, `data/implements.jsonl`: годы публикаций и связь публикаций с репозиториями.",
        "- `data/authored.jsonl`, `data/person_departments.jsonl`: авторы публикаций и их отделы в графе.",
        "- `data/mentions.jsonl`: упоминания репозиториев в публикациях, решения классификации и уверенность.",
        "- `data/repo_links.jsonl`, `data/graph_persons_github.jsonl`: ссылки в публикациях и совпадения авторов с GitHub.",
        "- `data/fork_owners.json`: числовые признаки аккаунтов владельцев форков.",
        "- `data/fork_compare.json`: сравнение форков с исходной веткой, коммиты и авторы коммитов.",
        "- `data/forks_by_repo.json`: метаданные исходных репозиториев, размер, звёзды, язык, даты создания и последнего push форков.",
        "- `data/graph_profiles.jsonl`, `data/github_profiles.jsonl`: факты заполнения профиля владельца репозитория.",
        "",
        "## Идентификаторы",
        "",
        "- `repo_id`: идентификатор репозитория из PAUK.",
        "- `repo`: `owner/name` из `panel.json`.",
        "- `year`: год строки панели.",
        "",
        "## Признаки года",
        "",
        "- `created`, `forks`, `changed_forks`, `derivative_forks`, `independent_derivative_forks`: из `panel.json`.",
        "- `forks_to_year`, `changed_forks_to_year`, `derivative_forks_to_year`, `independent_derivative_forks_to_year`: накопительные суммы по годам из `forks_classified.json`.",
        "",
        "## Статические признаки репозитория",
        "",
        "- `created_at`, `last_updated`, `stars_num`, `forks_num`, `is_fork`, `archived`, `has_readme`, `language`, `license`, `owner_login`, `owner_type`, `implements_count`: из `repo_ownership.json`.",
        "- `contributors_count`, `cited_urls_count`, `itmo_source_count`: длины списков из `repo_ownership.json`.",
        "- `owner_profile_company_present`, `owner_profile_location_present`, `owner_profile_name_present`: факты заполнения полей профиля из `graph_profiles.jsonl`, без самих значений.",
        "- `owner_github_profile_fetched`, `owner_github_repo_count`, `owner_github_email_count`, `owner_github_commit_name_count`, `owner_github_group_count`: факт загрузки профиля и длины его списков из `github_profiles.jsonl`.",
        "- `upstream_github_forks_count`, `upstream_github_pages`, `upstream_github_api_ok`, `upstream_pushed_lag_days`: число форков, число страниц API, успешность запроса и давность последнего push из `forks_by_repo.json`.",
        "- `repo_is_itmo`, `repo_access_lag_days`, `repo_last_update_lag_days`: признак отбора в граф, давность даты доступа из `created_at` и давность последнего обновления по дате снимка 2026-11-20.",
        "",
        "Эти поля повторяются во всех годах репозитория и взяты из локального снимка 2026-11-20.",
        "",
        "## Признаки графа до года `year`",
        "",
        "- `publication_count_to_year`: число публикаций репозитория с годом не больше `year`.",
        "- `publication_first_year`, `publication_last_year`, `publication_year_span`: границы и ширина окна лет публикаций.",
        "- `publication_has_code_share`: доля публикаций с `has_code = true`.",
        "- `author_count_to_year`, `author_department_count_to_year`, `author_with_department_share`: число авторов, число отделов и доля авторов с отделом.",
        "- `mention_count_to_year`: число упоминаний репозитория в публикациях с годом не больше `year`.",
        "- `relevant_mention_count_to_year`, `relevant_mention_share`, `false_mention_share`, `null_mention_share`: числа и доли по `is_relevant`.",
        "- `ambiguous_mention_count_to_year`: упоминания с причиной `ambiguous_url_extraction` или с несколькими `candidate_urls`.",
        "- `llm_confidence_mean`: среднее `llm_confidence` там, где оно заполнено.",
        "- `publication_link_count_to_year`, `relevant_publication_link_share`, `github_publication_link_share`: число ссылок в публикациях репозитория и доли релевантных ссылок и ссылок на GitHub из `repo_links.jsonl`.",
        "- `author_github_match_share`: доля авторов публикаций репозитория с совпадением в `graph_persons_github.jsonl`.",
        "- `publication_doi_share`: доля публикаций с заполненным `doi`.",
        "- `mention_first_year`, `mention_last_year`, `mention_candidate_urls_mean`: границы лет упоминаний и среднее число кандидатных ссылок в упоминаниях.",
        "",
        "## Признаки GitHub по форкам года `year`",
        "",
        "- `fork_ahead_by_sum`, `fork_behind_by_sum`, `fork_own_commits_sum`, `fork_files_total_sum`, `fork_code_additions_sum`, `fork_functional_additions_sum`, `fork_added_files_sum`: суммы из `forks_classified.json`.",
        "- `fork_pr_count_sum`, `fork_pr_merged_count`, `fork_pr_merge_rate`, `fork_pr_days_to_merge_mean`: число PR, число слитых, доля слитых, среднее число дней до слияния.",
        "- `fork_independent_count`, `fork_bot_count`: числа форков с независимым происхождением и ботов.",
        "- `fork_owner_followers_mean`, `fork_owner_public_repos_mean`, `fork_owner_account_age_days_mean`, `fork_owner_is_org_count`: средние и счётчик по аккаунтам владельцев из `fork_owners.json`, возраст считается на конец года `year`.",
        "- `fork_size_kb_mean`, `fork_stars_num_mean`, `fork_language_diversity`, `fork_age_days_mean`, `fork_days_to_last_push_mean`: размер, звёзды, число языков, возраст форка и число дней до последнего push из `forks_by_repo.json`.",
        "- `fork_total_commits_sum`, `fork_commit_authors_mean`, `fork_compare_status_ok_count`: сумма коммитов в сравнении, среднее число авторов коммитов и число успешных сравнений из `fork_compare.json`.",
        "- `fork_pr_open_count_sum`, `fork_pr_closed_unmerged_count_sum`: открытые PR и закрытые PR без слияния из `forks_classified.json`.",
        "- `fork_owner_bio_present_mean`, `fork_owner_blog_present_mean`, `fork_owner_email_present_mean`, `fork_owner_api_ok_count`: заполненность полей профиля и успешность запроса аккаунта владельца из `fork_owners.json`.",
        "- `fork_first_own_commit_lag_days_mean`, `fork_commit_recency_days_mean`: число дней от создания форка до первого собственного коммита и давность коммитов из `fork_compare.json`.",
        "",
        "Даты возраста и давности push ограничены датой локального снимка 2026-11-20.",
        "",
        "## Цели",
        "",
        "Все цели бинарные и считаются по форкам с `fork_year = year + 1`.",
        "",
        "- `target_derivative_next_year`: есть хотя бы один форк категории `derivative`. Это исходный `panel.target_next_year`, переименованный при сборке набора.",
        "- `target_any_changed_next_year`: есть хотя бы один форк категории `derivative`, `modified` или `contribution`.",
        "- `target_contribution_next_year`: есть хотя бы один форк категории `contribution`.",
        "- `target_pr_merged_next_year`: есть хотя бы один форк с `pr_merged = true`.",
        "",
        "## Строки панели по годам",
        "",
        "| Год | Строк |",
        "|---|---:|",
    ]
    by_year = Counter(row["year"] for row in rows)
    lines.extend(f"| {year} | {by_year[year]} |" for year in sorted(by_year))
    lines.extend(["", f"Итого {len(rows)}.", "", "## Положительные строки", ""])
    lines.extend(f"- `{name}`: {positives[name]}" for name in TARGETS)
    README_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    panel = load_json(PANEL_PATH)
    forks = load_json(FORKS_PATH)
    ownership = load_json(OWNERSHIP_PATH)
    rows = build_rows(
        panel,
        forks,
        ownership,
        load_lines(PUBLICATIONS_PATH),
        load_lines(IMPLEMENTS_PATH),
        load_lines(AUTHORED_PATH),
        load_lines(PERSON_DEPARTMENTS_PATH),
        load_lines(MENTIONS_PATH),
        load_lines(REPO_LINKS_PATH),
        load_lines(GRAPH_PERSONS_GITHUB_PATH),
        load_json(FORK_OWNERS_PATH),
        load_json(FORK_COMPARE_PATH),
        load_json(FORKS_BY_REPO_PATH),
        load_lines(GRAPH_PROFILES_PATH),
        load_lines(GITHUB_PROFILES_PATH),
    )
    assert len(rows) == len(panel)
    assert len({(row["repo_id"], row["year"]) for row in rows}) == len(rows)
    assert all(name in rows[0] for name in FIELDNAMES)
    write_csv(rows)
    write_readme(rows)
    print(f"{CSV_PATH}: {len(rows)} rows, {len(FIELDNAMES)} columns")
    print(f"{README_PATH}: provenance")


if __name__ == "__main__":
    main()

"""Build the predictive analytics dataset from the fork analysis outputs."""

from __future__ import annotations

import csv
import json
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
OUT = ROOT / "out"

PANEL_PATH = OUT / "panel.json"
FORKS_PATH = OUT / "forks_classified.json"
OWNERSHIP_PATH = DATA / "repo_ownership.json"
CSV_PATH = OUT / "analytics_dataset.csv"
README_PATH = OUT / "analytics_dataset.md"

CHANGED = frozenset({"derivative", "modified", "contribution"})
INDEPENDENT = frozenset({"external", "itmo_other_team", "unknown"})

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
)
FORK_FEATURES = (
    "fork_ahead_by_sum",
    "fork_own_commits_sum",
    "fork_functional_additions_sum",
    "fork_pr_merged_count",
    "fork_independent_count",
)
TARGETS = (
    "target_derivative_next_year",
    "target_any_changed_next_year",
    "target_contribution_next_year",
    "target_pr_merged_next_year",
)
FIELDNAMES = ("repo_id", "repo", "year", *PANEL_FEATURES, *REPO_FEATURES, *FORK_FEATURES, *TARGETS)


def load_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def as_int(value) -> int:
    if value is None or value == "":
        return 0
    return int(value)


def as_bool(value) -> int:
    return int(bool(value))


def as_text(value) -> str:
    return "" if value is None else str(value)


def fork_year(row: dict) -> int | None:
    value = row.get("fork_year")
    return None if value is None else int(value)


def fork_aggregates(forks: list[dict]) -> dict:
    result = defaultdict(lambda: defaultdict(int))
    for row in forks:
        year = fork_year(row)
        if year is None:
            continue
        values = result[(row["repo_id"], year)]
        values["fork_ahead_by_sum"] += as_int(row.get("ahead_by"))
        values["fork_own_commits_sum"] += as_int(row.get("own_commits"))
        values["fork_functional_additions_sum"] += as_int(row.get("functional_additions"))
        values["fork_pr_merged_count"] += as_bool(row.get("pr_merged"))
        values["fork_independent_count"] += int(row.get("independence") in INDEPENDENT)
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


def repo_features(meta: dict) -> dict:
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
    }


def build_rows(panel: list[dict], forks: list[dict], ownership: list[dict]) -> list[dict]:
    aggregates = fork_aggregates(forks)
    targets = target_counts(forks)
    meta = {row["id"]: row for row in ownership}
    rows = []

    for item in panel:
        repo_id = item["repo_id"]
        year = as_int(item["year"])
        next_targets = targets[(repo_id, year + 1)]
        row = {
            "repo_id": repo_id,
            "repo": item["repo"],
            "year": year,
        }
        row.update({name: as_int(item.get(name)) for name in PANEL_FEATURES})
        row.update(repo_features(meta.get(repo_id, {})))
        row.update({name: as_int(aggregates[(repo_id, year)].get(name)) for name in FORK_FEATURES})
        row.update({
            "target_derivative_next_year": int(next_targets["derivative"] > 0),
            "target_any_changed_next_year": int(next_targets["any_changed"] > 0),
            "target_contribution_next_year": int(next_targets["contribution"] > 0),
            "target_pr_merged_next_year": int(next_targets["pr_merged"] > 0),
        })
        assert row["target_derivative_next_year"] == as_int(item.get("target_next_year"))
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
        "",
        "## Идентификаторы",
        "",
        "- `repo_id`: идентификатор репозитория из PAUK.",
        "- `repo`: `owner/name` из `panel.json`.",
        "- `year`: год строки панели.",
        "",
        "## Признаки",
        "",
        "- `created`, `forks`, `changed_forks`, `derivative_forks`, `independent_derivative_forks`: из `panel.json`.",
        "- `created_at`, `last_updated`, `stars_num`, `forks_num`, `is_fork`, `archived`, `has_readme`, `language`, `license`, `owner_login`, `owner_type`, `implements_count`: из `repo_ownership.json`.",
        "- `contributors_count`: длина списка `contributors` из `repo_ownership.json`.",
        "- `cited_urls_count`: длина списка `cited_urls` из `repo_ownership.json`.",
        "- `fork_ahead_by_sum`, `fork_own_commits_sum`, `fork_functional_additions_sum`, `fork_pr_merged_count`, `fork_independent_count`: суммы по форкам с `fork_year = year` из `forks_classified.json`.",
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
    rows = build_rows(panel, forks, ownership)
    assert len(rows) == len(panel)
    assert len({(row["repo_id"], row["year"]) for row in rows}) == len(rows)
    write_csv(rows)
    write_readme(rows)
    print(f"{CSV_PATH}: {len(rows)} rows, {len(FIELDNAMES)} columns")
    print(f"{README_PATH}: provenance")


if __name__ == "__main__":
    main()

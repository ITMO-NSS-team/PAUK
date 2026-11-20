"""Build the report tables and the summary numbers."""

from __future__ import annotations

import csv
import json
from collections import Counter
from datetime import datetime
from pathlib import Path

DATA = Path(__file__).resolve().parent / "data"
OUT = Path(__file__).resolve().parent / "out"

forks = json.loads((OUT / "forks_classified.json").read_text(encoding="utf-8"))
panel = json.loads((OUT / "panel.json").read_text(encoding="utf-8"))
population = set(json.loads((OUT / "population.json").read_text(encoding="utf-8"))["repo_ids"])
forks = [row for row in forks if row["repo_id"] in population]
repos_meta = {
    row["repo_id"]: row
    for row in json.loads((DATA / "forks_by_repo.json").read_text(encoding="utf-8"))
    if row["repo_id"] in population
}
ownership = {row["id"]: row for row in json.loads((DATA / "repo_ownership.json").read_text(encoding="utf-8"))}

INDEPENDENT = {"external", "itmo_other_team", "unknown"}


def parse(value: str | None):
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def pct(part: int, total: int) -> str:
    return f"{100 * part / total:.1f}%" if total else "0%"


derivative = [f for f in forks if f["category"] == "derivative"]
independent_derivative = [f for f in derivative if f["independence"] in INDEPENDENT]
changed = [f for f in forks if f["category"] in ("derivative", "modified", "contribution")]

summary = {
    "repositories_query": len(repos_meta),
    "repositories_with_forks": sum(1 for r in repos_meta.values() if r["forks"]),
    "forks": len(forks),
    "forks_inactive": sum(1 for f in forks if f["category"] == "inactive"),
    "forks_changed": len(changed),
    "forks_contribution": sum(1 for f in forks if f["category"] == "contribution"),
    "forks_modified": sum(1 for f in forks if f["category"] == "modified"),
    "forks_derivative": len(derivative),
    "forks_derivative_independent": len(independent_derivative),
    "forks_unknown": sum(1 for f in forks if f["category"] == "unknown"),
    "independence": dict(Counter(f["independence"] for f in forks)),
    "derivative_independence": dict(Counter(f["independence"] for f in derivative)),
    "repos_with_derivative": len({f["repo_id"] for f in derivative}),
    "repos_with_independent_derivative": len({f["repo_id"] for f in independent_derivative}),
    "panel_rows": len(panel),
    "panel_positive_rows": sum(p["target_next_year"] for p in panel),
    "panel_repos_with_positive": len({p["repo_id"] for p in panel if p["target_next_year"]}),
    "panel_years": dict(sorted(Counter(p["year"] for p in panel).items())),
    "events_by_year": dict(sorted(Counter(f["fork_year"] for f in derivative if f["fork_year"]).items())),
    "forks_by_year": dict(sorted(Counter(f["fork_year"] for f in forks if f["fork_year"]).items())),
}
(OUT / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8")

lines = []
lines.append("# Анализ форков научных репозиториев ИТМО\n")
lines.append("## Сводка\n")
lines.append("| Показатель | Значение |")
lines.append("|---|---:|")
rows = [
    ("Репозиториев проверено", summary["repositories_query"]),
    ("из них с форками", summary["repositories_with_forks"]),
    ("Форков обнаружено", summary["forks"]),
    ("Форков без собственных изменений", summary["forks_inactive"]),
    ("Форков с собственными изменениями", summary["forks_changed"]),
    ("из них contribution", summary["forks_contribution"]),
    ("из них modified", summary["forks_modified"]),
    ("из них derivative", summary["forks_derivative"]),
    ("Форков с недоступным сравнением", summary["forks_unknown"]),
    ("Репозиториев с подтверждённым производным развитием", summary["repos_with_derivative"]),
    ("Наблюдений «репозиторий — год»", summary["panel_rows"]),
    ("Положительных наблюдений", summary["panel_positive_rows"]),
]
for name, value in rows:
    lines.append(f"| {name} | {value} |")

lines.append("\n## Принадлежность владельцев форков\n")
lines.append("| Категория | Форков |")
lines.append("|---|---:|")
for key, value in sorted(summary["independence"].items(), key=lambda item: -item[1]):
    lines.append(f"| {key} | {value} |")

lines.append("\n## Производные проекты\n")
lines.append("| Форк | Исходный | Владелец | Принадлежность | Коммитов | Функциональных строк | PR |")
lines.append("|---|---|---|---|---:|---:|---|")
for fork in sorted(derivative, key=lambda f: (f["upstream"], f["fork_owner"])):
    prs = ", ".join(f"#{p['number']}" for p in fork["prs"]) or "—"
    lines.append(
        f"| {fork['fork_url']} | {fork['upstream']} | {fork['fork_owner']} | "
        f"{fork['independence']} | {fork['own_commits']} | {fork['functional_additions']} | {prs} |"
    )

lines.append("\n## Динамика по годам\n")
lines.append("| Год | Форков создано | С изменениями | Производных | Независимых производных |")
lines.append("|---|---:|---:|---:|---:|")
years = sorted({y for y in summary["forks_by_year"] if y})
changed_by_year = Counter(f["fork_year"] for f in changed if f["fork_year"])
deriv_by_year = Counter(f["fork_year"] for f in derivative if f["fork_year"])
indep_by_year = Counter(f["fork_year"] for f in independent_derivative if f["fork_year"])
for year in years:
    lines.append(
        f"| {year} | {summary['forks_by_year'].get(year, 0)} | {changed_by_year.get(year, 0)} | "
        f"{deriv_by_year.get(year, 0)} | {indep_by_year.get(year, 0)} |"
    )

lines.append("\n## Панель «репозиторий — год»\n")
positives = [p for p in panel if p["target_next_year"]]
lines.append("| Репозиторий | Год | Форков | Положительный год |")
lines.append("|---|---:|---:|---:|")
for row in sorted(positives, key=lambda p: (p["repo"], p["year"])):
    lines.append(f"| {row['repo']} | {row['year']} | {row['forks']} | да |")

# Fork-level CSV with the evidence.
with (OUT / "forks.csv").open("w", encoding="utf-8", newline="") as fh:
    writer = csv.writer(fh)
    writer.writerow([
        "upstream", "fork", "fork_owner", "category", "independence", "ahead_by",
        "own_commits", "functional_files", "functional_additions", "prs",
        "fork_created_at", "first_own_commit", "reason", "independence_reason",
    ])
    for fork in forks:
        writer.writerow([
            fork["upstream"], fork["fork_url"], fork["fork_owner"], fork["category"],
            fork["independence"], fork["ahead_by"], fork["own_commits"],
            ";".join(fork["functional_files"][:5]), fork["functional_additions"],
            ";".join(p["url"] for p in fork["prs"]), fork["fork_created_at"],
            fork["first_own_commit"], fork["category_reason"], fork["independence_reason"],
        ])

(OUT / "report_tables.md").write_text("\n".join(lines), encoding="utf-8")

# Lag distributions.
lags = []
for fork in forks:
    created, first = parse(fork["fork_created_at"]), parse(fork["first_own_commit"])
    if created and first:
        lags.append((first - created).days)
lags.sort()
print("fork -> first own commit lag days:", lags[:10], "...", lags[-5:] if len(lags) > 5 else "")
print("lag median:", lags[len(lags) // 2] if lags else None, "n =", len(lags))

repo_lag = []
for repo_id in {f["repo_id"] for f in independent_derivative}:
    created = parse((repos_meta.get(repo_id) or {}).get("created_at"))
    first = min(
        (parse(f["fork_created_at"]) for f in independent_derivative if f["repo_id"] == repo_id and parse(f["fork_created_at"])),
        default=None,
    )
    if created and first:
        repo_lag.append(((first - created).days, (ownership.get(repo_id) or {}).get("name")))
repo_lag.sort()
print("repo creation -> first independent derivative (days):", repo_lag)

print(json.dumps({k: v for k, v in summary.items() if k not in ("panel_years", "forks_by_year")}, ensure_ascii=False, indent=1))

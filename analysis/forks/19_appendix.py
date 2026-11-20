"""Appendix lists for every funnel stage of the report.

Deterministic: reads the extracted JSONL under `data/`, the derived ownership
table `data/repo_ownership.json` and `out/population.json`, and writes
`out/appendix.md`. Two funnels live in the report and both are covered here:
the corpus selection over `IMPLEMENTS` (288 -> 122 -> 119 -> 105) and the
mention channel over `MENTIONS_LINK` (629 links). Repository identifiers are
converted to `owner/name` through the URLs in `graph_repositories.jsonl`;
unresolved mention links are listed by the URLs saved on the relation.
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from pathlib import Path

BASE = Path(__file__).resolve().parent
DATA = BASE / "data"
OUT = BASE / "out"
OUT.mkdir(parents=True, exist_ok=True)
GITHUB = re.compile(r"https?://github\.com/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)")


def load(name: str) -> list[dict]:
    with (DATA / f"{name}.jsonl").open(encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def full_name(url: str | None) -> str:
    match = GITHUB.match(url or "")
    return match.group(1) if match else (url or "")


def main() -> None:
    mentions = load("mentions")
    implements = load("implements")
    name_of = {row["id"]: full_name(row["url"]) for row in load("graph_repositories")}

    ownership = json.loads((DATA / "repo_ownership.json").read_text(encoding="utf-8"))
    by_id = {row["id"]: row for row in ownership}
    mongo_repos = {row["id"]: row for row in load("repositories")}
    population = set(json.loads((OUT / "population.json").read_text(encoding="utf-8"))["repo_ids"])

    def label_of(row: dict) -> str:
        return f"{row['owner']}/{row['name']}"

    selected = {row["id"] for row in ownership if row["is_itmo"]}
    without_forks = {i for i in selected if not (mongo_repos.get(i) or {}).get("is_fork")}
    fork_excluded = sorted(selected - without_forks, key=lambda i: label_of(by_id[i]))
    owner_excluded = sorted(without_forks - population, key=lambda i: label_of(by_id[i]))

    lines = [
        "# Приложение: списки этапов воронки\n",
        "Снимок 2026-11-20. Источник — `data/*.jsonl` (выгрузка `01_extract.py`), "
        "`data/repo_ownership.json`, `out/population.json`; сборка — `19_appendix.py`.\n",
        "## Отбор корпуса (`IMPLEMENTS`)\n",
        "Репозитории, реализующие публикации ИТМО (`IMPLEMENTS`), и их отсев.\n",
        "| Этап | Репозиториев |",
        "|---|---:|",
        f"| Репозитории с `IMPLEMENTS` | {len(ownership)} |",
        f"| Принадлежат ИТМО по правилу | {len(selected)} |",
        f"| Минус репозитории-форки | {len(without_forks)} |",
        f"| Минус владельцы вне авторов и организаций | {len(population)} |",
        "",
        f"### Итоговая выборка, {len(population)} репозиториев\n",
    ]
    for repo_id in sorted(population, key=lambda i: label_of(by_id[i])):
        row = by_id.get(repo_id) or {}
        lines.append(f"- {label_of(row)} — `IMPLEMENTS` {row.get('implements_count', '')}")
    lines += ["", f"### Исключены как репозитории-форки, {len(fork_excluded)}\n"]
    lines += [f"- {label_of(by_id[i])}" for i in fork_excluded]
    lines += ["", f"### Исключены по владельцу, {len(owner_excluded)}\n"]
    lines += [f"- {label_of(by_id[i])}" for i in owner_excluded]
    lines += [""]

    impl_pairs = {(row["publication_id"], row["repo_id"]) for row in implements}

    def label(row: dict) -> str:
        if row["target_label"] == "Repository":
            return name_of.get(row["target_id"], row["target_id"])
        urls = row.get("candidate_urls") or [row["target_id"]]
        return " ".join(urls)

    def list_section(title: str, rows: list[dict]) -> list[str]:
        groups: dict[str, list[dict]] = defaultdict(list)
        for row in rows:
            groups[label(row)].append(row)
        lines = [f"### {title} ({len(rows)})\n"]
        for key in sorted(groups, key=lambda k: (-len(groups[k]), k)):
            publications = {row["publication_id"] for row in groups[key]}
            lines.append(f"- {key} — связей {len(groups[key])}, публикаций {len(publications)}")
        lines.append("")
        return lines

    repo_targets = [row for row in mentions if row["target_label"] == "Repository"]
    candidates = [row for row in mentions if row["target_label"] == "LinkCandidate"]
    relevant = [row for row in mentions if row["is_relevant"] is True]
    irrelevant = [row for row in mentions if row["is_relevant"] is False]
    undecided = [row for row in mentions if row["is_relevant"] is None]
    self_mentions = [
        row for row in repo_targets if (row["publication_id"], row["target_id"]) in impl_pairs
    ]

    lines += [
        "## Канал упоминаний (`MENTIONS_LINK`)\n",
        "Не тот же набор, что выше: отношение `MENTIONS_LINK` и множество упомянутых "
        "репозиториев. Служит проверкой гипотезы «публикация ссылается на чужой код».\n",
        "| Этап | Связей |",
        "|---|---:|",
        f"| Все `MENTIONS_LINK` | {len(mentions)} |",
        f"| Целевой узел `Repository` | {len(repo_targets)} |",
        f"| Целевой узел `LinkCandidate` | {len(candidates)} |",
        f"| `is_relevant = true` | {len(relevant)} |",
        f"| `is_relevant = false` | {len(irrelevant)} |",
        f"| `is_relevant = null` | {len(undecided)} |",
        f"| Публикация реализует упомянутый репозиторий | {len(self_mentions)} |",
        "",
    ]
    for title, rows in (
        ("Репозитории со связью `MENTIONS_LINK`", repo_targets),
        ("Неразрешённые ссылки (`LinkCandidate`)", candidates),
        ("`is_relevant = true`", relevant),
        ("`is_relevant = false`", irrelevant),
        ("`is_relevant = null`", undecided),
        ("Самоупоминания: публикация реализует упомянутый репозиторий", self_mentions),
    ):
        lines += list_section(title, rows)

    path = OUT / "appendix.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    print(f"appendix -> {path} ({len(lines)} lines)")
    print(f"  IMPLEMENTS: {len(ownership)} -> ITMO {len(selected)} -> no forks {len(without_forks)} -> selected {len(population)}")


if __name__ == "__main__":
    main()

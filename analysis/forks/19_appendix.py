"""Appendix lists for every funnel stage of the report.

Deterministic: reads only the extracted JSONL under `data/` and writes
`out/appendix.md`. Repository identifiers are converted to `owner/name` through
the URLs in `graph_repositories.jsonl`; unresolved links are listed by the URLs
saved on the relation.
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
        return [json.loads(line) for line in fh]


def full_name(url: str | None) -> str:
    match = GITHUB.match(url or "")
    return match.group(1) if match else (url or "")


def main() -> None:
    mentions = load("mentions")
    implements = load("implements")
    name_of = {row["id"]: full_name(row["url"]) for row in load("graph_repositories")}

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
        lines = [f"## {title} ({len(rows)})\n"]
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

    lines = [
        "# Приложение: списки этапов воронки\n",
        "Снимок 2026-11-20. Источник — `data/*.jsonl` (выгрузка `01_extract.py`).\n",
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


if __name__ == "__main__":
    main()

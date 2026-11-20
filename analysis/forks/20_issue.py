"""Prepare the three datasets to report to PAUK, as one markdown file.

Reads only the extracted JSONL under `data/` plus `out/population.json`, so the
result is reproducible from a snapshot. Writes `out/issue_pauk.md`; the issue
itself is opened by the researcher after checking against a live snapshot.
"""

from __future__ import annotations

import json
from pathlib import Path

BASE = Path(__file__).resolve().parent
DATA = BASE / "data"
OUT = BASE / "out"
OUT.mkdir(parents=True, exist_ok=True)


def load(name: str) -> list[dict]:
    with (DATA / f"{name}.jsonl").open(encoding="utf-8") as fh:
        return [json.loads(line) for line in fh]


def cell(value) -> str:
    return str(value if value is not None else "").replace("|", "\\|")


def main() -> None:
    mentions = load("mentions")
    publications = {row["id"]: row for row in load("publications")}
    population = set(json.loads((OUT / "population.json").read_text(encoding="utf-8"))["repo_ids"])

    def pub(publication_id: str) -> str:
        row = publications.get(publication_id) or {}
        title = (row.get("title") or "")[:90]
        doi = row.get("doi") or ""
        return f"{title} ({doi})" if doi else title

    candidates = [row for row in mentions if row["target_label"] == "LinkCandidate"]
    undecided = [row for row in mentions if row["is_relevant"] is None]
    false_itmo = [
        row for row in mentions
        if row["is_relevant"] is False and row["target_id"] in population
    ]

    lines = [
        "# Данные для issue в PAUK\n",
        "Снимок 2026-11-20. Сгенерировано `20_issue.py`; перед заведением issue "
        "проверить на живом снимке.\n",
        f"## 1. Неразрешённые `LinkCandidate`: {len(candidates)}\n",
        "Ссылки на код, не сведённые к узлу `Repository`; «публикация — URL».\n",
        "| Публикация | URL-кандидат | `is_relevant` |",
        "|---|---|---|",
    ]
    for row in sorted(candidates, key=lambda r: r["publication_id"]):
        urls = " ".join(row.get("candidate_urls") or [row["target_id"]])
        lines.append(f"| {cell(pub(row['publication_id']))} | {cell(urls)} | {cell(row['is_relevant'])} |")

    nulls_ambiguous = [row for row in undecided if row.get("llm_confidence") is None]
    nulls_other = [row for row in undecided if row.get("llm_confidence") is not None]
    lines += [
        f"\n## 2. Связи `is_relevant = null`: {len(undecided)}\n",
        f"`ambiguous_url_extraction` без вердикта: {len(nulls_ambiguous)}; "
        f"с уверенностью, но без вердикта: {len(nulls_other)}.\n",
        "| Публикация | Цель | `llm_reason` | `llm_confidence` |",
        "|---|---|---|---|",
    ]
    for row in undecided:
        reason = (str(row.get("llm_reason") or ""))[:80]
        lines.append(
            f"| {cell(pub(row['publication_id']))} | {cell(row['target_id'])} "
            f"| {cell(reason)} | {cell(row.get('llm_confidence'))} |"
        )

    lines += [
        f"\n## 3. Связи `is_relevant = false` в выборке ИТМО: {len(false_itmo)}\n",
        "Публикация приводит репозиторий только в библиографии; проверяем, "
        "нельзя ли считать такое упоминание использованием.\n",
        "| Публикация | Репозиторий | `llm_confidence` | `llm_reason` |",
        "|---|---|---|---|",
    ]
    for row in false_itmo:
        reason = (str(row.get("llm_reason") or ""))[:160]
        lines.append(
            f"| {cell(pub(row['publication_id']))} | {cell(row['target_id'])} "
            f"| {cell(row.get('llm_confidence'))} | {cell(reason)} |"
        )

    path = OUT / "issue_pauk.md"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"issue data -> {path}")
    print(f"  candidates: {len(candidates)}")
    print(f"  null: {len(undecided)} ({len(nulls_ambiguous)} ambiguous, {len(nulls_other)} other)")
    print(f"  false in ITMO population: {len(false_itmo)}")


if __name__ == "__main__":
    main()

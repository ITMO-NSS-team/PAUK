"""Contacts vs output: does a person's number of co-authors track their
publication count or career length?

Reads the person-publication bipartite graph from Neo4j, projects it onto
Person-Person co-authorship, and writes scatter/percentile charts to
data/cache/charts/.

Read-only against the database; writes only the CSV and PNGs below OUT_DIR.
"""

from __future__ import annotations

import csv
from collections import defaultdict
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from neo4j import GraphDatabase  # noqa: E402

URI = "bolt://localhost:7688"
AUTH = ("neo4j", "testtest")
OUT_DIR = Path("data/cache/charts")

QUERY = """MATCH (p:Person)-[:AUTHORED]->(pub:Publication)
WITH p, count(DISTINCT pub.id) AS pubs, max(pub.year)-min(pub.year) AS span
MATCH (p)-[:AUTHORED]->(:Publication)<-[:AUTHORED]-(q:Person)
WHERE p.id <> q.id
RETURN p.id AS pid, coalesce(p.is_itmo, false) AS itmo, pubs, span,
       count(DISTINCT q.id) AS contacts"""


def pearson(xs: list[float], ys: list[float]) -> float:
    n = len(xs)
    if n == 0:
        return float("nan")
    xbar = sum(xs) / n
    ybar = sum(ys) / n
    cov = sum((x - xbar) * (y - ybar) for x, y in zip(xs, ys, strict=True))
    varx = sum((x - xbar) ** 2 for x in xs)
    vary = sum((y - ybar) ** 2 for y in ys)
    if varx == 0 or vary == 0:
        return float("nan")
    return cov / (varx * vary) ** 0.5


def q(values: list[float], pct: float) -> float:
    if not values:
        return float("nan")
    ordered = sorted(values)
    k = int(pct * (len(ordered) - 1))
    return ordered[k]


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    driver = GraphDatabase.driver(URI, auth=AUTH)
    result = driver.execute_query(QUERY, database_="neo4j")

    rows = [
        (r["pid"], r["itmo"], int(r["pubs"]), int(r["span"]), int(r["contacts"]))
        for r in result.records
    ]
    driver.close()

    with (OUT_DIR / "contacts_dataset.csv").open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["person_id", "itmo", "publications", "year_span", "contacts"])
        writer.writerows(rows)

    all_pubs = [float(r[2]) for r in rows]
    all_contacts = [float(r[4]) for r in rows]
    all_span = [float(r[3]) for r in rows]
    itmo = [r for r in rows if r[1]]
    ext = [r for r in rows if not r[1]]

    print(f"людей всего: {len(rows)}, ИТМО: {len(itmo)}, внешних: {len(ext)}")
    print("корреляция контакты ~ число работ: "
          f"все r={pearson(all_pubs, all_contacts):.4f}, "
          f"ИТМО r={pearson([float(r[2]) for r in itmo], [float(r[4]) for r in itmo]):.4f}, "
          f"внешние r={pearson([float(r[2]) for r in ext], [float(r[4]) for r in ext]):.4f}")
    print("корреляция контакты ~ разброс лет: "
          f"все r={pearson(all_span, all_contacts):.4f}, "
          f"ИТМО r={pearson([float(r[3]) for r in itmo], [float(r[4]) for r in itmo]):.4f}, "
          f"внешние r={pearson([float(r[3]) for r in ext], [float(r[4]) for r in ext]):.4f}")

    by_pubs: dict[int, list[float]] = defaultdict(list)
    by_pubs_itmo: dict[int, list[float]] = defaultdict(list)
    by_pubs_ext: dict[int, list[float]] = defaultdict(list)
    for r in rows:
        key = r[2] if r[2] <= 30 else 35
        by_pubs[key].append(float(r[4]))
        (by_pubs_itmo if r[1] else by_pubs_ext)[key].append(float(r[4]))

    by_span: dict[int, list[float]] = defaultdict(list)
    by_span_itmo: dict[int, list[float]] = defaultdict(list)
    by_span_ext: dict[int, list[float]] = defaultdict(list)
    for r in rows:
        by_span[r[3]].append(float(r[4]))
        (by_span_itmo if r[1] else by_span_ext)[r[3]].append(float(r[4]))

    plt.rcParams["font.family"] = "DejaVu Sans"

    # 1. Контакты против числа публикаций.
    fig, ax = plt.subplots(figsize=(10, 6))
    ax.scatter(all_pubs, all_contacts, s=4, alpha=0.25, c="#8ab4f8", linewidths=0)
    pub_bins = [(1, 1), (2, 2), (3, 3), (4, 4), (5, 5), (6, 10), (11, 20),
                (21, 30), (31, 50), (51, 100), (101, 200), (201, 500), (501, 10**9)]
    xs, med, lo, hi = [], [], [], []
    for lo_b, hi_b in pub_bins:
        counts = [r[2] for r in rows if lo_b <= r[2] <= hi_b]
        if not counts:
            continue
        contacts = [float(r[4]) for r in rows if lo_b <= r[2] <= hi_b]
        xs.append(q(counts, 0.5))
        med.append(q(contacts, 0.5))
        lo.append(q(contacts, 0.25))
        hi.append(q(contacts, 0.75))
    ax.plot(xs, med, color="#c53929", lw=2, label="медиана")
    ax.fill_between(xs, lo, hi, color="#c53929", alpha=0.18, label="квартили 25–75%")
    ax.set_xscale("log")
    ax.set_yscale("log")
    ax.set_xlabel("Число публикаций (на человека)")
    ax.set_ylabel("Число уникальных соавторов")
    ax.set_title("Зависимость числа соавторов от числа публикаций")
    ax.legend()
    ax.grid(True, which="both", alpha=0.25)
    fig.tight_layout()
    fig.savefig(OUT_DIR / "contacts_vs_articles.png", dpi=300)
    plt.close(fig)

    # 2. Контакты против разброса лет выхода работ.
    fig, ax = plt.subplots(figsize=(10, 6))
    ax.scatter(all_span, all_contacts, s=4, alpha=0.25, c="#8ab4f8", linewidths=0)
    xs = sorted(by_span)
    med = [q(by_span[x], 0.5) for x in xs]
    lo = [q(by_span[x], 0.25) for x in xs]
    hi = [q(by_span[x], 0.75) for x in xs]
    ax.plot(xs, med, color="#c53929", lw=2, label="медиана")
    ax.fill_between(xs, lo, hi, color="#c53929", alpha=0.18, label="квартили 25–75%")
    ax.set_yscale("log")
    ax.set_xlabel("Период публикаций, лет (макс. год − мин. год)")
    ax.set_ylabel("Число уникальных соавторов")
    ax.set_title("Зависимость числа соавторов от периода публикаций")
    ax.legend()
    ax.grid(True, which="both", alpha=0.25)
    fig.tight_layout()
    fig.savefig(OUT_DIR / "contacts_vs_span.png", dpi=300)
    plt.close(fig)

    # 3. ИТМО против внешних: медианы по двум осям.
    fig, axes = plt.subplots(1, 2, figsize=(13, 5))
    for ax, groups, title, xlabel in (
        (axes[0], (by_pubs_itmo, by_pubs_ext), "По числу публикаций", "Число публикаций"),
        (axes[1], (by_span_itmo, by_span_ext), "По периоду публикаций", "Период публикаций, лет"),
    ):
        x_max = max(max(g) if g else 0 for g in groups)
        xs = sorted(set(range(0, x_max + 1)) & set(groups[0]) | set(groups[1]))
        for gkey, series in enumerate((("ИТМО", "crimson"), ("внешние", "steelblue"))):
            label, color = series
            pts = [(x, q(groups[gkey][x], 0.5)) for x in xs if x in groups[gkey]]
            if pts:
                ax.plot([p[0] for p in pts], [p[1] for p in pts],
                        color=color, lw=2, label=label)
        ax.set_yscale("log")
        ax.set_title(title)
        ax.set_xlabel(xlabel)
        ax.set_ylabel("Медиана числа уникальных соавторов")
        ax.legend()
        ax.grid(True, which="both", alpha=0.25)
    fig.suptitle("Медианное число соавторов: ИТМО и внешние соавторы")
    fig.tight_layout()
    fig.savefig(OUT_DIR / "contacts_categories.png", dpi=300)
    plt.close(fig)

    # 4. Корреляции связей с числом работ и с периодом, по группам.
    r_pubs = {
        "все": pearson(all_pubs, all_contacts),
        "ИТМО": pearson([float(r[2]) for r in itmo], [float(r[4]) for r in itmo]),
        "внешние": pearson([float(r[2]) for r in ext], [float(r[4]) for r in ext]),
    }
    r_span = {
        "все": pearson(all_span, all_contacts),
        "ИТМО": pearson([float(r[3]) for r in itmo], [float(r[4]) for r in itmo]),
        "внешние": pearson([float(r[3]) for r in ext], [float(r[4]) for r in ext]),
    }
    labels = list(r_pubs)
    x = range(len(labels))
    width = 0.38
    fig, ax = plt.subplots(figsize=(9, 5))
    ax.bar([i - width / 2 for i in x], [r_pubs[k] for k in labels], width,
           label="Число соавторов и число работ", color="#c53929")
    ax.bar([i + width / 2 for i in x], [r_span[k] for k in labels], width,
           label="Число соавторов и период публикаций", color="#1a73e8")
    ax.axhline(0, color="black", lw=0.8)
    for i in x:
        for vals, off in ((r_pubs, -width / 2), (r_span, width / 2)):
            ax.text(i + off, vals[k := labels[i]], f"{vals[k]:.2f}",
                    ha="center", va="bottom", fontsize=9)
    ax.set_xticks(list(x))
    ax.set_xticklabels(labels)
    ax.set_xlabel("Группа людей")
    ax.set_ylabel("Коэффициент корреляции Пирсона")
    ax.set_ylim(-0.1, 0.65)
    ax.set_title("Корреляция числа соавторов с продуктивностью по группам")
    ax.legend()
    ax.grid(True, axis="y", alpha=0.25)
    fig.tight_layout()
    fig.savefig(OUT_DIR / "contacts_correlations.png", dpi=300)
    plt.close(fig)

    # 5. Медианы связей по числу публикаций, ИТМО против внешних.
    bins = [(1, 1), (2, 2), (3, 3), (4, 4), (5, 5),
            (6, 10), (11, 20), (21, 50), (51, 10**9)]
    labels = ["1", "2", "3", "4", "5", "6–10", "11–20", "21–50", ">50"]
    groups = {"ИТМО": (True, "#c53929"), "внешние": (False, "#1a73e8")}
    x = range(len(labels))
    width = 0.38
    fig, ax = plt.subplots(figsize=(11, 6))
    for i, (name, (flag, color)) in enumerate(groups.items()):
        medians = [q([r[4] for r in rows if r[1] is flag and lo <= r[2] <= hi], 0.5)
                   for lo, hi in bins]
        offsets = [p - width / 2 + i * width for p in x]
        ax.bar(offsets, medians, width, label=name, color=color)
        for p, v in zip(offsets, medians, strict=True):
            ax.text(p, v + 1, f"{v:g}", ha="center", va="bottom", fontsize=8)
    ax.set_xticks(list(x))
    ax.set_xticklabels(labels)
    ax.set_xlabel("Число публикаций (на человека)")
    ax.set_ylabel("Медиана числа уникальных соавторов")
    ax.set_title("Медианное число соавторов по числу публикаций: ИТМО и внешние соавторы")
    ax.legend()
    ax.grid(True, axis="y", alpha=0.25)
    fig.tight_layout()
    fig.savefig(OUT_DIR / "contacts_median_bars.png", dpi=300)
    plt.close(fig)

    print(f"графики сохранены в {OUT_DIR}")


if __name__ == "__main__":
    main()
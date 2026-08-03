"""Statistics + cheap health checks over the Neo4j graph.

Two entry points:
  * CLI — writes web/graph-stats.js (window.STATS) for the static page load;
  * snapshot() — imported by serve.py to answer /api/stats, so the
    "Пересчитать" button on the page recomputes straight from the DB.

Every check is deliberately cheap: counts, IS NULL / empty tests, group-by
duplicates and a few regexes. No LLM, no morphology, no graph algorithms —
a full pass is a couple of seconds.

On name validation specifically: a morphological analyzer (pymorphy3, with
its Name/Surn/Patr tags) was measured against this data and rejected — it
flags 30% of the surnames, almost all of them legitimate, because its
dictionary does not cover transliterated foreign names (Лю, Сюй, Гао, Нго).
The regex checks below fire on 0.6% and every hit is genuinely malformed.

Usage:
  python visualization/generate_stats.py [--out-dir web]
"""

import argparse
import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from neo4j import GraphDatabase

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parent.parent
OUT_DIR_DEFAULT = Path(__file__).resolve().parent / "web"

load_dotenv(REPO_ROOT / ".env")
NEO4J_URI = os.getenv("NEO4J_URI", "bolt://localhost:7687")
NEO4J_USER = os.getenv("NEO4J_USER", "neo4j")
NEO4J_PASSWORD = os.getenv("NEO4J_PASSWORD", "password")

# Java-regex character classes, as understood by Cypher's =~.
CYR, LAT = r"\\p{IsCyrillic}", r"\\p{IsLatin}"
RU_NAME_FIELDS = "[p.surname_ru, p.first_name_ru, p.second_name_ru]"


def scalar(drv, cypher):
    recs, _, _ = drv.execute_query(cypher)
    return recs[0][0] if recs else 0


def rows(drv, cypher):
    recs, _, _ = drv.execute_query(cypher)
    return [dict(r) for r in recs]


# --- node / relationship inventory --------------------------------------

NODE_COUNTS = [
    ("Публикации", "MATCH (p:Publication) RETURN count(p)"),
    ("Персоны всего", "MATCH (p:Person) RETURN count(p)"),
    ("— сотрудники ИТМО", "MATCH (p:Person:Itmo) RETURN count(p)"),
    ("— внешние соавторы", "MATCH (p:Person:External) RETURN count(p)"),
    ("Департаменты", "MATCH (d:Department) RETURN count(d)"),
    ("Репозитории", "MATCH (r:Repository) RETURN count(r)"),
    ("GitHub-профили", "MATCH (g:GitHubProfile) RETURN count(g)"),
    ("Ссылки-кандидаты", "MATCH (l:LinkCandidate) RETURN count(l)"),
]

REL_ORDER = ["AUTHORED", "PRODUCED_BY", "BELONGS_TO", "MENTIONS_LINK",
             "DEVELOPED_BY", "IMPLEMENTS", "OWNED_BY", "CONTRIBUTED_TO"]

REL_NOTE = {
    "AUTHORED": "человек → публикация",
    "PRODUCED_BY": "публикация → департамент",
    "BELONGS_TO": "человек → департамент",
    "MENTIONS_LINK": "публикация → ссылка",
    "DEVELOPED_BY": "репозиторий → департамент",
    "IMPLEMENTS": "репозиторий → публикация",
    "OWNED_BY": "репозиторий → профиль GitHub",
    "CONTRIBUTED_TO": "человек → репозиторий",
}

# --- health checks ------------------------------------------------------
# (group, title, count query, denominator query or None, warn, fail, hint)
# warn/fail are shares when a denominator is given, absolute counts otherwise.

CHECKS = [
    # -- пропуски --
    ("Пропуски", "Сотрудники без департамента",
     "MATCH (p:Person:Itmo) WHERE NOT (p)-[:BELONGS_TO]->() RETURN count(p)",
     "MATCH (p:Person:Itmo) RETURN count(p)", 0.05, 0.25,
     "Итоги по департаментам занижены."),
    ("Пропуски", "Публикации без единого автора",
     "MATCH (p:Publication) WHERE NOT (p)<-[:AUTHORED]-() RETURN count(p)",
     "MATCH (p:Publication) RETURN count(p)", 0.001, 0.01,
     "Ни с кем не связаны и не видны на карте."),
    ("Пропуски", "Публикации без автора из ИТМО",
     "MATCH (p:Publication) WHERE NOT (p)<-[:AUTHORED]-(:Person:Itmo) RETURN count(p)",
     "MATCH (p:Publication) RETURN count(p)", 0.01, 0.05,
     "Не попадают на карту."),
    ("Пропуски", "Публикации без аннотации",
     "MATCH (p:Publication) WHERE p.abstract IS NULL OR p.abstract = '' RETURN count(p)",
     "MATCH (p:Publication) RETURN count(p)", 0.05, 0.15,
     "По аннотациям ищутся ссылки на код — часть репозиториев не находится."),
    ("Пропуски", "Сотрудники без русского ФИО",
     "MATCH (p:Person:Itmo) WHERE p.surname_ru IS NULL OR trim(p.surname_ru) = '' "
     "RETURN count(p)",
     "MATCH (p:Person:Itmo) RETURN count(p)", 0.02, 0.10,
     "Показываются латиницей."),
    ("Пропуски", "Департаменты без русского названия",
     "MATCH (d:Department) WHERE d.name_ru IS NULL OR trim(d.name_ru) = '' RETURN count(d)",
     "MATCH (d:Department) RETURN count(d)", 0.05, 0.20,
     "Подписи получаются на смеси языков."),
    ("Пропуски", "Публикации без контактного автора",
     """MATCH (p:Publication) WHERE NOT EXISTS {
          (p)<-[a:AUTHORED]-() WHERE a.is_corresponding } RETURN count(p)""",
     "MATCH (p:Publication) RETURN count(p)", 0.10, 0.30, None),
    ("Пропуски", "Авторства без указания места работы",
     "MATCH ()-[a:AUTHORED]->() WHERE a.affiliation IS NULL OR a.affiliation = '' "
     "RETURN count(a)",
     "MATCH ()-[a:AUTHORED]->() RETURN count(a)", 0.01, 0.05,
     "Такого автора нельзя отнести ни к ИТМО, ни к внешним."),
    ("Пропуски", "Департаменты без вариантов написания",
     "MATCH (d:Department) WHERE size(coalesce(d.name_variants, [])) = 0 RETURN count(d)",
     "MATCH (d:Department) RETURN count(d)", 0.10, 0.40,
     "По ним департамент опознаётся в тексте статьи."),

    # -- имена --
    ("Имена", "Кириллица и латиница внутри одного слова",
     f"""MATCH (p:Person:Itmo) WHERE any(v IN {RU_NAME_FIELDS}
           WHERE v IS NOT NULL AND v =~ '.*({CYR}{LAT}|{LAT}{CYR}).*')
         RETURN count(p)""",
     "MATCH (p:Person:Itmo) RETURN count(p)", 1e-9, 0.005,
     "Сбой транслитерации: «Вершиinin», «Полевaя», «Аkhмеров»."),
    ("Имена", "Знак ударения внутри имени",
     f"""MATCH (p:Person:Itmo) WHERE any(v IN {RU_NAME_FIELDS}
           WHERE v IS NOT NULL AND v =~ '.*[\\\\u0300-\\\\u036F].*')
         RETURN count(p)""",
     "MATCH (p:Person:Itmo) RETURN count(p)", 1e-9, 0.005,
     "«Смоля́нская» — поиск по такому имени не найдёт человека."),
    ("Имена", "Фамилия из одной буквы",
     """MATCH (p:Person:Itmo) WHERE p.surname_ru IS NOT NULL
        AND size(trim(replace(p.surname_ru, '.', ''))) = 1 RETURN count(p)""",
     "MATCH (p:Person:Itmo) RETURN count(p)", 1e-9, 0.005,
     "Короткие фамилии схлопнулись до инициала."),
    ("Имена", "Русское ФИО записано латиницей",
     f"""MATCH (p:Person:Itmo)
         WHERE p.surname_ru IS NOT NULL AND trim(p.surname_ru) <> ''
           AND p.surname_ru =~ '[{LAT}\\\\s.-]+'
         RETURN count(p)""",
     "MATCH (p:Person:Itmo) RETURN count(p)", 1e-9, 0.005,
     "Транслитерация не отработала."),
    ("Имена", "Вместо имени только инициалы",
     """MATCH (p:Person:Itmo) WHERE p.first_name_ru IS NOT NULL
        AND trim(p.first_name_ru) <> ''
        AND size(trim(replace(replace(p.first_name_ru, '.', ''), ' ', ''))) <= 2
        RETURN count(p)""",
     "MATCH (p:Person:Itmo) RETURN count(p)", 0.05, 0.15,
     "В источнике не было полного имени."),

    # -- дубликаты --
    ("Дубликаты", "Полные тёзки среди сотрудников",
     """MATCH (p:Person:Itmo)
        WHERE p.surname_ru IS NOT NULL AND size(trim(p.surname_ru)) > 1
        WITH toLower(trim(p.surname_ru)) + '|' +
             toLower(trim(coalesce(p.first_name_ru, ''))) + '|' +
             toLower(trim(coalesce(p.second_name_ru, ''))) AS k, count(*) AS c
        WHERE c > 1 RETURN coalesce(sum(c - 1), 0)""",
     "MATCH (p:Person:Itmo) RETURN count(p)", 0.01, 0.05,
     "Совпадает всё ФИО целиком — либо однофамильцы, либо один человек дважды."),
    ("Дубликаты", "Одинаковая подпись «Фамилия И.»",
     """MATCH (p:Person:Itmo)
        WHERE p.surname_ru IS NOT NULL AND size(trim(p.surname_ru)) > 1
          AND p.first_name_ru IS NOT NULL AND trim(p.first_name_ru) <> ''
        WITH toLower(trim(p.surname_ru)) + ' ' +
             toLower(left(trim(p.first_name_ru), 1)) AS k, count(*) AS c
        WHERE c > 1 RETURN coalesce(sum(c - 1), 0)""",
     "MATCH (p:Person:Itmo) RETURN count(p)", 0.05, 0.12,
     "Именно так люди подписаны на карте — этих не различить визуально."),
    ("Дубликаты", "Одинаковое имя латиницей у разных людей",
     """MATCH (p:Person) WHERE p.name_en IS NOT NULL AND trim(p.name_en) <> ''
        WITH toLower(trim(p.name_en)) AS k, collect(p.id) AS ids WHERE size(ids) > 1
        WITH [x IN ids | CASE WHEN x STARTS WITH 'itmo_' THEN substring(x, 5)
                              WHEN x STARTS WITH 'ext_'  THEN substring(x, 4)
                              ELSE x END] AS sfx
        WITH reduce(a = [], s IN sfx | CASE WHEN s IN a THEN a ELSE a + s END) AS uniq
        WHERE size(uniq) > 1 RETURN coalesce(sum(size(uniq) - 1), 0)""",
     "MATCH (p:Person) RETURN count(p)", 0.001, 0.01,
     "Один автор заведён под разными идентификаторами."),
    ("Дубликаты", "Лишние публикации с тем же заголовком",
     """MATCH (p:Publication) WHERE p.title <> ''
        WITH toLower(trim(p.title)) AS k, count(*) AS c WHERE c > 1
        RETURN coalesce(sum(c - 1), 0)""",
     "MATCH (p:Publication) RETURN count(p)", 0.005, 0.02,
     "Обычно препринт и журнальная версия одной работы."),
    ("Дубликаты", "Публикации с одинаковым DOI",
     """MATCH (p:Publication) WHERE p.doi <> ''
        WITH toLower(p.doi) AS k, count(*) AS c WHERE c > 1
        RETURN coalesce(sum(c - 1), 0)""", None, 1, 10, None),
    ("Дубликаты", "Человек заведён и как сотрудник, и как внешний",
     """MATCH (i:Person:Itmo) WHERE i.id STARTS WITH 'itmo_'
        WITH i, 'ext_' + substring(i.id, 5) AS e
        MATCH (:Person:External {id: e}) RETURN count(*)""",
     "MATCH (p:Person:Itmo) RETURN count(p)", 0.01, 0.05,
     "Соавторство теряется, если на статье он подписан не от ИТМО."),
    ("Дубликаты", "Репозитории, различающиеся лишь регистром ссылки",
     """MATCH (r:Repository)
        WITH toLower(rtrim(r.url, '/')) AS k, count(*) AS c WHERE c > 1
        RETURN coalesce(sum(c - 1), 0)""", None, 1, 5, None),
    ("Дубликаты", "Департаменты с одинаковым названием",
     """MATCH (d:Department) WHERE trim(coalesce(d.name_ru, d.name_en, '')) <> ''
        WITH toLower(trim(coalesce(d.name_ru, d.name_en))) AS k, count(*) AS c
        WHERE c > 1 RETURN coalesce(sum(c - 1), 0)""", None, 1, 5, None),
    ("Дубликаты", "Повторяющиеся авторства",
     """MATCH (p:Person)-[a:AUTHORED]->(pub:Publication)
        WITH p, pub, count(a) AS c WHERE c > 1 RETURN count(*)""", None, 1, 10,
     "Один человек указан автором одной статьи дважды."),

    # -- противоречия --
    ("Противоречия", "Репозиторий привязан, но статья помечена как без кода",
     "MATCH (r:Repository)-[:IMPLEMENTS]->(p:Publication) WHERE p.has_code = false "
     "RETURN count(DISTINCT p)", None, 1, 20, None),
    ("Противоречия", "Статья помечена как с кодом, но репозитория нет",
     "MATCH (p:Publication) WHERE p.has_code = true AND NOT (p)<-[:IMPLEMENTS]-() "
     "RETURN count(p)", None, 1, 20, None),
    ("Противоречия", "Сотрудник сразу в нескольких департаментах",
     """MATCH (p:Person:Itmo)-[:BELONGS_TO]->(d:Department)
        WITH p, count(d) AS c WHERE c > 1 RETURN count(p)""",
     "MATCH (p:Person:Itmo) RETURN count(p)", 0.02, 0.10,
     "На карте у человека может быть только один департамент."),
    ("Противоречия", "Репозитории с испорченной ссылкой",
     r"""MATCH (r:Repository)
         WHERE r.url =~ '.*[^\x00-\x7F].*'
            OR NOT r.url =~ 'https?://[^/]+/[^/]+/[^/]+.*'
         RETURN count(r)""",
     "MATCH (r:Repository) RETURN count(r)", 0.005, 0.02,
     "Мусор, вытащенный из PDF вместе с адресом."),
    ("Противоречия", "Ссылка признана рабочей, но репозиторий не заведён",
     """MATCH (:Publication)-[m:MENTIONS_LINK]->(l:LinkCandidate)
        WHERE m.is_relevant = true AND NOT EXISTS {
          (r:Repository) WHERE toLower(rtrim(r.url, '/')) = toLower(rtrim(l.url, '/')) }
        RETURN count(*)""", None, 1, 20, None),
    ("Противоречия", "Ссылки без решения о релевантности",
     "MATCH ()-[m:MENTIONS_LINK]->() WHERE m.is_relevant IS NULL RETURN count(m)",
     None, 1, 20, None),
    ("Противоречия", "Репозитории без владельца",
     "MATCH (r:Repository) WHERE NOT (r)-[:OWNED_BY]->() RETURN count(r)",
     None, 1, 10, None),
    ("Противоречия", "Профили GitHub без репозиториев",
     "MATCH (g:GitHubProfile) WHERE NOT (g)<-[:OWNED_BY]-() RETURN count(g)",
     None, 1, 20, None),
    ("Противоречия", "Год не совпадает с датой публикации",
     "MATCH (p:Publication) WHERE p.year <> p.publication_date.year RETURN count(p)",
     None, 1, 50, None),
    ("Противоречия", "Узел связан сам с собой",
     "MATCH (n)-[e]->(n) RETURN count(e)", None, 1, 10, None),
]


def status_for(n, denom, warn, fail):
    """ok / warn / fail. Thresholds are shares when denom is given, else counts."""
    v = (n / denom) if denom else n
    if v >= fail:
        return "fail"
    if v >= warn:
        return "warn"
    return "ok"


def collect(drv):
    nodes = [{"label": label, "n": scalar(drv, cy)} for label, cy in NODE_COUNTS]

    on_map = scalar(drv, "MATCH (p:Publication) WHERE (p)<-[:AUTHORED]-(:Person:Itmo) "
                         "RETURN count(p)")
    for row in nodes:
        if row["label"] == "Публикации":
            row["note"] = f"на карте {on_map}"

    rel_counts = {r["t"]: r["c"] for r in rows(
        drv, "MATCH ()-[e]->() RETURN type(e) AS t, count(e) AS c")}
    rels = [{"type": t, "n": rel_counts.get(t, 0), "note": REL_NOTE.get(t, "")}
            for t in REL_ORDER if t in rel_counts]
    rels += [{"type": t, "n": c, "note": REL_NOTE.get(t, "")}
             for t, c in sorted(rel_counts.items()) if t not in REL_ORDER]

    checks = []
    for group, title, cy, denom_cy, warn, fail, hint in CHECKS:
        n = scalar(drv, cy)
        denom = scalar(drv, denom_cy) if denom_cy else None
        checks.append({
            "group": group, "title": title, "n": n, "of": denom,
            "pct": round(100.0 * n / denom, 1) if denom else None,
            "status": status_for(n, denom, warn, fail),
            "hint": hint,
        })

    years = rows(drv, """MATCH (p:Publication) WHERE p.year IS NOT NULL
                         RETURN p.year AS year, count(*) AS n ORDER BY year""")
    depts = rows(drv, """MATCH (d:Department)<-[:BELONGS_TO]-(p:Person:Itmo)
                         WITH d, count(p) AS n ORDER BY n DESC LIMIT 8
                         RETURN coalesce(d.name_ru, d.name_en) AS name, n""")

    return {
        "generated_at": datetime.now(timezone.utc).astimezone().strftime("%d.%m.%Y %H:%M"),
        "nodes": nodes,
        "rels": rels,
        "totals": {
            "nodes": scalar(drv, "MATCH (n) RETURN count(n)"),
            "rels": scalar(drv, "MATCH ()-[e]->() RETURN count(e)"),
        },
        "checks": checks,
        "years": years,
        "top_depts": depts,
    }


def snapshot():
    """Open a driver, collect, close. Used by the CLI and by serve.py."""
    drv = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD))
    try:
        drv.verify_connectivity()
        return collect(drv)
    finally:
        drv.close()


def write_js(stats, out_dir=OUT_DIR_DEFAULT):
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    path = out / "graph-stats.js"
    path.write_text("window.STATS=" + json.dumps(stats, ensure_ascii=False,
                                                 separators=(",", ":")) + ";\n",
                    encoding="utf-8")
    return path


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out-dir", default=str(OUT_DIR_DEFAULT))
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s",
                        datefmt="%Y-%m-%d %H:%M:%S")

    t0 = time.time()
    stats = snapshot()
    path = write_js(stats, args.out_dir)

    bad = [c for c in stats["checks"] if c["status"] != "ok"]
    logger.info("узлов %d, связей %d, проверок %d (с замечаниями %d)",
                stats["totals"]["nodes"], stats["totals"]["rels"],
                len(stats["checks"]), len(bad))
    logger.info("wrote %s (%.1f KB) за %.1f c", path, path.stat().st_size / 1024,
                time.time() - t0)


if __name__ == "__main__":
    main()

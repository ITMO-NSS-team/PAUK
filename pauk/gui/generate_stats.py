"""Statistics + cheap health checks over the Neo4j graph.

Three entry points:
  * CLI — writes web/graph-stats.js (window.STATS) for the static page load;
  * snapshot() — imported by serve.py to answer /api/stats, so the
    "Пересчитать" button on the page recomputes straight from the DB;
  * examples() — rows behind a single check, for the details popup and its
    CSV download.

The checks themselves live in checks.py. Every one of them is deliberately
cheap: counts, IS NULL / empty tests, group-by duplicates and a few regexes.
No LLM, no morphology, no graph algorithms — a full pass is a few seconds.

Usage:
  uv run python -m pauk.gui.generate_stats [--out-dir web]
"""

import argparse
import json
import logging
import time
from datetime import UTC, date, datetime
from datetime import time as _time
from pathlib import Path

from neo4j import GraphDatabase

from pauk.settings import settings

from .checks import BY_ID, CHECKS

logger = logging.getLogger(__name__)

OUT_DIR_DEFAULT = Path(__file__).resolve().parent / "web"

EXAMPLES_LIMIT_DEFAULT = 300
EXAMPLES_LIMIT_MAX = 5000


def scalar(drv, cypher, **params):
    recs, _, _ = drv.execute_query(cypher, **params)
    return recs[0][0] if recs else 0


def rows(drv, cypher, **params):
    recs, _, _ = drv.execute_query(cypher, **params)
    return [dict(r) for r in recs]


def _jsonable(v):
    """Neo4j temporal types and lists -> something json.dumps can handle."""
    if v is None or isinstance(v, (str, int, float, bool)):
        return v
    if isinstance(v, (list, tuple)):
        return [_jsonable(x) for x in v]
    if isinstance(v, (date, datetime, _time)):
        return v.isoformat()
    return str(v)


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
    for c in CHECKS:
        n = scalar(drv, c.count)
        denom = scalar(drv, c.of) if c.of else None
        checks.append({
            "id": c.id, "group": c.group, "title": c.title,
            "n": n, "of": denom,
            "pct": round(100.0 * n / denom, 1) if denom else None,
            "status": status_for(n, denom, c.warn, c.fail),
            "hint": c.hint,
            "has_examples": bool(c.examples),
        })

    years = rows(drv, """MATCH (p:Publication) WHERE p.year IS NOT NULL
                         RETURN p.year AS year, count(*) AS n ORDER BY year""")
    depts = rows(drv, """MATCH (d:Department)<-[:BELONGS_TO]-(p:Person:Itmo)
                         WITH d, count(p) AS n ORDER BY n DESC LIMIT 8
                         RETURN coalesce(d.name_ru, d.name_en) AS name, n""")

    return {
        "generated_at": datetime.now(UTC).astimezone().strftime("%d.%m.%Y %H:%M"),
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


def collect_examples(drv, check_id, limit=EXAMPLES_LIMIT_DEFAULT):
    """Rows behind one check. Returns columns + rows, ready for a table or CSV."""
    c = BY_ID.get(check_id)
    if c is None:
        raise KeyError(check_id)
    if not c.examples:
        raise ValueError(f"у проверки {check_id} нет запроса за примерами")

    limit = max(1, min(int(limit), EXAMPLES_LIMIT_MAX))
    recs, _, _ = drv.execute_query(c.examples, lim=limit)
    columns = list(recs[0].keys()) if recs else []
    data = [[_jsonable(r[k]) for k in columns] for r in recs]
    total = scalar(drv, c.count)

    return {
        "id": c.id, "title": c.title, "group": c.group,
        "hint": c.hint, "total": total,
        "columns": columns, "rows": data,
        "shown": len(data), "limit": limit,
        "truncated": len(data) >= limit,
    }


def _driver():
    return GraphDatabase.driver(settings.neo4j_uri, auth=(settings.neo4j_user, settings.neo4j_password))


def snapshot():
    """Open a driver, collect, close. Used by the CLI and by serve.py."""
    drv = _driver()
    try:
        drv.verify_connectivity()
        return collect(drv)
    finally:
        drv.close()


def examples(check_id, limit=EXAMPLES_LIMIT_DEFAULT):
    drv = _driver()
    try:
        drv.verify_connectivity()
        return collect_examples(drv, check_id, limit)
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
    ap.add_argument("--check", help="показать примеры по одной проверке и выйти")
    ap.add_argument("--limit", type=int, default=EXAMPLES_LIMIT_DEFAULT)
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s",
                        datefmt="%Y-%m-%d %H:%M:%S")

    if args.check:
        ex = examples(args.check, args.limit)
        print(f"{ex['title']}: всего {ex['total']}, показано {ex['shown']}")
        print(" | ".join(ex["columns"]))
        for r in ex["rows"][:20]:
            print(" | ".join(str(v)[:40] for v in r))
        return

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

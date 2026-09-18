"""Statistics and health checks over the Neo4j graph, for the admin
panel's "Здоровье БД" page (`health.py`, `health_routes.py`).

  * `collect()` - node/relationship counts, every check in `checks.py`, a few
    summaries; the worker runs it and `health.py` keeps the answer in Mongo;
  * `collect_examples()` - the rows behind one check, for its detail page.

Every check is deliberately cheap: counts, IS NULL / empty tests, group-by
duplicates and a few regexes. No LLM, no morphology, no graph algorithms -
a full pass is a few seconds.
"""

import logging
from datetime import UTC, date, datetime
from datetime import time as _time

from .checks import BY_ID, CHECKS, GROUP_EN

logger = logging.getLogger(__name__)

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

# is_itmo:Itmo/External label migration - #150.
NODE_COUNTS = [
    ("Публикации", "Publications", "MATCH (p:Publication) RETURN count(p)"),
    ("Персоны всего", "People total", "MATCH (p:Person) RETURN count(p)"),
    # Not a label: the loader writes one :Person and carries ITMO membership
    # as a sticky property — see the note in checks.py.
    ("— сотрудники ИТМО", "— ITMO staff",
     "MATCH (p:Person) WHERE p.is_itmo RETURN count(p)"),
    ("— внешние соавторы", "— external co-authors",
     "MATCH (p:Person) WHERE NOT coalesce(p.is_itmo, false) RETURN count(p)"),
    ("Департаменты", "Departments", "MATCH (d:Department) RETURN count(d)"),
    ("Репозитории", "Repositories", "MATCH (r:Repository) RETURN count(r)"),
    ("GitHub-профили", "GitHub profiles", "MATCH (g:GitHubProfile) RETURN count(g)"),
    ("Ссылки-кандидаты", "Candidate links", "MATCH (l:LinkCandidate) RETURN count(l)"),
]

REL_ORDER = [
    "AUTHORED",
    "PRODUCED_BY",
    "BELONGS_TO",
    "MENTIONS_LINK",
    "DEVELOPED_BY",
    "IMPLEMENTS",
    "OWNED_BY",
    "CONTRIBUTED_TO",
]

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

REL_NOTE_EN = {
    "AUTHORED": "person → publication",
    "PRODUCED_BY": "publication → department",
    "BELONGS_TO": "person → department",
    "MENTIONS_LINK": "publication → link",
    "DEVELOPED_BY": "repository → department",
    "IMPLEMENTS": "repository → publication",
    "OWNED_BY": "repository → GitHub profile",
    "CONTRIBUTED_TO": "person → repository",
}


def status_for(n, denom, warn, fail):
    """ok / warn / fail. Thresholds are shares when denom is given, else counts."""
    v = (n / denom) if denom else n
    if v >= fail:
        return "fail"
    if v >= warn:
        return "warn"
    return "ok"


#: Publications with at least one ITMO author: the note under the
#: publication count, and the only ones that reach the map.
ON_MAP = ("MATCH (p:Publication) WHERE EXISTS { (p)<-[:AUTHORED]-(a:Person) WHERE a.is_itmo } "
          "RETURN count(p)")

YEARS = """MATCH (p:Publication) WHERE p.year IS NOT NULL
           RETURN p.year AS year, count(*) AS n ORDER BY year"""

TOP_DEPTS = """MATCH (d:Department)<-[:BELONGS_TO]-(p:Person) WHERE p.is_itmo
               WITH d, count(p) AS n ORDER BY n DESC LIMIT 8
               RETURN coalesce(d.name_ru, d.name_en) AS name, d.name_en AS name_en, n"""

#: Everything this module asks the graph besides the checks themselves. A
#: list, so a test can walk it the way it walks CHECKS: the labels here had
#: drifted from the schema too, and showed zeros without a word.
QUERIES = [cypher for _label, _label_en, cypher in NODE_COUNTS] + [ON_MAP, YEARS, TOP_DEPTS]


def collect(drv):
    nodes = [
        {"label": label, "label_en": label_en, "n": scalar(drv, cy)}
        for label, label_en, cy in NODE_COUNTS
    ]

    on_map = scalar(drv, ON_MAP)
    for row in nodes:
        if row["label"] == "Публикации":
            row["note"] = f"на карте {on_map}"
            row["note_en"] = f"on the map: {on_map}"

    rel_counts = {
        r["t"]: r["c"] for r in rows(drv, "MATCH ()-[e]->() RETURN type(e) AS t, count(e) AS c")
    }
    rels = [
        {"type": t, "n": rel_counts.get(t, 0), "note": REL_NOTE.get(t, ""), "note_en": REL_NOTE_EN.get(t, "")}
        for t in REL_ORDER
        if t in rel_counts
    ]
    rels += [
        {"type": t, "n": c, "note": REL_NOTE.get(t, ""), "note_en": REL_NOTE_EN.get(t, "")}
        for t, c in sorted(rel_counts.items())
        if t not in REL_ORDER
    ]

    checks = []
    for c in CHECKS:
        # Per-check isolation: a query that fails against the current graph
        # (a property typed differently than the check assumes, say) must not
        # take the whole snapshot down with it — the tab would show nothing
        # at all instead of the 30-odd checks that did run.
        try:
            n = scalar(drv, c.count)
            denom = scalar(drv, c.of) if c.of else None
        except Exception as exc:
            logger.warning("проверка %s не выполнилась: %s", c.id, exc)
            # Exception text is already Python/English, so hint and hint_en match.
            error_hint = f"{type(exc).__name__}: {exc}"
            checks.append(
                {
                    "id": c.id,
                    "group": c.group,
                    "group_en": GROUP_EN.get(c.group, c.group),
                    "title": c.title,
                    "title_en": c.title_en,
                    "n": None,
                    "of": None,
                    "pct": None,
                    "status": "error",
                    "hint": error_hint,
                    "hint_en": error_hint,
                    "has_examples": False,
                }
            )
            continue
        checks.append(
            {
                "id": c.id,
                "group": c.group,
                "group_en": GROUP_EN.get(c.group, c.group),
                "title": c.title,
                "title_en": c.title_en,
                "n": n,
                "of": denom,
                "pct": round(100.0 * n / denom, 1) if denom else None,
                "status": status_for(n, denom, c.warn, c.fail),
                "hint": c.hint,
                "hint_en": c.hint_en,
                "has_examples": bool(c.examples),
            }
        )

    years = rows(drv, YEARS)
    depts = rows(drv, TOP_DEPTS)

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
        "id": c.id,
        "title": c.title,
        "title_en": c.title_en,
        "group": c.group,
        "group_en": GROUP_EN.get(c.group, c.group),
        "hint": c.hint,
        "hint_en": c.hint_en,
        "total": total,
        "columns": columns,
        "rows": data,
        "shown": len(data),
        "limit": limit,
        "truncated": len(data) >= limit,
    }

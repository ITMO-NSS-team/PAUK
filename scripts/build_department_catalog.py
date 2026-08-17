"""One-off generator of a draft official ITMO department catalogue.

Scrapes the FULL scientific-structure tree from ITMO's Russian structure page
(the "Основные образовательные и научные подразделения" section): faculties ->
institutes -> centres -> laboratories. Hierarchy is reconstructed from <ul>
nesting depth (a linear scan over the balanced tags, robust to the page's
unclosed <li> that make DOM parsers misattribute ancestors). Non-research units
(administrative / production / outreach) are filtered out. English names are
taken from the EN structure page by the shared numeric id -- official for the
faculty / centre tier.

Units without an official EN name (leaf laboratories -- no id, no EN page) are
left with an EMPTY name_en: they are filled separately (LLM RU->EN + manual
review), and author spellings are added to aliases from the affiliation corpus.
Those are one-off finishing steps -- automation is not required here.

The result is a DRAFT: review it by hand and complete it before promoting to
data/static/departments_catalog.json (the source of truth read by
pauk.storage.static.StaticStore).

Run from the project root:
    uv run python scripts/build_department_catalog.py
    uv run python scripts/build_department_catalog.py --out data/static/departments_catalog.json
"""

import argparse
import json
import logging
import re
from pathlib import Path

from pauk.sources.base import HttpClient

logger = logging.getLogger(__name__)

# The catalogue lives in data/static (read by pauk.storage.static.StaticStore).
STATIC_DIR = Path(__file__).resolve().parents[1] / "data" / "static"

USER_AGENT = "ITMO-Research-Monitor/1.0 (pauk)"
STRUCTURE_URL_RU = "https://itmo.ru/ru/department_units/obshchaya_struktura_universiteta.htm"
STRUCTURE_URL_EN = "https://en.itmo.ru/en/department_list/Academic_Structure.htm"
TIMEOUT = 60

# Boundaries of the scientific section on the RU page.
_SECTION_START = "Основные образовательные и научные"
_SECTION_END = "Административные (сервисные)"
_LINK_RE = re.compile(
    r"/(?:ru|en)/(?:view)?(?:faculty|unit|department|otherstructure)/(\d+)/[^\"'>]*\"[^>]*>\s*([^<]+?)\s*<",
    re.I,
)
_TOKEN_RE = re.compile(r"(<ul\b)|(</ul>)|(<a\b[^>]*>(.*?)</a>)", re.I | re.S)
# Non-research units to drop from the scientific tree (Russian page text).
_DROP_RE = re.compile(
    r"^отдел\b|опытно-экспериментальное производство|учебно-методическое объединение|"
    r"^ресурсный центр$|военный учебный центр|базовая профориентационная школа|"
    r"дополнительного образования для школьников|центр развития карьеры|"
    r"учебно-практический центр",
    re.I,
)


def _clean(raw: str) -> str:
    """Strip html tags/entities from a fragment and collapse whitespace."""
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", raw or "")).strip()


def parse_ru_tree(html: str) -> list[dict]:
    """Scientific-section units with hierarchy inferred from <ul> depth.

    Returns {name_ru, faculty_id, school_ru} in document order; school_ru is the
    nearest top-level ancestor (megafaculty / standalone top unit), and top-level
    units are their own school. Non-research units are filtered out.
    """
    start = html.find(_SECTION_START)
    end = html.find(_SECTION_END)
    if start == -1 or end == -1:
        raise SystemExit("scientific-section boundaries not found on the RU structure page.")
    segment = html[start:end]

    id_by_name = {name: int(fid) for fid, name in _LINK_RE.findall(segment)}
    depth, last_top, rows = 0, "", []
    for token in _TOKEN_RE.finditer(segment):
        if token.group(1):
            depth += 1
        elif token.group(2):
            depth = max(0, depth - 1)
        elif token.group(3):
            name = _clean(token.group(4))
            if not name or _DROP_RE.search(name):
                continue
            if depth == 1:
                last_top = name
            rows.append(
                {"name_ru": name, "faculty_id": id_by_name.get(name), "school_ru": name if depth == 1 else last_top}
            )
    seen, uniq = set(), []
    for row in rows:
        key = (row["name_ru"].lower(), row["school_ru"].lower())
        if key not in seen:
            seen.add(key)
            uniq.append(row)
    return uniq


def official_en_by_id(html: str) -> dict[int, str]:
    """id -> official English name from the EN structure page (faculty/centre tier)."""
    return {int(fid): _clean(name) for fid, name in _LINK_RE.findall(html)}


# Root organisation node: every top-level unit hangs off it (parent), and it is
# the one entry StaticStore keeps out of affiliation matching (kind=organization).
ROOT_EN = "ITMO University"
ROOT_RU = "Университет ИТМО"


def _slug(name: str) -> str:
    """Human-readable, stable uid from a name (graph node id, referenced by parent)."""
    return re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-"))


def _kind(name: str) -> str:
    """Coarse hierarchy level from a unit name, for graph styling."""
    low = name.lower()
    if low.startswith("school of") or "мегафак" in low:
        return "megafaculty"
    if "higher school" in low or low.endswith(" school") or "engineering school" in low:
        return "school"
    if low.startswith("faculty") or low.startswith("факультет"):
        return "faculty"
    if low.startswith("department") or low.startswith("кафедра") or low.startswith("департамент"):
        return "department"
    if "laborator" in low or "лаборатори" in low:
        return "lab"
    if low.startswith("institute") or low.endswith("institute") or "институт" in low:
        return "institute"
    if "center" in low or "centre" in low or "центр" in low:
        return "center"
    return "unit"


def build_catalog() -> list[dict]:
    """Assemble the draft in the schema StaticStore reads: name_en/name_ru/kind/
    parent/aliases/context_aliases, plus one root organisation entry.

    `parent` references a unit by its English name (that is how StaticStore derives
    parent_id). Top-level units point at the ROOT_EN organisation; sub-units point
    at their megafaculty's EN name. A megafaculty with no official EN yet leaves an
    unresolved parent for its children until the EN name is filled in by hand.
    """
    http = HttpClient(TIMEOUT, {"User-Agent": USER_AGENT})
    ru_units = parse_ru_tree(http.get_text(STRUCTURE_URL_RU))
    en_by_id = official_en_by_id(http.get_text(STRUCTURE_URL_EN))
    school_en = {u["name_ru"]: en_by_id.get(u["faculty_id"], "") for u in ru_units if u["school_ru"] == u["name_ru"]}
    logger.info("scientific units: %d | official EN from EN page: %d", len(ru_units), len(en_by_id))

    catalog, no_en = [], 0
    for u in ru_units:
        name_en = en_by_id.get(u["faculty_id"], "")
        if not name_en:
            no_en += 1
        is_top = u["school_ru"] == u["name_ru"]
        parent_name = ROOT_EN if is_top else (school_en.get(u["school_ru"]) or u["school_ru"])
        catalog.append(
            {
                "uid": _slug(name_en),
                "name_en": name_en,
                "name_ru": u["name_ru"],
                "kind": _kind(name_en or u["name_ru"]),
                "parent": _slug(parent_name),
                "aliases": [],
                "context_aliases": [],
            }
        )
    catalog.sort(key=lambda d: (d["parent"] or "", d["name_ru"].lower()))
    # The root organisation is an Organization node (not matched, no aliases):
    # fill ror_id by hand from the ROR registry.
    catalog.append(
        {
            "uid": _slug(ROOT_EN),
            "name_en": ROOT_EN,
            "name_ru": ROOT_RU,
            "kind": "organization",
            "parent": None,
            "ror_id": "",
            "country": "Russia",
            "type": "university",
        }
    )
    logger.warning("without official EN (fill via LLM RU->EN + manual): %d of %d", no_en, len(catalog))
    return catalog


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    default_out = STATIC_DIR / "departments_catalog.draft.json"
    parser = argparse.ArgumentParser(description="Generate a draft official ITMO department catalogue.")
    parser.add_argument(
        "--out",
        type=Path,
        default=default_out,
        help="where to write the draft JSON (default data/static/*.draft.json; "
        "the live departments_catalog.json is never overwritten).",
    )
    args = parser.parse_args()

    catalog = build_catalog()
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(catalog, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    logger.info(
        "wrote %d entries to %s. Review by hand, fill leaf EN (LLM RU->EN) and "
        "aliases from the corpus, then promote to departments_catalog.json.",
        len(catalog),
        args.out,
    )


if __name__ == "__main__":
    main()

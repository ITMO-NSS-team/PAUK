"""Builds all three kinds of nodes (authors/repositories/publications) in
two forms at once - "summary" (what's needed to draw a point on the map)
and "detail" (extended fields new_gui lazily loads after the map).
"""

from __future__ import annotations

import json

from .authorship import Authorship
from .departments import DepartmentAssignment, DepartmentTable


def dense_rank(values: dict[str, int]) -> dict[str, float]:
    """rank = dense rank of the metric / number of unique values, rounded to 3.

    Args:
        values: A metric by node id (e.g. an author's publication count).

    Returns:
        The same keys, value is a share from 0 to 1 (higher metric = closer to 1).

    Example:
        >>> dense_rank({"a": 1, "b": 5, "c": 5})
        {'a': 0.5, 'b': 1.0, 'c': 1.0}
    """
    uniq = sorted(set(values.values()))
    pos = {v: (i + 1) / len(uniq) for i, v in enumerate(uniq)}
    return {k: round(pos[v], 3) for k, v in values.items()}


# Only used in one place (truncating a publication title in detail) - a
# local constant next to the class it's for, not in config.py, the same
# principle as STRANDED_JITTER in layout.py.
PUB_TITLE_MAX_LEN = 200


def _parse_json_list(text: str | None) -> list:
    """Parses JSON-text (`funding`/`versions`/`affiliations`/`code_url` -
    everything `pauk.cache.export` writes as a serialized list, see
    `JSON_TEXT_FIELDS` in `pauk/graph/extract.py`) into a list, silently
    falling back to an empty list on missing/broken data - this is a
    snapshot field, not something worth failing the whole generation over.
    """
    if not text:
        return []
    try:
        parsed = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return []
    return parsed if isinstance(parsed, list) else [parsed]


def _initial(value: str) -> str:
    """First letter, capitalized, with a trailing period."""
    return f"{value[0].upper()}."


def _fmt_part(value: str, *, force_initial: bool) -> str:
    """Formats one part of a name (given name or patronymic) for the label.

    Collapses to an initial for the public display, or whenever the part
    sits next to another part (surname_ru/first_name_ru/second_name_ru all
    filled at once). Otherwise left as the author gave it - except a part
    that's ALREADY a bare initial (a single letter, with or without a
    period - that's how some LLM/catalog data arrives): a period is always
    added, which isn't truncation, just correct punctuation for what the
    data already says.
    """
    stripped = value.rstrip(".")
    if force_initial or len(stripped) == 1:
        return _initial(stripped)
    return value


def author_label(
    surname: str | None, first: str | None, second: str | None, *, public: bool = False
) -> str:
    """One format for any author: surname first, then initials.

    Takes three name parts of one language at a time - a label per language
    has to be assembled separately from surname_ru/first_name_ru/
    second_name_ru and surname_en/first_name_en/second_name_en. No fallback
    to a raw combined string: guessing surname/given-name/patronymic by word
    order is exactly the failure mode an LLM step in author_names.py already
    replaced - repeating it here for display would bring it back. An empty
    surname returns "" - the caller picks the fallback (e.g. the other
    language's label, or the free-text name_ru).
    """
    surname, first, second = surname or "", first or "", second or ""
    if not surname:
        return ""
    if public and len(surname) > 3:
        surname = surname[:3] + ".."
    if first and second:
        return f"{surname} {_fmt_part(first, force_initial=True)}{_fmt_part(second, force_initial=True)}"
    if second:  # only the patronymic survived - treat it as the initial
        return f"{surname} {_fmt_part(second, force_initial=True)}"
    if first:
        return f"{surname} {_fmt_part(first, force_initial=public)}"
    return surname


def author_variants(row: dict, label_ru: str, label_en: str) -> dict[str, list[str]]:
    """Other spellings of this person's name, minus whatever's already shown
    as the card title - kept separate by source.

    The private card's title (`new_gui/src/features/panels.ts`) is the
    truncated label (`label`/`label_en`) UNTIL detail has merged in, and
    once it has - the full `name_ru`/`name_en` (the full name instead of
    "Surname I.O."). Since `name_ru`/`name_en` become the title themselves,
    they're excluded from candidates for the collapsed list here - otherwise
    the same name would show twice. Sources that do go into the list:
    `name_variants` - what OpenAlex saw across the author's different
    publications; `other_names` - the name the author asks to be credited
    under (ORCID credit name), plus variants they registered on their own
    profile. Different in origin, so not merged into one list - that's the
    card's job (see `field.nameVariantsOpenAlex`/`field.nameVariantsOrcid`
    in `new_gui/src/core/i18n.ts`).
    """
    shown = {
        label_ru.casefold(),
        label_en.casefold(),
        (row.get("name_ru") or "").casefold(),
        (row.get("name_en") or "").casefold(),
    }

    def dedup(values: list[str]) -> list[str]:
        result = []
        for value in values:
            cleaned = " ".join((value or "").split())
            if cleaned and cleaned.casefold() not in shown:
                shown.add(cleaned.casefold())
                result.append(cleaned)
        return result

    return {
        "openalex": dedup(row.get("name_variants") or []),
        "orcid": dedup(row.get("other_names") or []),
    }


class AuthorNodeBuilder:
    """Builds author rows in two forms at once - holds the context shared
    by every row (assignment/table/positions) instead of threading it as a
    parameter to a free function."""

    def __init__(
        self,
        db: dict[str, list[dict]],
        authorship: Authorship,
        assignment: DepartmentAssignment,
        table: DepartmentTable,
        pos: dict[str, tuple[float, float]],
    ) -> None:
        self.db = db
        self.authorship = authorship
        self.assignment = assignment
        self.table = table
        self.pos = pos

    def build(self) -> tuple[list[dict], list[dict]]:
        """Builds author rows in two forms at once.

        Returns:
            `(summary, detail)` - `summary` goes into `graph-data.json`,
            `detail` into `authors-detail.json`. Both hold the SAME data
            regardless of build variant - privacy is no longer decided here
            by trimming fields, it's decided outside, by which folder
            `main()` writes the final file into (`authors-detail.json` only
            ever goes into `private/`, see `graph_builder.py`). The map
            label (`label`/`label_en`) is always the truncated form
            (`author_label(..., public=True)`) - the only option now that
            `graph-data.json` is one shared file for every variant; the full
            name is only visible via `name_ru`/`name_en` in detail, which is
            private by location.
        """
        # set() - a publication could have been counted twice from some data
        # mismatch, so count unique ids, not the raw list length.
        pubs_count = {per: len(set(self.authorship.author_pubs.get(per, []))) for per in self.assignment.static_depts}
        rank_a = dense_rank(pubs_count)
        summary: list[dict] = []
        detail: list[dict] = []
        for row in self.db["persons"]:
            pid_ = row["id"]  # "id", not "key" - that's the column name in the snapshot
            x, y = self.pos[pid_]
            # label_ru/label_en are always the truncated form (public=True):
            # the map is drawn from one shared file across every build
            # variant, so the full name can never live here, see build()'s docstring.
            label_ru = author_label(row["surname_ru"], row["first_name_ru"], row["second_name_ru"], public=True) or row.get("name_ru") or ""
            label_en = author_label(row["surname_en"], row["first_name_en"], row["second_name_en"], public=True) or label_ru
            summary.append(
                {
                    "key": pid_,
                    "kind": "author",
                    "dept": self.table.g(self.assignment.author_dept[pid_]),
                    "label": label_ru,
                    "label_en": label_en,
                    "pubs_count": pubs_count[pid_],
                    "rank": rank_a[pid_],
                    "gx": x,
                    "gy": y,
                }
            )
            detail.append(
                {
                    "key": pid_,
                    "openalex_id": row.get("openalex_id") or "",
                    "name_ru": row.get("name_ru") or "",
                    "name_en": row.get("name_en") or "",
                    "name_variants": author_variants(row, label_ru, label_en),
                    "degree": row["degree"] or "",
                    "github": row["github"] or "",
                    "orcid": row.get("orcid") or "",
                    "google_scholar": row.get("google_scholar") or "",
                    "openreview": row.get("openreview") or "",
                    "email": row.get("email") or "",
                    # emails - a native graph property list, like
                    # name_variants/other_names, not JSON-text (see _parse_json_list).
                    "emails": row.get("emails") or [],
                    "affiliations": _parse_json_list(row.get("affiliations")),
                }
            )
        return summary, detail


class RepoNodeBuilder:
    """Builds repository rows in two forms at once (summary/detail)."""

    def __init__(
        self,
        db: dict[str, list[dict]],
        assignment: DepartmentAssignment,
        table: DepartmentTable,
        pos: dict[str, tuple[float, float]],
    ) -> None:
        self.db = db
        self.assignment = assignment
        self.table = table
        self.pos = pos

    def build(self) -> tuple[list[dict], list[dict]]:
        """Builds repository rows in two forms at once.

        Returns:
            `(summary, detail)` - `summary` in `graph-data.json["repos"]`,
            `detail` in `repos-detail.json`.
        """
        # rank - the same dense 0..1 scale as authors (dense_rank), but ranked by stars.
        stars = {r["id"]: (r["stars_num"] or 0) for r in self.db["repositories"]}
        rank_r = dense_rank(stars)
        summary: list[dict] = []
        detail: list[dict] = []
        for row in self.db["repositories"]:
            rid = row["id"]
            x, y = self.pos[rid]
            summary.append(
                {
                    "key": rid,
                    "kind": "repo",
                    "dept": self.table.g(self.assignment.repo_dept[rid]),
                    "label": row["name"] or "",
                    "stars": row["stars_num"] or 0,
                    "rank": rank_r[rid],
                    "gx": x,
                    "gy": y,
                }
            )
            # Unlike authors, repositories have no --public restriction -
            # detail is written for every repository unconditionally.
            detail.append(
                {
                    "key": rid,
                    "description": row["description"] or "",
                    "url": row["url"] or "",
                    "has_readme": bool(row["has_readme"]),
                    "license": row.get("license") or "",
                    # contributors is a list of GitHub logins, not a count.
                    "contributors": row.get("contributors") or [],
                    "owner_type": row.get("owner_type") or "",
                }
            )
        return summary, detail


class PubNodeBuilder:
    """Builds publication rows in two forms at once (summary/detail).

    The detail part is what a separate `build_search_detail()` used to
    build for `graph-search.js`: title, journal, DOI, code. Different from
    the original in one way - code returns the link list as-is (already a
    list from `pauk.cache`), parsing `code_url` out of a JSON string is this
    class's job now, not the snapshot consumer's a layer up.
    """

    def __init__(
        self,
        authorship: Authorship,
        assignment: DepartmentAssignment,
        table: DepartmentTable,
        pos: dict[str, tuple[float, float]],
    ) -> None:
        self.authorship = authorship
        self.assignment = assignment
        self.table = table
        self.pos = pos

    def build(self) -> tuple[list[dict], list[dict]]:
        """Builds publication rows in two forms at once.

        Returns:
            `(summary, detail)` - `summary` in `graph-data.json["pubs"]`,
            `detail` in `pubs-detail.json`.
        """
        # rank - by author count (n_authors), not by year or department.
        n_authors_of = {pid: len(set(self.authorship.pub_authors[pid])) for pid in self.authorship.pub_ids}
        rank_p = dense_rank(n_authors_of)
        summary: list[dict] = []
        for row in self.authorship.pubs_rows:
            pid, year = row["id"], row["year"]
            x, y = self.pos[pid]
            # depts_all - ALL of a publication's departments (the full
            # PRODUCED_BY list plus the primary one, unioned in case the
            # primary somehow wasn't in pub_dept_rows) - used by the frontend
            # to highlight a publication in every one of its departments at
            # once, not just the primary one ("dept" below).
            depts_all = sorted(
                {self.table.g(d) for d in self.assignment.pub_dept_rows.get(pid, [])}
                | {self.table.g(self.assignment.pub_primary[pid])}
            )
            summary.append(
                {
                    "key": pid,
                    "kind": "pub",
                    "dept": self.table.g(self.assignment.pub_primary[pid]),
                    "depts": depts_all,
                    "year": year,
                    "n_authors": n_authors_of[pid],
                    "rank": rank_p[pid],
                    "gx": x,
                    "gy": y,
                }
            )

        detail: list[dict] = []
        for row in self.authorship.pubs_rows:
            title = row["title"] or ""
            if len(title) > PUB_TITLE_MAX_LEN:
                title = title[: PUB_TITLE_MAX_LEN - 1] + "…"  # -1 so the ellipsis doesn't push the total past the limit
            detail.append(
                {
                    "key": row["id"],
                    "label": title,
                    "journal": row["journal"] or "",
                    "doi": row["doi"] or "",
                    "has_code": bool(row["has_code"]),
                    "code_url": _parse_json_list(row["code_url"]),
                    "type": row.get("type") or "",
                    # fields is a native OpenAlex topic list, not JSON-text.
                    "fields": row.get("fields") or [],
                    "funding": _parse_json_list(row.get("funding")),
                    "versions": _parse_json_list(row.get("versions")),
                    "openalex_url": row.get("openalex_url") or "",
                    "abstract": row.get("abstract") or "",
                }
            )
        return summary, detail

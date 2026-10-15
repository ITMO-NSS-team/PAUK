"""Builds author, repository and publication nodes in two forms each.

"summary" is what's needed to draw a point on the map; "detail" holds extended
fields that pauk/gui/web loads lazily after the map.
"""

from __future__ import annotations

import json

from .authorship import Authorship
from .departments import DepartmentAssignment, DepartmentTable
from .grants import add_grant_keys


def dense_rank(values: dict[str, int]) -> dict[str, float]:
    """Dense rank of the metric divided by the number of unique values, rounded to 3.

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


# Local rather than in config.py: used in one place only (same as STRANDED_JITTER in layout.py).
PUB_TITLE_MAX_LEN = 200


def _parse_json_list(text: str | None) -> list:
    """Parses a JSON-text snapshot field into a list.

    Covers `funding`/`versions`/`affiliations`/`code_url`, see `JSON_TEXT_FIELDS`
    in `pauk/graph/extract.py`. Missing or broken data gives an empty list: it's
    not worth failing the whole generation over.
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

    Collapses to an initial for the public display, or when the part sits next
    to another part. Otherwise left as the author gave it, except that a bare
    initial (some LLM/catalog data) always gets a period.
    """
    stripped = value.rstrip(".")
    if force_initial or len(stripped) == 1:
        return _initial(stripped)
    return value


def author_label(
    surname: str | None, first: str | None, second: str | None, *, public: bool = False
) -> str:
    """One format for any author: surname first, then initials.

    Takes the three name parts of one language; a label per language is
    assembled separately. There is no fallback to a raw combined string:
    guessing surname/given-name/patronymic by word order is what the LLM step in
    author_names.py replaced. An empty surname returns "", and the caller picks
    the fallback.
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
    """Other spellings of this person's name, minus whatever is already the card title.

    The private card's title (`pauk/gui/web/src/features/panels.ts`) is the
    truncated label until detail has merged in, then the full `name_ru`/`name_en`.
    Those are excluded here so the same name does not show twice. Sources are
    kept separate: `name_variants` is what OpenAlex saw across publications,
    `other_names` is the ORCID credit name plus self-registered variants (see
    `field.nameVariantsOpenAlex`/`field.nameVariantsOrcid` in
    `pauk/gui/web/src/core/i18n.ts`).
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
    """Builds author rows in two forms, holding the shared context as state."""

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
            `detail` into `authors-detail.json`. Privacy is decided by which
            folder `write_site_data()` puts the file in, not by trimming
            fields here. The map label is the full "Surname I.O." form,
            `author_label(..., public=False)`.
        """
        # set() because a data mismatch could count a publication twice.
        pubs_count = {per: len(set(self.authorship.author_pubs.get(per, []))) for per in self.assignment.static_depts}
        rank_a = dense_rank(pubs_count)
        pub_roles: dict[str, dict[str, dict]] = {}
        for rel in self.db["authorship"]:
            pub_roles.setdefault(rel["per"], {})[rel["pid"]] = {
                "position": rel.get("position"),
                "corresponding": bool(rel.get("is_corresponding")),
            }
        summary: list[dict] = []
        detail: list[dict] = []
        for row in self.db["persons"]:
            pid_ = row["id"]  # "id", not "key": the snapshot column name
            x, y = self.pos[pid_]
            # Full surname + initials, see build()'s docstring.
            label_ru = author_label(row.get("surname_ru"), row.get("first_name_ru"), row.get("second_name_ru"), public=False) or row.get("name_ru") or ""
            # The *_en name parts are not populated by any pipeline stage, but
            # name_en (free-text transliteration) is. Falling back to it, not
            # to label_ru, keeps the EN interface from showing Russian names.
            # It is not run through author_label(): there is no reliable
            # surname/given-name split for free text.
            label_en = (
                author_label(row.get("surname_en"), row.get("first_name_en"), row.get("second_name_en"), public=False)
                or row.get("name_en")
                or label_ru
            )
            summary.append(
                {
                    "key": pid_,
                    "kind": "author",
                    "is_itmo": pid_ not in self.authorship.external_ids,
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
                    "degree": row.get("degree") or "",
                    "github": row.get("github") or "",
                    "orcid": row.get("orcid") or "",
                    "google_scholar": row.get("google_scholar") or "",
                    "email": row.get("email") or "",
                    "affiliations": _parse_json_list(row.get("affiliations")),
                    # Per-publication author position and corresponding flag, keyed by publication id.
                    "pub_roles": pub_roles.get(pid_, {}),
                    "created_at": row.get("created_at") or "",
                    "updated_at": row.get("updated_at") or "",
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
        group_of: dict[str, int],
        pos: dict[str, tuple[float, float]],
    ) -> None:
        self.db = db
        self.assignment = assignment
        self.table = table
        self.group_of = group_of
        self.pos = pos

    def build(self) -> tuple[list[dict], list[dict]]:
        """Builds repository rows in two forms at once.

        Returns:
            `(summary, detail)` - `summary` in `graph-data.json["repos"]`,
            `detail` in `repos-detail.json`.
        """
        # rank: the same dense 0..1 scale as authors, but by stars.
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
                    # Colour group on the repositories tab, see departments.py::repo_groups.
                    "group": self.group_of[rid],
                    "label": row["name"] or "",
                    "stars": row["stars_num"] or 0,
                    "rank": rank_r[rid],
                    "gx": x,
                    "gy": y,
                }
            )
            # Unlike authors, repository detail is written unconditionally.
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
    """Builds publication rows in two forms at once (summary/detail)."""

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
        # rank is by author count, not by year or department.
        n_authors_of = {pid: len(set(self.authorship.pub_authors[pid])) for pid in self.authorship.pub_ids}
        rank_p = dense_rank(n_authors_of)
        summary: list[dict] = []
        for row in self.authorship.pubs_rows:
            pid, year = row["id"], row["year"]
            x, y = self.pos[pid]
            # depts_all: ALL of a publication's departments (PRODUCED_BY list plus
            # the primary, in case it wasn't in pub_dept_rows), so the frontend
            # can highlight it in each of them.
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
                title = title[: PUB_TITLE_MAX_LEN - 1] + "…"  # -1 keeps the ellipsis within the limit
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
        add_grant_keys(detail)
        return summary, detail

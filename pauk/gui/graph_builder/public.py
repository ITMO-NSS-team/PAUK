"""The public variant of the site data: what GitHub Pages may serve.

The private files are rewritten, not filtered by the site: a field hidden by
CSS is still in the JSON anyone can download. Every row is cut down to a
whitelist, so a field added to the builder later stays private until someone
adds it here on purpose.

- Author names are masked to surname plus initials (`author_label(...,
  public=True)`), in both languages.
- Author keys are renumbered (`a0`, `a1`, ...). The real ones are OpenAlex ids,
  which lead to the full name in one lookup, or `manual_person_<surname>_<name>`,
  which spell it out. Sequential keys mean a shared public link to an author
  card does not survive a rebuild; that is the price of keys that carry nothing.
- `authors-detail.json` keeps only the per-publication author position and
  corresponding flag. Emails, ORCID/GitHub/Scholar ids, degrees, affiliations,
  name variants and the record timestamps stay in the private build.
"""

from __future__ import annotations

from .nodes import author_label

AUTHOR_FIELDS = ("key", "kind", "is_itmo", "dept", "label", "label_en", "pubs_count", "rank", "gx", "gy")
REPO_FIELDS = ("key", "kind", "dept", "group", "label", "stars", "rank", "gx", "gy")
PUB_FIELDS = ("key", "kind", "dept", "depts", "year", "n_authors", "rank", "gx", "gy")
AUTHOR_DETAIL_FIELDS = ("key", "pub_roles")
REPO_DETAIL_FIELDS = ("key", "description", "url", "has_readme", "license", "contributors", "owner_type")
# `versions` is left out: the site never reads it, and it repeats the pipeline's
# merge of duplicate records rather than anything a reader needs.
PUB_DETAIL_FIELDS = (
    "key", "label", "journal", "doi", "has_code", "code_url", "type", "fields", "funding", "openalex_url", "abstract",
)

# Which end of each edge list is an author key.
AUTHOR_EDGE_ENDS = {"coauth_edges": ("s", "t"), "repo_author_edges": ("t",), "all_edges": ("s",)}


def mask_free_text_name(text: str | None) -> str:
    """Surname plus initials for a name known only as one string.

    The snapshot writes these surname first ("Kochemazov Stepan Evgenievich"),
    so the first word is kept and every later one, including dotted initials
    like "M.D.", becomes an initial. A name written given-name first loses its
    surname to an initial instead, but no full given name survives either way.
    """
    words = (text or "").replace(".", " ").split()
    return " ".join([*words[:1], *(f"{word[0].upper()}." for word in words[1:])])


def public_author_labels(row: dict) -> tuple[str, str]:
    """`(label, label_en)` for the public map, from one snapshot person row."""
    label_ru = author_label(
        row.get("surname_ru"), row.get("first_name_ru"), row.get("second_name_ru"), public=True
    ) or mask_free_text_name(row.get("name_ru"))
    label_en = (
        author_label(row.get("surname_en"), row.get("first_name_en"), row.get("second_name_en"), public=True)
        or mask_free_text_name(row.get("name_en"))
        or label_ru
    )
    return label_ru, label_en


def _pick(row: dict, fields: tuple[str, ...]) -> dict:
    return {field: row[field] for field in fields if field in row}


def to_public(
    summary: dict, detail: dict[str, list[dict]], persons: list[dict]
) -> tuple[dict, dict[str, list[dict]]]:
    """Rewrites the private `(summary, detail)` of `GraphDataBuilder.build()` for publishing.

    Args:
        summary: Contents of the private `graph-data.json`.
        detail: `{"authors": [...], "repos": [...], "pubs": [...]}`, the private detail files.
        persons: Snapshot person rows, the source of the name parts for masking.

    Returns:
        The same shape, safe to publish.
    """
    person_by_id = {row["id"]: row for row in persons}
    new_key = {author["key"]: f"a{i}" for i, author in enumerate(summary["authors"])}

    authors = []
    for author in summary["authors"]:
        label, label_en = public_author_labels(person_by_id.get(author["key"], {}))
        authors.append({**_pick(author, AUTHOR_FIELDS), "key": new_key[author["key"]], "label": label, "label_en": label_en})

    public_summary = {
        **summary,
        "authors": authors,
        "repos": [_pick(row, REPO_FIELDS) for row in summary["repos"]],
        "pubs": [_pick(row, PUB_FIELDS) for row in summary["pubs"]],
    }
    for edges, ends in AUTHOR_EDGE_ENDS.items():
        public_summary[edges] = [{**edge, **{end: new_key[edge[end]] for end in ends}} for edge in summary[edges]]

    public_detail = {
        "authors": [
            {**_pick(row, AUTHOR_DETAIL_FIELDS), "key": new_key[row["key"]]}
            for row in detail["authors"]
            if row["key"] in new_key
        ],
        "repos": [_pick(row, REPO_DETAIL_FIELDS) for row in detail["repos"]],
        "pubs": [_pick(row, PUB_DETAIL_FIELDS) for row in detail["pubs"]],
    }
    return public_summary, public_detail

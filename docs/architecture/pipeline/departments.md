# `departments` stage

**What this covers:** how authors and their publications get linked to ITMO
departments.

**Files involved:** `pauk/pipeline/stages/departments.py`.

For each `Person` the stage joins the text of all its declared affiliations
(`person.authored[*].affiliation`), normalizes it (casefold, quotes and dashes
folded to spaces, whitespace collapsed) and matches the departments from
`StaticStore` against it. A department matches if its `name_en`, `name_ru` or
**any** of its `name_variants` is a substring of that text. Matching is plain
containment, not word-boundary: word boundaries would drop numbered
("2School of ...") and plural ("Sciences") forms without removing real false
positives.

Generic names such as "Department of Physics" also name foreign units, so a
department's `context_aliases` are matched in a second pass, only in an
affiliation part (split on newline, comma and semicolon) that is or neighbors a part with an ITMO marker (`itmo`, `ifmo`, the Russian
abbreviation, "Information Technologies, Mechanics ..."). Of several aliases
matching one part, the most specific wins.

The matched ids go to `Person.department_ids` and, for ITMO persons, to
`department_ids` of every publication the person authored. The stage also
rewrites the `departments` and `organizations` collections from the static
catalog (`data/static/departments_catalog.json`).

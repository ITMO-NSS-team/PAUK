"""A second collection group, overlapping the 2026 universe on purpose.

The graph accumulates every published group, while the dedup enrichment
stage only ever sees one of them: a researcher, a work or a repository
collected in two periods becomes two nodes that no per-group pass can
compare. This universe is the other period, built so that each kind of
cross-group duplicate appears exactly once.

Overlaps with tests/bench/universe.py
-------------------------------------
* W7000000005  literally the same work in both groups: one Publication node
               and one set of AUTHORED edges, never a second copy
* A31          external in the 2026 group, ITMO here -> the :Itmo label is
               sticky, and republishing the 2026 group must not undo it
* A71/A72      "Nikolay Nikitin" and "N. O. Nikitin" carry one ORCID in
               their OpenAlex records but live in different groups -> only
               `pauk dedup graph` can fold them
* W139         the 2026 version of record of the preprint published here as
               W80000002003 (same title, later date) -> folded graph-wide
* BenchOrg15/GammaLib  cited here under its pre-rename name old-gamma; both
               payloads carry GitHub's numeric id, so the two Repository
               nodes are one repository
"""

from __future__ import annotations

from .universe import (
    ITMO_INSTITUTION,
    NIKITIN_ORCID,
    author_id,
    author_name,
    build_crossref,
    build_universe,
)

SHARED_WORK_ID = "W7000000005"
STICKY_PERSON_ID = author_id(31)          # external in 2026, ITMO here
NIKITIN_2026_ID = author_id(71)
NIKITIN_2025_ID = author_id(72)
NIKITIN_2025_NAME = "N. O. Nikitin"

CROSS_PERIOD_PREPRINT_ID = "W80000002003"
CROSS_PERIOD_VOR_ID = "W70000000139"
CROSS_PERIOD_TITLE = "Bench cross-period study"

OLD_GAMMA_URL = "https://github.com/BenchOrg15/old-gamma"
OLD_GAMMA_REPO_ID = "github_benchorg15_old-gamma"
GAMMA_REPO_ID = "github_benchorg15_gammalib"

WORK_IDS_2025 = (
    SHARED_WORK_ID,
    "W80000002001",
    "W80000002002",
    CROSS_PERIOD_PREPRINT_ID,
    "W80000002004",
)


def _authorship(index: int, affiliation: str, itmo: bool, name: str | None = None) -> dict:
    author: dict = {"id": f"https://openalex.org/{author_id(index)}"}
    display_name = name or author_name(index)
    if display_name is not None:
        author["display_name"] = display_name
    entry: dict = {"author": author, "raw_affiliation_strings": [affiliation]}
    if itmo:
        entry["institutions"] = [ITMO_INSTITUTION]
    return entry


def _abstract(text: str) -> dict[str, list[int]]:
    index: dict[str, list[int]] = {}
    for position, word in enumerate(text.split()):
        index.setdefault(word, []).append(position)
    return index


ITMO_ROBOTICS = "ITMO University, School of Robotics, St. Petersburg, Russia"
ITMO_IACS = "ITMO University, Institute of Applied Computer Science, St. Petersburg, Russia"


def build_universe_2025(base: dict | None = None) -> dict:
    """The 2025 group's payloads, sharing the 2026 group's services.

    Args:
        base: A `build_universe()` result to reuse, so both groups agree on
            author records, repositories and the shared work. Built on
            demand when omitted.
    """
    base = base or build_universe()
    shared_work = next(work for work in base["works"] if work["id"].endswith(SHARED_WORK_ID))

    works: list[dict] = [
        shared_work,
        {
            "id": "https://openalex.org/W80000002001",
            "title": "Bench 2025 robotics report",
            "doi": "https://doi.org/10.7777/synth2025.001",
            "publication_date": "2025-03-10",
            "primary_location": {"source": {"display_name": "Synthetic Journal"}},
            # A31 is external in the 2026 group and ITMO here.
            "authorships": [
                _authorship(31, ITMO_ROBOTICS, itmo=True),
                _authorship(32, "External University 32", itmo=False),
            ],
            "abstract_inverted_index": _abstract("Short synthetic abstract 2025-001."),
        },
        {
            "id": "https://openalex.org/W80000002002",
            "title": "Bench 2025 evolutionary study",
            "doi": "https://doi.org/10.7777/synth2025.002",
            "publication_date": "2025-05-10",
            "primary_location": {"source": {"display_name": "Synthetic Journal"}},
            "authorships": [
                _authorship(72, ITMO_IACS, itmo=True, name=NIKITIN_2025_NAME),
                _authorship(17, ITMO_IACS, itmo=True),
            ],
            "abstract_inverted_index": _abstract("Short synthetic abstract 2025-002."),
        },
        {
            # The preprint of W139: same title, earlier date, own DOI.
            "id": f"https://openalex.org/{CROSS_PERIOD_PREPRINT_ID}",
            "title": CROSS_PERIOD_TITLE,
            "doi": "https://doi.org/10.7777/preprint2025.139",
            "publication_date": "2025-11-01",
            "primary_location": {"source": {"display_name": "Synthetic Preprint Server"}},
            "authorships": [
                _authorship(28, "ITMO University, Faculty of Photonics, St. Petersburg, Russia", itmo=True),
                _authorship(29, "ITMO University, Biotech Research Center, St. Petersburg, Russia", itmo=True),
            ],
            "abstract_inverted_index": _abstract("Short synthetic abstract 2025-003."),
        },
        {
            "id": "https://openalex.org/W80000002004",
            "title": "Bench 2025 code release",
            "doi": "https://doi.org/10.7777/synth2025.004",
            "publication_date": "2025-07-10",
            "primary_location": {"source": {"display_name": "Synthetic Journal"}},
            "authorships": [_authorship(33, "External University 33", itmo=False)],
            "abstract_inverted_index": _abstract(
                f"Short synthetic abstract 2025-004. Code: {OLD_GAMMA_URL} ."),
        },
    ]

    authors_api = dict(base["authors_api"])
    authors_api[NIKITIN_2025_ID] = {
        "id": f"https://openalex.org/{NIKITIN_2025_ID}",
        "display_name": NIKITIN_2025_NAME,
        "display_name_alternatives": ["Nikolay Nikitin"],
        # The same ORCID as A71 in the 2026 group: one researcher, split by
        # OpenAlex across two collection periods.
        "orcid": f"https://orcid.org/{NIKITIN_ORCID}",
    }

    # GitHub as it answered before the rename: the old name, the old URL and
    # the same numeric id the 2026 group saw under the new name.
    gamma = base["github"][("benchorg15", "gammalib")]
    github = dict(base["github"])
    github[("benchorg15", "old-gamma")] = {
        **gamma,
        "name": "old-gamma",
        "html_url": OLD_GAMMA_URL,
    }

    return {
        "works": works,
        "authors_api": authors_api,
        "github": github,
        "renamed": base["renamed"],
        "crossref": {**base["crossref"], **build_crossref(works)},
        "orcid": base["orcid"],
        "departments_catalog": base["departments_catalog"],
    }

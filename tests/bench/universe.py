"""Fully synthetic test universe: 124 works, 62 authors, 80 repositories.

Deterministic (no randomness) and self-describing: every tricky case the
pipeline must survive is a named constant here, and `build_universe()`
returns the raw payloads exactly as the external APIs would serve them.

Tricky cases covered
--------------------

Authors (A5000000001..A5000000059, 38 ITMO / 21 external before dedup):
* A01-A05  ITMO affiliation missing on odd-numbered works (identity merge,
           sticky is_itmo)
* A06/A07  same family name ("Ivanov") on the same work -> the Crossref
           ORCID match is ambiguous there and must NOT be assigned
* A08      hyphenated surname, Crossref match works via split()[-1]
* A09      multi-word surname ("van der Berg") — known limitation: the
           family-name heuristic can't match it (stays without ORCID)
* A10      display_name missing entirely (recovered by the authors API)
* A11      Unicode name with diacritics + Cyrillic name variant
* A12      listed twice in the same work's authorship list (real OpenAlex
           data quirk, W018)
* A13      ORCID already present in the OpenAlex author payload
* A14      ORCID arrives only via Crossref (plus an email via ORCID record)
* A16      OpenAlex authors endpoint fails -> persons stage FAILED

Split-author duplicates for the dedup stage (works W101..W110):
* A51/A52  same person split by OpenAlex: "Dmitry Kovalev" (ITMO) and
           "D. A. Kovalev" (external, affiliation missed) share one ORCID
           in their author records -> merged into A51, is_itmo survives
* A53/A54/A55  one person, three spellings: "Ekaterina Smirnova" lists
           "E. Smirnova" and "Екатерина Смирнова" among her name variants;
           each duplicate shares coauthor A17 -> the whole group folds
           transitively into A53 (most works)
* A56/A57  identical ITMO display names ("Ivan Volkov") with nothing
           explicit telling them apart -> merged by default into A56
* A58/A59  "Olga Fedorova" lists "O. Fedorova" as a variant and they share
           a coauthor, but their author records carry different ORCIDs ->
           an explicit difference: NOT merged and not even a candidate

Works (W7000000001..W7000000100):
* W001     GitHub URL with a sentence-ending period
* W002     GitHub URL with a ".git" suffix
* W003     GitHub URL with a trailing slash
* W004/5   the same repo cited in different letter case in two works
* W006/7   a renamed repo: W006 cites the old URL (the API serves the
           canonical payload, like GitHub's 301), W007 cites the new URL —
           must end up as ONE repository
* W008     URL of a deleted repo (404 -> stage FAILED -> LinkCandidate)
* W009     www.github.com citation
* W010     two different repos plus a duplicated URL in one abstract
* W011     gitlab.com URL — ignored by the GitHub regex
* W012     no abstract at all
* W013     no title (-> "Untitled"), no publication_date, no DOI
* W014     no DOI (crossref stage NOT_APPLICABLE)
* W015     DOI unknown to Crossref (crossref stage FAILED)
* W016     zero authorships
* W017     12 co-authors (incl. both Ivanovs -> Crossref ambiguity)
* W018     A12 appears twice in the authorship list
* W019     grants/funding present (stored as JSON text on the node) + 404 repo
* W020     pdf_url present + 404 repo (typo of a real name)

Duplicate publication records for the dedup stage (works W111..W120):
* W111/W112  one DOI re-indexed into two OpenAlex records; W112 carries no
             authors at all -> the documented record survives
* W113/W114  preprint and version of record: same title, different DOI,
             journal and date -> the later record survives and keeps both
             venues in `versions`
* W115-W117  one deposit re-released three times -> the newest survives
             holding three versions
* W118       untitled like W013: the "Untitled" placeholder must never
             merge two works
* W119/W120  one title differing only in letter case and spacing -> merged

Records OpenAlex has not finished processing:
* W121      every author.id is null — one author is keyed by ORCID, one by
            name, and the one with neither is the only authorship dropped
* W122      title deposited with publisher markup (<sub>, MathML, <i>)
* W123      a GitHub release archived on Zenodo: the repository is named
            only in the title, and the deposit says nothing about where its
            author works — the author's own OpenAlex record does
* W124      a consortium paper whose author list the works endpoint
            truncates: the ITMO participant is beyond the cut and only the
            single-work record names them, and the consortium itself sits in
            an author slot without being a person

Repositories: 80 canonical repos across 16 owners (5 each); all 80 are
cited at least once. 3 phantom URLs are cited but never existed (404), one
alias URL redirects to a canonical repo, and some owner payloads miss
`name`/`type`. A row written before a repository was renamed (STALE_REPO_ID)
is seeded between the two enrichment runs: only GitHub's numeric id ties it
to the row the new name produced.
"""

from __future__ import annotations

AUTHOR_IDS = [f"A50000000{i:02d}" for i in range(1, 60)]
WORK_IDS = [f"W70000000{i:02d}" for i in range(1, 125)]

# W124: a consortium paper. The list endpoint truncates its author list (the
# is_authors_truncated flag stands in for the 100-entry cap, which would be
# unwieldy to synthesize) and the ITMO participant is beyond the cut, so the
# collector must fetch the single-work record to find them. The full list
# also carries the consortium itself in an author slot — an organization,
# not a person.
CONSORTIUM_WORK = "W70000000124"
CONSORTIUM_ITMO_INDEX = 22  # existing ITMO author A5000000022
CONSORTIUM_ORG_NAME = "Synthetic Science Consortium Collaborators"
CONSORTIUM_FILLERS = [f"A59000001{i:02d}" for i in range(3)]

# W123: a GitHub release archived on Zenodo. OpenAlex indexes it as a work of
# its own, and the deposit names neither the repository as a link nor the
# author's affiliation — both have to come from elsewhere.
ARCHIVE_WORK = "W70000000123"
ARCHIVE_REPO_OWNER, ARCHIVE_REPO_NAME = "BenchOrg8", "AlphaTool"
ARCHIVE_TITLE = f"{ARCHIVE_REPO_OWNER}/{ARCHIVE_REPO_NAME}: Release v1.2.3"
ARCHIVE_REPO_ID = f"github_{ARCHIVE_REPO_OWNER.lower()}_{ARCHIVE_REPO_NAME.lower()}"
ARCHIVE_REPO_URL = f"https://github.com/{ARCHIVE_REPO_OWNER}/{ARCHIVE_REPO_NAME}"
ARCHIVE_AUTHOR_INDEX = 41
ARCHIVE_AUTHOR_ID = f"A50000000{ARCHIVE_AUTHOR_INDEX}"
ARCHIVE_AUTHOR_AFFILIATION = f"External University {ARCHIVE_AUTHOR_INDEX}"

# W121: a record OpenAlex has not disambiguated yet — the authors are listed
# but every author.id is null, so they can only be keyed by what they carry.
UNIDENTIFIED_WORK = "W70000000121"
UNIDENTIFIED_ORCID = "0000-0009-0000-0121"
UNIDENTIFIED_BY_ORCID = f"orcid_{UNIDENTIFIED_ORCID}"
UNIDENTIFIED_BY_NAME = "Marina Bez Ida"  # no id, no ORCID: keyed by name
UNIDENTIFIED_NAMELESS = 1  # neither id nor name: unkeyable

# W122: publisher markup OpenAlex passes through from Crossref verbatim.
MARKUP_WORK = "W70000000122"
MARKUP_TITLE = (
    "Growth of CaF <sub>2</sub> /Si(111) and monolayer "
    '<mml:math xmlns:mml="http://www.w3.org/1998/Math/MathML"> <mml:msub> '
    "<mml:mi>WSe</mml:mi> <mml:mn>2</mml:mn> </mml:msub> </mml:math> in "
    "<i>vacuo</i>"
)
MARKUP_TITLE_CLEAN = "Growth of CaF2/Si(111) and monolayer WSe2 in vacuo"

# Duplicate publication records (works W111..W120). One work reaches OpenAlex
# as several records: a re-indexed duplicate of one DOI, a preprint and its
# version of record, a deposit re-released per version. Titles differing only
# in case and spacing are the same title; the "Untitled" placeholder is not.
DUPLICATE_DOI = "https://doi.org/10.7777/synth.dup"
PUBLICATION_TITLES = {
    111: "Bench duplicate record",
    112: "Bench duplicate record",
    113: "Bench preprint and version of record",
    114: "Bench preprint and version of record",
    115: "Bench dataset deposit",
    116: "Bench dataset deposit",
    117: "Bench dataset deposit",
    119: "Bench Case Variant Study",
    120: "  bench case VARIANT   study ",
}
PUBLICATION_JOURNALS = {
    111: "Synthetic Journal",
    112: "Synthetic Journal",
    113: "Synthetic Preprint Server",
    114: "Synthetic Journal",
    115: "Synthetic Data Archive",
    116: "Synthetic Data Archive",
    117: "Synthetic Data Archive",
}
PUBLICATION_DOIS = {
    111: DUPLICATE_DOI,
    112: DUPLICATE_DOI,
    113: "https://doi.org/10.7777/preprint.113",
    114: "https://doi.org/10.7777/vor.114",
    115: "https://doi.org/10.7777/deposit.115",
    116: "https://doi.org/10.7777/deposit.116",
    117: "https://doi.org/10.7777/deposit.117",
}
PUBLICATION_DATES = {
    111: "2026-03-15",
    112: "2026-03-15",  # one DOI, one date: authors decide
    113: "2026-02-15",
    114: "2026-09-15",  # the version of record comes later
    115: "2026-01-20",
    116: "2026-04-20",
    117: "2026-07-20",
    119: "2026-06-05",
    120: "2026-05-05",
}
# Same authors on both records of a work: their authorships must collapse.
DUPLICATE_WORK_AUTHORS = {
    111: (21, 22),
    112: (),  # the re-indexed record carries no authors at all
    113: (24, 25),
    114: (24, 25),
    115: (26, 27),
    116: (26, 27),
    117: (26, 27),
    118: (30,),
    119: (28, 29),
    120: (28, 29),
}
# Merged-away work id -> the record that survives.
PUBLICATION_MERGES = {
    "W70000000112": "W70000000111",
    "W70000000113": "W70000000114",
    "W70000000115": "W70000000117",
    "W70000000116": "W70000000117",
    "W70000000120": "W70000000119",
}
UNTITLED_WORK_IDS = ("W7000000013", "W70000000118")

# A repository renamed on GitHub between two runs: the row written before the
# rename survives in prepared under its old id and URL, and only GitHub's
# numeric id ties it to the row the new name produced.
STALE_REPO_ID = "github_benchorg7_legacy-name"
STALE_REPO_URL = "https://github.com/BenchOrg7/legacy-name"
STALE_REPO_CANONICAL_ID = "github_benchorg7_alphatool"
STALE_REPO_PUBLICATION = "W7000000021"

# Split-author cases: ids merged away by the dedup stage and where they go.
DEDUP_MERGES = {
    "A5000000052": "A5000000051",
    "A5000000054": "A5000000053",
    "A5000000055": "A5000000053",
    "A5000000057": "A5000000056",
}
# (author index, filler coauthor index) per dedup work W101..W110.
DEDUP_WORK_AUTHORS = {
    101: (51, 20),
    102: (52, 21),
    103: (53, 17),
    104: (54, 17),
    105: (55, 17),
    110: (53, 24),
    106: (56, 22),
    107: (57, 22),
    108: (58, 23),
    109: (59, 23),
}
KOVALEV_ORCID = "0000-0006-0000-0051"
FEDOROVA_ORCIDS = {58: "0000-0007-0000-0058", 59: "0000-0007-0000-0059"}

ITMO_ROR = "https://ror.org/04txgxn49"
ITMO_INSTITUTION = {"ror": ITMO_ROR, "display_name": "ITMO University"}

# Official staff records for the russian_names stage: A06 ("Oleg Ivanov")
# matches, the two records behind one name must never match anyone, and
# every other author falls back to transliteration.
RUSSIAN_NAMES_CATALOG = [
    "Иванов Олег Петрович,Иванов,Олег,Петрович,к.т.н.",
    "Смирнов Иван Петрович,Смирнов,Иван,Петрович,к.т.н.",
    "Смирнов Иван Васильевич,Смирнов,Иван,Васильевич,д.х.н.",
]

# Root organisation the top-level units hang off of; exercises the Organization
# node + the Department-[:PART_OF]->Organization edge end to end (guards against
# organizations silently never reaching the graph, see ENTITY_FILES in graph/load.py).
ORG_UID = "itmo"
ORG_NAME = "ITMO University"
# uid of the one unit wired under the organisation, so the bench can assert its
# PART_OF edge resolves.
ORG_CHILD_UID = "institute-of-applied-computer-science"

DEPARTMENTS_CATALOG = [
    {
        "uid": ORG_UID,
        "name_en": ORG_NAME,
        "name_ru": "Университет ИТМО",
        "kind": "organization",
        "parent": None,
        "ror_id": "https://ror.org/04txgxn49",
        "country": "RU",
        "type": "education",
    },
    {
        "uid": "institute-of-applied-computer-science",
        "name_en": "Institute of Applied Computer Science",
        "name_ru": "Институт прикладной информатики",
        "kind": "institute",
        "parent": ORG_UID,
        "aliases": ["IACS"],
    },
    {
        "uid": "faculty-of-photonics",
        "name_en": "Faculty of Photonics",
        "name_ru": "Факультет фотоники",
        "kind": "faculty",
        "parent": None,
        "aliases": [],
    },
    {
        "uid": "biotech-research-center",
        "name_en": "Biotech Research Center",
        "name_ru": "Центр биотехнологий",
        "kind": "center",
        "parent": None,
        "aliases": ["BioTech Center"],
    },
    {
        "uid": "school-of-robotics",
        "name_en": "School of Robotics",
        "name_ru": "Школа робототехники",
        "kind": "school",
        "parent": None,
        "aliases": ["Robotics School"],
    },
    {
        "uid": "quantum-computing-lab",
        "name_en": "Quantum Computing Lab",
        "name_ru": "Лаборатория квантовых вычислений",
        "kind": "lab",
        "parent": None,
        "aliases": ["QC Lab"],
    },
]
# Only the units (the organisation is a separate root, never an affiliation slot),
# so the modulo assignment in _affiliation stays exactly as before the org was added.
DEPT_NAMES = [d["name_en"] for d in DEPARTMENTS_CATALOG if d.get("kind") != "organization"]

REPO_OWNERS = [f"BenchOrg{i}" for i in range(1, 17)]
REPO_NAMES = ["AlphaTool", "beta-kit", "GammaLib", "delta.util", "EpsilonNet"]

CITE_TRAILING_DOT = "https://github.com/BenchOrg1/AlphaTool."
CITE_GIT_SUFFIX = "https://github.com/BenchOrg1/beta-kit.git"
CITE_TRAILING_SLASH = "https://github.com/BenchOrg1/GammaLib/"
CITE_LOWERCASE = "https://github.com/benchorg2/gammalib"
CITE_UPPERCASE = "https://github.com/BENCHORG2/GAMMALIB"
CITE_RENAMED_OLD = "https://github.com/BenchOrg3/old-alpha"
CITE_RENAMED_NEW = "https://github.com/BenchOrg3/AlphaTool"
CITE_DELETED = "https://github.com/GoneOrg/vanished-repo"
CITE_WWW = "https://www.github.com/BenchOrg4/AlphaTool"
PHANTOM_2 = "https://github.com/GoneOrg/never-was"
PHANTOM_3 = "https://github.com/BenchOrg5/typo-name"
PHANTOM_URLS = (CITE_DELETED, PHANTOM_2, PHANTOM_3)

RENAMED_ALIASES = {("benchorg3", "old-alpha"): ("benchorg3", "alphatool")}

# Repos already covered by the special works W001..W010 above.
SPECIALLY_CITED = {
    ("benchorg1", "alphatool"),
    ("benchorg1", "beta-kit"),
    ("benchorg1", "gammalib"),
    ("benchorg1", "delta.util"),
    ("benchorg1", "epsilonnet"),
    ("benchorg2", "gammalib"),
    ("benchorg3", "alphatool"),
    ("benchorg4", "alphatool"),
}

SPECIAL_CITATIONS: dict[int, list[str]] = {
    1: [CITE_TRAILING_DOT],
    2: [CITE_GIT_SUFFIX],
    3: [CITE_TRAILING_SLASH],
    4: [CITE_LOWERCASE],
    5: [CITE_UPPERCASE],
    6: [CITE_RENAMED_OLD],
    7: [CITE_RENAMED_NEW],
    8: [CITE_DELETED],
    9: [CITE_WWW],
    10: [
        "https://github.com/BenchOrg1/delta.util",
        "https://github.com/BenchOrg1/EpsilonNet",
        "https://github.com/BenchOrg1/delta.util",
    ],
    11: ["https://gitlab.com/some/project"],
    19: [PHANTOM_2],
    20: [PHANTOM_3],
}


def _inverted_index(text: str) -> dict[str, list[int]]:
    """OpenAlex-style abstract_inverted_index for a short text."""
    index: dict[str, list[int]] = {}
    for position, word in enumerate(text.split()):
        index.setdefault(word, []).append(position)
    return index


def repo_github_id(owner: str, name: str) -> int:
    """GitHub's numeric id for a synthetic repository."""
    return 100_000 + REPO_OWNERS.index(owner) * 10 + REPO_NAMES.index(name)


def _canonical_repos() -> dict[tuple[str, str], dict]:
    repos: dict[tuple[str, str], dict] = {}
    for oi, owner in enumerate(REPO_OWNERS):
        owner_payload = {
            "login": owner,
            "html_url": f"https://github.com/{owner}",
            "type": "Organization" if oi % 2 == 0 else None,
            "name": f"{owner} Team" if oi % 3 else None,
        }
        for ri, name in enumerate(REPO_NAMES):
            repos[(owner.lower(), name.lower())] = {
                # GitHub's numeric id: one per repository, unchanged by the
                # renames below (an old and a new name serve one payload).
                "id": repo_github_id(owner, name),
                "name": name,
                "html_url": f"https://github.com/{owner}/{name}",
                "description": f"Synthetic repo {owner}/{name}" if ri % 2 == 0 else None,
                "stargazers_count": oi * 10 + ri,
                "owner": owner_payload,
            }
    return repos


def _author_name(i: int) -> str | None:
    special = {
        6: "Oleg Ivanov",
        7: "Pavel Ivanov",
        8: "Anna Petrova-Sidorova",
        9: "Jan van der Berg",
        10: None,
        11: "José Álvarez-Müller",
        51: "Dmitry Kovalev",
        52: "D. A. Kovalev",
        53: "Ekaterina Smirnova",
        54: "E. Smirnova",
        55: "Екатерина Смирнова",
        56: "Ivan Volkov",
        57: "Ivan Volkov",
        58: "Olga Fedorova",
        59: "O. Fedorova",
    }
    if i in special:
        return special[i]
    return f"Author{i:02d} Surname{i:02d}"


def _affiliation(i: int, itmo: bool) -> list[str]:
    if not itmo:
        return [f"External University {i}"]
    if i == 18:
        return ["ITMO University, IACS, St. Petersburg, Russia"]  # alias spelling
    if i == 23:
        return ["ITMO University, BioTech Center, St. Petersburg, Russia"]  # alias spelling
    if 17 <= i <= 30:
        dept = DEPT_NAMES[(i - 17) % len(DEPT_NAMES)]
        return [f"ITMO University, {dept}, St. Petersburg, Russia"]
    return ["ITMO University, St. Petersburg, Russia"]


def _is_itmo(i: int, n: int) -> bool:
    """A01..A30 and the dedup authors (except A52) are ITMO; A01..A05 lose
    the affiliation on odd works, A52 is the external half of a split."""
    if 31 <= i <= 50 or i == 52:
        return False
    return not (i <= 5 and n % 2 == 1)


def _authorship(i: int, n: int) -> dict:
    itmo = _is_itmo(i, n)
    author: dict = {"id": f"https://openalex.org/{AUTHOR_IDS[i - 1]}"}
    name = _author_name(i)
    if name is not None:
        author["display_name"] = name
    if i == 11:
        author["display_name_alternatives"] = ["Хосе Альварес-Мюллер"]
    entry: dict = {"author": author, "raw_affiliation_strings": _affiliation(i, itmo)}
    if itmo:
        entry["institutions"] = [ITMO_INSTITUTION]
    return entry


def build_universe() -> dict:
    repos = _canonical_repos()
    remaining = [key for key in repos if key not in SPECIALLY_CITED]  # 72 repos

    works: list[dict] = []
    for n, work_id in enumerate(WORK_IDS, start=1):
        work: dict = {"id": f"https://openalex.org/{work_id}"}

        if n == 124:
            work["title"] = "Synthetic consortium paper"
        elif n == 123:
            work["title"] = ARCHIVE_TITLE
            work["type"] = "software"
        elif n == 122:
            work["title"] = MARKUP_TITLE
        elif n not in (13, 118):  # both untitled: a placeholder is not a title
            work["title"] = PUBLICATION_TITLES.get(n, f"Synthetic paper {n:03d}")
        if n != 13:
            work["publication_date"] = PUBLICATION_DATES.get(n, f"2026-{(n - 1) % 12 + 1:02d}-15")
        if n == 113:
            work["type"] = "preprint"
        elif n == 114:
            work["type"] = "article"
        if n == 15:
            work["doi"] = "https://doi.org/10.9999/unknown-to-crossref"
        elif n not in (13, 14):
            work["doi"] = PUBLICATION_DOIS.get(n, f"https://doi.org/10.7777/synth.{n:03d}")
        if n in PUBLICATION_JOURNALS:
            work["primary_location"] = {"source": {"display_name": PUBLICATION_JOURNALS[n]}}

        if n == 124:
            # The truncated view the list endpoint serves: fillers only, the
            # ITMO participant is beyond the cut.
            work["is_authors_truncated"] = True
            work["authorships"] = [
                {
                    "author": {"id": f"https://openalex.org/{filler}", "display_name": f"Filler Person {i}"},
                    "raw_affiliation_strings": [f"External University {i}"],
                }
                for i, filler in enumerate(CONSORTIUM_FILLERS[:2])
            ]
        elif n == 16:
            work["authorships"] = []
        elif n == 123:
            # A self-deposit that names the author but not where they work.
            work["authorships"] = [
                {
                    "author": {
                        "id": f"https://openalex.org/{ARCHIVE_AUTHOR_ID}",
                        "display_name": _author_name(ARCHIVE_AUTHOR_INDEX),
                    }
                },
            ]
        elif n == 121:
            # Every author.id is null: one author carries an ORCID, one only a
            # name, one neither — the last cannot be keyed at all.
            work["authorships"] = [
                {
                    "author": {
                        "id": None,
                        "display_name": "Igor Bez Ida",
                        "orcid": f"https://orcid.org/{UNIDENTIFIED_ORCID}",
                    },
                    "institutions": [ITMO_INSTITUTION],
                    "raw_affiliation_strings": ["ITMO University, St. Petersburg, Russia"],
                },
                {
                    "author": {"id": None, "display_name": UNIDENTIFIED_BY_NAME},
                    "raw_affiliation_strings": ["External University 121"],
                },
                {"author": {"id": None, "display_name": None}, "raw_affiliation_strings": ["External University 121"]},
            ]
        elif n in DUPLICATE_WORK_AUTHORS:
            work["authorships"] = [_authorship(i, n) for i in DUPLICATE_WORK_AUTHORS[n]]
        elif n == 17:
            work["authorships"] = [_authorship(i, n) for i in (1, 2, 3, 4, 5, 6, 7, 8, 31, 32, 33, 34)]
        elif n == 18:
            work["authorships"] = [_authorship(12, n), _authorship(35, n), _authorship(12, n)]
        elif n in DEDUP_WORK_AUTHORS:
            work["authorships"] = [_authorship(i, n) for i in DEDUP_WORK_AUTHORS[n]]
        else:
            first = (n - 1) % 50 + 1
            second = (n + 16) % 50 + 1
            ids = [first] if first == second else [first, second]
            work["authorships"] = [_authorship(i, n) for i in ids]

        if n != 12:
            urls = SPECIAL_CITATIONS.get(n)
            if urls is None and n >= 21 and n % 10 != 0 and remaining:
                keys = [remaining.pop(0)]
                if n % 7 == 0 and remaining:
                    keys.append(remaining.pop(0))
                urls = [repos[key]["html_url"] for key in keys]
            text = f"Short synthetic abstract {n:03d}."
            if urls:
                text += " Code: " + " and ".join(urls)
                if n != 1:  # W001's URL carries the sentence period itself
                    text += " ."
            work["abstract_inverted_index"] = _inverted_index(text)

        if n == 19:
            work["grants"] = [{"funder_display_name": "Synthetic Science Fund", "grant_id": "SSF-19"}]
        if n == 20:
            work["best_oa_location"] = {"pdf_url": "https://example.org/w20.pdf"}
        works.append(work)

    assert not remaining, f"universe bug: {len(remaining)} repos never cited"

    dedup_alternatives = {
        51: ["D. A. Kovalev"],
        52: [],
        53: ["E. Smirnova", "Екатерина Смирнова", "Smirnova, Ekaterina"],
        54: [],
        55: [],
        56: ["I. Volkov"],
        57: ["I. Volkov"],
        58: ["O. Fedorova"],
        59: [],
    }
    authors_api: dict[str, dict | None] = {}
    for i, author_id in enumerate(AUTHOR_IDS, start=1):
        if i == 16:
            authors_api[author_id] = None  # endpoint fails
            continue
        payload: dict = {
            "id": f"https://openalex.org/{author_id}",
            "display_name": _author_name(i) or f"Recovered Name{i:02d}",
            "display_name_alternatives": dedup_alternatives.get(i, [f"A. Surname{i:02d}"]),
        }
        if i == ARCHIVE_AUTHOR_INDEX:
            # Their own record knows where they work, unlike their deposit.
            payload["affiliations"] = [
                {
                    "institution": {"display_name": ARCHIVE_AUTHOR_AFFILIATION},
                    "years": [2026],
                }
            ]
        if i == 13:
            payload["orcid"] = "https://orcid.org/0000-0001-0000-0013"
        if i in (51, 52):
            payload["orcid"] = f"https://orcid.org/{KOVALEV_ORCID}"
        if i in FEDOROVA_ORCIDS:
            payload["orcid"] = f"https://orcid.org/{FEDOROVA_ORCIDS[i]}"
        authors_api[author_id] = payload

    crossref: dict[str, dict] = {}
    for work in works:
        doi = work.get("doi", "").removeprefix("https://doi.org/")
        if not doi or doi.startswith("10.9999/"):
            continue
        # author.id is null on records OpenAlex has not disambiguated (W121).
        author_ids_in_work = [(e["author"].get("id") or "").rsplit("/", 1)[-1] for e in work.get("authorships", [])]
        both_ivanovs = "A5000000006" in author_ids_in_work and "A5000000007" in author_ids_in_work
        items = []
        for entry in work.get("authorships", []):
            name = entry["author"].get("display_name")
            if not name:
                continue
            author_id = (entry["author"].get("id") or "").rsplit("/", 1)[-1]
            item: dict = {"family": name.split()[-1]}
            if author_id == "A5000000014":
                item["ORCID"] = "https://orcid.org/0000-0002-0000-0014"
            if author_id == "A5000000008":
                item["ORCID"] = "https://orcid.org/0000-0005-0000-0008"  # hyphenated surname, must match
            if author_id in ("A5000000006", "A5000000007") and both_ivanovs:
                item["ORCID"] = "https://orcid.org/0000-0003-0000-0067"  # ambiguous on purpose
            if author_id == "A5000000009":
                item["family"] = "van der Berg"
                item["ORCID"] = "https://orcid.org/0000-0004-0000-0009"  # unmatchable
            items.append(item)
        crossref[doi] = {"message": {"author": items}}

    orcid_records = {
        "0000-0001-0000-0013": {"person": {"emails": {"email": []}}},
        "0000-0002-0000-0014": {"person": {"emails": {"email": [{"email": "a14@example.org"}]}}},
        "0000-0005-0000-0008": {"person": {}},  # record without an emails block
        # The author OpenAlex has not disambiguated: no author record to fetch,
        # but the ORCID they were keyed by still enriches them.
        UNIDENTIFIED_ORCID: {"person": {"emails": {"email": [{"email": "no.id@example.org"}]}}},
        KOVALEV_ORCID: {"person": {"emails": {"email": []}}},
        FEDOROVA_ORCIDS[58]: {"person": {"emails": {"email": []}}},
        FEDOROVA_ORCIDS[59]: {"person": {"emails": {"email": []}}},
    }

    # The complete record of the consortium paper, as the single-work
    # endpoint serves it: all fillers, the organization in an author slot,
    # and the ITMO participant last.
    consortium_full = next(w for w in works if w["id"].endswith(CONSORTIUM_WORK))
    consortium_full = {
        **{k: v for k, v in consortium_full.items() if k != "is_authors_truncated"},
        "authorships": [
            *[
                {
                    "author": {"id": f"https://openalex.org/{filler}", "display_name": f"Filler Person {i}"},
                    "raw_affiliation_strings": [f"External University {i}"],
                }
                for i, filler in enumerate(CONSORTIUM_FILLERS)
            ],
            {"author": {"id": "https://openalex.org/A5900000999", "display_name": CONSORTIUM_ORG_NAME}},
            _authorship(CONSORTIUM_ITMO_INDEX, 124),
        ],
    }
    for i, filler in enumerate(CONSORTIUM_FILLERS):
        authors_api[filler] = {
            "id": f"https://openalex.org/{filler}",
            "display_name": f"Filler Person {i}",
            "display_name_alternatives": [],
        }

    return {
        "works": works,
        "works_full": {CONSORTIUM_WORK: consortium_full},
        "authors_api": authors_api,
        "github": repos,
        "renamed": RENAMED_ALIASES,
        "crossref": crossref,
        "orcid": orcid_records,
        "departments_catalog": DEPARTMENTS_CATALOG,
    }

"""Fully synthetic test universe: 140 works, 71 authors, 81 repositories.

Deterministic (no randomness) and self-describing: every tricky case the
pipeline must survive is a named constant here, and `build_universe()`
returns the raw payloads exactly as the external APIs would serve them.

Tricky cases covered
--------------------

Authors (A5000000001..A5000000071, 51 ITMO / 20 external before dedup):
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

Identity traps the dedup stage must refuse (works W121..W131):
* A60/A61  "Lei Li" and "Tao Li": each is the only "Li" on their own work,
           so the Crossref backfill stamps ONE ORCID onto both prepared
           rows. Their OpenAlex records know no ORCID, and that raw record
           is the trusted source -> the poisoned value never merges them
* A62/A63/A64  "Sergey Popov" (ORCID X), "S. Popov" (no ORCID) and
           "Sergei Popov" (ORCID Y): the middle record is a legitimate
           variant of both, so pairwise rules would chain all three into
           one group spanning two ORCIDs -> the whole group is refused and
           journalled for review
* A65/A66  two ITMO persons both named "Kim": a single-token name is not
           enough evidence -> held, never merged
* A67      one person, two ITMO departments across two works
* A68      corresponding author (relationship property on AUTHORED)
* A69      the ORCID API fails on the first enrichment run and answers on
           the second -> persons stage FAILED then COMPLETED, email filled
* A70      ITMO affiliation written in Russian only — known limitation: the
           departments stage matches name_en and aliases, not name_ru
* A71      "Nikolay Nikitin", the 2026 half of a person split across
           collection periods (see universe_2025.py)

Works (W7000000001..W70000000140):
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

URL shapes and duplicate records with links (works W121..W133):
* W121     a deep link into a repository (/tree/main/src) plus the plain
           repository URL -> one repository, one link
* W122     an owner-only github.com URL is not a repository and must be
           ignored; the repository URL next to it is not
* W123     URLs carrying an "#anchor" and a "?query" suffix
* W124     one live repository and one 404 in the same abstract -> a
           Repository edge next to a LinkCandidate edge
* W126     GitHub answers 429 on the first enrichment run and 200 on the
           second -> the LinkCandidate published in between is promoted to
           the Repository, keeping the MENTIONS_LINK edge
* W132/W133  one DOI written as "http://dx.doi.org/...CaseDoi" and
           "https://doi.org/...casedoi" -> one publication, although the
           two records carry different titles and list their authors in
           opposite order

Works collected only by a later run (W134..W138, dated 2027):
The first collect uses a 2026 period selector and never sees them; they
arrive through a works-file selector between the two enrichment runs, so
everything already enriched must stay untouched while they are processed.
* W134/W135  a preprint carrying the abstract, the PDF link, the grant and
           the code link merges into a version of record that has none of
           them -> the surviving row inherits every gap
* W136     a citation written with an upper-case host ("HTTPS://GitHub.COM")
           must resolve to the repository, not to a LinkCandidate
* W137     GitHub serves a payload without `owner` and without `html_url`
           -> a Repository with no GitHubProfile and no OWNED_BY edge
* W138     a second publication citing an already-known 404 URL -> ONE
           LinkCandidate node with two MENTIONS_LINK edges

Works kept for the cross-period bench (W139/W140, see universe_2025.py):
* W139     the version of record whose preprint was collected in the 2025
           group -> only the graph-wide dedup can fold them
* W140     A71 (the 2026 half of a person split across periods) and A70

Repositories: 80 canonical repos across 16 owners (5 each) plus one
"orphan" payload without an owner block; all 81 are cited at least once.
4 phantom URLs are cited but never existed (404), one alias URL redirects
to a canonical repo, and some owner payloads miss `name`/`type`. A row
written before a repository was renamed (STALE_REPO_ID) is seeded between
the two enrichment runs: only GitHub's numeric id ties it to the row the
new name produced.
"""

from __future__ import annotations


def author_id(i: int) -> str:
    return f"A50000000{i:02d}"


def work_id(n: int) -> str:
    return f"W70000000{n:02d}"


AUTHOR_IDS = [author_id(i) for i in range(1, 72)]
WORK_IDS = [work_id(n) for n in range(1, 141)]

# Duplicate publication records (works W111..W120). One work reaches OpenAlex
# as several records: a re-indexed duplicate of one DOI, a preprint and its
# version of record, a deposit re-released per version. Titles differing only
# in case and spacing are the same title; the "Untitled" placeholder is not.
DUPLICATE_DOI = "https://doi.org/10.7777/synth.dup"
PUBLICATION_TITLES = {
    111: "Bench duplicate record", 112: "Bench duplicate record",
    113: "Bench preprint and version of record",
    114: "Bench preprint and version of record",
    115: "Bench dataset deposit", 116: "Bench dataset deposit", 117: "Bench dataset deposit",
    119: "Bench Case Variant Study", 120: "  bench case VARIANT   study ",
    # Merged by DOI alone: the two records were even titled differently.
    132: "Bench DOI resolver variant", 133: "Bench DOI resolver variant, revised",
    134: "Bench preprint carrying the full text",
    135: "Bench preprint carrying the full text",
    139: "Bench cross-period study",
}
PUBLICATION_JOURNALS = {
    111: "Synthetic Journal", 112: "Synthetic Journal",
    113: "Synthetic Preprint Server", 114: "Synthetic Journal",
    115: "Synthetic Data Archive", 116: "Synthetic Data Archive",
    117: "Synthetic Data Archive",
    134: "Synthetic Preprint Server", 135: "Synthetic Journal",
    139: "Synthetic Journal",
}
PUBLICATION_DOIS = {
    111: DUPLICATE_DOI, 112: DUPLICATE_DOI,
    113: "https://doi.org/10.7777/preprint.113", 114: "https://doi.org/10.7777/vor.114",
    115: "https://doi.org/10.7777/deposit.115", 116: "https://doi.org/10.7777/deposit.116",
    117: "https://doi.org/10.7777/deposit.117",
    # One DOI, two resolver prefixes and two spellings: bare and lowercase,
    # they are the same DOI.
    132: "http://dx.doi.org/10.7777/CaseDoi.132",
    133: "https://doi.org/10.7777/casedoi.132",
    134: "https://doi.org/10.7777/preprint.134", 135: "https://doi.org/10.7777/vor.135",
}
PUBLICATION_DATES = {
    111: "2026-03-15", 112: "2026-03-15",   # one DOI, one date: authors decide
    113: "2026-02-15", 114: "2026-09-15",   # the version of record comes later
    115: "2026-01-20", 116: "2026-04-20", 117: "2026-07-20",
    119: "2026-06-05", 120: "2026-05-05",
    132: "2026-02-10", 133: "2026-08-10",
    134: "2027-01-10", 135: "2027-11-10",   # collected only by the second run
    136: "2027-02-20", 137: "2027-03-20", 138: "2027-04-20",
    139: "2026-04-01",
}
# Same authors on both records of a work: their authorships must collapse.
DUPLICATE_WORK_AUTHORS = {
    111: (21, 22), 112: (),  # the re-indexed record carries no authors at all
    113: (24, 25), 114: (24, 25),
    115: (26, 27), 116: (26, 27), 117: (26, 27),
    118: (30,), 119: (28, 29), 120: (28, 29),
}
# Merged-away work id -> the record that survives.
PUBLICATION_MERGES = {
    work_id(112): work_id(111),
    work_id(113): work_id(114),
    work_id(115): work_id(117),
    work_id(116): work_id(117),
    work_id(120): work_id(119),
    work_id(132): work_id(133),
    work_id(134): work_id(135),
}
UNTITLED_WORK_IDS = (work_id(13), work_id(118))

# Works the 2026 period selector cannot see: they are dated 2027 and reach
# the group through a works-file selector between the two enrichment runs.
INCREMENTAL_WORK_NUMBERS = (134, 135, 136, 137, 138)
INCREMENTAL_WORK_IDS = tuple(work_id(n) for n in INCREMENTAL_WORK_NUMBERS)

# A repository renamed on GitHub between two runs: the row written before the
# rename survives in prepared under its old id and URL, and only GitHub's
# numeric id ties it to the row the new name produced.
STALE_REPO_ID = "github_benchorg7_legacy-name"
STALE_REPO_URL = "https://github.com/BenchOrg7/legacy-name"
STALE_REPO_CANONICAL_ID = "github_benchorg7_alphatool"
STALE_REPO_PUBLICATION = work_id(21)

# Split-author cases: ids merged away by the dedup stage and where they go.
DEDUP_MERGES = {
    author_id(52): author_id(51),
    author_id(54): author_id(53),
    author_id(55): author_id(53),
    author_id(57): author_id(56),
}
# (author index, filler coauthor index) per dedup work W101..W110.
DEDUP_WORK_AUTHORS = {
    101: (51, 20), 102: (52, 21),
    103: (53, 17), 104: (54, 17), 105: (55, 17), 110: (53, 24),
    106: (56, 22), 107: (57, 22),
    108: (58, 23), 109: (59, 23),
}
KOVALEV_ORCID = "0000-0006-0000-0051"
FEDOROVA_ORCIDS = {58: "0000-0007-0000-0058", 59: "0000-0007-0000-0059"}

# Authors of the works added for the identity traps and the URL shapes.
CASE_WORK_AUTHORS = {
    121: (60, 20), 122: (61, 20),
    123: (62, 17), 124: (63, 17), 125: (64, 17),
    126: (65, 18), 127: (66, 18),
    128: (67, 19), 129: (67, 19),
    130: (68, 20), 131: (69, 20),
    132: (22, 21), 133: (21, 22),   # one work, opposite author order
    134: (24, 25), 135: (24, 25),
    136: (21, 31), 137: (21, 31), 138: (21, 31),
    139: (28, 29), 140: (71, 70),
}
# The Crossref backfill matches by family name alone: one "Li" per work, so
# one ORCID lands on two different people. Only the OpenAlex author record
# (which knows no ORCID for either) can veto that.
NAMESAKE_ORCID = "0000-0009-0000-0060"
# Two Popovs with their own ORCIDs, bridged by a third record that has none.
POPOV_ORCIDS = {62: "0000-0010-0000-0062", 64: "0000-0010-0000-0064"}
SOKOLOV_ORCID = "0000-0011-0000-0069"
# The 2026 half of a person split across collection periods.
NIKITIN_ORCID = "0000-0012-0000-0071"
# The ORCID API answers only from the second enrichment run on.
FLAKY_ORCIDS = frozenset({SOKOLOV_ORCID})

ITMO_ROR = "https://ror.org/04txgxn49"
ITMO_INSTITUTION = {"ror": ITMO_ROR, "display_name": "ITMO University"}

DEPARTMENTS_CATALOG = [
    {"name_en": "Institute of Applied Computer Science", "name_ru": "Институт прикладной информатики",
     "aliases": ["IACS"]},
    {"name_en": "Faculty of Photonics", "name_ru": "Факультет фотоники", "aliases": []},
    {"name_en": "Biotech Research Center", "name_ru": "Центр биотехнологий", "aliases": ["BioTech Center"]},
    {"name_en": "School of Robotics", "name_ru": "Школа робототехники", "aliases": ["Robotics School"]},
    {"name_en": "Quantum Computing Lab", "name_ru": "Лаборатория квантовых вычислений", "aliases": ["QC Lab"]},
]
DEPT_NAMES = [d["name_en"] for d in DEPARTMENTS_CATALOG]

# One person writing their affiliation with a different department per work.
SPECIAL_AFFILIATIONS = {
    (67, 128): "ITMO University, Faculty of Photonics, St. Petersburg, Russia",
    (67, 129): "ITMO University, Quantum Computing Lab, St. Petersburg, Russia",
}
# Russian-only affiliation: the departments stage compares name_en and the
# aliases, so this ITMO author ends up without a department.
RUSSIAN_AFFILIATION = "Университет ИТМО, Институт прикладной информатики, Санкт-Петербург, Россия"
# (author index, work number) of authorships flagged as corresponding.
CORRESPONDING_AUTHORSHIPS = frozenset({(68, 130)})

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
PHANTOM_4 = "https://github.com/GoneOrg/ghost-tool"
PHANTOM_URLS = (CITE_DELETED, PHANTOM_2, PHANTOM_3, PHANTOM_4)

# A path inside a repository, an owner page, and cosmetic URL suffixes.
CITE_DEEP_LINK = "https://github.com/BenchOrg8/AlphaTool/tree/main/src"
CITE_DEEP_PLAIN = "https://github.com/BenchOrg8/AlphaTool"
CITE_OWNER_ONLY = "https://github.com/BenchOrg9"
CITE_NEXT_TO_OWNER = "https://github.com/BenchOrg9/beta-kit"
CITE_ANCHOR = "https://github.com/BenchOrg9/GammaLib#readme"
CITE_QUERY = "https://github.com/BenchOrg10/AlphaTool?tab=readme-ov-file"
CITE_MIXED_LIVE = "https://github.com/BenchOrg10/beta-kit"
CITE_UPPERCASE_HOST = "HTTPS://GitHub.COM/BenchOrg13/EpsilonNet"
CITE_PREPRINT_REPO = "https://github.com/BenchOrg12/GammaLib"

# GitHub answers 429 for this repository on the first enrichment run only:
# the publish in between records a LinkCandidate that the next publish must
# promote into the Repository.
FLAKY_REPO_URL = "https://github.com/BenchOrg11/AlphaTool"
FLAKY_REPO_ID = "github_benchorg11_alphatool"
RATE_LIMITED_ONCE = frozenset({("benchorg11", "alphatool")})

# A payload without `owner` and without `html_url`: the repository exists,
# but there is nothing to hang a GitHubProfile or an OWNED_BY edge on.
ORPHAN_REPO_URL = "https://github.com/BenchOrg14/orphan-tool"
ORPHAN_REPO_ID = "github_benchorg14_orphan-tool"
SPECIAL_REPO_PAYLOADS = {
    ("benchorg14", "orphan-tool"): {
        "id": 900_001,
        "name": "orphan-tool",
        "description": None,
        "stargazers_count": 7,
    },
}

RENAMED_ALIASES = {("benchorg3", "old-alpha"): ("benchorg3", "alphatool")}

# Repos already covered by the works with hand-written citations below.
SPECIALLY_CITED = {
    ("benchorg1", "alphatool"), ("benchorg1", "beta-kit"), ("benchorg1", "gammalib"),
    ("benchorg1", "delta.util"), ("benchorg1", "epsilonnet"),
    ("benchorg2", "gammalib"), ("benchorg3", "alphatool"), ("benchorg4", "alphatool"),
    ("benchorg8", "alphatool"), ("benchorg9", "beta-kit"), ("benchorg9", "gammalib"),
    ("benchorg10", "alphatool"), ("benchorg10", "beta-kit"), ("benchorg11", "alphatool"),
    ("benchorg12", "gammalib"), ("benchorg13", "epsilonnet"),
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
    10: ["https://github.com/BenchOrg1/delta.util",
         "https://github.com/BenchOrg1/EpsilonNet",
         "https://github.com/BenchOrg1/delta.util"],
    11: ["https://gitlab.com/some/project"],
    19: [PHANTOM_2],
    20: [PHANTOM_3],
    121: [CITE_DEEP_LINK, CITE_DEEP_PLAIN],
    122: [CITE_OWNER_ONLY, CITE_NEXT_TO_OWNER],
    123: [CITE_ANCHOR, CITE_QUERY],
    124: [CITE_MIXED_LIVE, PHANTOM_4],
    126: [FLAKY_REPO_URL],
    134: [CITE_PREPRINT_REPO],
    136: [CITE_UPPERCASE_HOST],
    137: [ORPHAN_REPO_URL],
    138: [PHANTOM_2],
}

# Works with no abstract at all: nothing for the code_links stage to read.
NO_ABSTRACT_WORKS = frozenset({12, 135})
FUNDED_WORKS = {19: "SSF-19", 134: "SSF-134"}
PDF_WORKS = {20: "https://example.org/w20.pdf", 134: "https://example.org/w134.pdf"}
# Only works numbered up to here take a repository from the shared pool; the
# later ones cite exactly what SPECIAL_CITATIONS says and nothing else.
LAST_FILLER_WORK = 110


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


def author_name(i: int) -> str | None:
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
        60: "Lei Li",
        61: "Tao Li",
        62: "Sergey Popov",
        63: "S. Popov",
        64: "Sergei Popov",
        65: "Kim",
        66: "Kim",
        67: "Marina Orlova",
        68: "Nadezhda Ivanenko",
        69: "Timur Sokolov",
        70: "Виктор Лебедев",
        71: "Nikolay Nikitin",
    }
    if i in special:
        return special[i]
    return f"Author{i:02d} Surname{i:02d}"


def _affiliation(i: int, itmo: bool, n: int) -> list[str]:
    if not itmo:
        return [f"External University {i}"]
    special = SPECIAL_AFFILIATIONS.get((i, n))
    if special:
        return [special]
    if i == 70:
        return [RUSSIAN_AFFILIATION]
    if i == 18:
        return ["ITMO University, IACS, St. Petersburg, Russia"]           # alias spelling
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
    name = author_name(i)
    if name is not None:
        author["display_name"] = name
    if i == 11:
        author["display_name_alternatives"] = ["Хосе Альварес-Мюллер"]
    entry: dict = {"author": author, "raw_affiliation_strings": _affiliation(i, itmo, n)}
    if itmo:
        entry["institutions"] = [ITMO_INSTITUTION]
    if (i, n) in CORRESPONDING_AUTHORSHIPS:
        entry["is_corresponding"] = True
    return entry


def _authorships_for(n: int) -> list[dict]:
    if n == 16:
        return []
    if n in DUPLICATE_WORK_AUTHORS:
        return [_authorship(i, n) for i in DUPLICATE_WORK_AUTHORS[n]]
    if n == 17:
        return [_authorship(i, n) for i in (1, 2, 3, 4, 5, 6, 7, 8, 31, 32, 33, 34)]
    if n == 18:
        return [_authorship(12, n), _authorship(35, n), _authorship(12, n)]
    if n in DEDUP_WORK_AUTHORS:
        return [_authorship(i, n) for i in DEDUP_WORK_AUTHORS[n]]
    if n in CASE_WORK_AUTHORS:
        return [_authorship(i, n) for i in CASE_WORK_AUTHORS[n]]
    first = (n - 1) % 50 + 1
    second = (n + 16) % 50 + 1
    ids = [first] if first == second else [first, second]
    return [_authorship(i, n) for i in ids]


def build_universe() -> dict:
    repos = _canonical_repos()
    remaining = [key for key in repos if key not in SPECIALLY_CITED]  # 64 repos

    works: list[dict] = []
    for n, wid in enumerate(WORK_IDS, start=1):
        work: dict = {"id": f"https://openalex.org/{wid}"}

        if n not in (13, 118):  # both untitled: a placeholder is not a title
            work["title"] = PUBLICATION_TITLES.get(n, f"Synthetic paper {n:03d}")
        if n != 13:
            work["publication_date"] = PUBLICATION_DATES.get(
                n, f"2026-{(n - 1) % 12 + 1:02d}-15")
        if n == 15:
            work["doi"] = "https://doi.org/10.9999/unknown-to-crossref"
        elif n not in (13, 14):
            work["doi"] = PUBLICATION_DOIS.get(n, f"https://doi.org/10.7777/synth.{n:03d}")
        if n in PUBLICATION_JOURNALS:
            work["primary_location"] = {"source": {"display_name": PUBLICATION_JOURNALS[n]}}

        work["authorships"] = _authorships_for(n)

        if n not in NO_ABSTRACT_WORKS:
            urls = SPECIAL_CITATIONS.get(n)
            if urls is None and 21 <= n <= LAST_FILLER_WORK and n % 10 != 0 and remaining:
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

        if n in FUNDED_WORKS:
            work["grants"] = [{"funder_display_name": "Synthetic Science Fund",
                               "grant_id": FUNDED_WORKS[n]}]
        if n in PDF_WORKS:
            work["best_oa_location"] = {"pdf_url": PDF_WORKS[n]}
        works.append(work)

    assert not remaining, f"universe bug: {len(remaining)} repos never cited"
    repos.update(SPECIAL_REPO_PAYLOADS)

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
        # The bridge record "S. Popov" is a legitimate variant of both Popovs.
        62: ["S. Popov"],
        63: [],
        64: ["S. Popov"],
        71: ["N. O. Nikitin"],
    }
    authors_api: dict[str, dict | None] = {}
    for i, aid in enumerate(AUTHOR_IDS, start=1):
        if i == 16:
            authors_api[aid] = None  # endpoint fails
            continue
        payload: dict = {
            "id": f"https://openalex.org/{aid}",
            "display_name": author_name(i) or f"Recovered Name{i:02d}",
            "display_name_alternatives": dedup_alternatives.get(i, [f"A. Surname{i:02d}"]),
        }
        if i == 13:
            payload["orcid"] = "https://orcid.org/0000-0001-0000-0013"
        if i in (51, 52):
            payload["orcid"] = f"https://orcid.org/{KOVALEV_ORCID}"
        if i in FEDOROVA_ORCIDS:
            payload["orcid"] = f"https://orcid.org/{FEDOROVA_ORCIDS[i]}"
        if i in POPOV_ORCIDS:
            payload["orcid"] = f"https://orcid.org/{POPOV_ORCIDS[i]}"
        if i == 69:
            payload["orcid"] = f"https://orcid.org/{SOKOLOV_ORCID}"
        if i == 71:
            payload["orcid"] = f"https://orcid.org/{NIKITIN_ORCID}"
        authors_api[aid] = payload

    crossref = build_crossref(works)

    orcid_records = {
        "0000-0001-0000-0013": {"person": {"emails": {"email": []}}},
        "0000-0002-0000-0014": {"person": {"emails": {"email": [{"email": "a14@example.org"}]}}},
        "0000-0005-0000-0008": {"person": {}},  # record without an emails block
        KOVALEV_ORCID: {"person": {"emails": {"email": []}}},
        FEDOROVA_ORCIDS[58]: {"person": {"emails": {"email": []}}},
        FEDOROVA_ORCIDS[59]: {"person": {"emails": {"email": []}}},
        NAMESAKE_ORCID: {"person": {"emails": {"email": []}}},
        POPOV_ORCIDS[62]: {"person": {"emails": {"email": []}}},
        POPOV_ORCIDS[64]: {"person": {"emails": {"email": []}}},
        SOKOLOV_ORCID: {"person": {"emails": {"email": [{"email": "t69@example.org"}]}}},
        NIKITIN_ORCID: {"person": {"emails": {"email": []}}},
    }

    return {
        "works": works,
        "authors_api": authors_api,
        "github": repos,
        "renamed": RENAMED_ALIASES,
        "crossref": crossref,
        "orcid": orcid_records,
        "departments_catalog": DEPARTMENTS_CATALOG,
    }


def bare_doi(doi: str | None) -> str:
    """The DOI as MockCrossrefClient keys it: without any resolver prefix."""
    value = doi or ""
    for prefix in ("https://doi.org/", "http://doi.org/",
                   "https://dx.doi.org/", "http://dx.doi.org/"):
        value = value.removeprefix(prefix)
    return value


def build_crossref(works: list[dict]) -> dict[str, dict]:
    """Crossref responses for every work whose DOI Crossref knows.

    Only the family name and (sometimes) an ORCID: exactly what the persons
    stage reads, and exactly the little it has to work with.
    """
    crossref: dict[str, dict] = {}
    for work in works:
        doi = bare_doi(work.get("doi"))
        if not doi or doi.startswith("10.9999/"):
            continue
        author_ids_in_work = [e["author"]["id"].rsplit("/", 1)[-1] for e in work.get("authorships", [])]
        both_ivanovs = author_id(6) in author_ids_in_work and author_id(7) in author_ids_in_work
        items = []
        for entry in work.get("authorships", []):
            name = entry["author"].get("display_name")
            if not name:
                continue
            aid = entry["author"]["id"].rsplit("/", 1)[-1]
            item: dict = {"family": name.split()[-1]}
            if aid == author_id(14):
                item["ORCID"] = "https://orcid.org/0000-0002-0000-0014"
            if aid == author_id(8):
                item["ORCID"] = "https://orcid.org/0000-0005-0000-0008"  # hyphenated surname, must match
            if aid in (author_id(6), author_id(7)) and both_ivanovs:
                item["ORCID"] = "https://orcid.org/0000-0003-0000-0067"  # ambiguous on purpose
            if aid == author_id(9):
                item["family"] = "van der Berg"
                item["ORCID"] = "https://orcid.org/0000-0004-0000-0009"  # unmatchable
            if aid in (author_id(60), author_id(61)):
                # One family name, two people, one ORCID: the poisoning the
                # dedup stage has to see through.
                item["ORCID"] = f"https://orcid.org/{NAMESAKE_ORCID}"
            items.append(item)
        crossref[doi] = {"message": {"author": items}}
    return crossref


def expected_authorship_pairs(works: list[dict] | None = None) -> set[tuple[str, str]]:
    """(person id, publication id) pairs the raw payloads imply.

    Computed straight from the OpenAlex payloads plus the id remappings the
    dedup stage is expected to apply, so the graph's AUTHORED edges can be
    checked against the source data rather than against the pipeline's own
    output.
    """
    pairs: set[tuple[str, str]] = set()
    for work in works if works is not None else build_universe()["works"]:
        wid = work["id"].rsplit("/", 1)[-1]
        publication_id = PUBLICATION_MERGES.get(wid, wid)
        for entry in work.get("authorships") or []:
            aid = entry["author"]["id"].rsplit("/", 1)[-1]
            pairs.add((DEDUP_MERGES.get(aid, aid), publication_id))
    return pairs

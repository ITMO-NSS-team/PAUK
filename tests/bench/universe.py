"""Fully synthetic test universe: 110 works, 59 authors, 80 repositories.

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
* A56/A57  true namesakes: two different ITMO "Ivan Volkov"s with a shared
           coauthor but no variant evidence -> NOT merged, reported in
           dedup_candidates.jsonl
* A58/A59  "Olga Fedorova" lists "O. Fedorova" as a variant and they share
           a coauthor, but their author records carry different ORCIDs ->
           NOT merged and not even a candidate

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

Repositories: 80 canonical repos across 16 owners (5 each); all 80 are
cited at least once. 3 phantom URLs are cited but never existed (404), one
alias URL redirects to a canonical repo, and some owner payloads miss
`name`/`type`.
"""

from __future__ import annotations

AUTHOR_IDS = [f"A50000000{i:02d}" for i in range(1, 60)]
WORK_IDS = [f"W70000000{i:02d}" for i in range(1, 111)]

# Split-author cases: ids merged away by the dedup stage and where they go.
DEDUP_MERGES = {
    "A5000000052": "A5000000051",
    "A5000000054": "A5000000053",
    "A5000000055": "A5000000053",
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
    ("benchorg1", "alphatool"), ("benchorg1", "beta-kit"), ("benchorg1", "gammalib"),
    ("benchorg1", "delta.util"), ("benchorg1", "epsilonnet"),
    ("benchorg2", "gammalib"), ("benchorg3", "alphatool"), ("benchorg4", "alphatool"),
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
}


def _inverted_index(text: str) -> dict[str, list[int]]:
    """OpenAlex-style abstract_inverted_index for a short text."""
    index: dict[str, list[int]] = {}
    for position, word in enumerate(text.split()):
        index.setdefault(word, []).append(position)
    return index


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

        if n != 13:
            work["title"] = f"Synthetic paper {n:03d}"
            work["publication_date"] = f"2026-{(n - 1) % 12 + 1:02d}-15"
        if n == 15:
            work["doi"] = "https://doi.org/10.9999/unknown-to-crossref"
        elif n not in (13, 14):
            work["doi"] = f"https://doi.org/10.7777/synth.{n:03d}"

        if n == 16:
            work["authorships"] = []
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
        author_ids_in_work = [e["author"]["id"].rsplit("/", 1)[-1] for e in work.get("authorships", [])]
        both_ivanovs = "A5000000006" in author_ids_in_work and "A5000000007" in author_ids_in_work
        items = []
        for entry in work.get("authorships", []):
            name = entry["author"].get("display_name")
            if not name:
                continue
            author_id = entry["author"]["id"].rsplit("/", 1)[-1]
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
        KOVALEV_ORCID: {"person": {"emails": {"email": []}}},
        FEDOROVA_ORCIDS[58]: {"person": {"emails": {"email": []}}},
        FEDOROVA_ORCIDS[59]: {"person": {"emails": {"email": []}}},
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

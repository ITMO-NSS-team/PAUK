"""Find publication pairs that look like versions of one work: records the
dedup stage keeps apart because their DOIs differ (pauk/pipeline/stages/
dedup.py, see docs/architecture/pipeline/dedup.md) - a preprint whose title
was polished for the journal, a conference paper extended into a journal
article, a Russian original and its Pleiades/MAIK English translation, or an
eLife-style `.v2` revision minted under its own DOI.

The rules and buckets live here and only read; `version_link.py` turns the
pairs into IS_VERSION edges, and `pauk versions report` into a journal.
Nothing merges nodes or writes `merged_ids`: a version is not a duplicate,
and a fuzzy title plus author overlap is corroborating evidence, not proof -
"Cliques and Constructors in 'Hats' Game. I" and "... II" score a
near-perfect title similarity and share every author with their sequel, and
are two different papers. See docs/architecture/pipeline/version-candidates.md
for the precision/recall picture per bucket.
"""

from __future__ import annotations

import json
import logging
import re
import unicodedata
from collections import defaultdict
from datetime import date
from difflib import SequenceMatcher
from functools import lru_cache
from itertools import combinations
from pathlib import Path

from pauk.pipeline.stages.dedup import PLACEHOLDER_TITLES, _norm_doi
from pauk.settings import Settings
from pauk.storage.atomic import AtomicWriter

from .client import Neo4jClient

logger = logging.getLogger(__name__)

CANDIDATES_FILENAME = "version_candidates.jsonl"

# MEDIUM_FUZZY is a third versions and two thirds sibling papers of one
# group, and its title similarity does not tell them apart (the bucket study
# in docs/architecture/pipeline/version-candidates.md). It is split further by
# `route_medium_fuzzy`: a shared abstract or a preprint is kept as is, the
# rest goes to an LLM and the venue pair decides what it declines.
MEDIUM_FUZZY = "MEDIUM_FUZZY"
MEDIUM_FUZZY_STRONG = "MEDIUM_FUZZY_STRONG"
MEDIUM_FUZZY_LLM_SAME = "MEDIUM_FUZZY_LLM_SAME"
MEDIUM_FUZZY_CONF_JOURNAL = "MEDIUM_FUZZY_CONF_JOURNAL"
MEDIUM_FUZZY_RELATED = "MEDIUM_FUZZY_RELATED"

# Buckets corroborated enough to be worth a human's time; the rest
# (ERRATUM, SERIES_NOT_VERSION, SUPPLEMENT_OR_REVIEW, COVER and the sibling
# papers MEDIUM_FUZZY_RELATED) are look-alike pairs kept so they are
# explained rather than silently dropped, but are never one work.
# MEDIUM_FUZZY itself stays here: unrouted (no LLM verdict yet) it is
# still the old bucket a human used to see whole.
MERGE_BUCKETS = frozenset({
    "MISSED_BY_NORM", "DOI_SIBLING", "PREPRINT_PUBLISHED",
    "RU_EN_TRANSLATION", "STRONG_FUZZY", MEDIUM_FUZZY,
    MEDIUM_FUZZY_STRONG, MEDIUM_FUZZY_LLM_SAME, MEDIUM_FUZZY_CONF_JOURNAL,
})

# Repository/preprint hosts, matched against either the DOI or the venue
# name. Order does not matter - a work never carries two of these.
# Anchored on a whole registrant: unanchored, "10.1101" also matched
# "10.11016/..." and "10.2139" matched "10.21390/...".
_PREPRINT_DOI = re.compile(
    r"^10\.(?:48550/arxiv\.|(?:1101|21203|31219|26434|20944|31234|2139|35542|13140|"
    r"55458|32942|33774)/)", re.I)
_PREPRINT_VENUE = re.compile(
    r"arxiv|biorxiv|medrxiv|chemrxiv|techrxiv|research\s*square|ssrn|preprints\.org|"
    r"\bosf\b|\brepec\b|authorea|figshare|zenodo|\bhal\b|working paper|preprint|cold spring harbor", re.I)
_ARXIV_IN_URL = re.compile(r"arxiv\.org/(?:abs|pdf)/([0-9]{4}\.[0-9]{4,5}|[a-z-]+/[0-9]{7})", re.I)
_ARXIV_IN_DOI = re.compile(r"10\.48550/arxiv\.([0-9]{4}\.[0-9]{4,5}|[a-z-]+/[0-9]{7})", re.I)

# One DOI is literally the other plus a version tag: "10.7554/elife.84874"
# vs "...84874.2" or "...84874.v2". Not "strip trailing digits" - that
# collapses every Zenodo/Figshare/IntechOpen DOI onto its registrant prefix.
_VERSION_TAG = re.compile(r"\.v?\d{1,3}$", re.I)

_ERRATUM_PREFIX = re.compile(
    r"^(correction|corrigendum|erratum|retraction(?:\s+(?:note|notice))?|retracted|"
    r"notice of retraction|publisher correction|author correction|expression of concern|addendum|"
    # Leading, too: APL prints "Publisher's Note: \u201c<title>\u201d" for a correction.
    r"publisher['\u2019]?s\s+note)"
    # The separator is required: "Correction of chromatic aberration" is a
    # paper, "Correction to: ..." / "Erratum: ..." is a notice. "for" counts
    # only with a colon and a hyphen only between spaces, or "Correction for
    # motion artifacts" and "Correction-based approach" become notices.
    r"(\s*\bto\b\s*:?|\s+for\s*:|\s*:|\s*[—–]|\s+-)\s*", re.I)
# The same notice spelled as a suffix: "... (Erratum)", "...: publisher's note".
_ERRATUM_SUFFIX = re.compile(
    r"\s*(?:\((?:erratum|corrigendum|retracted|retraction)\)|[:.]\s*publisher['’]?s\s+note)\s*$", re.I)
# OpenAlex types of a notice about another record - never a version of it,
# even when the notice reuses the original title verbatim.
_NOTICE_TYPES = frozenset({"erratum", "retraction"})

# The publisher's own record for an issue cover or a note about the article:
# a DOI of its own, the article's authors and title, and not a version of it.
# Wiley prints these as "Front Cover: <title>", "Cover Feature: <title>",
# "<title> (Adv. Mater. 13/2022)" or a frontispiece rubric
# "Polymer Coatings: <title>" in the article's own journal.
_COVER_PREFIX = re.compile(
    r"^(?:(?:front|back|inside(?:\s+(?:front|back))?)\s+cover(?:\s+(?:picture|image|art|feature))?|"
    r"cover\s+(?:feature|picture|image|art|profile|story)|frontispiece)\s*[:\u2014\u2013-]\s*", re.I)
# "(Adv. Mater. 13/2022)", "(Small 18/2021)": a journal abbreviation and an
# issue number. Case-sensitive: the abbreviation is capitalised.
_COVER_ISSUE_SUFFIX = re.compile(r"\s*\(\s*[A-Z][\w.&\s-]{0,40}?\s+\d{1,2}\s*/\s*\d{4}\s*\)\s*$")
_RUBRIC = re.compile(r"^([^:]{2,40}):\s+(.+)$")
_RUBRIC_MAX_WORDS = 4

# A record whose title only makes sense next to another paper's - a
# supplementary file, a dataset deposit, a peer-review report - not a
# version of the paper it is keyed off.
_DEPOSIT_HEAD = re.compile(
    r"^(additional file\s*\d*\s*(of|for)?|supplementary|supporting information|"
    r"data (from|for)|replication data for|peer review|review report|reviewer report)\b", re.I)

# Titles that differ only by a trailing series marker are different works in
# a series, not versions of one work ("Part I" / "Part II", "Volume 2").
# A named marker is a series entry even when only one title carries it
# ("... Chemistry" vs "...: Volume 2").
_SERIES_TAIL = re.compile(
    r"\s*(?:[.,:;—–-]\s*)?(?:"
    r"part\s+(?:[ivx]+|\d+|one|two|three|four|five)"
    r"|(?:vol|volume)\s*\.?\s*\d+"
    r"|no\.?\s*\d+"
    r"|supplementary (?:file|material|data)\s*\d*"
    r")\s*$", re.I)
# A bare trailing number is one only when both titles end in one and the
# numbers differ ("Game. I" / "Game. II"): otherwise "of COVID-19" vs "of
# COVID" is a series.
_SERIES_BARE_TAIL = re.compile(r"\s+([ivx]{1,4}|\d{1,3})$", re.I)
# "#" sits outside \b: a word boundary before it needs a word character
# right before, which " #3" never has.
_SERIES_NUM = re.compile(r"(?:\b(?:part|pt|episode|chapter|no)|#)\s*\.?\s*([ivx]+|\d{1,3})\b", re.I)
# A bare section number between sentences: "Disturbance Observers: Methods and
# Applications. II. Applications", "... beer industries. 2. Study of ...".
# Case-sensitive, so the English pronoun "I" mid-sentence never counts.
_SERIES_SECTION = re.compile(r"[.:]\s+([IVX]{1,4}|\d{1,2})\.(?:\s|$)")
_ROMAN = {"i": 1, "ii": 2, "iii": 3, "iv": 4, "v": 5, "vi": 6, "vii": 7, "viii": 8, "ix": 9, "x": 10}

_CYRILLIC = re.compile(r"[Ѐ-ӿ]")
_NON_WORD = re.compile(r"[^a-z0-9а-я]+")
_HAS_LATIN = re.compile(r"[a-z]")
# Cyrillic letters a publisher's typesetting slips into a Latin word
# ("nonlinear сontrol" with a Cyrillic "с"). Only mixed-script words are
# mapped: a Russian word must keep its letters to be compared at all.
_LATIN_LOOKALIKES = str.maketrans("аеорсухкмтвн", "aeopcyxkmtbh")
_STOPWORDS = frozenset({
    "a", "an", "the", "of", "and", "or", "for", "to", "in", "on", "with",
    "by", "at", "as", "from", "via", "using", "based",
    "и", "в", "во", "на", "с", "со", "по", "для", "от", "к", "из", "о", "об", "при", "или",
})

# Front/back matter: a shared title here means nothing about the work.
_GENERIC_TITLES = PLACEHOLDER_TITLES | {
    "contributors", "list of contributors", "about the authors", "author index",
    "subject index", "index", "editorial board", "editorial", "foreword",
    "preface", "introduction", "front matter", "back matter",
    "table of contents", "contents", "acknowledgements", "acknowledgments",
    "reviewers", "list of reviewers", "keynote", "keynotes", "abstracts",
    "poster abstracts", "author biographies", "erratum", "corrigendum",
    "editorial note", "in memoriam", "obituary", "news", "book reviews",
    "cover image", "issue information", "masthead", "copyright",
}

# Software releases and data deposits form their own version series
# (repository/entity dedup already handles them); a paper title heuristic
# has nothing useful to say about "asl/BandageNG: Release v2026.4.1".
_NON_PAPER_TYPES = frozenset({"software", "dataset", "peer-review", "grant", "other", "paratext"})

# Proceedings typed as journals or book series by OpenAlex count as
# conferences by their name: "Journal of Physics: Conference Series",
# IFAC-PapersOnLine, LNCS, Procedia, MEDINFO's "Studies in Health Technology
# and Informatics", or an IEEE event named after its year.
_CONFERENCE_VENUE = re.compile(
    r"conference|proceedings|symposium|workshop|congress|colloquium|\bconf\b|\bproc\.|"
    r"papersonline|lecture notes in|procedia|advances in intelligent systems|"
    r"communications in computer and information science|studies in health technology|"
    r"web of conferences|\bceur\b|\bspie\b|^\d{4}\s", re.I)

_MIN_TITLE_TOKENS = 4
# Caps on how large a shared-author or shared-title-token bucket may be
# before it stops being paired whole: a prolific author's whole back
# catalogue, or a generic word every third title carries, would otherwise
# turn one bucket into thousands of pairs that say nothing about identity.
# An author over the cap is still blocked within (_prolific_author_pairs);
# a token over it is dropped.
_MAX_PUBS_PER_AUTHOR = 90
_MAX_PUBS_PER_TOKEN = 45


# Every record sits in hundreds of blocked pairs; the per-record text work
# below is cached so it runs once per title/abstract, not once per pair.
_TEXT_CACHE_SIZE = 1 << 16


@lru_cache(maxsize=_TEXT_CACHE_SIZE)
def _fold_title(title: str | None) -> str:
    """Aggressive normalization for comparison: strip accents/markup that a
    publisher's re-typesetting changes (LaTeX, sub/superscript unicode,
    Cyrillic look-alikes) and drop words too common to carry identity."""
    if not title:
        return ""
    # Drop combining marks rather than everything non-ASCII: the latter
    # folds a Russian title to "" (or to its stray Latin fragments).
    text = "".join(ch for ch in unicodedata.normalize("NFKD", title) if not unicodedata.combining(ch))
    words = _NON_WORD.sub(" ", text.lower()).split()
    words = [word.translate(_LATIN_LOOKALIKES) if _HAS_LATIN.search(word) else word for word in words]
    return " ".join(word for word in words if word not in _STOPWORDS)


@lru_cache(maxsize=_TEXT_CACHE_SIZE)
def _title_tokens(title: str | None) -> frozenset[str]:
    return frozenset(word for word in _fold_title(title).split() if len(word) > 2)


def _jaccard(a: set | frozenset, b: set | frozenset) -> float:
    return len(a & b) / len(a | b) if (a or b) else 0.0


@lru_cache(maxsize=_TEXT_CACHE_SIZE)
def _shingles(text: str | None, n: int = 3) -> frozenset[str]:
    words = _fold_title(text).split()
    if len(words) < n:
        return frozenset(words)
    return frozenset(" ".join(words[i:i + n]) for i in range(len(words) - n + 1))


def _title_similarity(folded_a: str, folded_b: str, token_jaccard: float, floor: float = 0.0) -> float:
    """max(token Jaccard, SequenceMatcher ratio): the first catches reordered
    words, the second point edits (a hyphen, an ending).

    SequenceMatcher is the hot spot of the whole report, so below `floor` the
    exact ratio is skipped: when even its quick_ratio() upper bound cannot
    reach `floor`, the token Jaccard is returned instead. Callers comparing
    the result against a threshold >= `floor` get the same decision either
    way.
    """
    matcher = SequenceMatcher(None, folded_a, folded_b)
    if token_jaccard < floor and matcher.quick_ratio() < floor:
        return token_jaccard
    return max(token_jaccard, matcher.ratio())


def _arxiv_id(row: dict) -> str | None:
    for value in (row.get("doi"), *(row.get("pdf_urls") or ()), row.get("openalex_url")):
        if not value:
            continue
        match = _ARXIV_IN_DOI.search(value) or _ARXIV_IN_URL.search(value)
        if match:
            return match.group(1).lower()
    return None


def _is_preprint(row: dict) -> bool:
    return (bool(_PREPRINT_DOI.match(_norm_doi(row.get("doi")) or ""))
            or bool(_PREPRINT_VENUE.search(row.get("journal") or ""))
            or (row.get("type") or "").lower() == "preprint")


def _has_cyrillic(row: dict) -> bool:
    return bool(_CYRILLIC.search(row.get("journal") or "")) or bool(_CYRILLIC.search(row.get("title") or ""))


def _doi_version_siblings(doi_a: str | None, doi_b: str | None) -> bool:
    if not doi_a or not doi_b or doi_a == doi_b:
        return False
    shorter, longer = sorted((doi_a, doi_b), key=len)
    return longer.startswith(shorter) and bool(_VERSION_TAG.fullmatch(longer[len(shorter):]))


def _series_num(title: str) -> tuple[int, str] | None:
    """(series number, folded title text before it), or None."""
    match = _SERIES_NUM.search(title) or _SERIES_SECTION.search(title)
    if not match:
        return None
    token = match.group(1).lower()
    number = _ROMAN.get(token, int(token) if token.isdigit() else None)
    return (number, _fold_title(title[:match.start()])) if number is not None else None


def _series_variant(title_a: str, title_b: str, folded_a: str, folded_b: str) -> bool:
    """The two titles are one work's series entries, not versions of one
    work: a shared stem with a different "Part N" / roman numeral, or a
    section number that differs between otherwise-identical word lists
    ("... I. Methods" vs "... II. Applications")."""
    stem_a, stem_b = _SERIES_TAIL.sub("", folded_a).strip(), _SERIES_TAIL.sub("", folded_b).strip()
    if stem_a == stem_b and stem_a and folded_a != folded_b:
        return True
    bare_a, bare_b = _SERIES_BARE_TAIL.search(stem_a), _SERIES_BARE_TAIL.search(stem_b)
    if bare_a and bare_b and bare_a.group(1) != bare_b.group(1) \
            and stem_a[:bare_a.start()] == stem_b[:bare_b.start()] and bare_a.start():
        return True
    num_a, num_b = _series_num(title_a), _series_num(title_b)
    # Different numbers alone say nothing: "... beer industries. Part 3" and
    # "Thermophysical properties of colourants. Part 1" are two unrelated
    # series. Only a shared series name before the number makes them parts
    # of one series.
    if num_a and num_b and num_a[0] != num_b[0] and num_a[1] and num_b[1] \
            and SequenceMatcher(None, num_a[1], num_b[1]).ratio() >= 0.9:
        return True
    words_a, words_b = folded_a.split(), folded_b.split()
    if len(words_a) == len(words_b):
        diffs = [(x, y) for x, y in zip(words_a, words_b, strict=True) if x != y]
        if diffs and all(x in _ROMAN and y in _ROMAN for x, y in diffs):
            return True
    return False


def _notice_subject(row: dict) -> str | None:
    """The title a correction/retraction notice is about, with the notice
    marker stripped; None when the record is not a notice."""
    title = row.get("title") or ""
    prefix = _ERRATUM_PREFIX.match(title)
    if prefix:
        title = title[prefix.end():]
    stripped = _ERRATUM_SUFFIX.sub("", title)
    if prefix or stripped != title or (row.get("type") or "").lower() in _NOTICE_TYPES:
        return stripped
    return None


def _erratum_of(a: dict, b: dict) -> tuple[dict, dict] | None:
    """(erratum, original) if one record is a correction/retraction notice
    about the other: "Correction to: <the other>", "<the other> (Erratum)",
    or an OpenAlex `erratum`/`retraction` record reusing the title. The
    notice may follow the translated edition's wording ("Elevated
    Coefficient of Coupling" vs "increased coupling coefficient"), so a
    close title counts, not only a literal one."""
    for erratum, original in ((a, b), (b, a)):
        subject = _notice_subject(erratum)
        if subject is None:
            continue
        stripped = _fold_title(subject)
        base = _fold_title(original.get("title"))
        if not stripped or not base:
            continue
        # Containment counts only for a subject long enough to identify a
        # paper, and only on word boundaries: "Correction to: Introduction"
        # is not about every title with "introduction" in it.
        shorter, longer = sorted((stripped, base), key=len)
        contained = len(shorter.split()) >= _MIN_TITLE_TOKENS and f" {shorter} " in f" {longer} "
        if stripped == base or contained or _title_similarity(
                stripped, base, _jaccard(_title_tokens(subject), _title_tokens(original.get("title"))),
                floor=0.75) >= 0.75:
            return erratum, original
    return None


def _cover_of(a: dict, b: dict) -> tuple[dict, dict] | None:
    """(cover, article) if one record is the publisher's cover or note for
    the other: a cover marker around the other's title, a `paratext` record
    carrying it, or a short rubric before it in the same journal."""
    for cover, article in ((a, b), (b, a)):
        title = cover.get("title") or ""
        base = _fold_title(article.get("title"))
        if not base:
            continue
        stripped = _COVER_ISSUE_SUFFIX.sub("", _COVER_PREFIX.sub("", title, count=1))
        if stripped != title or (cover.get("type") or "").lower() == "paratext":
            folded = _fold_title(stripped)
            if folded and (folded == base or _title_similarity(
                    folded, base, _jaccard(_title_tokens(stripped), _title_tokens(article.get("title"))),
                    floor=0.9) >= 0.9):
                return cover, article
            continue
        # A rubric alone is not a marker: "Review: <title>" in another venue
        # can be a paper of its own. Only the article's own journal, and only
        # the article's exact title after it.
        rubric = _RUBRIC.match(title)
        venue_a, venue_b = (cover.get("journal") or "").casefold(), (article.get("journal") or "").casefold()
        if rubric and venue_a and venue_a == venue_b \
                and len(rubric.group(1).split()) <= _RUBRIC_MAX_WORDS \
                and _fold_title(rubric.group(2)) == base:
            return cover, article
    return None


def _venue_kind(row: dict) -> str | None:
    """"conference", "journal" or None (a repository, a book, nothing known)."""
    name = row.get("journal") or ""
    venue_type = (row.get("venue_type") or "").lower()
    if venue_type == "repository" or _PREPRINT_VENUE.search(name):
        return None
    if venue_type == "conference" or _CONFERENCE_VENUE.search(name):
        return "conference"
    if venue_type == "journal" or (not venue_type and name):
        return "journal"
    return None


def _date_gap_days(a: dict, b: dict) -> int | None:
    date_a, date_b = a.get("publication_date"), b.get("publication_date")
    if date_a and date_b:
        try:
            return abs((date.fromisoformat(str(date_a)) - date.fromisoformat(str(date_b))).days)
        except ValueError:
            pass
    if a.get("year") and b.get("year"):
        return abs(a["year"] - b["year"]) * 365
    return None


def _signals(a: dict, b: dict, **extra) -> dict:
    return {
        "record_a": a["id"], "title_a": a.get("title"),
        "record_b": b["id"], "title_b": b.get("title"),
        "venue_a": a.get("journal"), "venue_b": b.get("journal"),
        "type_a": a.get("type"), "type_b": b.get("type"),
        "venue_kind_a": _venue_kind(a), "venue_kind_b": _venue_kind(b),
        **extra,
    }


def classify_pair(a: dict, b: dict) -> tuple[str, dict] | None:
    """Bucket one candidate pair by the evidence it carries, or drop it.

    Args:
        a, b: Publication rows shaped like Neo4jClient.fetch_publications_for_
            dedup() - id, type, doi, title, journal, publication_date, year,
            abstract, authors ([{person_id, name, position}, ...]).

    Returns:
        (bucket, signals) or None. `bucket` is one of MERGE_BUCKETS (a
        version link worth a human's confirmation) or one of ERRATUM /
        SERIES_NOT_VERSION / SUPPLEMENT_OR_REVIEW / COVER (a look-alike
        explained, not a version). MEDIUM_FUZZY is returned whole;
        `route_medium_fuzzy` splits it. `signals` carries every number the decision used,
        for the review journal.
    """
    title_a, title_b = a.get("title") or "", b.get("title") or ""
    folded_a, folded_b = _fold_title(title_a), _fold_title(title_b)
    if not folded_a or not folded_b or folded_a in _GENERIC_TITLES or folded_b in _GENERIC_TITLES:
        return None

    erratum = _erratum_of(a, b)
    if erratum:
        return "ERRATUM", _signals(a, b, erratum=erratum[0]["id"], original=erratum[1]["id"])
    if _notice_subject(a) is not None or _notice_subject(b) is not None:
        # A correction/retraction notice about some third record: whatever
        # its title resembles here, it is not a version of it.
        return None

    cover = _cover_of(a, b)
    if cover:
        return "COVER", _signals(a, b, cover=cover[0]["id"], article=cover[1]["id"])

    if _series_variant(title_a, title_b, folded_a, folded_b):
        return "SERIES_NOT_VERSION", _signals(a, b)

    token_jaccard = _jaccard(_title_tokens(title_a), _title_tokens(title_b))

    if _DEPOSIT_HEAD.match(title_a.strip()) or _DEPOSIT_HEAD.match(title_b.strip()):
        # Only when the deposit's own title actually tracks the paper's -
        # otherwise every deposit sharing an author with any paper would
        # bucket here.
        if token_jaccard >= 0.5:
            title_sim = _title_similarity(folded_a, folded_b, token_jaccard)
            return "SUPPLEMENT_OR_REVIEW", _signals(a, b, title_sim=title_sim)
        return None

    doi_a, doi_b = _norm_doi(a.get("doi")), _norm_doi(b.get("doi"))
    min_tokens = min(len(folded_a.split()), len(folded_b.split()))
    # Checked before the first bucket, not only before the fuzzy ones: two
    # releases of one software package share their title verbatim.
    non_paper = any((row.get("type") or "").lower() in _NON_PAPER_TYPES for row in (a, b))

    if folded_a == folded_b and doi_a != doi_b and min_tokens >= _MIN_TITLE_TOKENS and not non_paper:
        return "MISSED_BY_NORM", _signals(a, b, title_sim=1.0, doi_a=doi_a, doi_b=doi_b)
    if _doi_version_siblings(doi_a, doi_b):
        title_sim = _title_similarity(folded_a, folded_b, token_jaccard)
        # Publishers also mint "<article DOI>.N" for the article's own
        # figures and tables (Cifra 10.60797, AIP supplementary material):
        # the DOI relation is the same, but such a component carries its own
        # caption as the title. A revision keeps the paper's title. A caption
        # can still read like the paper's title ("Классификация
        # мультимодальных интерфейсов..." under "Обзор мультимодальных
        # интерфейсов..."), so a component typed as a deposit never counts.
        similar = title_sim >= 0.75 and min_tokens >= _MIN_TITLE_TOKENS and not non_paper
        bucket = "DOI_SIBLING" if similar else "SUPPLEMENT_OR_REVIEW"
        return bucket, _signals(a, b, title_sim=round(title_sim, 3), doi_a=doi_a, doi_b=doi_b)

    if min_tokens < _MIN_TITLE_TOKENS or non_paper:
        return None

    # 0.75 is the lowest title threshold of any bucket below.
    title_sim = _title_similarity(folded_a, folded_b, token_jaccard, floor=0.75)
    if title_sim < 0.75:
        return None

    authors_a = {author["person_id"] for author in a.get("authors") or [] if author and author.get("person_id")}
    authors_b = {author["person_id"] for author in b.get("authors") or [] if author and author.get("person_id")}
    shared_authors = authors_a & authors_b
    author_jaccard = _jaccard(authors_a, authors_b)
    gap = _date_gap_days(a, b)
    abstract_a, abstract_b = a.get("abstract"), b.get("abstract")
    abstract_sim = _jaccard(_shingles(abstract_a), _shingles(abstract_b)) if (abstract_a and abstract_b) else None
    split = _is_preprint(a) != _is_preprint(b)

    signals = _signals(
        a, b, title_sim=round(title_sim, 3), token_jaccard=round(token_jaccard, 3),
        author_jaccard=round(author_jaccard, 3), shared_authors=len(shared_authors),
        date_gap_days=gap, preprint_split=split,
        abstract_sim=round(abstract_sim, 3) if abstract_sim is not None else None,
        doi_a=doi_a, doi_b=doi_b,
    )

    if split and title_sim >= 0.80 and shared_authors and (gap is None or gap <= 365 * 3):
        return "PREPRINT_PUBLISHED", signals
    if _has_cyrillic(a) != _has_cyrillic(b) and title_sim >= 0.75 \
            and (shared_authors or (abstract_sim is not None and abstract_sim >= 0.4)):
        return "RU_EN_TRANSLATION", signals
    if title_sim >= 0.90 and (author_jaccard >= 0.30 or len(shared_authors) >= 2) \
            and (gap is None or gap <= 1100):
        return "STRONG_FUZZY", signals
    if title_sim >= 0.78 and (author_jaccard >= 0.50 or (abstract_sim is not None and abstract_sim >= 0.45)) \
            and (gap is None or gap <= 365 * 4):
        return MEDIUM_FUZZY, signals
    return None


def route_medium_fuzzy(a: dict, b: dict, signals: dict, judge=None) -> str:
    """Which part of MEDIUM_FUZZY a pair belongs to.

    The study behind it (172 hand-labelled pairs) found three signals the
    title similarity misses: a shared abstract or a preprint in the pair
    (0.81 precision on their own), the venue pair (conference to journal is
    the usual extension, two conferences or two journals almost never a
    version), and an LLM reading both records. Two talks on one result at
    two conferences are two works; that is what the prompt says, and why
    the conservative model is the one used.

    Args:
        a, b: The pair, shaped as for `classify_pair`.
        signals: What `classify_pair` returned with MEDIUM_FUZZY.
        judge: Callable (a, b) -> {"same_work": bool, "reason": str} or
            None when it has no verdict. None skips the LLM step and leaves
            the pair as MEDIUM_FUZZY.

    Returns:
        One of the MEDIUM_FUZZY_* buckets, or MEDIUM_FUZZY unrouted. The
        verdict, when there is one, is added to `signals`.
    """
    abstract_sim = signals.get("abstract_sim")
    if signals.get("preprint_split") or (abstract_sim is not None and abstract_sim >= 0.45):
        return MEDIUM_FUZZY_STRONG
    verdict = judge(a, b) if judge is not None else None
    if verdict is None:
        return MEDIUM_FUZZY
    signals["llm_verdict"] = verdict["same_work"]
    signals["llm_reason"] = verdict.get("reason")
    if verdict["same_work"]:
        return MEDIUM_FUZZY_LLM_SAME
    if {signals.get("venue_kind_a"), signals.get("venue_kind_b")} == {"conference", "journal"}:
        return MEDIUM_FUZZY_CONF_JOURNAL
    return MEDIUM_FUZZY_RELATED


def _prolific_author_pairs(members: list[str], titles: dict[str, str | None]) -> set[tuple[str, str]]:
    """Pairs within one prolific author's bucket that share at least two
    title tokens rare for that author. Pairing the whole bucket is quadratic
    noise, but dropping it loses exactly the authors most likely to have a
    preprint and its journal version. Any pair that can reach the 0.75 title
    threshold shares words, so this keeps recall at a fraction of the pairs."""
    by_token: dict[str, list[str]] = defaultdict(list)
    for pub_id in members:
        for token in _title_tokens(titles[pub_id]):
            by_token[token].append(pub_id)
    shared: dict[tuple[str, str], int] = defaultdict(int)
    for ids in by_token.values():
        if len(ids) <= _MAX_PUBS_PER_TOKEN:
            for pair in combinations(sorted(ids), 2):
                shared[pair] += 1
    return {pair for pair, count in shared.items() if count >= 2}


def _candidate_pairs(rows: list[dict]) -> set[tuple[str, str]]:
    """Pairs worth classifying: rows sharing an author, a rare title token,
    or an arXiv id. Full O(n^2) comparison is not needed - see the module
    docstring's precision concern for why blocking, not correctness, is the
    binding constraint here."""
    by_author: dict[str, list[str]] = defaultdict(list)
    by_token: dict[str, list[str]] = defaultdict(list)
    by_arxiv: dict[str, list[str]] = defaultdict(list)
    titles = {row["id"]: row.get("title") for row in rows}
    for row in rows:
        for author in row.get("authors") or []:
            if author and author.get("person_id"):
                by_author[author["person_id"]].append(row["id"])
        for token in _title_tokens(row.get("title")):
            by_token[token].append(row["id"])
        arxiv_id = _arxiv_id(row)
        if arxiv_id:
            by_arxiv[arxiv_id].append(row["id"])

    pairs: set[tuple[str, str]] = set()
    for members in by_author.values():
        unique = sorted(set(members))
        if len(unique) > _MAX_PUBS_PER_AUTHOR:
            pairs.update(_prolific_author_pairs(unique, titles))
        elif len(unique) >= 2:
            pairs.update(combinations(unique, 2))
    for members in by_token.values():
        unique = sorted(set(members))
        if 2 <= len(unique) <= _MAX_PUBS_PER_TOKEN:
            pairs.update(combinations(unique, 2))
    for members in by_arxiv.values():
        unique = sorted(set(members))
        if len(unique) > 1:
            pairs.update(combinations(unique, 2))
    return pairs


def find_version_candidates(client, judge=None, rows: list[dict] | None = None) -> list[dict]:
    """Every candidate version pair the graph currently holds, as review-
    journal rows (status "held", never "merged" - see the module
    docstring). `client` is a Neo4jClient (or a compatible double, e.g.
    tests.bench.mocks.RecordingNeo4jClient). `judge` routes MEDIUM_FUZZY,
    see `route_medium_fuzzy`. `rows` skips the fetch when the caller has
    the publications already."""
    rows = client.fetch_publications_for_dedup() if rows is None else rows
    by_id = {row["id"]: row for row in rows}
    report = []
    # Sorted, so two runs over one graph write the same journal and can be
    # diffed to see what a publish added.
    for a_id, b_id in sorted(_candidate_pairs(rows)):
        result = classify_pair(by_id[a_id], by_id[b_id])
        if result is None:
            continue
        bucket, signals = result
        if bucket == MEDIUM_FUZZY:
            bucket = route_medium_fuzzy(by_id[a_id], by_id[b_id], signals, judge)
        report.append({
            "status": "held",
            "entity": "publication",
            "bucket": bucket,
            "mergeable": bucket in MERGE_BUCKETS,
            **signals,
        })
    return report


def run_version_report(config: Settings, output: Path | None = None) -> Path:
    """CLI entry point for `pauk versions report`.

    Read-only against Neo4j (no Mongo, no lock - nothing here writes to the
    graph). Writes the candidates to a review journal, one JSON object per
    line, and returns its path.
    """
    client = Neo4jClient(config.neo4j_uri, config.neo4j_user, config.neo4j_password)
    try:
        report = find_version_candidates(client)
    finally:
        client.close()

    path = output or (config.cache_dir / CANDIDATES_FILENAME)
    path.parent.mkdir(parents=True, exist_ok=True)
    with AtomicWriter(path) as fh:
        for row in report:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")

    mergeable = sum(1 for row in report if row["mergeable"])
    logger.info("version candidates: journal in %s — %d pair(s), %d worth a look",
                path, len(report), mergeable)
    return path

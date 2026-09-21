"""Report publication pairs that look like versions of one work but were not
folded by the exact-DOI / exact-title dedup stage (pauk/pipeline/stages/
dedup.py, see docs/architecture/pipeline/dedup.md): a preprint whose title
was polished for the journal, a conference paper extended into a journal
article, a Russian original and its Pleiades/MAIK English translation, or an
eLife-style `.v2` revision minted under its own DOI.

Read-only and report-only. Nothing here merges nodes, writes `merged_ids`,
or touches the graph - every pair is journalled with status "held", the same
as a person-dedup pair the review journal could not resolve on its own
(pauk/pipeline/stages/dedup.py). The reason is precision: unlike an exact
DOI or title match, a fuzzy title plus author overlap is corroborating
evidence, not proof - "Cliques and Constructors in 'Hats' Game. I" and "...
II" score a near-perfect title similarity and share every author with their
sequel, and are two different papers. A human confirms before anything here
becomes a merge; see docs/architecture/pipeline/version-candidates.md for
the full precision/recall picture per bucket.
"""

from __future__ import annotations

import json
import logging
import re
import unicodedata
from collections import defaultdict
from datetime import date
from difflib import SequenceMatcher
from itertools import combinations
from pathlib import Path

from pauk.pipeline.stages.dedup import PLACEHOLDER_TITLES, _norm_doi
from pauk.settings import Settings
from pauk.storage.atomic import AtomicWriter

from .client import Neo4jClient

logger = logging.getLogger(__name__)

CANDIDATES_FILENAME = "version_candidates.jsonl"

# Buckets corroborated enough to be worth a human's time; the rest
# (ERRATUM, SERIES_NOT_VERSION, SUPPLEMENT_OR_REVIEW) are look-alike pairs
# reported so they are explained rather than silently dropped, but are
# never a version relationship.
MERGE_BUCKETS = frozenset({
    "MISSED_BY_NORM", "DOI_SIBLING", "PREPRINT_PUBLISHED",
    "RU_EN_TRANSLATION", "STRONG_FUZZY", "MEDIUM_FUZZY",
})

# Repository/preprint hosts, matched against either the DOI or the venue
# name. Order does not matter - a work never carries two of these.
_PREPRINT_DOI = re.compile(
    r"10\.(48550/arxiv|1101|21203|31219|26434|20944|31234|2139|35542|13140|"
    r"55458|32942|33774|preprints)", re.I)
_PREPRINT_VENUE = re.compile(
    r"arxiv|biorxiv|medrxiv|chemrxiv|techrxiv|research\s*square|ssrn|preprints\.org|"
    r"osf|repec|authorea|figshare|zenodo|hal\b|working paper|preprint|cold spring harbor", re.I)
_ARXIV_IN_URL = re.compile(r"arxiv\.org/(?:abs|pdf)/([0-9]{4}\.[0-9]{4,5}|[a-z-]+/[0-9]{7})", re.I)
_ARXIV_IN_DOI = re.compile(r"10\.48550/arxiv\.([0-9]{4}\.[0-9]{4,5}|[a-z-]+/[0-9]{7})", re.I)

# One DOI is literally the other plus a version tag: "10.7554/elife.84874"
# vs "...84874.2" or "...84874.v2". Not "strip trailing digits" - that
# collapses every Zenodo/Figshare/IntechOpen DOI onto its registrant prefix.
_VERSION_TAG = re.compile(r"\.v?\d{1,3}$", re.I)

_ERRATUM_PREFIX = re.compile(
    r"^(correction|corrigendum|erratum|retraction(?:\s+note)?|"
    r"publisher correction|author correction|expression of concern|addendum)"
    r"\s*(to|for|:|—|-|–)?\s*", re.I)

# A record whose title only makes sense next to another paper's - a
# supplementary file, a dataset deposit, a peer-review report - not a
# version of the paper it is keyed off.
_DEPOSIT_HEAD = re.compile(
    r"^(additional file\s*\d*\s*(of|for)?|supplementary|supporting information|"
    r"data (from|for)|replication data for|peer review|review report|reviewer report)\b", re.I)

# Titles that differ only by a trailing series marker are different works in
# a series, not versions of one work ("Part I" / "Part II", "Volume 2").
_SERIES_TAIL = re.compile(
    r"\s*(?:[.,:;—–-]\s*)?(?:"
    r"part\s+(?:[ivx]+|\d+|one|two|three|four|five)"
    r"|(?:vol|volume)\s*\.?\s*\d+"
    r"|no\.?\s*\d+"
    r"|[ivx]{1,4}"
    r"|\d{1,3}"
    r"|supplementary (?:file|material|data)\s*\d*"
    r")\s*$", re.I)
_SERIES_NUM = re.compile(r"\b(?:part|pt|episode|chapter|no|#)\s*\.?\s*([ivx]+|\d{1,3})\b", re.I)
_ROMAN = {"i": 1, "ii": 2, "iii": 3, "iv": 4, "v": 5, "vi": 6, "vii": 7, "viii": 8, "ix": 9, "x": 10}

_CYRILLIC = re.compile(r"[Ѐ-ӿ]")
_STOPWORDS = frozenset({
    "a", "an", "the", "of", "and", "or", "for", "to", "in", "on", "with",
    "by", "at", "as", "from", "via", "using", "based",
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
_NON_PAPER_TYPES = frozenset({"software", "dataset", "peer-review", "grant", "other"})

_MIN_TITLE_TOKENS = 4
# Caps on how large a shared-author or shared-title-token bucket may be
# before it stops producing pairs: a prolific author's whole back catalogue,
# or a generic word every third title carries, would otherwise turn one
# bucket into thousands of pairs that say nothing about identity.
_MAX_PUBS_PER_AUTHOR = 90
_MAX_PUBS_PER_TOKEN = 45


def _fold_title(title: str | None) -> str:
    """Aggressive normalization for comparison: strip accents/markup that a
    publisher's re-typesetting changes (LaTeX, sub/superscript unicode,
    Cyrillic look-alikes) and drop words too common to carry identity."""
    if not title:
        return ""
    text = unicodedata.normalize("NFKD", title).encode("ascii", "ignore").decode()
    text = re.sub(r"[^a-z0-9]+", " ", text.lower())
    return " ".join(word for word in text.split() if word not in _STOPWORDS)


def _title_tokens(title: str | None) -> set[str]:
    return {word for word in _fold_title(title).split() if len(word) > 2}


def _jaccard(a: set, b: set) -> float:
    return len(a & b) / len(a | b) if (a or b) else 0.0


def _shingles(text: str | None, n: int = 3) -> set[str]:
    words = _fold_title(text).split()
    if len(words) < n:
        return set(words)
    return {" ".join(words[i:i + n]) for i in range(len(words) - n + 1)}


def _arxiv_id(row: dict) -> str | None:
    for value in (row.get("doi"), row.get("pdf_url"), row.get("openalex_url")):
        if not value:
            continue
        match = _ARXIV_IN_DOI.search(value) or _ARXIV_IN_URL.search(value)
        if match:
            return match.group(1).lower()
    return None


def _is_preprint(row: dict) -> bool:
    return (bool(_PREPRINT_DOI.search(row.get("doi") or ""))
            or bool(_PREPRINT_VENUE.search(row.get("journal") or ""))
            or (row.get("type") or "").lower() == "preprint")


def _has_cyrillic(row: dict) -> bool:
    return bool(_CYRILLIC.search(row.get("journal") or "")) or bool(_CYRILLIC.search(row.get("title") or ""))


def _doi_version_siblings(doi_a: str | None, doi_b: str | None) -> bool:
    if not doi_a or not doi_b or doi_a == doi_b:
        return False
    shorter, longer = sorted((doi_a, doi_b), key=len)
    return longer.startswith(shorter) and bool(_VERSION_TAG.fullmatch(longer[len(shorter):]))


def _series_num(title: str) -> int | None:
    match = _SERIES_NUM.search(title)
    if not match:
        return None
    token = match.group(1).lower()
    return _ROMAN.get(token, int(token) if token.isdigit() else None)


def _series_variant(title_a: str, title_b: str, folded_a: str, folded_b: str) -> bool:
    """The two titles are one work's series entries, not versions of one
    work: a shared stem with a different "Part N" / roman numeral, or a
    section number that differs between otherwise-identical word lists
    ("... I. Methods" vs "... II. Applications")."""
    stem_a, stem_b = _SERIES_TAIL.sub("", folded_a).strip(), _SERIES_TAIL.sub("", folded_b).strip()
    if stem_a == stem_b and stem_a and folded_a != folded_b:
        return True
    num_a, num_b = _series_num(title_a), _series_num(title_b)
    if num_a is not None and num_b is not None and num_a != num_b:
        return True
    words_a, words_b = folded_a.split(), folded_b.split()
    if len(words_a) == len(words_b):
        diffs = [(x, y) for x, y in zip(words_a, words_b, strict=True) if x != y]
        if diffs and all(x in _ROMAN and y in _ROMAN for x, y in diffs):
            return True
    return False


def _erratum_of(a: dict, b: dict) -> tuple[dict, dict] | None:
    """(erratum, original) if one title is "Correction to: <the other>"."""
    for erratum, original in ((a, b), (b, a)):
        match = _ERRATUM_PREFIX.match(erratum.get("title") or "")
        if not match:
            continue
        stripped = _fold_title((erratum["title"] or "")[match.end():])
        base = _fold_title(original.get("title"))
        if stripped and base and (stripped == base or stripped in base or base in stripped):
            return erratum, original
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
        SERIES_NOT_VERSION / SUPPLEMENT_OR_REVIEW (a look-alike explained,
        not a version). `signals` carries every number the decision used,
        for the review journal.
    """
    title_a, title_b = a.get("title") or "", b.get("title") or ""
    folded_a, folded_b = _fold_title(title_a), _fold_title(title_b)
    if not folded_a or not folded_b or folded_a in _GENERIC_TITLES or folded_b in _GENERIC_TITLES:
        return None

    erratum = _erratum_of(a, b)
    if erratum:
        return "ERRATUM", _signals(a, b, erratum=erratum[0]["id"], original=erratum[1]["id"])

    if _series_variant(title_a, title_b, folded_a, folded_b):
        return "SERIES_NOT_VERSION", _signals(a, b)

    token_jaccard = _jaccard(_title_tokens(title_a), _title_tokens(title_b))
    sequence_ratio = SequenceMatcher(None, folded_a, folded_b).ratio()
    title_sim = max(token_jaccard, sequence_ratio)

    if _DEPOSIT_HEAD.match(title_a.strip()) or _DEPOSIT_HEAD.match(title_b.strip()):
        # Only when the deposit's own title actually tracks the paper's -
        # otherwise every deposit sharing an author with any paper would
        # bucket here.
        if token_jaccard >= 0.5:
            return "SUPPLEMENT_OR_REVIEW", _signals(a, b, title_sim=title_sim)
        return None

    doi_a, doi_b = _norm_doi(a.get("doi")), _norm_doi(b.get("doi"))
    min_tokens = min(len(folded_a.split()), len(folded_b.split()))

    if folded_a == folded_b and doi_a != doi_b and min_tokens >= _MIN_TITLE_TOKENS:
        return "MISSED_BY_NORM", _signals(a, b, title_sim=1.0, doi_a=doi_a, doi_b=doi_b)
    if _doi_version_siblings(doi_a, doi_b):
        return "DOI_SIBLING", _signals(a, b, title_sim=title_sim, doi_a=doi_a, doi_b=doi_b)

    if min_tokens < _MIN_TITLE_TOKENS:
        return None
    if (a.get("type") or "").lower() in _NON_PAPER_TYPES or (b.get("type") or "").lower() in _NON_PAPER_TYPES:
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
        return "MEDIUM_FUZZY", signals
    return None


def _candidate_pairs(rows: list[dict]) -> set[tuple[str, str]]:
    """Pairs worth classifying: rows sharing an author, a rare title token,
    or an arXiv id. Full O(n^2) comparison is not needed - see the module
    docstring's precision concern for why blocking, not correctness, is the
    binding constraint here."""
    by_author: dict[str, list[str]] = defaultdict(list)
    by_token: dict[str, list[str]] = defaultdict(list)
    by_arxiv: dict[str, list[str]] = defaultdict(list)
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
    for bucket, limit in ((by_author, _MAX_PUBS_PER_AUTHOR), (by_token, _MAX_PUBS_PER_TOKEN)):
        for members in bucket.values():
            unique = sorted(set(members))
            if 2 <= len(unique) <= limit:
                pairs.update(combinations(unique, 2))
    for members in by_arxiv.values():
        unique = sorted(set(members))
        if len(unique) > 1:
            pairs.update(combinations(unique, 2))
    return pairs


def find_version_candidates(client) -> list[dict]:
    """Every candidate version pair the graph currently holds, as review-
    journal rows (status "held", never "merged" - see the module
    docstring). `client` is a Neo4jClient (or a compatible double, e.g.
    tests.bench.mocks.RecordingNeo4jClient)."""
    rows = client.fetch_publications_for_dedup()
    by_id = {row["id"]: row for row in rows}
    report = []
    for a_id, b_id in _candidate_pairs(rows):
        result = classify_pair(by_id[a_id], by_id[b_id])
        if result is None:
            continue
        bucket, signals = result
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

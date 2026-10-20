"""Link a collected GitHub account to the ITMO author behind it.

An address decides alone, an exact name needs a bridge or corroboration, a
fuzzy name needs both. Signals and weights: docs/architecture/pipeline/github-match.md.
"""

from __future__ import annotations

import json
import logging
import re
import unicodedata
from datetime import UTC, datetime
from difflib import SequenceMatcher
from typing import NamedTuple

from pauk.models import Contribution, GitHubProfile, Person, Publication, Repository
from pauk.models.processing import ProcessingState, ProcessingStatus
from pauk.pipeline.stages.dedup import folded_ids
from pauk.storage import review
from pauk.storage.atomic import AtomicWriter

from .base import EnrichmentStage
from .emails import author_surnames, pick_email

logger = logging.getLogger(__name__)

MATCHES_FILENAME = "github_matches.jsonl"

# Loose on purpose: "Ivan Petrov" and "Ivan Petrovsky" score 0.88, so a fuzzy
# name never decides alone.
NAME_EXACT = 0.999
NAME_FUZZY = 0.86

# An email alone reaches 1.0, a name alone does not.
SIGNAL_WEIGHTS = {
    "email_exact": 1.0,
    "name_exact": 0.6,
    "name_alternative": 0.6,
    "name_fuzzy": 0.4,
    "itmo_email": 0.3,
    "login_surname": 0.3,
    "owner": 0.3,
    "org_itmo": 0.3,
    "itmo_profile": 0.2,
}

# Signals that stand behind a name without identifying anyone by themselves.
CORROBORATING = ("itmo_profile", "itmo_email", "login_surname", "owner", "org_itmo")

# Signals about the person rather than the university; they make a match `high`.
STRONG = ("email_exact", "login_surname", "owner")

# As a word: "RITMO, University of Oslo" is a Norwegian centre. social_graph seeds on it too.
ITMO_IDENTITY_PATTERN = re.compile(r"\b(?:itmo|итмо)\b", re.I)

# A weak signal; a bare "Sankt" would match unrelated text.
PETERSBURG_PATTERN = re.compile(
    r"\b(?:saint|st\.?|sankt)[-\s]*peters?burg\b|\bсанкт[-\s]?петербург",
    re.I,
)

# Both, for `itmo_profile`; social_graph uses the identity pattern alone.
ITMO_IN_TEXT = re.compile(
    f"{ITMO_IDENTITY_PATTERN.pattern}|{PETERSBURG_PATTERN.pattern}", re.I,
)

ITMO_EMAIL_DOMAIN = "@itmo.ru"


def _norm_name(value: str | None) -> str:
    """A name reduced to lowercase latin words, for comparing spellings."""
    if not value:
        return ""
    stripped = "".join(char for char in unicodedata.normalize("NFKD", value)
                       if not unicodedata.combining(char))
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9\s]", " ", stripped.lower())).strip()


def _norm_email(value: str | None) -> str:
    return (value or "").strip().lower()


def name_similarity(first: str, second: str) -> float:
    """How alike two normalized names are, from 0 to 1, whatever the word order.

    The same words give 1.0 only for two or more: single surnames would match each other.
    """
    if not first or not second:
        return 0.0
    first_words, second_words = set(first.split()), set(second.split())
    if len(first_words) >= 2 and first_words == second_words:
        return 1.0
    return SequenceMatcher(None, " ".join(sorted(first_words)),
                           " ".join(sorted(second_words))).ratio()


def best_name_similarity(candidates: set[str], author: set[str]) -> tuple[float, tuple[str, str]]:
    """The closest pair among every spelling of both sides."""
    best, pair = 0.0, ("", "")
    for candidate in candidates:
        for known in author:
            similarity = name_similarity(candidate, known)
            if similarity > best:
                best, pair = similarity, (candidate, known)
    return best, pair


def login_carries_surname(login: str, surnames: set[str]) -> bool:
    """Whether the login is built from any spelling of the author's surname."""
    lowered = login.lower()
    return any(surname in lowered or SequenceMatcher(None, lowered, surname).ratio() >= 0.85
               for surname in surnames)


def score_account(account: dict, author: dict, email_hit: bool) -> tuple[float, list[str], dict]:
    """Signals the pair produces, their total weight, and what backs them."""
    signals: list[str] = []
    evidence: dict = {}

    if email_hit:
        signals.append("email_exact")
        evidence["email"] = sorted(account["emails"] & author["emails"])

    similarity, pair = best_name_similarity(account["names"], author["trusted"])
    if similarity >= NAME_EXACT:
        signals.append("name_exact")
    else:
        similarity, pair = best_name_similarity(account["names"], author["names"])
        if similarity >= NAME_EXACT:
            signals.append("name_alternative")
        elif similarity >= NAME_FUZZY:
            signals.append("name_fuzzy")
    if similarity >= NAME_FUZZY:
        evidence["name_pair"], evidence["name_similarity"] = pair, round(similarity, 2)

    if account["itmo_text"]:
        signals.append("itmo_profile")
    if any(email.endswith(ITMO_EMAIL_DOMAIN) for email in account["emails"]):
        signals.append("itmo_email")
    if login_carries_surname(account["login"], author["surnames"]):
        signals.append("login_surname")
    if account["is_owner"]:
        signals.append("owner")
    if account["org_itmo"]:
        signals.append("org_itmo")

    score = min(1.0, sum(SIGNAL_WEIGHTS[signal] for signal in signals))
    return score, signals, evidence


def confidence(signals: list[str], in_bridge: bool) -> str:
    """How much the match rests on evidence about this person.

    "probable" is a name plus an ITMO-wide signal, not a doubt: doubtful pairs go to review.
    """
    return "high" if in_bridge or any(signal in signals for signal in STRONG) else "probable"


def hold_reason(signals: list[str], ambiguous: bool = False) -> str:
    """Why a pair sent to review goes to a person, in the words the queue files it under."""
    if ambiguous:
        return "several authors fit this account equally well"
    if "name_exact" in signals:
        return "the name matches exactly and nothing else backs it"
    return "a similar name and a shared publication, nothing more"


def decide(signals: list[str], in_bridge: bool) -> str:
    """What to do with a pair: merge it, show it to a human, or drop it."""
    if "email_exact" in signals:
        return "matched"
    corroborated = any(signal in signals for signal in CORROBORATING)
    if "name_exact" in signals or "name_alternative" in signals:
        if in_bridge or corroborated:
            return "matched"
        # OpenAlex lumps namesakes together: a spelling the main name does not back asks nobody alone.
        return "review" if "name_exact" in signals else "rejected"
    if "name_fuzzy" in signals:
        if not in_bridge:
            return "rejected"
        return "matched" if corroborated else "review"
    return "rejected"


class Fit(NamedTuple):
    """One author scored against one account."""

    author_id: str
    score: float
    signals: list[str]
    evidence: dict
    decision: str


class Match(NamedTuple):
    """What the rules make of one account.

    Attributes:
        best: The author to link or to ask about, or None when nobody fits.
        candidates: Every author still in the running, `best` among them.
        refused: Authors a person said the account is not theirs, as the rules see them now.
    """

    best: Fit | None
    candidates: frozenset[str]
    refused: list[Fit]


def match_account(account: dict, authors: dict[str, dict], email_index: dict[str, str],
                  name_index: dict[str, set[str]], bridge: dict[str, set[str]],
                  verdicts: dict[str, str] | None = None) -> Match:
    """The author this account belongs to, and every other author it could be.

    Only authors reachable by publication, address or full name are scored. A
    tie on score goes to a shared address, then to a bridge; a tie on all three
    goes to a person. What people answered about this account (`verdicts`, by
    author) comes first: a "yes" wins the choice, a "no" leaves the author out of
    it, so the question moves on to the next one.
    """
    verdicts = verdicts or {}
    bridge_ids: set[str] = set()
    for publication_id in account["publication_ids"]:
        bridge_ids |= bridge.get(publication_id, set())
    email_ids = {email_index[email] for email in account["emails"] if email in email_index}
    name_ids: set[str] = set()
    for name in account["names"]:
        if len(name.split()) >= 2:
            name_ids |= name_index.get(name, set())

    best: tuple[tuple[bool, bool, float, bool, bool], Fit] | None = None
    candidates: set[str] = set()
    refusals: list[Fit] = []
    matched: list[tuple[bool, bool, float, bool, bool]] = []
    # Sorted: on a tie the question must name the same author on every run.
    for author_id in sorted(bridge_ids | email_ids | name_ids):
        author = authors.get(author_id)
        if author is None:
            continue
        in_bridge = author_id in bridge_ids
        score, signals, evidence = score_account(account, author, author_id in email_ids)
        decision = decide(signals, in_bridge)
        said = verdicts.get(author_id)
        if decision == "rejected" and said != review.SAME:
            continue
        evidence["in_bridge"] = in_bridge
        fit = Fit(author_id, score, signals, evidence, decision)
        if said == review.DIFFERENT:
            refusals.append(fit)
            continue
        candidates.add(author_id)
        rank = (said == review.SAME, decision == "matched", score, "email_exact" in signals, in_bridge)
        if best is None or rank > best[0]:
            best = (rank, fit)
        if decision == "matched":
            matched.append(rank)

    if best is None:
        return Match(None, frozenset(), refusals)
    rank, fit = best
    if fit.decision == "matched" and matched.count(rank) > 1:
        # Two authors fit equally well; picking either would be a guess.
        fit.evidence["ambiguous"] = True
        fit = fit._replace(decision="review")
    return Match(fit, frozenset(candidates), refusals)


class GitHubMatchStage(EnrichmentStage):
    name = "github_match"

    @staticmethod
    def _answered(decisions: list[dict], answers: dict[frozenset[str], str]) -> list[dict]:
        """Apply people's answers over the rules, both ways.

        Args:
            decisions: The rows the rules produced, changed in place.
            answers: What people said, keyed by the login and the person.

        Returns:
            Rows for the queue: unanswered pairs a person has to settle, and
            answered ones the rules now dispute.
        """
        questions: list[dict] = []
        for row in decisions:
            answered = answers.get(frozenset((row["login"], row["person"])))
            if answered is not None:
                # So the journal shows a person overruled the signals, not the rules themselves.
                row["rule"] = "manual"
            if answered == review.SAME:
                row["decision"] = "matched"
            elif answered == review.DIFFERENT:
                if row["decision"] == "matched":
                    # The answer stands; the rules now disagreeing is worth a look.
                    questions.append({**row, "status": "disputed",
                                      "rule": ", ".join(row["signals"])})
                row["decision"] = "rejected"
            elif row["decision"] == "review":
                questions.append({
                    **row, "status": "held",
                    "held_because": [hold_reason(row["signals"],
                                                 row["evidence"].get("ambiguous", False))],
                })
        return questions

    def _authors(self, people: list[Person]) -> tuple[dict, dict, dict]:
        """ITMO authors keyed by id, plus lookups by email and by full name."""
        authors: dict[str, dict] = {}
        for person in people:
            if not person.is_itmo:
                continue
            # ORCID spellings too: an account may be signed with any of them.
            names = {_norm_name(name) for name in
                     (person.name_raw, *person.name_variants, *person.other_names)}
            main = _norm_name(person.name_raw)
            # An OpenAlex spelling stands alone only when it says no more than the main name.
            variants = {_norm_name(name) for name in person.name_variants}
            trusted = {main, *(_norm_name(name) for name in person.other_names),
                       *(name for name in variants
                         if not person.name_raw or set(name.split()) <= set(main.split()))}
            authors[person.id] = {
                "names": {name for name in names if name},
                "trusted": {name for name in trusted if name},
                # Every known address: an account is recognised by whichever it used.
                "emails": {email for email in
                           {_norm_email(address) for address in (person.email, *person.emails)}
                           if email},
                # The email collector's rule, so both read a surname the same way.
                "surnames": author_surnames(person),
                "github": person.github,
            }

        email_index: dict[str, str] = {}
        name_index: dict[str, set[str]] = {}
        for author_id, author in authors.items():
            for email in author["emails"]:
                email_index.setdefault(email, author_id)
            for name in author["names"]:
                if len(name.split()) >= 2:
                    name_index.setdefault(name, set()).add(author_id)
        return authors, email_index, name_index

    def _accounts(self, profiles: list[GitHubProfile],
                  repositories: list[Repository]) -> dict[str, dict]:
        """Harvested accounts, aggregated across every repository they appear on."""
        by_url = {repository.url: repository for repository in repositories}
        owners = {repository.owner_login for repository in repositories if repository.owner_login}
        itmo_orgs = {owner.lower() for owner in owners if ITMO_IDENTITY_PATTERN.search(owner)}

        accounts: dict[str, dict] = {}
        for profile in profiles:
            if profile.type == "organization":
                continue
            text = f"{profile.company or ''} {profile.location or ''} {profile.description or ''}"
            names = {_norm_name(name) for name in (profile.name, profile.login, *profile.commit_names)}
            publication_ids: set[str] = set()
            is_owner = False
            for url in profile.repos:
                repository = by_url.get(url)
                if repository is None:
                    continue
                publication_ids |= set(repository.publication_ids)
                is_owner = is_owner or repository.owner_login == profile.login
            accounts[profile.login] = {
                "login": profile.login,
                "url": profile.html_url or f"https://github.com/{profile.login}",
                # Shown beside the question: a different name or a far city settles most at a glance.
                "profile": {"name": profile.name, "company": profile.company,
                            "location": profile.location},
                "names": {name for name in names if name},
                "emails": {_norm_email(email) for email in profile.emails if _norm_email(email)},
                "itmo_text": bool(ITMO_IN_TEXT.search(text)),
                "org_itmo": any((by_url[url].owner_login or "").lower() in itmo_orgs
                                for url in profile.repos if url in by_url),
                "publication_ids": publication_ids,
                "repos": set(profile.repos),
                "is_owner": is_owner,
            }
        return accounts

    @staticmethod
    def _bridge(people: list[Person]) -> dict[str, set[str]]:
        """publication id -> the ITMO authors who wrote it."""
        bridge: dict[str, set[str]] = {}
        for person in people:
            if not person.is_itmo:
                continue
            for authorship in person.authored:
                bridge.setdefault(authorship.publication_id, set()).add(person.id)
        return bridge

    def run(self) -> dict[str, int]:
        people = list(self.prepared.read_models("persons", Person))
        profiles = list(self.prepared.read_models("github_profiles", GitHubProfile))
        repositories = list(self.prepared.read_models("repositories", Repository))
        # Read but unused: the bridge comes from the authorships on each person.
        list(self.prepared.read_models("publications", Publication))

        authors, email_index, name_index = self._authors(people)
        accounts = self._accounts(profiles, repositories)
        bridge = self._bridge(people)
        logger.info("github_match: %d accounts against %d ITMO authors",
                    len(accounts), len(authors))

        aliases = folded_ids(people)
        # Through folded ids: an answer about a since-merged person keeps applying.
        answers = review.github_decisions(self.prepared.db, aliases)
        verdicts: dict[str, dict[str, str]] = {}
        for members, verdict in answers.items():
            for login in (member for member in members if member in accounts):
                for person_id in members - {login}:
                    verdicts.setdefault(login, {})[person_id] = verdict

        by_id = {person.id: person for person in people}
        repositories_by_url = {repository.url: repository for repository in repositories}

        def row_of(login: str, fit: Fit) -> dict:
            return {
                "login": login,
                "url": accounts[login]["url"],
                "profile": accounts[login]["profile"],
                "person": fit.author_id,
                "name_raw": by_id[fit.author_id].name_raw,
                "score": round(fit.score, 2),
                "signals": fit.signals,
                "evidence": fit.evidence,
                "decision": fit.decision,
                "confidence": confidence(fit.signals, fit.evidence.get("in_bridge", False)),
                "repos": sorted(accounts[login]["repos"]),
            }

        decisions: list[dict] = []
        chosen: dict[str, dict] = {}
        candidates: dict[str, frozenset[str]] = {}
        # Sorted: two runs over the same data must reach the same decisions.
        for login in sorted(accounts):
            match = match_account(accounts[login], authors, email_index, name_index, bridge,
                                  verdicts.get(login))
            candidates[login] = match.candidates
            decisions += [row_of(login, fit) for fit in match.refused]
            if match.best is not None:
                chosen[login] = row_of(login, match.best)
                decisions.append(chosen[login])

        questions = self._answered(decisions, answers)

        stats = {"matched": 0, "review": 0}
        filled_emails = 0
        linked: set[str] = set()
        for row in decisions:
            stats[row["decision"]] = stats.get(row["decision"], 0) + 1
            if row["decision"] != "matched":
                continue
            person = by_id[row["person"]]
            if not self.selected("persons", person.id):
                continue
            linked.add(row["login"])
            state = person.processing.get(self.name)
            person.github = person.github or row["login"]
            # Only an author without an address takes the account's: ORCID's stays.
            if not person.email:
                person.email = pick_email(accounts[row["login"]]["emails"])
                if person.email:
                    filled_emails += 1
            for url in accounts[row["login"]]["repos"]:
                repository = repositories_by_url.get(url)
                if repository is None:
                    continue
                role = "owner" if repository.owner_login == row["login"] else "contributor"
                contribution = Contribution(repository_id=repository.id, role=role)
                if contribution not in person.contributed_to:
                    person.contributed_to.append(contribution)
            person.processing[self.name] = ProcessingState(
                status=ProcessingStatus.COMPLETED,
                attempts=(state.attempts if state else 0) + 1,
                finished_at=datetime.now(UTC),
                result_count=1,
            )

        self.prepared.write_models("persons", people)
        review.record_held(self.prepared.db, questions, source=review.STAGE)
        review.record_disputed(self.prepared.db, questions)
        # A link counts only where it was written: a scoped run leaves the others alone.
        covered = {person.id for person in people if self.selected("persons", person.id)}
        still_asked = {login: frozenset() if login in linked else candidates[login] for login in accounts}
        review.retire_github(self.prepared.db, dict.fromkeys(accounts) | chosen, still_asked,
                             covered, aliases)
        # A file beside dedup's journal: it is read by people, not by the pipeline.
        journal_path = self.config.audit_dir / self.prepared.group / MATCHES_FILENAME
        with AtomicWriter(journal_path) as handle:
            for row in decisions:
                handle.write(json.dumps(row, ensure_ascii=False) + "\n")
        logger.info("github_match: %d matched, %d for review, %d emails filled; see %s",
                    stats["matched"], stats.get("review", 0), filled_emails, journal_path)
        return {"github_matched": stats["matched"],
                "github_review": stats.get("review", 0),
                "github_emails": filled_emails,
                "github_accounts": len(accounts)}

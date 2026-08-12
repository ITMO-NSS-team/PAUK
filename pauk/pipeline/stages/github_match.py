"""Identify the GitHub account behind an ITMO author.

A repository cited by a paper is worked on by people, and one of them is
often the paper's author — but the two records share no identifier. The
harvest in `repositories` collects the accounts; this stage decides which
author, if any, each account belongs to.

Nothing about an account proves identity on its own, so the decision
weighs several signals at once:

    email      an address the account committed with, or published on its
               profile, that the author is also known by. Decisive: people
               do not share addresses.
    name       the account's display name or the name in its commits,
               against the author's name and the spellings OpenAlex knows.
    bridge     the account was found on a repository cited by a paper this
               author wrote. Not proof — a paper has many authors and a
               repository many contributors — but it narrows the field
               from every ITMO employee to a handful.
    profile    ITMO named in the account's company, location or bio.
    login      the author's surname inside the login itself.
    owner      the account owns the repository rather than contributing.

An email settles the question by itself. A name needs the bridge or some
corroboration behind it, and a fuzzy name needs both — the surnames this
university's authors carry (Smirnov, Ivanov, Novikov) collide inside one
department, let alone across GitHub.

Decisions are journalled to github_matches.jsonl: `matched` writes
Person.github, `review` waits for a human, and the reason for either is
recorded next to the evidence that produced it.
"""

from __future__ import annotations

import json
import logging
import re
import unicodedata
from datetime import UTC, datetime
from difflib import SequenceMatcher

from pauk.models import GitHubProfile, Person, Publication, Repository
from pauk.models.processing import ProcessingState, ProcessingStatus
from pauk.storage.atomic import AtomicWriter

from .base import EnrichmentStage

logger = logging.getLogger(__name__)

MATCHES_FILENAME = "github_matches.jsonl"

# Identical token sets score exactly 1.0, so anything under NAME_EXACT is a
# character-level resemblance. The fuzzy threshold is deliberately loose and
# catches names that are not the same person — "Ivan Petrov" against "Ivan
# Petrovsky" scores 0.88 — which is why a fuzzy name never decides on its
# own: decide() demands the bridge and corroboration behind it.
NAME_EXACT = 0.999
NAME_FUZZY = 0.86

# How much each signal adds. An email alone reaches the ceiling; a name
# alone does not, which is what forces corroboration for the rest.
SIGNAL_WEIGHTS = {
    "email_exact": 1.0,
    "name_exact": 0.6,
    "name_fuzzy": 0.4,
    "itmo_email": 0.3,
    "login_surname": 0.3,
    "owner": 0.3,
    "org_itmo": 0.3,
    "itmo_profile": 0.2,
}

# Signals that stand behind a name without identifying anyone by themselves.
CORROBORATING = ("itmo_profile", "itmo_email", "login_surname", "owner", "org_itmo")

# ITMO in a profile, as a word: "RITMO, University of Oslo" is a Norwegian
# centre whose name contains the same four letters.
ITMO_IN_TEXT = re.compile(r"\bitmo\b|saint[- ]petersburg|sankt", re.I)

ITMO_EMAIL_DOMAIN = "@itmo.ru"

# A surname shorter than this is an initial or a particle, and matching a
# login against it would fire on almost anything.
MIN_SURNAME = 4


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
    """How alike two normalized names are, from 0 to 1.

    Word order carries no information here — "Petrov Ivan" and "Ivan
    Petrov" are one name — so two names built from the same words match
    exactly. A single word is not enough for that: half the surnames in
    the pool would match each other.
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


def login_carries_surname(login: str, surname: str | None) -> bool:
    """Whether the login is built from the author's surname."""
    if not surname:
        return False
    lowered = login.lower()
    return surname in lowered or SequenceMatcher(None, lowered, surname).ratio() >= 0.85


def score_account(account: dict, author: dict, email_hit: bool) -> tuple[float, list[str], dict]:
    """Signals the pair produces, their total weight, and what backs them."""
    signals: list[str] = []
    evidence: dict = {}

    if email_hit:
        signals.append("email_exact")
        evidence["email"] = sorted(account["emails"] & author["emails"])

    similarity, pair = best_name_similarity(account["names"], author["names"])
    if similarity >= NAME_EXACT:
        signals.append("name_exact")
    elif similarity >= NAME_FUZZY:
        signals.append("name_fuzzy")
    if similarity >= NAME_FUZZY:
        evidence["name_pair"], evidence["name_similarity"] = pair, round(similarity, 2)

    if account["itmo_text"]:
        signals.append("itmo_profile")
    if any(email.endswith(ITMO_EMAIL_DOMAIN) for email in account["emails"]):
        signals.append("itmo_email")
    if login_carries_surname(account["login"], author["surname"]):
        signals.append("login_surname")
    if account["is_owner"]:
        signals.append("owner")
    if account["org_itmo"]:
        signals.append("org_itmo")

    score = min(1.0, sum(SIGNAL_WEIGHTS[signal] for signal in signals))
    return score, signals, evidence


def decide(signals: list[str], in_bridge: bool) -> str:
    """What to do with a pair: merge it, show it to a human, or drop it."""
    if "email_exact" in signals:
        return "matched"
    corroborated = any(signal in signals for signal in CORROBORATING)
    if "name_exact" in signals:
        if in_bridge or corroborated:
            return "matched"
        return "review"
    if "name_fuzzy" in signals:
        if not in_bridge:
            return "rejected"
        return "matched" if corroborated else "review"
    return "rejected"


def match_account(account: dict, authors: dict[str, dict], email_index: dict[str, str],
                  name_index: dict[str, set[str]], bridge: dict[str, set[str]]):
    """The author this account belongs to, or None if nobody fits.

    Only three groups are considered: authors reachable through a shared
    publication, through an address, or through a full name. Scoring every
    author against every account would be both slow and pointless — the
    rest produce no signal at all.
    """
    bridge_ids: set[str] = set()
    for publication_id in account["publication_ids"]:
        bridge_ids |= bridge.get(publication_id, set())
    email_ids = {email_index[email] for email in account["emails"] if email in email_index}
    name_ids: set[str] = set()
    for name in account["names"]:
        if len(name.split()) >= 2:
            name_ids |= name_index.get(name, set())

    best = None
    accepted: list[tuple[float, str]] = []
    for author_id in bridge_ids | email_ids | name_ids:
        author = authors.get(author_id)
        if author is None:
            continue
        in_bridge = author_id in bridge_ids
        score, signals, evidence = score_account(account, author, author_id in email_ids)
        decision = decide(signals, in_bridge)
        if decision == "rejected":
            continue
        evidence["in_bridge"] = in_bridge
        rank = (decision == "matched", score)
        if best is None or rank > best[0]:
            best = (rank, author_id, score, signals, evidence, decision)
        if decision == "matched":
            accepted.append((score, author_id))

    if best is None:
        return None
    _, author_id, score, signals, evidence, decision = best
    if decision == "matched":
        top = max(score for score, _ in accepted)
        if len({author for score, author in accepted if score == top}) > 1:
            # Two authors fit equally well; picking either would be a guess.
            decision = "review"
            evidence["ambiguous"] = True
    return author_id, score, signals, evidence, decision


class GitHubMatchStage(EnrichmentStage):
    name = "github_match"

    def _authors(self, people: list[Person]) -> tuple[dict, dict, dict]:
        """ITMO authors keyed by id, plus lookups by email and by full name."""
        authors: dict[str, dict] = {}
        for person in people:
            if not person.is_itmo:
                continue
            names = {_norm_name(name) for name in (person.name_en, *person.name_variants)}
            words = _norm_name(person.name_en).split()
            authors[person.id] = {
                "names": {name for name in names if name},
                "emails": {email for email in {_norm_email(person.email)} if email},
                # The surname is the last word of a romanized name; a short
                # one is an initial and matches logins by accident.
                "surname": words[-1] if words and len(words[-1]) >= MIN_SURNAME else None,
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
        itmo_orgs = {owner.lower() for owner in owners if ITMO_IN_TEXT.search(owner or "")}

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
        # Publications are read only to keep the stage honest about what it
        # saw; the bridge itself comes from the authorships on each person.
        list(self.prepared.read_models("publications", Publication))

        authors, email_index, name_index = self._authors(people)
        accounts = self._accounts(profiles, repositories)
        bridge = self._bridge(people)
        logger.info("github_match: %d accounts against %d ITMO authors",
                    len(accounts), len(authors))

        by_id = {person.id: person for person in people}
        decisions: list[dict] = []
        # Sorted so a run does not depend on dictionary order: two runs over
        # the same data must reach the same decisions.
        for login in sorted(accounts):
            result = match_account(accounts[login], authors, email_index, name_index, bridge)
            if result is None:
                continue
            author_id, score, signals, evidence, decision = result
            decisions.append({
                "login": login,
                "url": accounts[login]["url"],
                "person": author_id,
                "name_en": by_id[author_id].name_en,
                "score": round(score, 2),
                "signals": signals,
                "evidence": evidence,
                "decision": decision,
                "repos": sorted(accounts[login]["repos"]),
            })

        stats = {"matched": 0, "review": 0}
        for row in decisions:
            stats[row["decision"]] = stats.get(row["decision"], 0) + 1
            if row["decision"] != "matched":
                continue
            person = by_id[row["person"]]
            if not self.selected("persons", person.id):
                continue
            state = person.processing.get(self.name)
            person.github = person.github or row["login"]
            person.processing[self.name] = ProcessingState(
                status=ProcessingStatus.COMPLETED,
                attempts=(state.attempts if state else 0) + 1,
                finished_at=datetime.now(UTC),
                result_count=1,
            )

        self.prepared.write_models("persons", people)
        journal_path = self.prepared.group_dir / MATCHES_FILENAME
        with AtomicWriter(journal_path) as handle:
            for row in decisions:
                handle.write(json.dumps(row, ensure_ascii=False) + "\n")
        logger.info("github_match: %d matched, %d for review — see %s",
                    stats["matched"], stats.get("review", 0), journal_path)
        return {"github_matched": stats["matched"],
                "github_review": stats.get("review", 0),
                "github_accounts": len(accounts)}
"""Collect author emails from the full text of a paper.

A paper prints the addresses of its authors, usually next to the
affiliations on the first page. OpenAlex does not carry them, and ORCID
holds one for about five percent of the people here, so the text is the
richest source available — and it costs nothing, since code_links already
stores the text it downloaded.

An address on a page belongs to one of the authors, but the page does not
say which. The local part does: people write `dukhanov@itmo.ru`, and the
surname inside it names its owner. An address whose local part fits two
authors of the same paper is dropped rather than guessed at — two
Petrovs on one paper is exactly the case this must not get wrong.

Papers also compress the addresses of several authors into one line:

    {lvkarakchieva, pvtrifonov}@itmo.ru

which is read as the two addresses it stands for.

The other source is the page the author linked from their ORCID record.
Addresses there are usually written to defeat scrapers — "name [at]
itmo.ru", "name&#64;itmo.ru" — so the text is un-obfuscated before it is
read. Only the page of the person being resolved is fetched, and only when
the surname in the address confirms it is theirs.
"""

from __future__ import annotations

import logging
import re
import unicodedata
from datetime import UTC, datetime

from pauk.models import Person, Publication
from pauk.models.processing import ProcessingState, ProcessingStatus
from pauk.sources.base import HttpClient

from .base import EnrichmentStage

logger = logging.getLogger(__name__)

# Domains as they end in running text. Anchoring on a known suffix keeps
# "et al.2020@" and file names out; the negative lookahead stops the match
# from eating the first letters of the next word.
TLD = (r"(?:ru|com|org|net|edu|gov|io|info|biz|name|eu|de|fr|uk|us|cn|jp|kr"
       r"|in|it|es|nl|se|fi|no|ch|at|cz|pl|by|kz|ua)")
EMAIL_RE = re.compile(rf"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+?\.{TLD}(?![A-Za-z])", re.I)
# "{first, second}@domain" — several authors sharing one institutional host.
BRACE_RE = re.compile(rf"\{{([^{{}}@]+)\}}@([A-Za-z0-9.\-]+?\.{TLD})(?![A-Za-z])", re.I)

# A surname shorter than this inside a local part matches by accident.
MIN_SURNAME = 4

MAILTO_RE = re.compile(r"mailto:([A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,})", re.I)

# A page written to keep scrapers out still has to be readable by a person,
# so the address is spelled rather than hidden.
OBFUSCATION = (
    (re.compile(r"&#0*64;|&#x0*40;|&commat;", re.I), "@"),
    (re.compile(r"\s*[\[\(\{<]\s*at\s*[\]\)\}>]\s*", re.I), "@"),
    (re.compile(r"\s*[\[\(\{<]\s*dot\s*[\]\)\}>]\s*", re.I), "."),
    (re.compile(r"\s*@\s*"), "@"),
)

# Pages fetched per person. An author lists one homepage; the cap is there
# so a record with a dozen links cannot stall the stage.
MAX_PAGES_PER_PERSON = 3

PAGE_TIMEOUT = 12

# An address at the university identifies an employee better than a personal
# one, whichever source either came from.
INSTITUTIONAL_DOMAINS = ("@itmo.ru", "@ifmo.ru")


def pick_email(addresses: set[str]) -> str | None:
    """The address that best identifies an employee, among those on offer.

    A university address wins outright; otherwise the shortest, which is a
    stable choice and tends to be the plain `name@host` rather than a
    numbered alias.
    """
    usable = sorted(email for email in addresses
                    if "@" in email and "noreply" not in email)
    if not usable:
        return None
    institutional = [email for email in usable if email.endswith(INSTITUTIONAL_DOMAINS)]
    return institutional[0] if institutional else min(usable, key=len)


def _letters(value: str | None) -> str:
    """Lowercase latin letters only, for comparing a surname to a local part."""
    stripped = "".join(char for char in unicodedata.normalize("NFKD", value or "")
                       if not unicodedata.combining(char))
    return re.sub(r"[^a-z]", "", stripped.lower())


def author_surnames(person: Person) -> set[str]:
    """Every surname this author is published under.

    The last word of a romanized name, taken from the display name and
    from each spelling OpenAlex knows — an author writing as "Dukhanov"
    in one paper and "Duhanov" in another is one person with two.
    """
    surnames = set()
    for name in (person.name_en, *person.name_variants, *person.other_names):
        words = [_letters(word) for word in (name or "").split()]
        words = [word for word in words if word]
        if len(words) >= 2 and len(words[-1]) >= MIN_SURNAME:
            surnames.add(words[-1])
    return surnames


def emails_in_text(text: str) -> set[str]:
    """Every address the text states, braces expanded."""
    found: set[str] = set()
    for inside, domain in BRACE_RE.findall(text):
        for part in re.split(r"[;,]", inside):
            part = part.strip().strip(".")
            if part:
                found.add(f"{part}@{domain}".lower())
    for match in EMAIL_RE.findall(text):
        found.add(match.lower().rstrip("."))
    return found


def emails_in_html(html: str) -> set[str]:
    """Addresses a page states, including the ones written to hide them."""
    found = {match.lower() for match in MAILTO_RE.findall(html)}
    text = html
    for pattern, replacement in OBFUSCATION:
        text = pattern.sub(replacement, text)
    return found | emails_in_text(text)


def owner_of(email: str, authors: list[tuple[str, set[str]]]) -> str | None:
    """The author whose surname is inside the address, when only one is.

    Two authors fitting one address means the paper has namesakes and the
    address names neither of them unambiguously.
    """
    local_part = _letters(email.split("@")[0])
    if not local_part:
        return None
    owners = [person_id for person_id, surnames in authors
              if any(surname in local_part for surname in surnames)]
    return owners[0] if len(owners) == 1 else None


class EmailsStage(EnrichmentStage):
    """Fills Person.email from the papers the person wrote and the page they list."""

    name = "emails"

    def _from_homepage(self, person: Person, surnames: set[str]) -> set[str]:
        """Addresses on the author's own page that carry their surname.

        A lab page lists the whole group, so an address is only taken when
        the surname inside it is this person's — the same test the papers
        get. Any failure to fetch is silent: a page is a courtesy source,
        and a dead link must not fail the run.
        """
        pages = [url for url in (person.homepage,) if url][:MAX_PAGES_PER_PERSON]
        if not pages:
            return set()
        client = HttpClient(PAGE_TIMEOUT, {"User-Agent": "PAUK/2.0"})
        found: set[str] = set()
        for url in pages:
            try:
                html = client.get_bytes(url, retries=1).decode("utf-8", "replace")
            except Exception:
                continue
            self.raw.append("page", {"url": url, "length": len(html)}, {"person": person.id})
            for email in emails_in_html(html):
                if owner_of(email, [(person.id, surnames)]) == person.id:
                    found.add(email)
        return found

    def run(self) -> dict[str, int]:
        publications = list(self.prepared.read_models("publications", Publication))
        people = list(self.prepared.read_models("persons", Person))
        by_id = {person.id: person for person in people}

        # Only ITMO authors are worth resolving, and only those with a
        # surname long enough to be found inside a local part.
        authors_of: dict[str, list[tuple[str, set[str]]]] = {}
        for person in people:
            if not person.is_itmo:
                continue
            surnames = author_surnames(person)
            if not surnames:
                continue
            for authorship in person.authored:
                authors_of.setdefault(authorship.publication_id, []).append(
                    (person.id, surnames))

        found: dict[str, set[str]] = {}
        changed = pages_read = 0
        for publication in publications:
            if not self.selected("publications", publication.id):
                continue
            state = publication.processing.get(self.name)
            if not self.needs_attempt(state):
                continue
            authors = authors_of.get(publication.id, [])
            addresses = emails_in_text(publication.full_text or "") if publication.full_text else set()
            resolved = 0
            for email in addresses:
                owner = owner_of(email, authors)
                if owner is not None:
                    found.setdefault(owner, set()).add(email)
                    resolved += 1
            publication.processing[self.name] = ProcessingState(
                status=ProcessingStatus.COMPLETED if resolved else ProcessingStatus.COMPLETED_EMPTY,
                attempts=(state.attempts if state else 0) + 1,
                finished_at=datetime.now(UTC),
                result_count=resolved,
            )
            changed += 1

        # The page an author linked from ORCID, read only for those still
        # without an address: everyone else already has a better one.
        for person in people:
            if not person.is_itmo or person.email or not person.homepage:
                continue
            if not self.selected("persons", person.id):
                continue
            surnames = author_surnames(person)
            if not surnames:
                continue
            addresses = self._from_homepage(person, surnames)
            pages_read += 1
            if addresses:
                found.setdefault(person.id, set()).update(addresses)

        filled = 0
        for person_id, addresses in found.items():
            person = by_id[person_id]
            # Every address found is kept for the matcher, which recognises
            # an account by whichever one it used. Only the address shown on
            # the card is left alone once ORCID has stated one.
            person.emails = sorted(set(person.emails) | addresses)
            if person.email:
                continue
            person.email = pick_email(addresses)
            if person.email:
                filled += 1

        if changed:
            self.prepared.write_models("publications", publications)
        # Addresses are stored even when the card already shows one, so the
        # write happens whenever any were found, not only when one was used.
        if found:
            self.prepared.write_models("persons", people)
        logger.info("emails: %d publications and %d pages read, %d authors given an address",
                    changed, pages_read, filled)
        return {"publications": changed, "pages": pages_read, "emails_filled": filled}

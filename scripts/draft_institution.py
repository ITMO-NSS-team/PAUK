"""Draft a university's profile and department catalogue from ROR and OpenAlex.

A starting point for a person to review, not a finished catalogue. Steps:

1. ROR: the university's names, acronyms, domains and city.
2. OpenAlex: a sample of the university's works in the period, with every
   authorship's raw affiliation strings.
3. The parts of member affiliations (split on ";" and ",") that occur for more
   than one author: how people actually write the units.
4. An LLM drafts `profile.json` (see pauk/institution.py) and the catalogue
   from those, following the matcher's rules (docs/architecture/pipeline/departments.md).
5. The departments matcher runs with the draft over the same sample: share of
   member authors matched, external authors matched (false positives), units
   never hit, the most frequent member phrases left unmatched. The LLM gets
   that report back once and revises the draft.

Writes data/static/<key>/profile.json, data/static/<key>/departments_catalog.json
and data/reports/institutions/<key>.md (the validation report). OpenAlex list
requests cost $0.0001 each (about $0.003 for 3,000 works); the LLM about
$0.5-1 per university with claude-opus-5.5.

Without the model: `--brief` prints what the model would be shown (ROR data
and the member phrases), a person writes the profile and catalogue as JSON in
the model's reply format, and `--draft FILE` runs that through the same
checks and writes the same files. `--cache DIR` keeps the OpenAlex sample, so
the two calls and any `--refine` fetch it once.

Run from the project root:
    uv run python scripts/draft_institution.py mit 042nb2s44
    uv run python scripts/draft_institution.py mit 042nb2s44 --works 3000 --model anthropic/claude-opus-5.5
    uv run python scripts/draft_institution.py mit 042nb2s44 --cache samples --brief
    uv run python scripts/draft_institution.py mit 042nb2s44 --cache samples --draft mit.json
"""

from __future__ import annotations

import argparse
import html
import json
import logging
import re
import sys
import tempfile
from collections import Counter, defaultdict
from dataclasses import fields
from datetime import date, timedelta
from pathlib import Path

from pauk.institution import Institution, load
from pauk.pipeline.stages.departments import _contains, _context_names, _match_names, _match_person, _normalize
from pauk.settings import settings
from pauk.sources.base import HttpClient
from pauk.sources.llm import OpenRouterClient
from pauk.storage.static import StaticStore

logger = logging.getLogger("draft_institution")

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "data" / "static"
REPORTS = ROOT / "data" / "reports" / "institutions"
DEFAULT_MODEL = "anthropic/claude-opus-5.5"
# A phrase seen for a single author says nothing about how the unit is written.
MIN_AUTHORS = 2
MAX_PHRASES = 400
LLM_TIMEOUT = 900

SYSTEM_PROMPT = """You build reference data for PAUK, a pipeline that links a university's authors to its
departments by matching their OpenAlex affiliation strings. Accuracy matters more than size:
every unit you list must exist at this university. Reply with one JSON object only."""

TASK = """University: {name} (ROR {ror}, {country}, city {city}).
ROR names and labels: {names}
ROR acronyms: {acronyms}
ROR domains: {domains}
ROR related organisations (hospitals, institutes, parents, children): {related}

How members of this university write their affiliations: the most frequent comma- or
semicolon-separated parts of member affiliation strings in {works} works of {period},
with the number of distinct authors writing each part:
{phrases}

Produce JSON with two keys.

"profile": the matcher's profile of the university, every field below (Python `re` syntax,
matched case-insensitively against affiliation text):
- short_name: the usual short name in English (e.g. "ETH Zurich", "MIPT").
- short_name_local: the same in the local language.
- name_en, name_local: the official full name in English and in the local language.
- language: language of the university's own names ("Russian", "German", "French", "English", "Chinese", ...).
- local_script: "cyrillic" for Russian, otherwise "latin" (names of Chinese, Japanese and Korean
  people are taken in Latin script).
- has_patronymic: true only for Russian universities.
- surname_particles: lowercase particles that belong to given names in citations (e.g. ["van", "von", "de"]); [] if none.
- email_domains: bare domains of the university's addresses, e.g. ["mit.edu"]; subdomains count automatically.
- marker: a regex for SHORT forms that name this university as a word: acronyms and short names.
  Use word boundaries as (?<![^\\W\\d_]) ... (?![^\\W\\d_]). Never include an acronym another
  well-known organisation uses (MSU is also Michigan State; NSU also Nova Southeastern; "mit" is a
  German word); for such universities rely on long_name and a distinctive marker_replacement.
- long_name: a regex for the spelled-out names in every language and spelling seen above
  (e.g. "Massachusetts Institute of Technology", "Eidgenössische Technische Hochschule Zürich",
  "ETH Zürich"), tolerant of extra spaces.
- marker_replacement: the token long_name is replaced with before matching; marker MUST match it.
- place_words: a regex alternation of single lowercase WORDS (no spaces) that appear in the
  university's address parts: city, district, street and building words, state, country in local
  and English forms, postal words. A part made only of these words is skipped.
- other_organisations: a regex for neighbouring organisations that appear next to this university
  in affiliations and own units of their own: other universities' acronyms, affiliated hospitals,
  national labs, academies, companies. The words university/academy/college/hospital are already
  covered.
- unit_words: a regex for words that name THIS university's own units but would otherwise read as
  another organisation (e.g. "college" where units are "College of ...", "hospital" never). "" if none.
- identity: a regex naming the university in a GitHub profile (acronym as a word, full name).
- city: a regex for the city.
- site_url: the university's website.

"catalog": a list of units. Each unit: {{"uid", "name_en", "name_local", "kind", "parent",
"aliases", "context_aliases"}}.
- uid: unique lowercase ASCII slug. parent: the uid of the parent unit, or "{key}" for a top-level
  unit. At most three levels below the university.
- kind: one of school, faculty, college, institute, department, center, lab, program, campus.
- Include every unit that the phrase list shows members using (schools, faculties, colleges,
  departments, institutes, centres, labs, graduate programs) and the official top-level academic
  units even when not seen. Do not include units you are not sure exist at this university.
  Merge spelling variants of one unit into one entry.
- Do not include affiliated hospitals or other organisations: they are not units.
- How matching works, which decides where each name goes:
  * name_en, name_local and every alias match ANYWHERE in a person's affiliation text, also in
    their affiliations with other universities. So `aliases` holds only forms that name this
    university's unit unambiguously: forms containing the university name or acronym ("ETH Zurich
    D-PHYS", "MIT CSAIL"), and distinctive proper names ("Computer Science and Artificial
    Intelligence Laboratory", "Lincoln Laboratory").
  * context_aliases match only in an affiliation part that sits next to the university's name.
    Put every GENERIC name there: "Department of Physics", "School of Medicine", "Physics
    Department", "Dept. of Chemistry", local-language generic names, abbreviations like "EECS".
    A name_en or name_local that is generic MUST also be listed in context_aliases; then it is
    matched only next to the university.
  * Names never contain commas or semicolons: affiliations are cut into parts on them.
  * A name matches when it starts a word, so "Faculty" alone would match inside other text: never
    list one-word generic names.
- Add the spelling variants seen in the phrase list (English and local) to aliases or
  context_aliases following those rules.

Also "notes": one short paragraph on anything a reviewer should check.
"""

REVISE = """Your draft was run through the matcher on the same sample. Report:
{report}

Revise the draft and reply with the complete JSON (same keys: profile, catalog, notes):
- add units and variants for the unmatched member phrases that name a unit of this university;
- fix the marker or long_name if member strings are not recognised as this university;
- move a name that matched external authors to context_aliases, or narrow it;
- drop units that do not exist; keep units that exist even if not hit in this sample.
"""


def fetch_ror(client: HttpClient, ror: str) -> dict:
    record = client.get_json(f"https://api.ror.org/v2/organizations/{ror}")
    names = [n for n in record.get("names") or []]
    location = (record.get("locations") or [{}])[0].get("geonames_details") or {}
    return {
        "name": next((n["value"] for n in names if "ror_display" in n.get("types", [])), names[0]["value"] if names else ror),
        "names": sorted({f"{n['value']} ({n.get('lang') or '-'})" for n in names if "acronym" not in n.get("types", [])}),
        "acronyms": sorted({n["value"] for n in names if "acronym" in n.get("types", [])}),
        "domains": record.get("domains") or [],
        "links": [link.get("value") for link in record.get("links") or []],
        "city": location.get("name") or "",
        "country": location.get("country_name") or "",
        "related": sorted({f"{r.get('label')} [{r.get('type')}]" for r in record.get("relationships") or []})[:60],
        "related_labels": sorted({r.get("label") for r in record.get("relationships") or []
                                  if r.get("type") == "related" and r.get("label")}),
    }


def fetch_works(client: HttpClient, ror: str, date_from: str, date_to: str, limit: int) -> list[dict]:
    params = {"filter": f"authorships.institutions.ror:{ror},from_publication_date:{date_from},to_publication_date:{date_to}",
              "select": "id,authorships", "per_page": 100}
    if settings.openalex_api_key:
        params["api_key"] = settings.openalex_api_key
    count = client.get_json("https://api.openalex.org/works", params={**params, "per_page": 1})["meta"]["count"]
    works: list[dict] = []
    if count <= limit:
        cursor = "*"
        while cursor:
            page = client.get_json("https://api.openalex.org/works", params={**params, "cursor": cursor})
            works += page["results"]
            cursor = page["meta"].get("next_cursor")
    else:
        for page in range(1, (limit + 99) // 100 + 1):
            works += client.get_json("https://api.openalex.org/works",
                                     params={**params, "sample": limit, "seed": 7, "page": page})["results"]
    return works


def authorships(works: list[dict], ror: str) -> list[tuple[str, bool, str]]:
    """(author id, is a member, affiliation text) for every authorship with an affiliation."""
    rows = []
    for work in works:
        for authorship in work.get("authorships") or []:
            author = (authorship.get("author") or {}).get("id")
            text = html.unescape("; ".join(authorship.get("raw_affiliation_strings") or []))
            if author and text:
                member = any(ror in (i.get("ror") or "") for i in authorship.get("institutions") or [])
                rows.append((author, member, text))
    return rows


def member_phrases(rows: list[tuple[str, bool, str]]) -> list[tuple[str, int]]:
    authors_by_phrase: dict[str, set[str]] = defaultdict(set)
    for author, member, text in rows:
        if not member:
            continue
        for segment in re.split(r"[;\n]", text):
            for part in segment.split(","):
                part = " ".join(part.split()).strip(" .-")
                if 3 <= len(part) <= 120 and re.search(r"[^\W\d_]{3}", part):
                    authors_by_phrase[part].add(author)
    counted = [(p, len(a)) for p, a in authors_by_phrase.items() if len(a) >= MIN_AUTHORS]
    return sorted(counted, key=lambda x: -x[1])[:MAX_PHRASES]


def to_institution(key: str, ror: str, profile: dict) -> Institution:
    known = {f.name for f in fields(Institution)}
    data = {k: v for k, v in profile.items() if k in known}
    data.update(key=key, ror_id=ror, staff_catalog=None, mongo_db=f"pauk_{key}")
    data["email_domains"] = tuple(data.get("email_domains") or ())
    data["surname_particles"] = tuple(data.get("surname_particles") or ())
    institution = Institution(**data)
    for name in ("marker", "long_name", "place_words", "other_organisations", "identity", "city"):
        re.compile(getattr(institution, name) or "(?!)")
    if institution.unit_words:
        re.compile(institution.unit_words)
    if not institution.marker_re.search(institution.marker_replacement):
        raise ValueError(f"marker {institution.marker!r} does not match marker_replacement {institution.marker_replacement!r}")
    return institution


def catalog_entries(key: str, ror: str, institution: Institution, catalog: list[dict]) -> list[dict]:
    root = {"uid": key, "name_en": institution.name_en, "name_local": institution.name_local, "kind": "organization",
            "parent": None, "ror_id": f"https://ror.org/{ror}", "type": "university"}
    entries, seen = [root], {key}
    for unit in catalog:
        uid = re.sub(r"[^a-z0-9-]+", "-", (unit.get("uid") or "").casefold()).strip("-")
        if not uid or uid in seen or not unit.get("name_en"):
            continue
        seen.add(uid)
        entries.append({
            "uid": uid, "name_en": unit["name_en"].strip(), "name_local": (unit.get("name_local") or "").strip(),
            "kind": unit.get("kind") or "department", "parent": unit.get("parent") or key,
            "aliases": [a for a in unit.get("aliases") or [] if a and a.strip()],
            "context_aliases": [a for a in unit.get("context_aliases") or [] if a and not re.search("[,;]", a)],
        })
    # A unit whose parent was dropped or never existed hangs off the university.
    for entry in entries[1:]:
        if entry["parent"] not in seen:
            entry["parent"] = key
    return entries


def demote_generic_names(institution: Institution, entries: list[dict], rows: list[tuple[str, bool, str]]) -> int:
    """Make every plain name that other organisations also use a context alias.

    A plain name matches anywhere in a person's affiliations. The model is told
    to keep generic names out of them, yet "School of Materials Science and
    Engineering" slipped through for USTC and matched a dozen Chinese
    universities. So the sample decides: a name found in an affiliation string
    that does not name this university is generic, and from now on matches
    only next to it.
    """
    foreign = [_normalize(text) for _, _, text in rows
               if not (institution.marker_re.search(text) or institution.long_name_re.search(text))]
    blob = "\n".join(foreign)
    demoted = 0
    for entry in entries[1:]:
        context = set(entry.get("context_aliases") or [])
        for name in [entry["name_en"], entry.get("name_local") or "", *(entry.get("aliases") or [])]:
            normalized = _normalize(name)
            if not normalized or name in context or re.search("[,;]", name):
                continue
            if _contains(normalized, blob):
                entry.setdefault("context_aliases", []).append(name)
                context.add(name)
                demoted += 1
        entry["aliases"] = [a for a in entry.get("aliases") or [] if a not in context]
    return demoted


def drop_related_organisations(entries: list[dict], related: list[str], institution: Institution) -> list[str]:
    """Remove units that are separate organisations with a ROR of their own.

    ROR lists them as "related" to the university: affiliated hospitals,
    medical centres, partner colleges (Columbia University Irving Medical
    Center, Barnard College). Their staff are not the university's members,
    so they are not its units either. A dropped unit's children stay,
    attached to the university: a department of medicine is the
    university's even when it sits in the medical centre.
    """
    university = {_normalize(name) for name in (institution.name_en, institution.name_local, institution.short_name) if name}

    def core(name: str) -> str:
        text = _normalize(name)
        for words in university:
            text = text.replace(words, " ")
        return " ".join(text.split())

    labels = {core(label) for label in related if len(core(label).split()) >= 2}
    dropped = []
    for entry in entries[1:]:
        names = {core(n) for n in [entry["name_en"], entry.get("name_local") or "",
                                   *(entry.get("aliases") or []), *(entry.get("context_aliases") or [])] if n}
        if names & labels:
            dropped.append(entry["uid"])
    kept = [entry for entry in entries if entry["uid"] not in dropped]
    for entry in kept[1:]:
        if entry["parent"] in dropped:
            entry["parent"] = entries[0]["uid"]
    entries[:] = kept
    return dropped


def validate(institution: Institution, entries: list[dict], rows: list[tuple[str, bool, str]]) -> dict:
    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / "departments_catalog.json").write_text(json.dumps(entries, ensure_ascii=False), encoding="utf-8")
        departments = StaticStore(Path(tmp)).departments()
    matchers = [(d.id, _match_names(d)) for d in departments]
    ctx = [(d.id, _context_names(d)) for d in departments if d.context_aliases]
    texts: dict[str, list[str]] = defaultdict(list)
    member: dict[str, bool] = {}
    marked_members = members_total = externals_marked = externals_total = 0
    for author, is_member, text in rows:
        texts[author].append(text)
        member[author] = member.get(author, False) or is_member
        hit = bool(institution.marker_re.search(text) or institution.long_name_re.search(text))
        if is_member:
            members_total += 1
            marked_members += hit
        else:
            externals_total += 1
            externals_marked += hit
    results = {author: _match_person(t, matchers, ctx, institution) for author, t in texts.items()}
    members = [a for a in results if member[a]]
    externals = [a for a in results if not member[a]]
    hits = Counter(unit for a in members for unit in results[a])
    unmatched = Counter()
    for author in members:
        if results[author]:
            continue
        for text in texts[author]:
            for segment in re.split(r"[;\n]", text):
                for part in segment.split(","):
                    part = " ".join(part.split()).strip(" .-")
                    if re.search(r"(?i)depart|dept|school|facult|college|institut|lab|cent(?:er|re)|program|division|"
                                 r"кафедр|факультет|институт|лаборатор|école|fakult|lehrstuhl|graduate", part):
                        unmatched[part] += 1
    return {
        "members": len(members),
        "members_matched": sum(1 for a in members if results[a]),
        "externals": len(externals),
        "externals_matched": [(a, results[a], texts[a][0][:200]) for a in externals if results[a]],
        "member_strings_marked": marked_members / max(members_total, 1),
        "external_strings_marked": externals_marked / max(externals_total, 1),
        "units": len(departments),
        "units_hit": hits,
        "units_never_hit": [d.id for d in departments if d.id not in hits],
        "top_unmatched": unmatched.most_common(60),
    }


def report_text(stats: dict) -> str:
    coverage = stats["members_matched"] / max(stats["members"], 1)
    lines = [
        f"- member authors matched to a unit: {stats['members_matched']} of {stats['members']} ({coverage:.1%})",
        f"- external authors matched (false positives unless they once worked here): "
        f"{len(stats['externals_matched'])} of {stats['externals']}",
        f"- member affiliation strings recognised as the university: {stats['member_strings_marked']:.1%}; "
        f"external strings recognised: {stats['external_strings_marked']:.1%}",
        f"- units: {stats['units']}, never hit in the sample: {len(stats['units_never_hit'])}",
        "- most frequent units: " + ", ".join(f"{u} {n}" for u, n in stats["units_hit"].most_common(15)),
        "- external matches (sample): " + "; ".join(f"{u} <- {t}" for _, u, t in stats["externals_matched"][:15]),
        "- unmatched member phrases that look like units (authors): "
        + "; ".join(f"{p} ({n})" for p, n in stats["top_unmatched"]),
        "- units never hit: " + ", ".join(stats["units_never_hit"][:80]),
    ]
    return "\n".join(lines)


def ask(client: OpenRouterClient, prompt: str) -> dict:
    reply = client.chat_json(prompt, system_prompt=SYSTEM_PROMPT, max_tokens=64000)
    if not reply or "profile" not in reply or "catalog" not in reply:
        raise RuntimeError(f"LLM reply unusable: {client.last_error or 'missing profile/catalog'}")
    return reply


def draft_with_model(args: argparse.Namespace, ror: dict, works_count: int, date_from: str,
                     phrases: list[tuple[str, int]], rows: list[tuple[str, bool, str]]) -> tuple:
    """Draft with the model, validate, and let it revise once against the report."""
    llm = OpenRouterClient(LLM_TIMEOUT, settings.openrouter_api_key, args.model, settings.openrouter_proxy_url)
    prompt = TASK.format(
        name=ror["name"], ror=args.ror, country=ror["country"], city=ror["city"], names="; ".join(ror["names"]),
        acronyms=", ".join(ror["acronyms"]) or "-", domains=", ".join(ror["domains"]) or "-",
        related="; ".join(ror["related"]) or "-", works=works_count, period=f"{date_from} to {args.to}",
        phrases="\n".join(f"{n}\t{p}" for p, n in phrases), key=args.key,
    )
    draft = ask(llm, prompt)
    institution = to_institution(args.key, args.ror, draft["profile"])
    stats = validate(institution, catalog_entries(args.key, args.ror, institution, draft["catalog"]), rows)
    first = report_text(stats)
    logger.info("%s first draft:\n%s", args.key, first)

    revised = ask(llm, f"{prompt}\n\nYour draft:\n{json.dumps(draft, ensure_ascii=False)}\n\n" + REVISE.format(report=first))
    try:
        institution = to_institution(args.key, args.ror, revised["profile"])
        entries = catalog_entries(args.key, args.ror, institution, revised["catalog"])
        draft = revised
    except (ValueError, re.error) as exc:
        logger.warning("%s: revision rejected (%s), keeping the first draft", args.key, exc)
        institution = to_institution(args.key, args.ror, draft["profile"])
        entries = catalog_entries(args.key, args.ror, institution, draft["catalog"])
    return draft, institution, entries, first


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("key", help="profile key, lowercase ASCII, e.g. mit")
    parser.add_argument("ror", help="bare ROR id, e.g. 042nb2s44")
    parser.add_argument("--works", type=int, default=3000, help="sample size (default 3000)")
    parser.add_argument("--to", default=date.today().isoformat())
    parser.add_argument("--from", dest="date_from", default=None, help="default: one year before --to")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--refine", action="store_true",
                        help="no LLM: re-validate the existing draft and demote generic plain names")
    parser.add_argument("--brief", action="store_true",
                        help="no LLM: print the ROR data and member phrases a person drafts from")
    parser.add_argument("--draft", type=Path,
                        help="no LLM: a hand-written draft in the model's reply format ({profile, catalog, notes})")
    parser.add_argument("--phrases", type=int, default=150, help="phrases --brief prints (default 150)")
    parser.add_argument("--cache", type=Path, help="directory keeping the ROR record and the OpenAlex sample")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    date_from = args.date_from or (date.fromisoformat(args.to) - timedelta(days=365)).isoformat()

    cached = args.cache / f"{args.key}.json" if args.cache else None
    if cached and cached.exists():
        sample = json.loads(cached.read_text(encoding="utf-8"))
        ror, works_count, rows = sample["ror"], sample["works"], [tuple(row) for row in sample["rows"]]
    else:
        http = HttpClient(60, {"User-Agent": "PAUK/2.0 (mailto:pauk@example.org)"})
        ror = fetch_ror(http, args.ror)
        works = fetch_works(http, args.ror, date_from, args.to, args.works)
        works_count, rows = len(works), authorships(works, args.ror)
        if cached:
            cached.parent.mkdir(parents=True, exist_ok=True)
            cached.write_text(json.dumps({"ror": ror, "works": works_count, "rows": rows}, ensure_ascii=False),
                              encoding="utf-8")
    phrases = member_phrases(rows)
    logger.info("%s: %d works, %d authorships, %d member phrases", args.key, works_count, len(rows), len(phrases))

    if args.brief:
        print(f"# {ror['name']} ({args.key}, ROR {args.ror}), {ror['city']}, {ror['country']}")
        print(f"names: {'; '.join(ror['names'])}\nacronyms: {', '.join(ror['acronyms']) or '-'}")
        print(f"domains: {', '.join(ror['domains']) or '-'}\nlinks: {', '.join(ror['links']) or '-'}")
        print(f"related: {'; '.join(ror['related']) or '-'}")
        print(f"member phrases (authors) from {works_count} works:")
        print("\n".join(f"{n}\t{p}" for p, n in phrases[:args.phrases]))
        return 0

    out = STATIC / args.key
    if args.refine:
        institution = load(out / "profile.json")
        entries = json.loads((out / "departments_catalog.json").read_text(encoding="utf-8"))
        before = report_text(validate(institution, entries, rows))
        dropped = drop_related_organisations(entries, ror["related_labels"], institution)
        if dropped:
            logger.info("%s: dropped units that are related organisations with their own ROR: %s", args.key, dropped)
        demoted = demote_generic_names(institution, entries, rows)
        after = report_text(validate(institution, entries, rows))
        (out / "departments_catalog.json").write_text(json.dumps(entries, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        report = REPORTS / f"{args.key}.md"
        report.write_text(report.read_text(encoding="utf-8") + f"\n## Refined ({demoted} plain names made context aliases, "
                          f"related organisations dropped: {', '.join(dropped) or 'none'})\n\n"
                          f"{after}\n\nBefore refining:\n\n{before}\n", encoding="utf-8")
        logger.info("%s refined, %d names demoted:\n%s", args.key, demoted, after)
        return 0

    if args.draft:
        draft = json.loads(args.draft.read_text(encoding="utf-8"))
        institution = to_institution(args.key, args.ror, draft["profile"])
        entries = catalog_entries(args.key, args.ror, institution, draft["catalog"])
        first = report_text(validate(institution, entries, rows))
        logger.info("%s hand-written draft:\n%s", args.key, first)
        author = f"hand from {args.draft.name}"
    else:
        draft, institution, entries, first = draft_with_model(args, ror, works_count, date_from, phrases, rows)
        author = args.model
    dropped = drop_related_organisations(entries, ror["related_labels"], institution)
    demoted = demote_generic_names(institution, entries, rows)
    stats = validate(institution, entries, rows)
    final = (report_text(stats) + f"\n- plain names made context aliases after the model: {demoted}"
             f"\n- units dropped as related organisations with their own ROR: {', '.join(dropped) or 'none'}")

    out.mkdir(parents=True, exist_ok=True)
    profile = {f.name: getattr(institution, f.name) for f in fields(Institution)}
    profile["email_domains"] = list(profile["email_domains"])
    profile["surname_particles"] = list(profile["surname_particles"])
    (out / "profile.json").write_text(json.dumps(profile, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (out / "departments_catalog.json").write_text(json.dumps(entries, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    REPORTS.mkdir(parents=True, exist_ok=True)
    (REPORTS / f"{args.key}.md").write_text(
        f"# {ror['name']} ({args.key}, ROR {args.ror})\n\n"
        f"Draft by {author} from {works_count} works of {date_from} to {args.to}; review before use.\n\n"
        f"## After revision\n\n{final}\n\n## First draft\n\n{first}\n\n## Notes from the model\n\n{draft.get('notes') or '-'}\n",
        encoding="utf-8")
    logger.info("%s final:\n%s", args.key, final)
    return 0


if __name__ == "__main__":
    sys.exit(main())

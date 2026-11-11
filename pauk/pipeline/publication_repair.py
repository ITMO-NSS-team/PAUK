"""Rebuild the prepared rows of a publication split before applying any changes."""

from __future__ import annotations

import hashlib
import re
from collections import defaultdict
from dataclasses import replace
from html import unescape
from urllib.parse import urlsplit

from bson.json_util import dumps

from pauk.models import GitHubProfile, Person, Publication, RepoLink, Repository
from pauk.models.processing import ProcessingStatus
from pauk.models.publication import VersionAuthor
from pauk.pipeline.normalize import OpenAlexNormalizer, _authorship_person_id, _fallback_person_id
from pauk.pipeline.stages.base import PreparedSelection
from pauk.pipeline.stages.code_links import CodeLinksStage
from pauk.pipeline.stages.dedup import (
    DedupStage,
    _merge_publication,
    _norm_doi,
    _stored_code_urls,
    _version_of,
)
from pauk.pipeline.stages.departments import _context_names, _match_names, _match_person
from pauk.pipeline.stages.link_relevance import LinkRelevanceStage
from pauk.pipeline.stages.persons import PersonsStage
from pauk.pipeline.stages.repositories import RepositoriesStage, _url_repo_id
from pauk.storage import PreparedStore, RawStore
from pauk.storage.pdf import PdfStore
from pauk.storage.static import StaticStore
from pauk.urls import normalize_repo_url

STAGING_GROUP = "publication-split"


class CannotRebuild(ValueError):
    """The available data cannot safely replace the merged state."""


def _content(doc):
    return {key: value for key, value in (doc or {}).items() if key not in ("_id", "_version", "groups")}


def _fingerprint(doc):
    return hashlib.sha256(dumps(doc, sort_keys=True).encode()).hexdigest()


def _dump(model):
    return model.model_dump(mode="json", by_alias=True)


def _save_change(scratch, entity, row, before, groups):
    groups = sorted(set(groups) | set((before or {}).get("groups") or []))
    if not groups:
        raise CannotRebuild(f"{entity}: no source group for {row.get('id')}")
    # PreparedStore keeps fields outside the model, such as metadata from an import.
    row = {**_content(before), **row}
    key = row[PreparedStore.KEY_FIELDS.get(entity, "id")]
    scratch.changes.replace_one({"_id": f"{entity}:{key}"}, {
        "entity": entity, "key": key, "row": row, "groups": groups, "before": _fingerprint(before),
    }, upsert=True)


def apply_prepared_changes(db, scratch):
    pending = []
    for change in scratch.changes.find().sort("_id", 1):
        entity, key, row, groups = (change[field] for field in ("entity", "key", "row", "groups"))
        before = db[PreparedStore.COLLECTIONS[entity]].find_one({"_id": key})
        after = {name: value for name, value in row.items() if value is not None}
        if _content(before) == after and set(groups) <= set(before.get("groups") or []):
            continue
        if _fingerprint(before) != change["before"] and _content(before) != after:
            raise CannotRebuild(f"{entity}:{key} changed since the repair was prepared")
        pending.append(change)
    for change in pending:
        for group in change["groups"]:
            PreparedStore(db, group).upsert_rows(change["entity"], [change["row"]])


def _work_sources(db, ids):
    keys = sorted(set(ids) | {f"https://openalex.org/{work_id}" for work_id in ids})
    sources = {}
    groups = defaultdict(set)
    query = {"source": "openalex_works", "payload.id": {"$in": keys}}
    for row in db.raw.find(query).sort([("fetched_at", 1), ("_id", 1)]):
        work_id = row["payload"]["id"].rsplit("/", 1)[-1]
        sources[work_id] = row["payload"]
        groups[work_id].add(row["group"])
    if missing := set(ids) - sources.keys():
        raise CannotRebuild("missing raw OpenAlex source for " + ", ".join(sorted(missing)))
    return sources, groups


def _source_pdfs(works):
    """Reject shared URLs whose record of origin cannot be established."""
    by_url = defaultdict(set)
    owners = defaultdict(set)
    locations = {work_id: [work.get("best_oa_location"), work.get("primary_location"),
                           *(work.get("locations") or [])] for work_id, work in works.items()}
    for work_id, work in works.items():
        primary = (work.get("primary_location") or {}).get("pdf_url")
        for location in locations[work_id]:
            location = location or {}
            if url := location.get("pdf_url"):
                by_url[url].add(work_id)
                if url == primary:
                    owners[url].add(work_id)
    dois = {work_id: _norm_doi(work.get("doi")) for work_id, work in works.items()}
    for work_id, work in works.items():
        original = locations[work_id]
        verified = []
        for location in original:
            if not location or not (url := location.get("pdf_url")):
                continue
            if work.get("type") == "article" and location.get("version") == "submittedVersion":
                continue
            same_doi = dois[work_id] and all(dois[other] == dois[work_id] for other in by_url[url])
            if len(by_url[url]) == 1 or owners[url] == {work_id} or same_doi:
                verified.append(location)
        yield work_id, {**work, "best_oa_location": None, "locations": verified}, any(
            location and location.get("pdf_url") for location in original
        ) and not verified


def _old_people(db, sources, ids):
    author_ids = set()
    for work in sources.values():
        for authorship in work.get("authorships") or []:
            author = authorship.get("author") or {}
            person_id = _authorship_person_id(author) or _fallback_person_id(author)
            if person_id:
                author_ids.add(person_id)
    query = {"$or": [
        {"_id": {"$in": sorted(author_ids)}}, {"merged_ids": {"$in": sorted(author_ids)}},
        {"authored.publication_id": {"$in": sorted(ids)}},
    ]}
    return {row["_id"]: row for row in db.persons.find(query)}


def _department_rows(config, publications, people):
    static = StaticStore(config.static_dir)
    departments = {row.id: row for row in static.departments()}
    organizations = {row.id: row for row in static.organizations()}
    matchers = [(key, _match_names(row)) for key, row in departments.items()]
    context = [(key, _context_names(row)) for key, row in departments.items()]
    selected = set()
    for person in people:
        if person.is_itmo:
            for authored in person.authored:
                ids = _match_person([unescape(authored.affiliation or "")], matchers, context)
                publication = publications[authored.publication_id]
                publication.department_ids = list(dict.fromkeys([*publication.department_ids, *ids]))
                selected.update(ids)
    for department_id in list(selected):
        while parent := departments[department_id].parent_id:
            if parent in selected:
                break
            selected.add(parent)
            department_id = parent
    return {
        "departments": [departments[key] for key in sorted(selected)],
        "organizations": [organizations[key] for key in sorted(
            {departments[key].organization_id for key in selected} & organizations.keys()
        )],
    }


def _fold_sources(prepared, raw, config, publications, people, aliases):
    authors = defaultdict(list)
    for person in people:
        for authored in person.authored:
            authors[authored.publication_id].append(VersionAuthor(
                person_id=person.id, name=person.name_raw, position=authored.position,
            ))
    for alias, survivor in aliases.items():
        main, extra = publications[survivor], publications[alias]
        if not main.versions:
            main.versions = [_version_of(main, authors[survivor])]
        _merge_publication(main, extra, authors[alias])
        main.merged_ids.append(alias)
        main.processing.pop("link_relevance", None)
        main.full_text = "\n\n".join(dict.fromkeys(filter(None, [main.full_text, extra.full_text]))) or None
    prepared.write_models("publications", [
        pub for pub in publications.values() if pub.id not in aliases
    ])
    if aliases:
        DedupStage(prepared, raw, config)._repoint_publication_references(aliases)


def _repo_keys(doc):
    return {normalize_repo_url(url) for url in [doc.get("url"), *(doc.get("cited_urls") or [])] if url}


def _repo_sources(db, old_urls, links):
    urls = set().union(*old_urls.values()) | {
        normalize_repo_url(link.url) for row in links for link in row.links
    }
    ids = sorted({repo_id for url in urls if (repo_id := _url_repo_id(url))})
    patterns = [
        re.compile(r"^https?://(?:www\.)?" + re.escape(urlsplit(url).netloc + urlsplit(url).path)
                   + r"(?:\.git)?/*$", re.IGNORECASE)
        for url in sorted(urls) if _url_repo_id(url)
    ]
    query = {"$or": [
        {"publication_ids": {"$in": sorted(old_urls)}}, {"_id": {"$in": ids}},
        {"url": {"$in": patterns}}, {"cited_urls": {"$in": patterns}},
    ]}
    return {row["_id"]: row for row in db.repositories.find(query)}


def _stage_repositories(db, scratch, prepared, raw, config, old_urls, groups, aliases):
    links = list(prepared.read_models("repo_links", RepoLink))
    existing = _repo_sources(db, old_urls, links)
    by_url = {key: row for row in existing.values() for key in _repo_keys(row)}
    seeded = {}
    for row in links:
        for link in row.links:
            key = normalize_repo_url(link.url)
            doc = by_url.get(key)
            repo_id = _url_repo_id(key)
            if doc and repo_id:
                seeded[repo_id] = Repository.model_validate({
                    **doc, "id": repo_id, "publication_ids": [],
                })
    prepared.upsert_models("repositories", seeded.values())
    RepositoriesStage(prepared, raw, config).run()
    rebuilt = list(prepared.read_models("repositories", Repository))
    for repo in rebuilt:
        if (state := repo.processing.get("repositories")) and state.status == ProcessingStatus.FAILED:
            raise CannotRebuild(f"{repo.id}: repository enrichment failed: {state.error}")
        canonical = db.repositories.find_one({"_id": repo.id})
        if canonical:
            existing[repo.id] = canonical
    desired = {
        repo_id: {**_content(doc), "id": repo_id, "publication_ids": sorted({
            aliases.get(work_id, work_id) for work_id in doc.get("publication_ids") or []
            if not (_repo_keys(doc) & old_urls.get(work_id, set()))
        })}
        for repo_id, doc in existing.items()
    }
    for repo in rebuilt:
        state = repo.processing.get("repositories")
        old = existing.get(repo.id) or by_url.get(normalize_repo_url(repo.url))
        repo_id = old["_id"] if old else repo.id
        target = desired.setdefault(repo_id, {**_dump(repo), "id": repo_id})
        old_state = (target.get("_processing") or {}).get("repositories") or {}
        if old and state and old_state.get("status") != ProcessingStatus.COMPLETED:
            enriched = _dump(repo)
            for field in ("name", "url", "github_id", "owner_login", "description", "stars_num", "topics",
                          "language", "forks_num", "archived", "is_fork", "last_updated", "license",
                          "has_readme", "access_date"):
                target[field] = enriched[field]
            target["_processing"] = {**(target.get("_processing") or {}),
                                     "repositories": enriched["_processing"]["repositories"]}
        target["publication_ids"] = sorted(set(target["publication_ids"]) | set(repo.publication_ids))
        target["cited_urls"] = list(dict.fromkeys([*(target.get("cited_urls") or []), *repo.cited_urls]))
    all_groups = set().union(*groups.values())
    mentioned_by = defaultdict(set)
    for link_row in links:
        for link in link_row.links:
            mentioned_by[normalize_repo_url(link.url)].add(link_row.publication_id)
    for repo_id, row in desired.items():
        before = existing.get(repo_id)
        work_ids = set(row["publication_ids"])
        work_ids.update(set().union(*(mentioned_by[key] for key in _repo_keys(row))))
        repo_groups = set().union(*(groups.get(work_id, set()) for work_id in work_ids))
        _save_change(scratch, "repositories", row, before, repo_groups or (
            set(before.get("groups") or []) if before else all_groups
        ))
    for profile in prepared.read_models("github_profiles", GitHubProfile):
        if not db.github_profiles.find_one({"_id": profile.id}):
            _save_change(scratch, "github_profiles", _dump(profile), None, all_groups)


def _old_repository_evidence(db, ids, publications, links):
    evidence = defaultdict(set)

    def add(work_id, pub=None, repo_links=None):
        pub = pub or {}
        urls = _stored_code_urls(pub.get("code_url"))
        urls.extend(link["repository_url"] for link in pub.get("mentions_links") or [] if link.get("repository_url"))
        urls.extend(link["url"] for link in (repo_links or {}).get("links") or [] if link.get("url"))
        evidence[work_id].update(normalize_repo_url(url) for url in urls)

    for work_id in ids:
        add(work_id, publications.get(work_id), links.get(work_id))
    # A previous enrichment may already have replaced RepoLink while the
    # vanished URL still owns a membership in Repository.publication_ids.
    for revision in db.revisions.find({
        "entity_type": {"$in": ["publications", "repo_links"]},
        "entity_id": {"$in": sorted(ids)},
    }, {"entity_id": 1, "entity_type": 1, "snapshot": 1}):
        work_id, snapshot = revision["entity_id"], revision["snapshot"]
        if revision["entity_type"] == "publications":
            add(work_id, pub=snapshot)
        else:
            add(work_id, repo_links=snapshot)
    return evidence


def _check_relevance(pub):
    state = pub.processing["link_relevance"]
    if state.status == ProcessingStatus.FAILED:
        raise CannotRebuild(f"{pub.id}: code classification failed: {state.error}")


def stage_repair(db, scratch, config, staging_dir, targets, aliases, source_data):
    """Write a durable, scoped change set in an isolated database."""
    ids = set(targets) | set(aliases)
    sources, groups = source_data
    prepared = PreparedStore(scratch, STAGING_GROUP)
    raw = RawStore(scratch, STAGING_GROUP)
    old_pubs = {row["_id"]: row for row in db.publications.find({"_id": {"$in": sorted(ids)}})}
    for work_id, doc in old_pubs.items():
        groups[work_id].update(doc.get("groups") or [])
    for alias, survivor in aliases.items():
        groups[survivor].update(groups[alias])
    old_people = _old_people(db, sources, ids)
    for person in old_people.values():
        seed = {**_content(person), "id": person["_id"], "authored": []}
        prepared.upsert_rows("persons", [seed])
    live_pdfs = PdfStore(db, config.pdf_dir)
    needs_pdf = {work_id for work_id in ids if live_pdfs.exists(work_id)}
    for work_id, payload, missing_pdf in _source_pdfs(sources):
        raw.append("openalex_works", payload, {})
        if missing_pdf:
            needs_pdf.add(work_id)
    OpenAlexNormalizer(raw, prepared).run()
    # Older cache entries identify a work ID even when its PDF came from an
    # inherited URL. Only this repair's downloads can establish provenance.
    CodeLinksStage(prepared, raw, replace(config, data_dir=staging_dir)).run()
    publications = {pub.id: pub for pub in prepared.read_models("publications", Publication)}
    for pub in publications.values():
        state = pub.processing["code_links"]
        if state.status == ProcessingStatus.FAILED:
            raise CannotRebuild(f"{pub.id}: code extraction failed: {state.error}")
        if pub.id in needs_pdf and not pub.full_text:
            raise CannotRebuild(f"{pub.id}: unable to attribute a PDF to this version")
    people = list(prepared.read_models("persons", Person))
    persons_stage = PersonsStage(prepared, raw, config)
    for person in people:
        persons_stage._fill_missing_affiliations(person, publications)
    prepared.write_models("persons", people)
    department_rows = _department_rows(config, publications, people)
    old_links = {row["_id"]: row for row in db.repo_links.find({"_id": {"$in": sorted(ids)}})}
    live_aliases = frozenset(aliases.keys() & (old_pubs.keys() | old_links.keys()))
    if live_aliases:
        selection = PreparedSelection("publications", live_aliases)
        LinkRelevanceStage(prepared, raw, config, selection=selection).run()
        for pub in prepared.get_models("publications", live_aliases, Publication):
            _check_relevance(pub)
            pub.department_ids = publications[pub.id].department_ids
            publications[pub.id] = pub
            if pub.id in old_pubs:
                _save_change(scratch, "publications", _dump(pub), old_pubs[pub.id], groups[pub.id])
        for row in prepared.get_models("repo_links", live_aliases, RepoLink):
            _save_change(scratch, "repo_links", _dump(row), old_links.get(row.publication_id),
                         groups[row.publication_id])
    _fold_sources(prepared, raw, config, publications, people, aliases)
    LinkRelevanceStage(prepared, raw, config).run()
    for pub in prepared.read_models("publications", Publication):
        _check_relevance(pub)
        _save_change(scratch, "publications", _dump(pub), old_pubs.get(pub.id), groups[pub.id])
    old_urls = _old_repository_evidence(db, ids, old_pubs, old_links)
    _stage_repositories(db, scratch, prepared, raw, config, old_urls, groups, aliases)
    for row in prepared.read_models("repo_links", RepoLink):
        work_id = row.publication_id
        _save_change(scratch, "repo_links", _dump(row), old_links.get(work_id), groups[work_id])
    for person in prepared.read_models("persons", Person):
        before = old_people.get(person.id)
        outside = [row for row in (before or {}).get("authored") or []
                   if row["publication_id"] not in ids]
        new = [_dump(row) for row in person.authored]
        if not before and not new:
            continue
        person_groups = set().union(*(groups[row["publication_id"]] for row in new))
        _save_change(scratch, "persons", {**_dump(person), "authored": outside + new}, before, person_groups)
    for entity, rows in department_rows.items():
        for row in rows:
            before = db[entity].find_one({"_id": row.id})
            _save_change(scratch, entity, _content(before) if before else _dump(row), before,
                         set().union(*groups.values()))


def repair_pdf_writes(db, scratch, config, staging_dir, work_ids):
    staged = PdfStore(scratch, replace(config, data_dir=staging_dir).pdf_dir)
    live = PdfStore(db, config.pdf_dir)
    cached_ids = set(scratch.pdfs.distinct("_id"))
    for work_id in work_ids:
        if work_id in cached_ids and not staged.exists(work_id):
            raise CannotRebuild(f"{work_id}: staged PDF is missing from {staging_dir}")
        if work_id not in cached_ids and live.exists(work_id):
            raise CannotRebuild(f"{work_id}: a PDF entered the live cache after staging")
    writes = {}
    for work_id in sorted(cached_ids):
        data = staged.read(work_id)
        if not live.exists(work_id) or live.read(work_id) != data or not db.pdfs.find_one(work_id):
            writes[work_id] = data
    return writes

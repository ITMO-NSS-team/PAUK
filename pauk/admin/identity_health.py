from __future__ import annotations

from collections import Counter

from pauk.models.identity import normalize_external_value


def _check(identifier: str, title: str, n: int, hint: str) -> dict:
    return {
        "id": identifier,
        "group": "Идентичность и источники",
        "group_en": "Identity and sources",
        "title": title,
        "title_en": title,
        "n": n,
        "of": None,
        "pct": None,
        "status": "fail" if n else "ok",
        "hint": hint,
        "hint_en": hint,
        "has_examples": False,
    }


def collect(db) -> list[dict]:
    publications = list(db.publications.find({}, {
        "id": 1, "doi": 1, "identifiers": 1, "source_records": 1, "source_keys": 1,
    }))
    publication_ids = {row.get("id") for row in publications}
    people = list(db.persons.find({}, {"id": 1, "identifiers": 1}))
    people_ids = {row.get("id") for row in people}

    bad_identity_keys = 0
    for row in db.identity_keys.find({}, {"entity": 1, "entity_id": 1, "scheme": 1}):
        entity = row.get("entity")
        known = publication_ids if entity == "publication" else people_ids if entity == "person" else set()
        expected_prefix = f"{entity}:{row.get('scheme')}:"
        bad_identity_keys += (
            row.get("entity_id") not in known
            or not str(row.get("_id") or "").startswith(expected_prefix)
        )
    for entity, rows in (("publication", publications), ("person", people)):
        for row in rows:
            for identifier in row.get("identifiers") or []:
                normalized_key = identifier.get("normalized_key")
                if not normalized_key:
                    bad_identity_keys += 1
                    continue
                identity = db.identity_keys.find_one({"_id": f"{entity}:{normalized_key}"})
                bad_identity_keys += not identity or identity.get("entity_id") != row.get("id")

    dois: Counter[str] = Counter()
    for row in publications:
        row_dois = {
            normalize_external_value("doi", value)
            for value in [
                row.get("doi"),
                *(record.get("doi") for record in row.get("source_records") or []),
            ]
            if value
        }
        for identifier in row.get("identifiers") or []:
            if identifier.get("scheme") == "doi" and identifier.get("value"):
                row_dois.add(normalize_external_value("doi", identifier["value"]))
        dois.update(row_dois)
    duplicate_dois = sum(count - 1 for count in dois.values() if count > 1)

    dangling = 0
    for person in db.persons.find({}, {"authored.publication_id": 1}):
        dangling += sum(
            authorship.get("publication_id") not in publication_ids
            for authorship in person.get("authored") or []
        )
    dangling += db.repo_links.count_documents({"publication_id": {"$nin": list(publication_ids)}})
    for repository in db.repositories.find({}, {"publication_ids": 1}):
        dangling += sum(value not in publication_ids for value in repository.get("publication_ids") or [])

    orphan_records = 0
    dangling_source_authors = 0
    for publication in publications:
        for record in publication.get("source_records") or []:
            identity = db.identity_keys.find_one({"_id": f"publication:{record.get('key')}"})
            orphan_records += not identity or identity.get("entity_id") != publication.get("id")
            dangling_source_authors += sum(
                bool(author.get("person_id")) and author.get("person_id") not in people_ids
                for author in record.get("authors") or []
            )

    return [
        _check("identity_key_owner", "Identity keys with a missing owner", bad_identity_keys,
               "Every identity key must route to one existing entity."),
        _check("duplicate_normalized_doi", "Duplicate normalized publication DOI", duplicate_dois,
               "A normalized DOI must have one publication owner."),
        _check("dangling_publication_reference", "Dangling publication references", dangling,
               "Authorships and repository links must address an existing publication."),
        _check("source_record_owner", "Source records without an identity owner", orphan_records,
               "Every source record key must route back to its publication."),
        _check("dangling_source_author", "Source authors without a person owner",
               dangling_source_authors,
               "Every resolved source author must address an existing person."),
    ]

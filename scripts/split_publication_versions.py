"""Take apart publications folded on a shared title under two different DOIs.

Dedup used to fold any two records with one title into one Publication,
keeping the folded record only as an entry of `versions`. It now folds on a
DOI, and on a title only where one side has no DOI
(`pauk.pipeline.stages.dedup.publication_buckets`). A preprint and its
article, or an original and its translation, are two nodes related by
IS_VERSION (`pauk versions link`). This repairs what the old rule folded.

A folded record is taken apart when its ledger entry carries a DOI other
than the survivor's and the version rules (`classify_pair`) give the pair a
bucket. A record the rules drop (a generic or a too-short title) stays
folded: nothing says it is a work of its own. So does one whose raw payload
is gone: its ledger entry would be the only copy left, and nothing could
rebuild the record from it.

The ledger lacks what a node needs (type, fields, funding, departments), so
nothing is built from it. Taking an id out of `merged_ids` is what lets the
ordinary pipeline rebuild the record from its raw OpenAlex payload:

- Mongo: the survivor's `merged_ids` and `versions` lose the entry, and the
  authors only the folded record listed lose their authorship of the
  survivor. The next `pauk normalize` of the record's group recreates it
  with its own authorships, and `pauk enrich` runs every stage on it. The
  survivor's group is normalized again too, which restores an authorship
  of the survivor that the ledger did not list.
- Neo4j: the survivor node's `merged_ids` and `versions` are rewritten too,
  or the next publish would read the old map and fold the record again.
  Edges the survivor inherited from it are left to `pauk admin prune`,
  which removes whatever no prepared row asks for any more.

Holds the graph lock. Dry run by default; `--apply` writes. Prints the
commands to run afterwards, in order.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from collections import defaultdict
from pathlib import Path

from pauk.graph.audit import actor_context, audited_client
from pauk.graph.client import chunked
from pauk.graph.version_candidates import classify_pair
from pauk.jobs.locks import Busy, held
from pauk.jobs.models import GRAPH
from pauk.pipeline.stages.dedup import _norm_doi
from pauk.settings import settings
from pauk.storage import PreparedStore
from pauk.storage.mongo import get_mongo_client

logger = logging.getLogger(__name__)

OPENALEX = "https://openalex.org/"


def raw_work(db, work_id: str) -> tuple[dict, str] | None:
    """The newest raw OpenAlex payload of one work, and its group."""
    envelope = db.raw.find_one(
        {"source": "openalex_works", "payload.id": {"$in": [OPENALEX + work_id, work_id]}},
        sort=[("fetched_at", -1)])
    return (envelope["payload"], envelope["group"]) if envelope else None


def _row(work_id: str, entry: dict, payload: dict | None, fallback: dict | None = None) -> dict:
    """A record shaped for `classify_pair`, from its ledger entry and raw payload."""
    payload = payload or {}
    fallback = fallback or {}
    source = (payload.get("primary_location") or {}).get("source") or {}
    return {
        "id": work_id,
        "title": entry.get("title"),
        "doi": entry.get("doi"),
        "journal": entry.get("journal"),
        "venue_type": source.get("type") or fallback.get("venue_type"),
        "type": payload.get("type") or fallback.get("type"),
        "publication_date": entry.get("publication_date"),
        "year": entry.get("year"),
        "abstract": entry.get("abstract"),
        "authors": [{"person_id": author["person_id"]} for author in entry.get("authors") or []
                    if author and author.get("person_id")],
    }


def plan_splits(canonical: dict, versions: list[dict], merged_ids: list[str],
                raw_of) -> list[dict]:
    """The folded records of one survivor that are works of their own.

    Args:
        canonical: The survivor: id, doi, type, venue_type, title, ...
        versions: Its ledger, one dict per record, its own included.
        merged_ids: The ids folded into it.
        raw_of: work id -> (payload, group) or None.

    Returns:
        One dict per record to take apart: id, bucket, its lone authors
        (on it and not on the survivor) and the group its raw payload is in.
        Without a ledger entry of the survivor's own its authors are not
        known apart from the folded record's, so no authorship is taken
        away (`lone_authors` empty, `survivor_authors_known` False): a
        leftover authorship is less harm than a real one removed.
    """
    by_id = {entry.get("openalex_id"): entry for entry in versions}
    own = by_id.get(canonical["id"])
    authors_known = own is not None
    if own is None:
        # A graph row's authors are the union of both records: good enough
        # for the rules to see shared authors, not to tell who is lone.
        own = {key: canonical.get(key) for key in
               ("title", "doi", "journal", "publication_date", "year", "abstract", "authors")}
    survivor = _row(canonical["id"], own, None, canonical)
    survivor_doi = _norm_doi(canonical.get("doi") or own.get("doi"))
    splits = []
    for work_id in merged_ids:
        entry = by_id.get(work_id)
        if entry is None:
            continue
        doi = _norm_doi(entry.get("doi"))
        if not doi or doi == survivor_doi:
            continue
        raw = raw_of(work_id)
        if raw is None:
            logger.warning("split: %s (folded into %s) has no raw payload, left folded",
                           work_id, canonical["id"])
            continue
        result = classify_pair(survivor, _row(work_id, entry, raw[0]))
        if result is None:
            continue
        own_authors = {author["person_id"] for author in survivor["authors"]}
        survivor_raw = raw_of(canonical["id"])
        splits.append({
            "survivor": canonical["id"],
            "id": work_id,
            "bucket": result[0],
            "lone_authors": sorted({author["person_id"] for author in entry.get("authors") or []}
                                   - own_authors) if authors_known else [],
            "survivor_authors_known": authors_known,
            # Both are normalized again: the record is rebuilt in its own
            # group, and the survivor's gives back any authorship of it the
            # ledger did not know about and the repair took away.
            "groups": sorted({found[1] for found in (raw, survivor_raw) if found}),
        })
    return splits


def mongo_splits(db, raw_of) -> list[dict]:
    collection = db[PreparedStore.COLLECTIONS["publications"]]
    splits = []
    for doc in collection.find({"merged_ids.0": {"$exists": True}}):
        canonical = {"id": doc["_id"], **doc}
        splits += plan_splits(canonical, doc.get("versions") or [], doc.get("merged_ids") or [], raw_of)
    return splits


def graph_splits(client, raw_of) -> list[dict]:
    splits = []
    for row in client.fetch_publications_for_dedup():
        if not row.get("merged_ids"):
            continue
        versions = row.get("versions") or []
        if isinstance(versions, str):
            versions = json.loads(versions)
        splits += plan_splits(row, versions, row["merged_ids"], raw_of)
    return splits


def apply_mongo(db, splits: list[dict]) -> None:
    publications = db[PreparedStore.COLLECTIONS["publications"]]
    persons = db[PreparedStore.COLLECTIONS["persons"]]
    for split in splits:
        publications.update_one({"_id": split["survivor"]}, {
            "$pull": {"merged_ids": split["id"], "versions": {"openalex_id": split["id"]}},
            "$inc": {"_version": 1}})
        for person_id in split["lone_authors"]:
            persons.update_one({"_id": person_id},
                               {"$pull": {"authored": {"publication_id": split["survivor"]}},
                                "$inc": {"_version": 1}})


def apply_graph(client, splits: list[dict]) -> None:
    taken: dict[str, set[str]] = defaultdict(set)
    for split in splits:
        taken[split["survivor"]].add(split["id"])
    rows = {row["id"]: row for row in client.fetch_publications_for_dedup() if row["id"] in taken}
    updates = []
    for survivor, ids in taken.items():
        row = rows[survivor]
        versions = row.get("versions") or []
        if isinstance(versions, str):
            versions = json.loads(versions)
        updates.append((survivor, {
            "merged_ids": [merged for merged in row.get("merged_ids") or [] if merged not in ids],
            "versions": json.dumps([entry for entry in versions if entry.get("openalex_id") not in ids],
                                   ensure_ascii=False),
        }))
    for chunk in chunked(updates):
        client.upsert_nodes_batch("Publication", chunk)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--apply", action="store_true", help="write the splits (default: dry run)")
    parser.add_argument("--report", type=Path, default=Path("data/reports/split-publication-versions.json"))
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    mongo = get_mongo_client(settings)
    db = mongo[settings.mongo_db]
    client = audited_client(settings, db)
    cache: dict[str, tuple[dict, str] | None] = {}

    def raw_of(work_id: str):
        if work_id not in cache:
            cache[work_id] = raw_work(db, work_id)
        return cache[work_id]

    try:
        with held(db, GRAPH):
            in_mongo = mongo_splits(db, raw_of)
            in_graph = graph_splits(client, raw_of)
            by_bucket: dict[str, int] = defaultdict(int)
            for split in {(s["survivor"], s["id"]): s for s in [*in_mongo, *in_graph]}.values():
                by_bucket[split["bucket"]] += 1
            print(f"prepared rows to take apart: {len(in_mongo)}")
            print(f"graph nodes to take apart:   {len(in_graph)}")
            for bucket, count in sorted(by_bucket.items(), key=lambda item: -item[1]):
                print(f"  {count:>5}  {bucket}")
            unknown = sum(not split["survivor_authors_known"] for split in in_mongo)
            if unknown:
                print(f"survivors without their own ledger entry (authorships left as they are): {unknown}")
            args.report.parent.mkdir(parents=True, exist_ok=True)
            args.report.write_text(json.dumps({"mongo": in_mongo, "graph": in_graph},
                                              ensure_ascii=False, indent=2))
            print(f"report: {args.report}")
            if not args.apply:
                print("dry run: nothing written, --apply to write")
                return 0
            apply_mongo(db, in_mongo)
            with actor_context("split-publication-versions", source="script"):
                apply_graph(client, in_graph)
    except Busy as error:
        print(error, file=sys.stderr)
        return 1
    finally:
        client.close()
        mongo.close()

    groups = sorted({group for split in [*in_mongo, *in_graph] for group in split["groups"]})
    print("\nnext, in this order:")
    for group in groups:
        print(f"  uv run pauk normalize --group {group}")
        print(f"  uv run pauk enrich --group {group}")
        print(f"  uv run pauk publish graph --group {group}")
    print("  uv run pauk admin prune            # look at the plan first, then --apply")
    print("  uv run pauk versions link --llm")
    return 0


if __name__ == "__main__":
    sys.exit(main())

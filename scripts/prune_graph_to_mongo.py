"""Delete from the graph what MongoDB no longer holds.

`publish graph` only ever adds: every node and relationship goes in through
MERGE, and nothing removes what has disappeared from the source. So a row
deleted or a claim withdrawn in MongoDB stays in the graph forever, and the
display copy drifts away from the source of truth without saying so.

This closes that gap for the two entities a data repair can shrink:
IMPLEMENTS claims and GitHubProfile nodes. It is deliberately narrow —
persons and publications are folded by `dedup graph`, which lives only in
the graph (`merged_ids`), and comparing those against MongoDB would call
every merged-away node stale and undo the dedup.

Dry run by default. Run where Neo4j is reachable.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

from neo4j import GraphDatabase

from pauk.settings import settings
from pauk.storage import PreparedStore
from pauk.storage.mongo import get_mongo_client

BATCH = 500


def chunks(items: list, size: int = BATCH):
    for start in range(0, len(items), size):
        yield items[start:start + size]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--apply", action="store_true", help="delete (default: dry run)")
    parser.add_argument("--report", type=Path, default=Path("data/reports/prune-graph.json"))
    args = parser.parse_args()

    mongo = get_mongo_client(settings)
    db = mongo[settings.mongo_db]
    repositories = list(db[PreparedStore.COLLECTIONS["repositories"]].find(
        {}, {"publication_ids": 1, "_processing": 1}))
    mongo_claims = {(r["_id"], p) for r in repositories for p in (r.get("publication_ids") or [])}
    failed = {r["_id"] for r in repositories
              if ((r.get("_processing") or {}).get("repositories") or {}).get("status") == "failed"}
    mongo_logins = {(d.get("login") or "").lower()
                    for d in db[PreparedStore.COLLECTIONS["github_profiles"]].find({}, {"login": 1})}
    mongo.close()

    driver = GraphDatabase.driver(settings.neo4j_uri, auth=(settings.neo4j_user, settings.neo4j_password))
    with driver.session(default_access_mode="READ") as session:
        graph_claims = {(r["repo"], r["pub"]) for r in session.run(
            "MATCH (r:Repository)-[:IMPLEMENTS]->(p:Publication) RETURN r.id AS repo, p.id AS pub")}
        graph_logins = {(r["login"] or "").lower() for r in session.run(
            "MATCH (g:GitHubProfile) RETURN g.login AS login")}
        # A profile the graph still points at from a live node must not be
        # removed: the edge would go with it and the repository would lose
        # its owner. Nothing should be in here — it is a guard, not a filter.
        attached = {(r["login"] or "").lower() for r in session.run(
            "MATCH (g:GitHubProfile) WHERE (g)<-[:OWNED_BY]-() OR ()-[:CONTRIBUTED_TO]->(g) "
            "RETURN g.login AS login")}

    stale_claims = sorted(graph_claims - mongo_claims)
    missing_claims = sorted(mongo_claims - graph_claims)
    stale_logins = sorted(graph_logins - mongo_logins)
    risky = sorted(set(stale_logins) & attached)

    print(f"IMPLEMENTS   mongo={len(mongo_claims)}  graph={len(graph_claims)}")
    print(f"  stale in the graph (to delete): {len(stale_claims)}")
    print(f"  in mongo but absent from graph: {len(missing_claims)}")
    from_failed = sum(1 for repo, _ in missing_claims if repo in failed)
    print(f"      of which the loader skips because the repository is FAILED: {from_failed}")
    for repo, pub in missing_claims:
        if repo not in failed:
            print(f"      unexplained: {repo} -> {pub}")
    print(f"\nGitHubProfile  mongo={len(mongo_logins)}  graph={len(graph_logins)}")
    print(f"  stale in the graph (to delete): {len(stale_logins)}")
    print(f"  of those still attached to a live node: {len(risky)}")
    for login in risky:
        print(f"      keeping {login}: still an owner or a contribution target")

    to_delete_logins = [login for login in stale_logins if login not in set(risky)]

    if args.apply:
        with driver.session() as session:
            removed_rels = 0
            for batch in chunks(stale_claims):
                removed_rels += session.run(
                    "UNWIND $pairs AS pair "
                    "MATCH (r:Repository {id: pair[0]})-[i:IMPLEMENTS]->(p:Publication {id: pair[1]}) "
                    "DELETE i RETURN count(*) AS n",
                    pairs=[list(pair) for pair in batch]).single()["n"]
            removed_nodes = 0
            for batch in chunks(to_delete_logins):
                removed_nodes += session.run(
                    "UNWIND $logins AS login "
                    "MATCH (g:GitHubProfile) WHERE toLower(g.login) = login "
                    "DETACH DELETE g RETURN count(*) AS n", logins=batch).single()["n"]
        print(f"\ndeleted {removed_rels} IMPLEMENTS, {removed_nodes} GitHubProfile")
    else:
        print(f"\ndry run — would delete {len(stale_claims)} IMPLEMENTS "
              f"and {len(to_delete_logins)} GitHubProfile; pass --apply")
    driver.close()

    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps({
        "created_at": datetime.now(UTC).isoformat(), "applied": args.apply,
        "stale_implements": [list(p) for p in stale_claims],
        "stale_profiles": to_delete_logins,
        "kept_because_attached": risky,
        "in_mongo_absent_from_graph": [list(p) for p in missing_claims],
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"report: {args.report}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

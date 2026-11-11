"""Publish a rebuilt publication family and reconcile just its incident edges."""

from collections import defaultdict

from pauk.graph.client import chunked, resolve_folded_id
from pauk.graph.extract import NODE_REGISTRY, extract_node, extract_relationships, person_spec
from pauk.graph.jsonl_loader import extract_repo_links
from pauk.graph.mutations import RELATIONSHIPS, as_stored
from pauk.graph.overrides import DELETE, LINK, override_id, tombstoned_ids
from pauk.graph.version_link import REL_TYPE, reconcile_version_pair
from pauk.storage import review
from pauk.urls import normalize_repo_url

_REBUILT_RELATIONS = {"AUTHORED", "PRODUCED_BY", "MENTIONS_LINK", "IMPLEMENTS"}
_MENTION_FIELDS = (
    "context", "page_number", "pdf_id", "context_source", "reference_label",
    "url_ambiguous", "candidate_urls", "availability",
)
_AUTHORED_FIELDS = ("position", "affiliation", "affiliation_source", "is_corresponding")


def _incident(client, publication_ids):
    result = {}
    for work_id in sorted(publication_ids):
        for edge in client.fetch_node_relationships("Publication", work_id):
            for label in edge["labels"]:
                triple = (("Publication", edge["type"], label) if edge["outgoing"]
                          else (label, edge["type"], "Publication"))
                match = RELATIONSHIPS.get(triple)
                if match is None:
                    continue
                other_id = edge["other_id"]
                if edge["outgoing"] and match != "id":
                    other_id = edge["other_props"].get(match)
                if other_id is not None:
                    src, tgt = (work_id, other_id) if edge["outgoing"] else (other_id, work_id)
                    result[(*triple, src, tgt)] = edge["relationship_props"]
    return result


def known_version_pairs(client, db, work_ids, original_pairs):
    pairs = {tuple(sorted(pair)) for pair in original_pairs}
    edges = {
        (src, tgt): {key: value for key, value in props.items() if key not in ("created_at", "updated_at")}
        for (_, rel, _, src, tgt), props in _incident(client, work_ids).items()
        if rel == REL_TYPE and src != tgt
    }
    pairs.update(tuple(sorted(pair)) for pair in edges)
    for row in db[review.COLLECTION].find({"kind": review.PUB_VERSION, "members": {"$in": sorted(work_ids)}}):
        if len(set(row["members"])) == 2:
            pairs.add(tuple(sorted(row["members"])))
    for row in _manual_relationships(db, work_ids):
        if row["rel_type"] == REL_TYPE and row["src_id"] != row["target_id"]:
            pairs.add(tuple(sorted((row["src_id"], row["target_id"]))))
    return sorted(pairs), edges


def _manual_relationships(db, ids):
    return db.graph_overrides.find({
        "kind": "rel", "active": True,
        "$or": [
            {"src_label": "Publication", "src_id": {"$in": sorted(ids)}},
            {"tgt_label": "Publication", "target_id": {"$in": sorted(ids)}},
        ],
    })


def _repair_nodes(client, db, rows):
    repository_urls = {}
    persons = {}
    repositories = {}
    skipped = set()
    created = []
    for entity, spec_name in (("organizations", "organization"), ("departments", "department"),
                              ("persons", "itmo_person"), ("github_profiles", "github_profile"),
                              ("repositories", "repository"), ("publications", "publication")):
        for row in rows.get(entity, ()):
            spec = person_spec(row) if entity == "persons" else NODE_REGISTRY[spec_name]
            label, (work_id, props) = extract_node(row, spec)
            if label in ("Person", "Repository"):
                work_id = resolve_folded_id(client, label, work_id)
            manual = db.graph_overrides.find_one({"_id": override_id(label, work_id), "active": True}) or {}
            manual_fields = as_stored(manual.get("fields") or {})
            if manual.get("op") == DELETE:
                skipped.add((label, work_id))
                continue
            before = client.fetch_node_properties(label, work_id)
            if label == "Publication":
                props = {**dict.fromkeys(spec.prop_fields), **props, **manual_fields}
                if before is None or any(before.get(key) != value for key, value in props.items()):
                    client.upsert_nodes_batch(label, [(work_id, props)])
            elif before is None:
                props.update(manual_fields)
                if label in {"Department", "Repository"}:
                    created.append((work_id, row, spec))
                if label == "Person":
                    client.upsert_person_nodes_batch([(work_id, props)])
                else:
                    client.upsert_nodes_batch(label, [(work_id, props)])
            if label == "Person":
                persons[row["id"]] = work_id
            if label == "Repository":
                repositories[row["id"]] = work_id
                url = (before or props)["url"]
                repository_urls.update({normalize_repo_url(key): url
                                        for key in [url, row["url"], *(row.get("cited_urls") or [])]})
    for node_id, row, spec in created:
        for (src, tgt, rel, match), edges in extract_relationships(row, spec).items():
            if rel not in {"PART_OF", "OWNED_BY"}:
                continue
            for _, key, props in edges:
                if (tgt, key) not in skipped:
                    client.upsert_relationships_batch(src, tgt, rel, [(node_id, key, props)], tgt_match_prop=match)
    return persons, repositories, repository_urls, skipped


def _prepared_edges(client, db, rows, targets, persons, repositories, urls, skipped):
    desired = {}
    for row in rows["persons"]:
        if row["id"] not in persons:
            continue
        auth = {**row, "authored": [item for item in row["authored"] if item["publication_id"] in targets]}
        for (_, target, rel, _), edges in extract_relationships(auth, person_spec(row)).items():
            if rel != "AUTHORED":
                continue
            for person, work, props in edges:
                key = ("Person", rel, target, persons[person], work)
                old = desired.get(key)
                if old is not None:
                    props = {**old, "is_corresponding": old["is_corresponding"] or props["is_corresponding"]}
                desired[key] = {**dict.fromkeys(_AUTHORED_FIELDS), **props}
    for row in rows["publications"]:
        for department in row.get("department_ids") or []:
            if ("Department", department) not in skipped:
                desired["Publication", "PRODUCED_BY", "Department", row["id"], department] = {}
    for row in rows["repositories"]:
        if row["id"] in repositories:
            for work in set(row.get("publication_ids") or []) & targets:
                desired["Repository", "IMPLEMENTS", "Publication", repositories[row["id"]], work] = {}
    deleted_candidates = tombstoned_ids(db, "LinkCandidate")
    for row in rows["repo_links"]:
        nodes, repos, candidates, _ = extract_repo_links(
            row, urls, dropped_candidates=deleted_candidates, synchronize_relevance=True,
        )
        for node_id, props in nodes:
            before = client.fetch_node_properties("LinkCandidate", node_id)
            if before is None:
                client.upsert_nodes_batch("LinkCandidate", [(node_id, props)])
        for target, edges in (("Repository", repos), ("LinkCandidate", candidates)):
            for src, tgt, props in edges:
                desired["Publication", "MENTIONS_LINK", target, src, tgt] = {
                    **dict.fromkeys(_MENTION_FIELDS), **props,
                }
    return desired


def _sync_edges(client, current, desired):
    removals = defaultdict(list)
    additions = defaultdict(list)
    updates = defaultdict(list)
    for key in sorted(current.keys() - desired.keys()):
        src_label, rel, tgt_label, src, tgt = key
        removals[src_label, tgt_label, rel, RELATIONSHIPS[key[:3]]].append((src, tgt))
    for key, props in sorted(desired.items()):
        src_label, rel, tgt_label, src, tgt = key
        batch = (src_label, tgt_label, rel, RELATIONSHIPS[key[:3]])
        if key not in current:
            additions[batch].append((src, tgt, props))
        elif any(current[key].get(name) != value for name, value in props.items()):
            updates[batch].append((src, tgt, props))
    for operation, batches in ((client.upsert_relationships_batch, additions),
                               (client.set_relationship_properties_batch, updates),
                               (client.delete_relationships_batch, removals)):
        for (src, tgt, rel, match), batch in batches.items():
            for chunk in chunked(batch):
                matched = operation(src, tgt, rel, chunk, tgt_match_prop=match)
                if batches is not removals and matched < len(chunk):
                    raise RuntimeError(f"Could only write {matched}/{len(chunk)} {rel} edges")


def apply_graph_repair(client, db, scratch, targets, aliases, original_pairs, version_pairs, history):
    rows = defaultdict(list)
    for change in scratch.changes.find().sort("_id", 1):
        if change["entity"] in {"publications", "repo_links"} and change["key"] not in targets:
            continue
        rows[change["entity"]].append(change["row"])
    persons, repositories, urls, skipped = _repair_nodes(client, db, rows)
    # A fold may have existed only in prepared data when the repair started.
    live_aliases = [(alias, survivor) for alias, survivor in aliases.items()
                    if client.fetch_node_properties("Publication", alias) is not None]
    if live_aliases:
        client.merge_publication_nodes_batch(live_aliases)
        _repair_nodes(client, db, {"publications": rows["publications"]})
    current = {key: props for key, props in _incident(client, targets).items() if key[1] in _REBUILT_RELATIONS}
    desired = _prepared_edges(client, db, rows, targets, persons, repositories, urls, skipped)
    old_parents = {work_id: survivor for survivor, work_id in original_pairs}
    prior_aliases = {**aliases, **old_parents}
    deleted = set()
    for row in _manual_relationships(db, targets | aliases.keys()):
        sl, rel, tl = (row[key] for key in ("src_label", "rel_type", "tgt_label"))
        if rel not in _REBUILT_RELATIONS:
            continue
        src, tgt = row["src_id"], row["target_id"]
        if sl in ("Person", "Repository"):
            src = resolve_folded_id(client, sl, src)
        if tl == "Repository":
            tgt = urls.get(normalize_repo_url(tgt), tgt)
        key = (sl, rel, tl, aliases.get(src, src) if sl == "Publication" else src,
               aliases.get(tgt, tgt) if tl == "Publication" else tgt)
        if row["op"] == DELETE:
            deleted.add(key)
        if row["op"] == LINK:
            old_key = (sl, rel, tl, prior_aliases.get(src, src) if sl == "Publication" else src,
                       prior_aliases.get(tgt, tgt) if tl == "Publication" else tgt)
            if key in current or old_key in current:
                desired.setdefault(key, current[key] if key in current else current[old_key])
    _sync_edges(client, current, {key: props for key, props in desired.items() if key not in deleted})
    previous_by_pair = defaultdict(dict)
    for a, b in version_pairs:
        src = resolve_folded_id(client, "Publication", a)
        tgt = resolve_folded_id(client, "Publication", b)
        if src == tgt:
            continue
        previous = previous_by_pair[tuple(sorted((src, tgt)))]
        for original, direction in (((a, b), (src, tgt)), ((b, a), (tgt, src))):
            old = tuple(prior_aliases.get(key, key) for key in original)
            if old != direction and old in history:
                previous[direction] = history[old]
    for pair, previous in previous_by_pair.items():
        reconcile_version_pair(client, db, pair, record_question=True, previous=previous)

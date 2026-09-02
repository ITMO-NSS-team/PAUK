"""Builds web/graph-data.js and web/graph-search.js from a graph snapshot.

Usage:
  uv run python -m pauk.cli cache export  # writes the snapshot, once
  uv run python -m pauk.gui.generate_data [--out-dir web] [--seed 42] [--cache path]
"""

import argparse
import json
import logging
import random
import time
from collections import Counter, defaultdict
from itertools import combinations
from pathlib import Path

from .config import (
    COAUTH_MIN_W,
    FA2_ITER_AUTHORS,
    FA2_ITER_PUBS,
    FA2_ITER_REPOS,
    MIN_SEP_AUTHORS,
    MIN_SEP_PUBS,
    MIN_SEP_REPOS,
    NO_CLUSTER_NAME,
    NO_CLUSTER_NAME_EN,
    NO_DEPT_COLOR,
    NO_DEPT_NAME,
    NO_DEPT_NAME_EN,
    PUB_DEPT_EDGE_K,
    PUB_DEPT_EDGE_WEIGHT,
    PUB_EDGE_MIN_W,
    PUB_LAYOUT_TOP_K,
    REPO_CLUSTER_MIN,
    REPO_DEPT_EDGE_K,
    REPO_DEPT_EDGE_WEIGHT,
    REPO_EDGE_TOP_K,
    REPO_GROUP_CAP,
    REPO_W_COAUTHOR,
    REPO_W_OWNER,
    REPO_W_PERSON,
    REPO_W_PUB,
)
from .layout import (
    co_membership_weights,
    dense_rank,
    fa2_blended_layout,
    golden_color,
    majority_vote,
    sparse_dept_edges,
    spread_min_distance,
    top_k_edges,
)

logger = logging.getLogger(__name__)


def _initial(value: str) -> str:
    """First letter, capitalized, with a trailing period."""
    return f"{value[0].upper()}."


def _fmt_part(value: str, *, force_initial: bool) -> str:
    """A given-name-or-patronymic part, formatted for a label.

    Forced down to an initial for public display or whenever the part sits
    beside another part (surname_ru/first_name_ru/second_name_ru all
    filled). Left as the author actually has it otherwise — except a part
    that already IS a bare initial (one letter, with or without a trailing
    period LLM/catalog data occasionally carries) always gets its period:
    that's not truncation, it's just correct punctuation for what the data
    already says.
    """
    stripped = value.rstrip(".")
    if force_initial or len(stripped) == 1:
        return _initial(stripped)
    return value


def author_label(surname: str | None, first: str | None, second: str | None,
                 *, public: bool = False) -> str:
    """One shape for every author: surname first, then initials.

    Takes one language's three name parts at a time - build one label per
    language from surname_ru/first_name_ru/second_name_ru and
    surname_en/first_name_en/second_name_en separately. No fallback to a
    combined raw string here: guessing surname/given/patronymic out of
    word order is exactly the failure mode author_names.py's LLM step
    replaced, and doing it again here for display would reintroduce it.
    An empty surname returns "" - the caller decides the fallback (e.g. the
    other language's label, or the free-text name_ru).
    """
    surname, first, second = surname or "", first or "", second or ""
    if not surname:
        return ""
    if public and len(surname) > 3:
        surname = surname[:3] + ".."
    if first and second:
        return f"{surname} {_fmt_part(first, force_initial=True)}{_fmt_part(second, force_initial=True)}"
    if second:  # only the patronymic survived — treat it as the initial
        return f"{surname} {_fmt_part(second, force_initial=True)}"
    if first:
        return f"{surname} {_fmt_part(first, force_initial=public)}"
    return surname


def author_variants(row, label_ru: str, label_en: str) -> list[str]:
    """Other spellings of this person's name, without the ones on show.

    The card already displays the RU and EN labels and the full Russian
    name; everything else OpenAlex knows about this author (and the full
    Russian name when the label is only surname + initials) goes into the
    collapsed list.
    """
    shown = {label_ru.casefold(), label_en.casefold()}
    candidates = [row.get("name_ru") or "", *(row.get("name_variants") or [])]
    variants = []
    for value in candidates:
        cleaned = " ".join((value or "").split())
        if cleaned and cleaned.casefold() not in shown:
            shown.add(cleaned.casefold())
            variants.append(cleaned)
    return variants


def repo_cluster_keys(repo_ids, dept_of, org_of, field_of, min_size=REPO_CLUSTER_MIN):
    """Group each repository, strongest claim first: department, else the
    organization that owns it, else the field of the papers it implements.

    An inferred group of one is not a group — it spends a unique hue on a
    single dot — so a tier is offered only when it has `min_size` members, and
    a repository the tier turns down falls through to the next one.

    Departments are exempt from the threshold: a department exists outside
    this map, and dropping it would break the colour it shares with the other
    two tabs. `org_of` is expected to already exclude personal accounts, which
    never form a group at all — an account says who pushed the code, not what
    it belongs to.
    """
    org_size = Counter(org for org in org_of.values() if org)
    keys = {}
    for rid in repo_ids:
        if dept_of.get(rid):
            keys[rid] = ("dept", dept_of[rid])
        elif org_size.get(org_of.get(rid), 0) >= min_size:
            keys[rid] = ("org", org_of[rid])
        elif field_of.get(rid):
            keys[rid] = ("field", field_of[rid])
        else:
            keys[rid] = None

    field_size = Counter(k for k in keys.values() if k and k[0] == "field")
    return {
        rid: (None if key and key[0] == "field" and field_size[key] < min_size else key)
        for rid, key in keys.items()
    }


def build_graph_data(db, seed: int, public: bool = False):
    dept_name = {row["id"]: (row["name_ru"] or row["name_en"] or "") for row in db["departments"]}
    dept_name_en = {row["id"]: (row["name_en"] or "") for row in db["departments"]}

    # --- authorship: only publications with at least one ITMO author ----------
    pub_authors = defaultdict(list)
    author_pubs = defaultdict(list)
    for pid, per in db["authorship"]:
        pub_authors[pid].append(per)
        author_pubs[per].append(pid)

    pubs_rows = [r for r in db["publications"] if r["id"] in pub_authors]
    pub_ids = {r["id"] for r in pubs_rows}
    logger.info(
        "Publications with ITMO authors: %d of %d",
        len(pubs_rows),
        len(db["publications"]),
    )

    # --- static author departments (:Person:Itmo)-[:BELONGS_TO]->(:Department) -
    static_depts = {row["id"]: [] for row in db["persons"]}
    for per, did in db["person_depts"]:
        if did in dept_name:
            static_depts[per].append(did)

    # --- publication departments: full list + primary --------------------------
    pub_dept_rows = defaultdict(list)
    for pid, did in db["pub_depts"]:
        if pid in pub_ids and did in dept_name and did not in pub_dept_rows[pid]:
            pub_dept_rows[pid].append(did)

    pub_primary = {}
    for pid in pub_ids:
        primary = majority_vote(static_depts.get(per, []) for per in pub_authors[pid])
        if primary is None and pub_dept_rows.get(pid):
            primary = pub_dept_rows[pid][0]
        pub_primary[pid] = primary

    # --- author department: from their most recent publication ------------------
    pub_date = {r["id"]: (r["publication_date"] or "") for r in pubs_rows}
    author_dept = {}
    for per in static_depts:
        dept = None
        for pid in sorted(
            author_pubs.get(per, []),
            key=lambda p: (pub_date.get(p, ""), p),
            reverse=True,
        ):
            if pub_primary.get(pid):
                dept = pub_primary[pid]
                break
        author_dept[per] = dept

    # --- repository department --------------------------------------------------
    repo_pub_map = defaultdict(list)
    for rid, pid in db["repo_pubs"]:
        if pid in pub_ids:
            repo_pub_map[rid].append(pid)
    repo_dept_rows = defaultdict(list)
    for rid, did in db["repo_depts"]:
        if did in dept_name and did not in repo_dept_rows[rid]:
            repo_dept_rows[rid].append(did)

    # ITMO people credited on a repository — also the "person" edge signal below
    repo_contributors = defaultdict(set)
    for rid, per, _role in db["repo_persons"]:
        repo_contributors[rid].add(per)

    repo_dept = {}
    for row in db["repositories"]:
        rid = row["id"]
        primary = majority_vote(
            [pub_primary[p]] for p in repo_pub_map.get(rid, []) if pub_primary.get(p)
        )
        if primary is None and repo_dept_rows.get(rid):
            primary = repo_dept_rows[rid][0]
        if primary is None:
            # Last resort: the people who actually wrote it. Weaker than the
            # publication it implements — someone can contribute far outside
            # their own department — so it only speaks when nothing else does.
            primary = majority_vote(
                static_depts.get(per, []) for per in repo_contributors.get(rid, ())
            )
        repo_dept[rid] = primary

    # --- graph department table: sort by size, reindex --------------------------
    usage = Counter()
    for d in author_dept.values():
        if d:
            usage[d] += 1
    for d in pub_primary.values():
        if d:
            usage[d] += 1
    for d in repo_dept.values():
        if d:
            usage[d] += 1
    for rows in (pub_dept_rows, repo_dept_rows):
        for depts in rows.values():
            for d in depts:
                usage[d] += 0

    ordered = sorted(usage, key=lambda d: (-usage[d], dept_name[d]))
    gid = {d: i for i, d in enumerate(ordered)}
    no_dept_gid = len(ordered)

    def g(dept_db_id):
        return gid[dept_db_id] if dept_db_id else no_dept_gid

    n_auth = Counter(g(d) for d in author_dept.values())
    n_pub = Counter(g(pub_primary[p]) for p in pub_ids)
    n_repo = Counter(g(repo_dept[row["id"]]) for row in db["repositories"])

    departments = [
        {
            "id": gid[d],
            "name": dept_name[d],
            "name_en": dept_name_en[d],
            "color": golden_color(gid[d]),
            "n": n_auth[gid[d]] + n_pub[gid[d]] + n_repo[gid[d]],
            "n_authors": n_auth[gid[d]],
            "n_pubs": n_pub[gid[d]],
            "n_repos": n_repo[gid[d]],
        }
        for d in ordered
    ]
    departments.append(
        {
            "id": no_dept_gid,
            "name": NO_DEPT_NAME,
            "name_en": NO_DEPT_NAME_EN,
            "color": NO_DEPT_COLOR,
            "n": n_auth[no_dept_gid] + n_pub[no_dept_gid] + n_repo[no_dept_gid],
            "n_authors": n_auth[no_dept_gid],
            "n_pubs": n_pub[no_dept_gid],
            "n_repos": n_repo[no_dept_gid],
        }
    )
    logger.info('Departments: %d (+ "%s")', len(ordered), NO_DEPT_NAME)

    # --- repository clusters: department, else GitHub org, else OpenAlex field ---
    # A department is known for well under half the repositories, so colouring
    # this tab by department alone leaves most of the map grey. The owning
    # organization covers more of it than the department does, and the field
    # of the papers a repository implements catches part of the rest. Three
    # tiers, strongest claim first; a repository takes the first that answers.
    pub_fields = {row["id"]: (row.get("fields") or []) for row in pubs_rows}
    owner_type = {row["id"]: (row.get("owner_type") or "") for row in db["repositories"]}
    repo_owner_login = {row["id"]: (row["owner"] or "") for row in db["repositories"]}

    # An inferred group of one is not a group — it spends a unique hue on a
    # single dot — so each tier is offered only when it has enough members,
    # and a repository the tier turns down falls through to the next one.
    # Departments are exempt: a department exists outside this map, and
    # dropping it would break the colour it shares with the other two tabs.
    # A personal account never forms a group at all — it says who pushed the
    # code, not what it belongs to, and there are 173 of them.
    org_of = {
        row["id"]: repo_owner_login[row["id"]].lower()
        for row in db["repositories"]
        if owner_type.get(row["id"]) == "organization" and repo_owner_login.get(row["id"])
    }

    def field_of(rid):
        # majority_vote and not Counter.most_common: `repo_pubs` comes back
        # from Neo4j with no ORDER BY, so on a tie most_common would let the
        # row order pick the field — and with it the cluster and the colour —
        # anew on every run over the same data.
        return majority_vote(pub_fields.get(pid, []) for pid in repo_pub_map.get(rid, []))

    repo_ids_all = [row["id"] for row in db["repositories"]]
    repo_cluster_key = repo_cluster_keys(
        repo_ids_all,
        repo_dept,
        org_of,
        {rid: field_of(rid) for rid in repo_ids_all},
    )

    cluster_sizes = Counter(k for k in repo_cluster_key.values() if k)
    # Sorted by size, then by key, so the ids are stable between runs.
    ordered_clusters = sorted(cluster_sizes, key=lambda k: (-cluster_sizes[k], k))
    cluster_id = {k: i for i, k in enumerate(ordered_clusters)}
    no_cluster_id = len(ordered_clusters)

    def cluster_label(key):
        kind, value = key
        if kind == "dept":
            return dept_name[value], dept_name_en[value] or dept_name[value]
        return value, value  # an org login and an OpenAlex field are already English

    repo_clusters = []
    for key in ordered_clusters:
        kind, value = key
        name, name_en = cluster_label(key)
        repo_clusters.append(
            {
                "id": cluster_id[key],
                "kind": kind,
                # A department keeps the colour it has on the other two tabs;
                # the rest continue the same golden-ratio walk past its end,
                # so their hues do not collide with any department's.
                "color": (
                    golden_color(gid[value]) if kind == "dept"
                    else golden_color(len(ordered) + cluster_id[key])
                ),
                "name": name,
                "name_en": name_en,
                "n": cluster_sizes[key],
                **({"dept": gid[value]} if kind == "dept" else {}),
            }
        )
    repo_clusters.append(
        {
            "id": no_cluster_id,
            "kind": "none",
            "color": NO_DEPT_COLOR,
            "name": NO_CLUSTER_NAME,
            "name_en": NO_CLUSTER_NAME_EN,
            "n": sum(1 for k in repo_cluster_key.values() if not k),
        }
    )
    by_kind = Counter(k[0] for k in repo_cluster_key.values() if k)
    logger.info(
        "Repository clusters: %d (dept %d, org %d, field %d repositories; %d unclustered)",
        len(ordered_clusters),
        by_kind["dept"],
        by_kind["org"],
        by_kind["field"],
        sum(1 for k in repo_cluster_key.values() if not k),
    )

    # --- co-authorship graph and FA2 layout -------------------------------------
    # layout weight = joint publications + joint repos + shared dept; exported
    coauth = Counter()
    for _pid, pers in pub_authors.items():
        for a, b in combinations(sorted(set(pers)), 2):
            coauth[(a, b)] += 1

    rng = random.Random(seed)
    author_layout_w = Counter(coauth)
    for pers in repo_contributors.values():
        for a, b in combinations(sorted(pers), 2):
            author_layout_w[(a, b)] += 1
    for pair, w in sparse_dept_edges(set(static_depts), author_dept, rng).items():
        author_layout_w[pair] += w

    t0 = time.time()
    pos_authors, (n_giant, e_giant, n_small, n_single) = fa2_blended_layout(
        author_layout_w, set(static_depts), FA2_ITER_AUTHORS, seed
    )
    pos_authors = spread_min_distance(pos_authors, MIN_SEP_AUTHORS, seed)
    logger.info(
        "FA2 over authors: giant %d nodes / %d edges, blended: %d small comps + %d singles, min-sep %.1f, %.1f s",
        n_giant,
        e_giant,
        n_small,
        n_single,
        MIN_SEP_AUTHORS,
        time.time() - t0,
    )

    # --- publication-to-publication graph (shared ITMO authors) -----------------
    pub_pair_w = Counter()
    for _per, plist in author_pubs.items():
        for a, b in combinations(sorted(set(plist)), 2):
            pub_pair_w[(a, b)] += 1

    # Publications get their own FA2 layout (not their authors' centroid,
    # which degenerates for solo-authored pubs). Full w>=1 graph is ~310k
    # edges, so keep only each pub's top-K strongest links.
    t0 = time.time()
    pub_layout_w = top_k_edges(pub_pair_w, PUB_LAYOUT_TOP_K)
    for pair, w in sparse_dept_edges(
        pub_ids,
        pub_primary,
        rng,
        k=PUB_DEPT_EDGE_K,
        weight=PUB_DEPT_EDGE_WEIGHT,
        taper_size=150,
    ).items():
        pub_layout_w[pair] = pub_layout_w.get(pair, 0) + w

    pos_pubs, (n_giant_p, e_giant_p, n_small_p, n_single_p) = fa2_blended_layout(
        pub_layout_w, pub_ids, FA2_ITER_PUBS, seed
    )
    pos_pubs = spread_min_distance(pos_pubs, MIN_SEP_PUBS, seed)
    logger.info(
        "FA2 over publications: giant %d nodes / %d edges, blended: %d small comps + %d singles, min-sep %.1f, %.1f s",
        n_giant_p,
        e_giant_p,
        n_small_p,
        n_single_p,
        MIN_SEP_PUBS,
        time.time() - t0,
    )

    # --- repository edges: four signals, not just a shared publication ----------
    # One rule (both repos implement the same publication) leaves ~90% of the
    # repositories with no edge at all, and FA2 has nothing to lay out. Each
    # signal below is a group of repositories that belong together for a
    # different reason; co_membership_weights turns each into weighted pairs.
    repo_ids = {row["id"] for row in db["repositories"]}
    repo_all_pubs = defaultdict(set)
    for rid, pid in db["repo_pubs"]:
        if rid in repo_ids:
            repo_all_pubs[rid].add(pid)

    def _groups(member_of):
        """repo -> keys  =>  key -> repos"""
        by_key = defaultdict(set)
        for rid, keys in member_of.items():
            for key in keys:
                by_key[key].add(rid)
        return by_key.values()

    repo_owner = {row["id"]: row["owner"] for row in db["repositories"] if row["owner"]}
    repo_coauthors = {
        rid: {per for pid in pids for per in pub_authors.get(pid, ())}
        for rid, pids in repo_all_pubs.items()
    }
    signals = {
        "pub": (_groups(repo_all_pubs), REPO_W_PUB),
        "person": (_groups({rid: pers for rid, pers in repo_contributors.items() if rid in repo_ids}), REPO_W_PERSON),
        "coauthor": (_groups(repo_coauthors), REPO_W_COAUTHOR),
        "owner": (_groups({rid: [owner] for rid, owner in repo_owner.items()}), REPO_W_OWNER),
    }

    repo_edge_w = defaultdict(float)
    repo_edge_kinds = defaultdict(list)
    for kind, (groups, weight) in signals.items():
        for pair, w in co_membership_weights(groups, weight, cap=REPO_GROUP_CAP).items():
            repo_edge_w[pair] += w
            repo_edge_kinds[pair].append(kind)
    repo_edge_w = top_k_edges(repo_edge_w, REPO_EDGE_TOP_K)

    # Same blended treatment as authors and publications: plain FA2 over a graph
    # this sparse drifts its disconnected components apart without bound, and
    # fit_coords then crushes the real content into a dot (see gui.md).
    repo_layout_w = dict(repo_edge_w)
    for pair, w in sparse_dept_edges(
        repo_ids, repo_dept, rng, k=REPO_DEPT_EDGE_K, weight=REPO_DEPT_EDGE_WEIGHT
    ).items():
        repo_layout_w[pair] = repo_layout_w.get(pair, 0.0) + w

    t0 = time.time()
    pos_repos, (n_giant_r, e_giant_r, n_small_r, n_single_r) = fa2_blended_layout(
        repo_layout_w, repo_ids, FA2_ITER_REPOS, seed
    )
    pos_repos = spread_min_distance(pos_repos, MIN_SEP_REPOS, seed)
    logger.info(
        "FA2 over repositories: giant %d nodes / %d edges, blended: %d small comps + %d singles, min-sep %.1f, %.1f s",
        n_giant_r,
        e_giant_r,
        n_small_r,
        n_single_r,
        MIN_SEP_REPOS,
        time.time() - t0,
    )

    # --- nodes ------------------------------------------------------------------
    # Missing external persons - #151.
    pubs_count = {per: len(set(author_pubs.get(per, []))) for per in static_depts}
    rank_a = dense_rank(pubs_count)
    authors = []
    for row in db["persons"]:
        pid_ = row["id"]
        x, y = pos_authors[pid_]
        label_ru = author_label(
            row["surname_ru"], row["first_name_ru"], row["second_name_ru"], public=public,
        ) or row.get("name_ru") or ""
        label_en = author_label(
            row["surname_en"], row["first_name_en"], row["second_name_en"], public=public,
        ) or label_ru
        author = {
            "key": pid_,
            "kind": "author",
            "dept": g(author_dept[pid_]),
            "label": label_ru,
            "label_en": label_en,
            "pubs_count": pubs_count[pid_],
            "rank": rank_a[pid_],
            "gx": x,
            "gy": y,
        }
        if not public:
            author["name_ru"] = row.get("name_ru") or ""
            author["name_variants"] = author_variants(row, label_ru, label_en)
            author["degree"] = row["degree"] or ""
            author["github"] = row["github"] or ""
            author["orcid"] = row.get("orcid") or ""
        authors.append(author)

    stars = {row["id"]: (row["stars_num"] or 0) for row in db["repositories"]}
    rank_r = dense_rank(stars)
    repos = []
    for row in db["repositories"]:
        rid = row["id"]
        x, y = pos_repos[rid]
        repos.append(
            {
                "key": rid,
                "kind": "repo",
                "dept": g(repo_dept[rid]),
                "cluster": cluster_id.get(repo_cluster_key[rid], no_cluster_id),
                "label": row["name"] or "",
                "description": row["description"] or "",
                "stars": row["stars_num"] or 0,
                "owner": row["owner"] or "",
                "owner_type": row.get("owner_type") or "",
                "url": row["url"] or "",
                "language": row.get("language") or "",
                "topics": row.get("topics") or [],
                "license": row.get("license") or "",
                "last_updated": row.get("last_updated") or "",
                "archived": bool(row.get("archived")),
                "forks": row.get("forks_num") or 0,
                "is_fork": bool(row.get("is_fork")),
                "rank": rank_r[rid],
                "gx": x,
                "gy": y,
            }
        )

    n_authors_of = {pid: len(set(pub_authors[pid])) for pid in pub_ids}
    rank_p = dense_rank(n_authors_of)
    pubs = []
    for row in pubs_rows:
        pid, year = row["id"], row["year"]
        x, y = pos_pubs[pid]
        depts_all = sorted({g(d) for d in pub_dept_rows.get(pid, [])} | {g(pub_primary[pid])})
        pubs.append(
            {
                "key": pid,
                "kind": "pub",
                "dept": g(pub_primary[pid]),
                "depts": depts_all,
                "year": year,
                "n_authors": n_authors_of[pid],
                "rank": rank_p[pid],
                "gx": x,
                "gy": y,
            }
        )

    # --- edges ------------------------------------------------------------------
    coauth_edges = [{"s": a, "t": b, "w": w} for (a, b), w in coauth.items() if w >= COAUTH_MIN_W]

    pub_edges = [
        {"s": a, "t": b, "w": w} for (a, b), w in pub_pair_w.items() if w >= PUB_EDGE_MIN_W
    ]

    repo_edges = [
        {"s": a, "t": b, "w": round(w, 2), "via": repo_edge_kinds[(a, b)]}
        for (a, b), w in repo_edge_w.items()
    ]

    dept_pair_w = Counter()
    for pid in pub_ids:
        ds = sorted({g(author_dept[per]) for per in pub_authors[pid]} - {no_dept_gid})
        for a, b in combinations(ds, 2):
            dept_pair_w[(a, b)] += 1
    dept_edges = [{"s": a, "t": b, "w": w} for (a, b), w in dept_pair_w.items()]

    repo_author_edges = [
        {"s": rid, "t": per, "role": role}
        for rid, per, role in db["repo_persons"]
        if per in static_depts
    ]
    repo_pub_edges = [{"s": rid, "t": pid} for rid, pid in db["repo_pubs"] if pid in pub_ids]
    all_edges = [{"s": per, "t": pid} for pid, per in db["authorship"]]

    logger.info(
        "Edges: coauth %d, pub %d, repo %d, dept %d, repo-author %d, repo-pub %d, authorship %d",
        len(coauth_edges),
        len(pub_edges),
        len(repo_edges),
        len(dept_edges),
        len(repo_author_edges),
        len(repo_pub_edges),
        len(all_edges),
    )

    return {
        "departments": departments,
        "repo_clusters": repo_clusters,
        "dept_edges": dept_edges,
        "authors": authors,
        "coauth_edges": coauth_edges,
        "repos": repos,
        "repo_edges": repo_edges,
        "repo_author_edges": repo_author_edges,
        "repo_pub_edges": repo_pub_edges,
        "pubs": pubs,
        "pub_edges": pub_edges,
        "all_edges": all_edges,
    }


def build_search_detail(db, graph):
    """Publication details for graph-search.js (loaded after the map)."""
    pub_ids = {p["key"] for p in graph["pubs"]}
    detail = []
    for row in db["publications"]:
        pid = row["id"]
        if pid not in pub_ids:
            continue
        code_url = row["code_url"]
        try:
            urls = json.loads(code_url) if code_url else []
        except json.JSONDecodeError:
            urls = []
        title = row["title"] or ""
        if len(title) > 200:
            title = title[:199] + "…"
        detail.append(
            {
                "key": pid,
                "label": title,
                "journal": row["journal"] or "",
                "doi": row["doi"] or "",
                "has_code": bool(row["has_code"]),
                "code_url": urls if isinstance(urls, list) else [urls],
            }
        )
    return detail


def dump_js(data, prefix: str, suffix: str, path: Path):
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    path.write_text(prefix + payload + suffix, encoding="utf-8")
    logger.info("Wrote %s (%.1f MB)", path, path.stat().st_size / 1e6)


def main():
    from pauk.cache.graph_snapshot import read_snapshot
    from pauk.settings import settings

    data_dir = Path(__file__).resolve().parent / "data"

    parser = argparse.ArgumentParser(description="Static data generation for the web visualization")
    parser.add_argument(
        "--public",
        action="store_true",
        help="drop personal fields (name_ru, name_variants, degree, "
        "github, orcid) from every author — for a build that leaves the "
        "corporate network, e.g. the GitHub Pages deploy",
    )
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=None,
        help="where to write graph-data.js and graph-search.js "
        f"(default: {data_dir / 'public'} with --public, else {data_dir / 'private'})",
    )
    parser.add_argument("--seed", type=int, default=42, help="FA2 layout seed")
    parser.add_argument(
        "--cache",
        type=Path,
        default=settings.cache_dir / "graph_snapshot.json",
        help="path to a graph snapshot created by 'pauk cache export'",
    )
    args = parser.parse_args()
    if args.out_dir is None:
        args.out_dir = data_dir / ("public" if args.public else "private")

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    args.out_dir.mkdir(parents=True, exist_ok=True)

    t0 = time.time()
    db = read_snapshot(args.cache)

    graph = build_graph_data(db, seed=args.seed, public=args.public)
    dump_js(graph, "window.GRAPH=", "", args.out_dir / "graph-data.js")

    detail = build_search_detail(db, graph)
    dump_js(
        detail,
        "(function(){var d=",
        ";if(typeof window._onDetailReady==='function')window._onDetailReady(d);else window._pendingDetail=d;})();",
        args.out_dir / "graph-search.js",
    )

    logger.info("Done in %.1f s", time.time() - t0)


if __name__ == "__main__":
    main()

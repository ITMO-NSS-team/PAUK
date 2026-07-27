"""Барьеры whole-set шаги после потока."""
from __future__ import annotations

import logging
from collections.abc import Iterable

from .conveyor import PipelinePerson
from .helpers import (
    EMAIL_RE,
    UnionFind,
    jloads,
    match_login,
    norm_email,
    norm_name,
    normalize,
    pick_email,
    repo_owner,
    usable_email,
)
from .stages import DepartmentRegistry

logger = logging.getLogger(__name__)


def _person_view(p: PipelinePerson) -> dict:
    """Персона в виде, который ждёт скоринг match_login
    Почты и other_names добираются из собранного профиля."""
    names = {norm_name(p.name_en)} | {norm_name(v) for v in p.name_variants}
    emails = {norm_email(p.email)} if p.email else set()
    prof = p.profile or {}
    orc = prof.get("orcid") or {}
    emails |= {norm_email(e) for e in orc.get("emails") or []}
    names |= {norm_name(o) for o in orc.get("other_names") or []}
    toks = norm_name(p.name_en).split() if p.name_en else []
    return {
        "names": {n for n in names if n},
        "emails": {e for e in emails if e},
        "name_en": p.name_en,
        "surname": toks[-1] if toks and len(toks[-1]) >= 4 else None,
        "github": p.github,
    }


def aggregate_candidates(rows: Iterable[dict], itmo_orgs: set[str]) -> dict[str, dict]:
    """Строки github_candidates -> агрегация по логину."""
    logins: dict[str, dict] = {}
    for r in rows:
        login = r["github_login"]
        c = logins.setdefault(login, {
            "login": login, "url": r.get("github_url"), "names": set(), "emails": set(),
            "itmo_text": False, "org_itmo": False,
            "pub_ids": set(), "repos": set(), "is_owner": False,
        })
        c["url"] = r.get("github_url") or c["url"]
        c["repos"].add(r.get("repo_url"))
        pub_ids = r.get("publication_ids")
        c["pub_ids"].update(pub_ids if isinstance(pub_ids, list) else jloads(pub_ids))
        if r.get("source") == "repo_owner":
            c["is_owner"] = True
        commit_names = r.get("commit_names")
        for n in (r.get("gh_name"), login,
                  *(commit_names if isinstance(commit_names, list) else jloads(commit_names))):
            if norm_name(n):
                c["names"].add(norm_name(n))
        commit_emails = r.get("commit_emails")
        for e in (r.get("gh_email"),
                  *(commit_emails if isinstance(commit_emails, list) else jloads(commit_emails))):
            if norm_email(e):
                c["emails"].add(norm_email(e))
        m = EMAIL_RE.search(r.get("gh_blog") or "")      # в blog иногда лежит почта
        if m and norm_email(m.group(0)):
            c["emails"].add(norm_email(m.group(0)))
        blob = f"{r.get('gh_company') or ''} {r.get('gh_location') or ''} {r.get('gh_bio') or ''}".lower()
        if "itmo" in blob or "saint petersburg" in blob or "sankt" in blob:
            c["itmo_text"] = True
        if repo_owner(r.get("repo_url")) in itmo_orgs:
            c["org_itmo"] = True
    return logins


def match_github(persons: dict[str, PipelinePerson], candidate_rows: Iterable[dict],
                 bridge: dict[str, set[str]], itmo_orgs: set[str] | None = None) -> list[dict]:
    """Сопоставляет github логины с персонами по всему набору."""
    views = {pid: _person_view(p) for pid, p in persons.items()}
    email_index: dict[str, str] = {}
    name_index: dict[str, set[str]] = {}
    for pid, v in views.items():
        for e in v["emails"]:
            email_index.setdefault(e, pid)
        for n in v["names"]:
            if len(n.split()) >= 2:
                name_index.setdefault(n, set()).add(pid)

    logins = aggregate_candidates(candidate_rows, itmo_orgs or set())
    decisions: list[dict] = []
    new_github = 0
    for login, cand in sorted(logins.items()):
        result = match_login(login, cand, views, email_index, name_index, bridge)
        if result is None:
            continue
        pid, score, signals, evidence, decision = result
        decisions.append({"login": login, "person_id": pid, "score": score,
                          "signals": signals, "evidence": evidence, "decision": decision})
        if decision == "matched":
            person = persons[pid]
            if not person.github:
                person.github = login
                views[pid]["github"] = login
                new_github += 1
            for e in sorted(cand["emails"]):                 # почты логина -> кандидаты
                u = usable_email(e)
                if u and ("commit", u) not in person.email_candidates:
                    person.email_candidates.append(("commit", u))
    matched = sum(1 for d in decisions if d["decision"] == "matched")
    logger.info("[match_github] логинов %d, matched %d, review %d, новых привязок %d",
                len(logins), matched, len(decisions) - matched, new_github)
    return decisions

def dedup_departments(registry: DepartmentRegistry, persons: dict[str, PipelinePerson]) -> int:
    """Сливает департаменты алиасы  в канонические.
    Переписывает department_ids у персон, проигравших убирает из реестра."""
    depts = registry.departments
    name_to_id = {normalize(d.name_en): did for did, d in depts.items()}
    uf = UnionFind()
    for did in depts:
        uf.find(did)
    for did, d in depts.items():
        for variant in d.name_variants:
            other = name_to_id.get(normalize(variant))
            if other and other != did:
                uf.union(did, other)

    remap: dict[str, str] = {}
    merged = 0
    for members in uf.groups().values():
        if len(members) < 2:
            continue
        canonical = max(members, key=lambda x: (len(depts[x].name_variants), x))
        canon = depts[canonical]
        known = {normalize(v) for v in [canon.name_en, *canon.name_variants]}
        for loser in (x for x in members if x != canonical):
            logger.info("[dedup] департамент «%s» → «%s»", depts[loser].name_en, canon.name_en)
            for cand in [depts[loser].name_en, *depts[loser].name_variants]:
                if normalize(cand) not in known:
                    canon.name_variants.append(cand)
                    known.add(normalize(cand))
            remap[loser] = canonical
            del depts[loser]
            merged += 1

    if remap:
        for p in persons.values():
            if p.department_ids:
                p.department_ids = list(dict.fromkeys(
                    remap.get(d, d) for d in p.department_ids))
    return merged


def dedup_persons(persons: dict[str, PipelinePerson]) -> int:
    """Сливает дубли персон."""
    by_name: dict[str, list[PipelinePerson]] = {}
    for p in persons.values():
        key = normalize(p.name_en or "")
        if key:
            by_name.setdefault(key, []).append(p)

    merged = 0
    for rows in by_name.values():
        if len(rows) < 2:
            continue
        canonical = max(rows, key=lambda r: (bool(r.github), len(r.name_variants), r.id))
        for loser in (r for r in rows if r.id != canonical.id):
            logger.info("[dedup] персона «%s»: %s → %s", canonical.name_en, loser.id, canonical.id)
            canonical.name_variants = list(dict.fromkeys(
                canonical.name_variants + loser.name_variants))
            seen_pubs = {a.publication_id for a in canonical.authored}
            canonical.authored += [a for a in loser.authored if a.publication_id not in seen_pubs]
            seen_repos = {c.repository_id for c in canonical.contributed_to}
            canonical.contributed_to += [c for c in loser.contributed_to
                                         if c.repository_id not in seen_repos]
            canonical.department_ids = list(dict.fromkeys(
                canonical.department_ids + loser.department_ids))
            for field in ("github", "email", "google_scholar", "openreview", "orcid",
                          "surname_ru", "first_name_ru", "second_name_ru"):
                if not getattr(canonical, field) and getattr(loser, field):
                    setattr(canonical, field, getattr(loser, field))
            del persons[loser.id]
            merged += 1
    return merged


def finalize_emails(persons: dict[str, PipelinePerson]) -> int:
    """Выбор лучшего email из кандидатов всех источников."""
    filled = 0
    for p in persons.values():
        if p.email or not p.email_candidates:
            continue
        best = pick_email(p.email_candidates)
        if best:
            p.email = best
            filled += 1
    logger.info("[finalize_emails] заполнено: %d", filled)
    return filled

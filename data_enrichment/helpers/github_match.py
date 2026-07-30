"""Скоринг сопоставления github логинов с персонами."""
from __future__ import annotations

import re
from difflib import SequenceMatcher

NAME_EXACT = 0.999

NAME_FUZZY = 0.86

def name_sim(a: str, b: str) -> float:
    """Сходство двух нормализованных имён: равенство множеств токенов или ratio."""
    if not a or not b:
        return 0.0
    ta, tb = set(a.split()), set(b.split())
    if len(ta) >= 2 and ta == tb:
        return 1.0

    return SequenceMatcher(None, " ".join(sorted(ta)), " ".join(sorted(tb))).ratio()

def best_name_sim(cands: set[str], persons: set[str]) -> tuple[float, tuple[str, str]]:
    best, pair = 0.0, ("", "")
    for c in cands:
        for p in persons:
            s = name_sim(c, p)
            if s > best:
                best, pair = s, (c, p)
    return best, pair

def login_has_surname(login: str, surname: str | None) -> bool:
    """Логин содержит фамилию персоны"""
    if not surname:
        return False
    low = login.lower()
    return surname in low or SequenceMatcher(None, low, surname).ratio() >= 0.85

def repo_owner(url: str | None) -> str | None:
    m = re.search(r"github\.com/([^/]+)/", url or "")
    return m.group(1).lower() if m else None

def score_person(cand: dict, person: dict, email_hit: bool):
    """Возвращает (score, signals, evidence) для пары логин<->персона."""
    signals: list[str] = []
    evidence: dict = {}

    if email_hit:
        signals.append("email_exact")
        evidence["email"] = sorted(cand["emails"] & person["emails"])

    sim, pair = best_name_sim(cand["names"], person["names"])
    if sim >= NAME_EXACT:
        signals.append("name_exact")
    elif sim >= NAME_FUZZY:
        signals.append("name_fuzzy")
    if sim >= NAME_FUZZY:
        evidence["name_pair"], evidence["name_sim"] = pair, round(sim, 2)

    itmo_email = any(e.endswith("@itmo.ru") for e in cand["emails"])
    if cand["itmo_text"]:
        signals.append("itmo_profile")
    if itmo_email:
        signals.append("itmo_email")

    if login_has_surname(cand["login"], person.get("surname")):
        signals.append("login_surname")
    if cand["is_owner"]:
        signals.append("owner")
    if cand["org_itmo"]:
        signals.append("org_itmo")

    weights = {"email_exact": 1.0, "name_exact": 0.6, "name_fuzzy": 0.4,
               "itmo_profile": 0.2, "itmo_email": 0.3, "login_surname": 0.3,
               "owner": 0.3, "org_itmo": 0.3}
    score = min(1.0, sum(weights[s] for s in signals))
    return score, signals, evidence

def decide(signals: list[str], in_bridge: bool) -> str:
    if "email_exact" in signals:
        return "matched"
    corrob = any(s in signals for s in
                 ("itmo_profile", "itmo_email", "login_surname", "owner", "org_itmo"))
    if "name_exact" in signals:
        if in_bridge:
            return "matched"
        return "matched" if corrob else "review"
    if "name_fuzzy" in signals:
        if in_bridge and corrob:
            return "matched"
        if in_bridge:
            return "review"
        return "rejected"
    return "rejected"

def match_login(login, cand, persons, email_index, name_index, bridge):
    """Лучшая персона для одного логина: (person_id, score, signals, evidence)."""
    bridge_pids = set()
    for pub in cand["pub_ids"]:
        bridge_pids |= bridge.get(pub, set())

    email_pids = {email_index[e] for e in cand["emails"] if e in email_index}

    global_pids = set()
    for n in cand["names"]:
        if len(n.split()) >= 2:
            global_pids |= name_index.get(n, set())

    best = None
    matched = []
    for pid in bridge_pids | email_pids | global_pids:
        if pid not in persons:
            continue
        in_bridge = pid in bridge_pids
        score, signals, evidence = score_person(cand, persons[pid], pid in email_pids)
        d = decide(signals, in_bridge)
        if d == "rejected":
            continue
        evidence["in_bridge"] = in_bridge
        rank = (d == "matched", score)
        if best is None or rank > best[0]:
            best = (rank, pid, score, signals, evidence, d)
        if d == "matched":
            matched.append((score, pid))
    if best is None:
        return None
    _, pid, score, signals, evidence, d = best
    if d == "matched":
        top = max(s for s, _ in matched)
        if len({p for s, p in matched if s == top}) > 1:
            d = "review"
            evidence["ambiguous"] = True
    return pid, score, signals, evidence, d

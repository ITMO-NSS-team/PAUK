import argparse
import json
import re
import sqlite3
import unicodedata
from difflib import SequenceMatcher

from config import DB_PATH, PROFILES_DB_PATH

NAME_EXACT = 0.999
NAME_FUZZY = 0.86

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS github_matches (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    person_id    TEXT,
    person_name  TEXT,
    github_login TEXT,
    github_url   TEXT,
    score        REAL,
    signals      TEXT,   -- JSON [str]
    evidence     TEXT,   -- JSON {email, name_pair, name_sim}
    decision     TEXT,   -- matched | review | rejected
    repos        TEXT,   -- JSON [repo_url]
    matched_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(person_id, github_login)
);
CREATE INDEX IF NOT EXISTS idx_ghmatch_login    ON github_matches(github_login);
CREATE INDEX IF NOT EXISTS idx_ghmatch_decision ON github_matches(decision);
"""


# --- Нормализация имён/почт ---------------------------------------------


def strip_accents(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))


def norm_name(s: str | None) -> str:
    if not s:
        return ""
    s = strip_accents(s).lower()
    s = re.sub(r"[^a-z0-9\s]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


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


def norm_email(e: str | None) -> str:
    return (e or "").strip().lower()


def jloads(s, default):
    try:
        return json.loads(s) if s else default
    except (TypeError, ValueError):
        return default


# --- Загрузка данных -----------------------------------------------------


def load_persons(main: sqlite3.Connection, prof: sqlite3.Connection):
    """person_id -> {names:set, emails:set, name_en}. + индекс email -> person_id."""
    persons: dict[str, dict] = {}
    for pid, name_en, variants, email in main.execute(
        "SELECT id, name_en, name_variants, email FROM persons_itmo"
    ):
        names = {norm_name(name_en)} if name_en else set()
        for v in jloads(variants, []):
            names.add(norm_name(v))
        emails = {norm_email(email)} if email else set()
        toks = norm_name(name_en).split()
        surname = toks[-1] if toks and len(toks[-1]) >= 4 else None
        persons[pid] = {"names": {n for n in names if n}, "emails": {e for e in emails if e},
                        "name_en": name_en, "surname": surname}

    for pid, emails_j, other_j in prof.execute(
        "SELECT person_id, emails, other_names FROM person_profiles"
    ):
        if pid not in persons:
            continue
        for e in jloads(emails_j, []):
            persons[pid]["emails"].add(norm_email(e))
        for o in jloads(other_j, []):
            persons[pid]["names"].add(norm_name(o))
        persons[pid]["emails"].discard("")
        persons[pid]["names"].discard("")

    email_index: dict[str, str] = {}
    name_index: dict[str, set[str]] = {}
    for pid, p in persons.items():
        for e in p["emails"]:
            email_index.setdefault(e, pid)
        for n in p["names"]:
            if len(n.split()) >= 2:
                name_index.setdefault(n, set()).add(pid)
    return persons, email_index, name_index


def load_pub_authors(main: sqlite3.Connection) -> dict[str, set[str]]:
    """publication_id -> {itmo-person_id}."""
    bridge: dict[str, set[str]] = {}
    for pub_id, pid in main.execute(
        "SELECT publication_id, person_id FROM publication_authors WHERE person_type='itmo'"
    ):
        bridge.setdefault(pub_id, set()).add(pid)
    return bridge


def load_candidates(prof: sqlite3.Connection) -> dict[str, dict]:
    """github_login -> агрегат по всем его репозиториям."""
    logins: dict[str, dict] = {}
    rows = prof.execute(
        """
        SELECT github_login, github_url, source, repo_url, publication_ids,
               gh_name, gh_email, gh_company, gh_location, commit_emails, commit_names
        FROM github_candidates
        """
    ).fetchall()
    for (login, url, source, repo_url, pub_ids_j, gh_name, gh_email,
         gh_company, gh_location, commit_emails_j, commit_names_j) in rows:
        c = logins.setdefault(login, {
            "login": login, "url": url, "names": set(), "emails": set(),
            "itmo_text": False, "pub_ids": set(), "repos": set(), "is_owner": False,
        })
        c["url"] = url or c["url"]
        c["repos"].add(repo_url)
        c["pub_ids"].update(jloads(pub_ids_j, []))
        if source == "repo_owner":
            c["is_owner"] = True
        for n in (gh_name, login, *jloads(commit_names_j, [])):
            nn = norm_name(n)
            if nn:
                c["names"].add(nn)
        for e in (gh_email, *jloads(commit_emails_j, [])):
            ee = norm_email(e)
            if ee:
                c["emails"].add(ee)
        blob = f"{gh_company or ''} {gh_location or ''}".lower()
        if "itmo" in blob or "saint petersburg" in blob or "sankt" in blob:
            c["itmo_text"] = True
    return logins


# --- Скоринг -------------------------------------------------------------


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

    weights = {"email_exact": 1.0, "name_exact": 0.6, "name_fuzzy": 0.4,
               "itmo_profile": 0.2, "itmo_email": 0.3, "login_surname": 0.3,
               "owner": 0.3}
    score = min(1.0, sum(weights[s] for s in signals))
    return score, signals, evidence


def decide(signals: list[str], in_bridge: bool) -> str:
    if "email_exact" in signals:
        return "matched"
    corrob = any(s in signals for s in
                 ("itmo_profile", "itmo_email", "login_surname", "owner"))
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


# --- Запись --------------------------------------------------------------


def save_match(prof, login, cand, pid, persons, score, signals, evidence, decision):
    prof.execute(
        """
        INSERT OR REPLACE INTO github_matches
            (person_id, person_name, github_login, github_url, score,
             signals, evidence, decision, repos)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (pid, persons[pid]["name_en"], login, cand["url"], score,
         json.dumps(signals), json.dumps(evidence, ensure_ascii=False),
         decision, json.dumps(sorted(cand["repos"]))),
    )


def apply_to_main(main, login, cand, pid):
    """Проставляет github персоне и repository_persons (только role owner/contributor)."""
    main.execute(
        "UPDATE persons_itmo SET github = ? WHERE id = ? AND (github IS NULL OR github = '')",
        (login, pid),
    )
    role = "owner" if cand["is_owner"] else "contributor"
    for repo_url in cand["repos"]:
        row = main.execute("SELECT id FROM repositories WHERE url = ?", (repo_url,)).fetchone()
        if row:
            main.execute(
                "INSERT OR IGNORE INTO repository_persons (repository_id, person_id, role) VALUES (?, ?, ?)",
                (row[0], pid, role),
            )


# --- Драйвер -------------------------------------------------------------


def run(apply: bool) -> None:
    main = sqlite3.connect(DB_PATH, timeout=30)
    main.execute("PRAGMA foreign_keys = ON")
    prof = sqlite3.connect(PROFILES_DB_PATH, timeout=30)
    prof.executescript(SCHEMA_SQL)

    persons, email_index, name_index = load_persons(main, prof)
    bridge = load_pub_authors(main)
    logins = load_candidates(prof)

    print(f"Кандидатов-логинов: {len(logins)}; персон ИТМО: {len(persons)}\n")
    stats = {"matched": 0, "review": 0, "rejected": 0, "no_target": 0, "new_github": 0}

    prof.execute("DELETE FROM github_matches")
    for login, cand in sorted(logins.items()):
        result = match_login(login, cand, persons, email_index, name_index, bridge)
        if result is None:
            stats["no_target"] += 1
            continue
        pid, score, signals, evidence, decision = result
        save_match(prof, login, cand, pid, persons, score, signals, evidence, decision)
        stats[decision] += 1
        if decision == "matched":
            already = persons[pid]["name_en"]
            print(f"  [{decision}] {login:22} -> {already:28} {','.join(signals)}")
            if apply:
                had = main.execute(
                    "SELECT github FROM persons_itmo WHERE id = ?", (pid,)
                ).fetchone()
                if not (had and had[0]):
                    stats["new_github"] += 1
                apply_to_main(main, login, cand, pid)
        elif decision == "review":
            print(f"  [review ] {login:22} -> {persons[pid]['name_en']:28} {evidence.get('name_sim')}")

    prof.commit()
    if apply:
        main.commit()
    prof.close()
    main.close()

    print()
    print("Итог матчинга")
    print("-" * 40)
    print(f"  matched:            {stats['matched']}")
    print(f"  review:             {stats['review']}")
    print(f"  rejected:           {stats['rejected']}")
    print(f"  без цели (no_target):{stats['no_target']}")
    if apply:
        print(f"  новых github в persons_itmo: {stats['new_github']}")
    else:
        print("  (--apply не задан: основная БД не менялась)")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Сводит GitHub-логины с сотрудниками ИТМО.")
    parser.add_argument("--apply", action="store_true",
                        help="Проставить matched-привязки в persons_itmo и repository_persons.")
    return parser.parse_args()


def main() -> None:
    run(parse_args().apply)


if __name__ == "__main__":
    main()

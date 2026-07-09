import argparse
import json
import logging
import re
import sqlite3

from config import DB_PATH, SQLITE_TIMEOUT

logger = logging.getLogger(__name__)

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")


def jloads(s):
    try:
        return json.loads(s) if s else []
    except (TypeError, ValueError):
        return []


def tail(v: str | None) -> str | None:
    return v.rstrip("/").split("/")[-1] if v else None


def scholar_url(researcher_urls: str | None) -> str | None:
    for u in jloads(researcher_urls):
        if "scholar.google" in (u.get("url") or ""):
            return u["url"]
    return None


def gitlab_url(researcher_urls: str | None) -> str | None:
    for u in jloads(researcher_urls):
        if "gitlab.com" in (u.get("url") or "").lower():
            return u["url"]
    return None


def usable_email(e: str | None) -> str | None:
    e = (e or "").strip().lower()
    return e if e and "@" in e and "noreply" not in e else None


def best_github_email(prof: sqlite3.Connection, login: str) -> str | None:
    for gh_email, commit_emails, gh_blog in prof.execute(
        "SELECT gh_email, commit_emails, gh_blog FROM github_candidates WHERE github_login = ?", (login,)
    ):
        cands = ([gh_email] if gh_email else []) + jloads(commit_emails)
        m = EMAIL_RE.search(gh_blog or "")   # в поле blog иногда лежит почта
        if m:
            cands.append(m.group(0))
        for e in cands:
            u = usable_email(e)
            if u:
                return u
    return None


def pick_email(cands: list[tuple[str, str]]) -> str | None:
    """Из кандидатов (src, email) — институциональный, иначе по порядку источника."""
    inst = [e for _, e in cands if e.endswith("@itmo.ru") or e.endswith("ifmo.ru")]
    if inst:
        return inst[0]
    order = {"orcid": 0, "page": 1, "pdf": 2, "commit": 3}
    return min(cands, key=lambda x: order.get(x[0], 9))[1] if cands else None


def collect(prof: sqlite3.Connection):
    """Кандидаты по полям из всех таблиц-источников."""
    email: dict[str, list] = {}
    github: dict[str, list] = {}
    scholar: dict[str, list] = {}
    openrev: dict[str, list] = {}
    linkedin: dict[str, list] = {}
    gitlab: dict[str, list] = {}

    for pid, emails_j, gh_j, ru_j, ln in prof.execute(
        "SELECT person_id, emails, github_urls, researcher_urls, linkedin FROM person_profiles"
    ):
        for e in jloads(emails_j):
            u = usable_email(e)
            if u:
                email.setdefault(pid, []).append(("orcid", u))
        urls = jloads(gh_j)
        if urls:
            github.setdefault(pid, []).append(tail(urls[0]))
        su = scholar_url(ru_j)
        if su:
            scholar.setdefault(pid, []).append(su)
        if ln:
            linkedin.setdefault(pid, []).append(ln)
        gl = gitlab_url(ru_j)
        if gl:
            gitlab.setdefault(pid, []).append(gl)

    for pid, em, source in prof.execute("SELECT person_id, email, source FROM collected_emails"):
        u = usable_email(em)
        if u:
            email.setdefault(pid, []).append((source, u))

    for pid, login in prof.execute(
        "SELECT DISTINCT person_id, github_login FROM github_matches WHERE decision = 'matched'"
    ):
        em = best_github_email(prof, login)
        if em:
            email.setdefault(pid, []).append(("commit", em))

    for pid, oid, ghl, gsc, ln in prof.execute(
        "SELECT person_id, openreview_id, github, gscholar, linkedin FROM openreview_profiles"
    ):
        if oid:
            openrev.setdefault(pid, []).append(oid)
        if ghl:
            github.setdefault(pid, []).append(tail(ghl))
        if gsc:
            scholar.setdefault(pid, []).append(gsc)
        if ln:
            linkedin.setdefault(pid, []).append(ln)

    return email, github, scholar, openrev, linkedin, gitlab


def run(apply: bool) -> None:
    main = sqlite3.connect(DB_PATH, timeout=SQLITE_TIMEOUT)
    prof = sqlite3.connect(DB_PATH, timeout=SQLITE_TIMEOUT)
    email, github, scholar, openrev, linkedin, gitlab = collect(prof)

    for col in ("linkedin", "gitlab"):
        try:
            main.execute(f"ALTER TABLE persons_itmo ADD COLUMN {col} TEXT")
        except sqlite3.OperationalError:
            pass

    stats = {"email": 0, "github": 0, "google_scholar": 0, "openreview": 0,
             "linkedin": 0, "gitlab": 0}
    rows = main.execute(
        "SELECT id, email, github, google_scholar, openreview, linkedin, gitlab FROM persons_itmo"
    ).fetchall()
    for pid, cur_e, cur_g, cur_s, cur_o, cur_ln, cur_gl in rows:
        updates = {}
        if not cur_e:
            e = pick_email(email.get(pid, []))
            if e:
                updates["email"] = e
        if not cur_g and github.get(pid):
            updates["github"] = github[pid][0]
        if not cur_s and scholar.get(pid):
            updates["google_scholar"] = scholar[pid][0]
        if not cur_o and openrev.get(pid):
            updates["openreview"] = openrev[pid][0]
        if not cur_ln and linkedin.get(pid):
            updates["linkedin"] = linkedin[pid][0]
        if not cur_gl and gitlab.get(pid):
            updates["gitlab"] = gitlab[pid][0]
        for k in updates:
            stats[k] += 1
        if apply and updates:
            sets = ", ".join(f"{k} = ?" for k in updates)
            main.execute(f"UPDATE persons_itmo SET {sets} WHERE id = ?", (*updates.values(), pid))

    if apply:
        main.commit()
    main.close()
    prof.close()

    logger.info("Заполнено:")
    for k in ("email", "github", "google_scholar", "openreview", "linkedin", "gitlab"):
        logger.info("%s %d", k, stats[k])
    if not apply:
        logger.info("--apply не задан: основная БД не менялась")


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description="Сборка финальной БД из всех источников.")
    parser.add_argument("--apply", action="store_true", help="Записать в persons_itmo.")
    run(parser.parse_args().apply)


if __name__ == "__main__":
    main()

import argparse
import json
import sqlite3

from config import DB_PATH, PROFILES_DB_PATH


def jloads(s):
    try:
        return json.loads(s) if s else []
    except (TypeError, ValueError):
        return []


def scholar_url(researcher_urls: str | None) -> str | None:
    for u in jloads(researcher_urls):
        url = u.get("url") or ""
        if "scholar.google" in url:
            return url
    return None


def github_login(github_urls: str | None) -> str | None:
    urls = jloads(github_urls)
    return urls[0].rstrip("/").split("/")[-1] if urls else None


def best_github_email(prof: sqlite3.Connection, login: str) -> str | None:
    """Первый реальный email логина из профиля/коммитов."""
    for gh_email, commit_emails in prof.execute(
        "SELECT gh_email, commit_emails FROM github_candidates WHERE github_login = ?", (login,)
    ):
        for e in ([gh_email] if gh_email else []) + jloads(commit_emails):
            e = (e or "").strip().lower()
            if e and "@" in e and "noreply" not in e:
                return e
    return None


def run(apply: bool) -> None:
    main = sqlite3.connect(DB_PATH, timeout=30)
    prof = sqlite3.connect(PROFILES_DB_PATH, timeout=30)
    rows = prof.execute(
        "SELECT person_id, emails, github_urls, researcher_urls FROM person_profiles"
    ).fetchall()

    stats = {"email": 0, "github": 0, "google_scholar": 0, "email_github": 0}
    email_filled = set()
    for pid, emails_j, gh_j, ru_j in rows:
        cur = main.execute(
            "SELECT email, github, google_scholar FROM persons_itmo WHERE id = ?", (pid,)
        ).fetchone()
        if not cur:
            continue
        email, github, scholar = cur

        updates = {}
        emails = jloads(emails_j)
        if not email and emails:
            updates["email"] = emails[0]
            email_filled.add(pid)
        if not github:
            gl = github_login(gh_j)
            if gl:
                updates["github"] = gl
        if not scholar:
            su = scholar_url(ru_j)
            if su:
                updates["google_scholar"] = su

        for k in updates:
            stats[k] += 1
        if apply and updates:
            sets = ", ".join(f"{k} = ?" for k in updates)
            main.execute(
                f"UPDATE persons_itmo SET {sets} WHERE id = ?", (*updates.values(), pid)
            )

    matched = prof.execute(
        "SELECT DISTINCT person_id, github_login FROM github_matches WHERE decision = 'matched'"
    ).fetchall()
    for pid, login in matched:
        if pid in email_filled:
            continue
        cur = main.execute("SELECT email FROM persons_itmo WHERE id = ?", (pid,)).fetchone()
        if not cur or cur[0]:
            continue
        em = best_github_email(prof, login)
        if em:
            stats["email_github"] += 1
            email_filled.add(pid)
            if apply:
                main.execute("UPDATE persons_itmo SET email = ? WHERE id = ?", (em, pid))

    if apply:
        main.commit()

    print("Заполнено из профилей:")
    print(f"  email (ORCID):   {stats['email']}")
    print(f"  email (GitHub):  {stats['email_github']}")
    print(f"  github:          {stats['github']}")
    print(f"  google_scholar:  {stats['google_scholar']}")
    if not apply:
        print("\n(--apply не задан: основная БД не менялась)")


def main() -> None:
    parser = argparse.ArgumentParser(description="Заливает профили в persons_itmo.")
    parser.add_argument("--apply", action="store_true", help="Записать в основную БД.")
    run(parser.parse_args().apply)


if __name__ == "__main__":
    main()

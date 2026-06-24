import argparse
import sqlite3
import time

from config import DB_PATH, GITHUB_REQUEST_DELAY, PROFILES_DB_PATH
from github_harvest import SCHEMA_SQL, GitHubHarvester


def load_seeds(main_db: str) -> list[tuple[str, str]]:
    """Сиды: сначала ИТМО org, затем личные аккаунты."""
    main = sqlite3.connect(main_db)
    orgs = [r[0] for r in main.execute("SELECT github_login FROM github_departments")]
    users = [r[0] for r in main.execute(
        "SELECT DISTINCT github FROM persons_itmo WHERE github > ''")]
    main.close()
    seen, seeds = set(), []
    for login, kind in [(o, "org") for o in orgs] + [(u, "user") for u in users]:
        if login and login.lower() not in seen:
            seen.add(login.lower())
            seeds.append((login, kind))
    return seeds


def list_owned_repos(gh, owner: str, max_repos: int) -> list[str]:
    """Публичные не-форк репозитории аккаунта url, до max_repos, свежие первыми."""
    urls = []
    for page in range(1, 11):
        data = gh.get(f"/users/{owner}/repos",
                      params={"per_page": 100, "page": page, "type": "owner", "sort": "updated"})
        time.sleep(GITHUB_REQUEST_DELAY)
        if not data:
            break
        for r in data:
            if not r.get("fork") and r.get("html_url"):
                urls.append(r["html_url"])
        if len(data) < 100 or len(urls) >= max_repos:
            break
    return urls[:max_repos]


def run(limit: int, max_repos: int, refresh: bool, commit_pages: int) -> None:
    h = GitHubHarvester(DB_PATH, PROFILES_DB_PATH, limit=None,
                        refresh=refresh, commit_pages=commit_pages)
    out = sqlite3.connect(PROFILES_DB_PATH, timeout=30)
    out.executescript(SCHEMA_SQL)
    done = {r[0] for r in out.execute("SELECT DISTINCT repo_url FROM github_candidates")}

    seeds = load_seeds(DB_PATH)
    if limit:
        seeds = seeds[:limit]
    print(f"Сидов: {len(seeds)} (ИТМО org + подтверждённые аккаунты)\n")

    stats = {"seeds": 0, "repos": 0, "candidates": 0}
    try:
        for i, (login, kind) in enumerate(seeds, 1):
            stats["seeds"] += 1
            repos = list_owned_repos(h.gh, login, max_repos)
            fresh = [u for u in repos if refresh or u not in done]
            print(f"  [{i}/{len(seeds)}] {kind:4} {login:24} репо:{len(repos):3} новых:{len(fresh)}")
            for repo_url in fresh:
                cands = h.harvest_repo(repo_url, [])
                h.save(out, repo_url, cands)
                done.add(repo_url)
                stats["repos"] += 1
                stats["candidates"] += len(cands)
                if stats["repos"] % 20 == 0:
                    out.commit()
        out.commit()
    except KeyboardInterrupt:
        print("\nПрервано, коммичу что успели.")
        out.commit()
    finally:
        print(f"\nСидов: {stats['seeds']}  репо: {stats['repos']}  "
              f"кандидатов: {stats['candidates']}  запросов GitHub: {h.gh.calls}")
        out.close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Расширение охвата по графу GitHub.")
    parser.add_argument("--limit", type=int, default=None, help="Сколько сидов взять.")
    parser.add_argument("--max-repos", type=int, default=30, help="Репо на сид.")
    parser.add_argument("--refresh", action="store_true", help="Пересобрать всё.")
    parser.add_argument("--commit-pages", type=int, default=0,
                        help="Страниц коммитов на репо (0 — без коммитов).")
    return parser.parse_args()


def main() -> None:
    a = parse_args()
    run(a.limit, a.max_repos, a.refresh, a.commit_pages)


if __name__ == "__main__":
    main()

import argparse
import json
import sqlite3
import time
from urllib.parse import urlparse

import requests
from config import (
    DB_PATH,
    GITHUB_API_URL,
    GITHUB_COMMIT_PAGES,
    GITHUB_REQUEST_DELAY,
    GITHUB_TOKEN,
    HTTP_TIMEOUT,
    MAX_ACCOUNT_REPO_PAGES,
    SQLITE_TIMEOUT,
    USER_AGENT,
)

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS github_candidates (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    github_login    TEXT NOT NULL,
    github_url      TEXT,
    user_type       TEXT,        -- User | Organization | Bot (из GitHub)
    source          TEXT,        -- repo_owner | repo_contributor
    repo_url        TEXT,        -- канонический https://github.com/owner/repo
    publication_ids TEXT,        -- JSON [publication_id] — мост к ИТМО-авторам
    gh_name         TEXT,
    gh_email        TEXT,
    gh_company      TEXT,
    gh_location     TEXT,
    gh_bio          TEXT,
    gh_blog         TEXT,
    gh_twitter      TEXT,
    commit_emails   TEXT,        -- JSON [email] из коммитов этого логина в репо
    commit_names    TEXT,        -- JSON [name]
    harvested_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(github_login, repo_url)
);

CREATE INDEX IF NOT EXISTS idx_ghcand_login ON github_candidates(github_login);
CREATE INDEX IF NOT EXISTS idx_ghcand_repo  ON github_candidates(repo_url);
"""

BOT_LOGINS = {"web-flow", "github-actions"}


def parse_repo_url(url: str) -> tuple[str, str] | None:
    """Из https://github.com/owner/repo достаёт (owner, repo). Иначе None."""
    try:
        parsed = urlparse(url)
    except ValueError:
        return None
    if "github.com" not in parsed.netloc.lower():
        return None
    segments = [s for s in parsed.path.split("/") if s]
    if len(segments) < 2:
        return None
    return segments[0], segments[1].removesuffix(".git")


def is_bot(login: str | None, user_type: str | None = None) -> bool:
    if not login:
        return True
    low = login.lower()
    return low in BOT_LOGINS or low.endswith("[bot]") or user_type == "Bot"


class GitHubClient:
    """Тонкий клиент GitHub REST API с учётом rate-limit."""

    def __init__(self) -> None:
        self.session = requests.Session()
        headers = {
            "User-Agent": USER_AGENT,
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if GITHUB_TOKEN:
            headers["Authorization"] = f"Bearer {GITHUB_TOKEN}"
        self.session.headers.update(headers)
        self.calls = 0

    def get(self, path: str, params: dict | None = None, retries: int = 3):
        """GET к GitHub API. Возвращает распарсенный JSON или None (404/ошибка)."""
        url = path if path.startswith("http") else f"{GITHUB_API_URL}{path}"
        self.calls += 1
        try:
            resp = self.session.get(url, params=params, timeout=HTTP_TIMEOUT)
        except requests.RequestException as exc:
            print(f"  запрос упал {url}: {exc}")
            return None

        if resp.status_code == 200:
            try:
                return resp.json()
            except ValueError:
                return None
        if resp.status_code == 404:
            return None
        # Исчерпан основной или вторичный rate-limit.
        if resp.status_code in (403, 429) and retries > 0:
            remaining = resp.headers.get("X-RateLimit-Remaining")
            if remaining == "0":
                reset = int(resp.headers.get("X-RateLimit-Reset", "0"))
                wait = max(0, reset - int(time.time())) + 2
                print(f"  rate-limit исчерпан, sleep {wait} сек")
                time.sleep(min(wait, 3600))
            else:  # вторичный лимит — Retry-After или дефолт
                wait = int(resp.headers.get("Retry-After", "60"))
                print(f"  вторичный лимит, sleep {wait} сек")
                time.sleep(wait)
            return self.get(path, params, retries - 1)
        print(f"  {resp.status_code} для {url}")
        return None


class GitHubHarvester:
    """Тянет с GitHub кандидатов-логины и сигналы по авторским репозиториям.

    Режимы сидов:
      repos (по умолчанию) — подтверждённые авторские репо из repo_links.
      accounts: репозитории ИТМО-организаций и уже подтверждённых
                 личных аккаунтов.
    """

    def __init__(self, source_db, out_db, limit, refresh, commit_pages,
                 mode="repos", max_repos_per_account=30) -> None:
        self.source_db = source_db
        self.out_db = out_db
        self.limit = limit
        self.refresh = refresh
        self.commit_pages = commit_pages
        self.mode = mode
        self.max_repos_per_account = max_repos_per_account
        self.gh = GitHubClient()
        self.profile_cache: dict[str, dict | None] = {}
        self.stats = {"repos": 0, "candidates": 0, "owners": 0,
                      "contributors": 0, "skipped_bots": 0, "seeds": 0}

    # --- Вход: режим repos -------------------------------------------------

    def load_repos(self, done_urls: set[str]) -> list[tuple[str, list[str]]]:
        """Подтверждённые github-репо: [(repo_url, [publication_id, ...])]."""
        conn = sqlite3.connect(self.source_db, timeout=SQLITE_TIMEOUT)
        rows = conn.execute(
            """
            SELECT url, publication_id FROM repo_links
            WHERE is_relevant = 1 AND host = 'github.com'
            ORDER BY url
            """
        ).fetchall()
        conn.close()

        by_url: dict[str, list[str]] = {}
        for url, pub_id in rows:
            by_url.setdefault(url, [])
            if pub_id and pub_id not in by_url[url]:
                by_url[url].append(pub_id)

        repos = []
        for url, pubs in by_url.items():
            if not self.refresh and url in done_urls:
                continue
            repos.append((url, pubs))
            if self.limit and len(repos) >= self.limit:
                break
        return repos

    # --- Вход: режим accounts (соцграф) -------------------------------------

    def load_seeds(self) -> list[tuple[str, str]]:
        """Сиды: сначала ИТМО-организации, затем подтверждённые личные аккаунты."""
        conn = sqlite3.connect(self.source_db, timeout=SQLITE_TIMEOUT)
        orgs = [r[0] for r in conn.execute("SELECT github_login FROM github_departments")]
        users = [r[0] for r in conn.execute(
            "SELECT DISTINCT github FROM persons_itmo WHERE github > ''")]
        conn.close()
        seen, seeds = set(), []
        for login, kind in [(o, "org") for o in orgs] + [(u, "user") for u in users]:
            if login and login.lower() not in seen:
                seen.add(login.lower())
                seeds.append((login, kind))
        if self.limit:
            seeds = seeds[: self.limit]
        return seeds

    def list_owned_repos(self, owner: str) -> list[str]:
        """Публичные не-форк репозитории аккаунта (url), свежие первыми."""
        urls = []
        for page in range(1, MAX_ACCOUNT_REPO_PAGES + 1):
            data = self.gh.get(
                f"/users/{owner}/repos",
                params={"per_page": 100, "page": page, "type": "owner", "sort": "updated"},
            )
            time.sleep(GITHUB_REQUEST_DELAY)
            if not data:
                break
            for r in data:
                if not r.get("fork") and r.get("html_url"):
                    urls.append(r["html_url"])
            if len(data) < 100 or len(urls) >= self.max_repos_per_account:
                break
        return urls[: self.max_repos_per_account]

    # --- Сбор по одному репо --------------------------------------------

    def fetch_profile(self, login: str) -> dict | None:
        """GET /users/{login} с кэшем. Боты/организации тоже вернутся."""
        if login in self.profile_cache:
            return self.profile_cache[login]
        data = self.gh.get(f"/users/{login}")
        time.sleep(GITHUB_REQUEST_DELAY)
        self.profile_cache[login] = data
        return data

    def mine_commit_identities(self, owner: str, repo: str) -> dict[str, dict]:
        """login -> {emails:set, names:set} из коммитов (git-identity автора)."""
        identities: dict[str, dict] = {}
        for page in range(1, self.commit_pages + 1):
            commits = self.gh.get(
                f"/repos/{owner}/{repo}/commits",
                params={"per_page": 100, "page": page},
            )
            time.sleep(GITHUB_REQUEST_DELAY)
            if not commits:
                break
            for c in commits:
                login = ((c.get("author") or {}).get("login"))
                git_author = (c.get("commit") or {}).get("author") or {}
                if not login:
                    continue
                slot = identities.setdefault(login, {"emails": set(), "names": set()})
                if git_author.get("email"):
                    slot["emails"].add(git_author["email"])
                if git_author.get("name"):
                    slot["names"].add(git_author["name"])
            if len(commits) < 100:
                break
        return identities

    def build_candidate(self, login, source, repo_url, pubs, user_type, commit_id):
        """Собирает строку-кандидата: профиль с GitHub + коммит-идентичности."""
        profile = self.fetch_profile(login) or {}
        ident = commit_id.get(login, {"emails": set(), "names": set()})
        return {
            "github_login": login,
            "github_url": profile.get("html_url") or f"https://github.com/{login}",
            "user_type": profile.get("type") or user_type,
            "source": source,
            "repo_url": repo_url,
            "publication_ids": json.dumps(pubs, ensure_ascii=False),
            "gh_name": profile.get("name"),
            "gh_email": profile.get("email"),
            "gh_company": profile.get("company"),
            "gh_location": profile.get("location"),
            "gh_bio": profile.get("bio"),
            "gh_blog": profile.get("blog") or None,
            "gh_twitter": profile.get("twitter_username"),
            "commit_emails": json.dumps(sorted(ident["emails"]), ensure_ascii=False),
            "commit_names": json.dumps(sorted(ident["names"]), ensure_ascii=False),
        }

    def harvest_repo(self, repo_url: str, pubs: list[str]) -> list[dict]:
        """Возвращает список кандидатов-словарей по одному репозиторию."""
        parsed = parse_repo_url(repo_url)
        if not parsed:
            return []
        owner, repo = parsed

        meta = self.gh.get(f"/repos/{owner}/{repo}")
        time.sleep(GITHUB_REQUEST_DELAY)
        if not meta:
            return []
        owner_login = (meta.get("owner") or {}).get("login")
        owner_type = (meta.get("owner") or {}).get("type")

        contributors = self.gh.get(
            f"/repos/{owner}/{repo}/contributors", params={"per_page": 100}
        ) or []
        time.sleep(GITHUB_REQUEST_DELAY)

        commit_id = self.mine_commit_identities(owner, repo) if self.commit_pages else {}

        # source по логину: owner важнее contributor.
        roles: dict[str, str] = {}
        if owner_login and owner_type == "User":
            roles[owner_login] = "repo_owner"
        for c in contributors:
            login = c.get("login")
            roles.setdefault(login, "repo_contributor")

        candidates = []
        for login, source in roles.items():
            if is_bot(login):
                self.stats["skipped_bots"] += 1
                continue
            cand = self.build_candidate(
                login, source, repo_url, pubs, owner_type, commit_id
            )
            if cand["user_type"] not in ("User", None):  # отсеять org/bot по факту
                self.stats["skipped_bots"] += 1
                continue
            candidates.append(cand)
            self.stats["owners" if source == "repo_owner" else "contributors"] += 1
        return candidates

    # --- Сохранение ------------------------------------------------------

    def save(self, out: sqlite3.Connection, repo_url: str, candidates: list[dict]) -> None:
        out.execute("DELETE FROM github_candidates WHERE repo_url = ?", (repo_url,))
        for c in candidates:
            out.execute(
                """
                INSERT OR IGNORE INTO github_candidates (
                    github_login, github_url, user_type, source, repo_url,
                    publication_ids, gh_name, gh_email, gh_company, gh_location,
                    gh_bio, gh_blog, gh_twitter, commit_emails, commit_names
                ) VALUES (
                    :github_login, :github_url, :user_type, :source, :repo_url,
                    :publication_ids, :gh_name, :gh_email, :gh_company, :gh_location,
                    :gh_bio, :gh_blog, :gh_twitter, :commit_emails, :commit_names
                )
                """,
                c,
            )

    # --- Драйвер ---------------------------------------------------------

    def run(self) -> None:
        if not GITHUB_TOKEN:
            print("GITHUB_TOKEN не задан в .env — лимит 60 запросов/час. "
                  "Создай токен: https://github.com/settings/tokens")

        out = sqlite3.connect(self.out_db, timeout=SQLITE_TIMEOUT)
        out.executescript(SCHEMA_SQL)
        done = {r[0] for r in out.execute("SELECT DISTINCT repo_url FROM github_candidates")}

        print(f"Источник:  {self.source_db}")
        print(f"Результат: {self.out_db}")
        try:
            if self.mode == "accounts":
                self._run_accounts(out, done)
            else:
                self._run_repos(out, done)
        except KeyboardInterrupt:
            print("\nПрервано пользователем.")
            out.commit()
        finally:
            self.print_summary()
            out.close()

    def _run_repos(self, out: sqlite3.Connection, done: set[str]) -> None:
        repos = self.load_repos(done)
        print(f"К обработке репозиториев: {len(repos)} (уже собрано: {len(done)})\n")
        for i, (repo_url, pubs) in enumerate(repos, 1):
            self.stats["repos"] += 1
            candidates = self.harvest_repo(repo_url, pubs)
            self.stats["candidates"] += len(candidates)
            self.save(out, repo_url, candidates)
            logins = ", ".join(c["github_login"] for c in candidates) or "—"
            print(f"  [{i}/{len(repos)}] {repo_url:55}  {logins}")
            if self.stats["repos"] % 10 == 0:
                out.commit()
        out.commit()

    def _run_accounts(self, out: sqlite3.Connection, done: set[str]) -> None:
        seeds = self.load_seeds()
        print(f"Сидов: {len(seeds)} (ИТМО-организации + подтверждённые аккаунты)\n")
        for i, (login, kind) in enumerate(seeds, 1):
            self.stats["seeds"] += 1
            repos = self.list_owned_repos(login)
            fresh = [u for u in repos if self.refresh or u not in done]
            print(f"  [{i}/{len(seeds)}] {kind:4} {login:24} репо:{len(repos):3} новых:{len(fresh)}")
            for repo_url in fresh:
                candidates = self.harvest_repo(repo_url, [])
                self.stats["repos"] += 1
                self.stats["candidates"] += len(candidates)
                self.save(out, repo_url, candidates)
                done.add(repo_url)
                if self.stats["repos"] % 20 == 0:
                    out.commit()
        out.commit()

    def print_summary(self) -> None:
        print()
        print("Итог сбора кандидатов с GitHub")
        print("-" * 40)
        if self.mode == "accounts":
            print(f"  Сидов обработано:        {self.stats['seeds']}")
        print(f"  Репозиториев обработано: {self.stats['repos']}")
        print(f"  Кандидатов собрано:      {self.stats['candidates']}")
        print(f"    из них owner:          {self.stats['owners']}")
        print(f"    из них contributor:    {self.stats['contributors']}")
        print(f"  Пропущено ботов/орг:     {self.stats['skipped_bots']}")
        print(f"  Запросов к GitHub:       {self.gh.calls}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Собирает кандидатов-логины и сигналы с GitHub (репо или соцграф)."
    )
    parser.add_argument("--mode", choices=("repos", "accounts"), default="repos",
                        help="repos — по авторским репо; accounts — по соцграфу аккаунтов.")
    parser.add_argument("--limit", type=int, default=None,
                        help="Сколько репозиториев/сидов обработать за запуск.")
    parser.add_argument("--refresh", action="store_true",
                        help="Пересобрать всё заново.")
    parser.add_argument("--commit-pages", type=int, default=GITHUB_COMMIT_PAGES,
                        help="Страниц коммитов на репо; 0 — не трогать коммиты.")
    parser.add_argument("--max-repos-per-account", type=int, default=30,
                        help="[accounts] Сколько репо брать с одного аккаунта/орги.")
    parser.add_argument("--source-db", default=str(DB_PATH),
                        help="БД с repo_links/publications/persons_itmo.")
    parser.add_argument("--out-db", default=str(DB_PATH),
                        help="БД для результатов.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    GitHubHarvester(
        source_db=args.source_db,
        out_db=args.out_db,
        limit=args.limit,
        refresh=args.refresh,
        commit_pages=args.commit_pages,
        mode=args.mode,
        max_repos_per_account=args.max_repos_per_account,
    ).run()


if __name__ == "__main__":
    main()

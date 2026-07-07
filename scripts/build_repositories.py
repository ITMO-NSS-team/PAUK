import hashlib
import json
import logging
import re
import sqlite3
import time
from datetime import date
from urllib.parse import urlparse

import requests
from config import (
    DB_PATH,
    DOWNLOAD_TIMEOUT,
    GITHUB_API_URL,
    GITHUB_TOKEN,
    REQUEST_DELAY,
)


logger = logging.getLogger(__name__)

# --- Идентификаторы и разбор URL ----------------------------------------


def repo_id_for(url: str) -> str:
    """Детерминированный id репозитория из URL (для идемпотентности)."""
    return "repo_" + hashlib.sha1(url.encode("utf-8")).hexdigest()[:12]


def ghdept_id_for(login: str) -> str:
    """Детерминированный id github-организации из логина."""
    return "ghdept_" + hashlib.sha1(login.lower().encode("utf-8")).hexdigest()[:12]


def parse_owner_repo(url: str) -> tuple[str, str] | None:
    """Возвращает (owner, repo) из host/owner/repo, иначе None."""
    try:
        parsed = urlparse(url)
    except ValueError:
        return None
    segments = [s for s in parsed.path.split("/") if s]
    if len(segments) < 2:
        return None
    return segments[0], segments[1]


# --- Сопоставление имён -------------------------------------------------


def normalize_person_name(s: str) -> str:
    if not s:
        return ""
    s = re.sub(r"[^\w\s]", " ", s)
    return re.sub(r"\s+", " ", s).strip().lower()


def build_person_name_cache(cur: sqlite3.Cursor) -> dict[str, str]:
    """Один раз строит словарь {norm_name: person_id} по name_en и name_variants."""
    cache: dict[str, str] = {}
    cur.execute("SELECT id, name_en, name_variants FROM persons_itmo")
    for pid, name_en, variants_raw in cur.fetchall():
        if name_en:
            cache.setdefault(normalize_person_name(name_en), pid)
        try:
            variants = json.loads(variants_raw) if variants_raw else []
        except (TypeError, ValueError):
            variants = []
        for v in variants:
            cache.setdefault(normalize_person_name(v), pid)
    return cache


def find_itmo_person_by_name(cache: dict[str, str], full_name: str) -> str | None:
    """Ищет persons_itmo по заранее построенному кэшу нормализованных имён."""
    norm = normalize_person_name(full_name)
    if not norm or len(norm.split()) < 2:
        return None
    return cache.get(norm)


# --- GitHub API ----------------------------------------------------------


class GitHubClient:
    """Тонкая обёртка над GitHub REST API с уважением к rate-limit."""

    def __init__(self) -> None:
        self.session = requests.Session()
        headers = {"Accept": "application/vnd.github+json"}
        if GITHUB_TOKEN:
            headers["Authorization"] = f"Bearer {GITHUB_TOKEN}"
        self.session.headers.update(headers)

    def get(self, path: str, params: dict | None = None) -> requests.Response | None:
        url = path if path.startswith("http") else f"{GITHUB_API_URL}{path}"
        try:
            resp = self.session.get(url, params=params, timeout=DOWNLOAD_TIMEOUT)
        except requests.RequestException as exc:
            logger.warning("GitHub запрос упал: %s", exc)
            return None

        # Исчерпан лимит - ждём до сброса (но не вечно).
        if resp.status_code == 403 and resp.headers.get("X-RateLimit-Remaining") == "0":
            reset = int(resp.headers.get("X-RateLimit-Reset", "0"))
            wait = max(0, reset - int(time.time())) + 2
            if wait > 0 and wait <= 3600:
                logger.info("GitHub rate-limit, ждём %d сек до сброса", wait)
                time.sleep(wait)
                return self.get(path, params)
        return resp

    def repo(self, owner: str, repo: str) -> dict | None:
        resp = self.get(f"/repos/{owner}/{repo}")
        if resp is not None and resp.status_code == 200:
            return resp.json()
        if resp is not None and resp.status_code == 404:
            logger.warning("репозиторий %s/%s не найден (404)", owner, repo)
        return None

    def has_readme(self, owner: str, repo: str) -> bool:
        resp = self.get(f"/repos/{owner}/{repo}/readme")
        return resp is not None and resp.status_code == 200

    def contributors(self, owner: str, repo: str) -> list[str]:
        resp = self.get(
            f"/repos/{owner}/{repo}/contributors", params={"per_page": 100}
        )
        if resp is None or resp.status_code != 200:
            return []
        try:
            return [c["login"] for c in resp.json() if c.get("login")]
        except (ValueError, TypeError):
            return []

    def user(self, login: str) -> dict | None:
        resp = self.get(f"/users/{login}")
        if resp is not None and resp.status_code == 200:
            return resp.json()
        return None


# --- Запись в БД ---------------------------------------------------------


def upsert_github_department(cur: sqlite3.Cursor, owner_obj: dict, gh: GitHubClient) -> str:
    """Создаёт/обновляет github_departments по объекту-владельцу из GitHub API."""
    login = owner_obj["login"]
    gid = ghdept_id_for(login)
    # Подробности организации (name, description, location) - из /users/{login}.
    details = gh.user(login) or {}
    cur.execute(
        """
        INSERT INTO github_departments
            (id, github_login, name, html_url, description, location, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(github_login) DO UPDATE SET
            name        = COALESCE(excluded.name, github_departments.name),
            description = COALESCE(excluded.description, github_departments.description),
            location    = COALESCE(excluded.location, github_departments.location)
        """,
        (
            gid,
            login,
            details.get("name"),
            owner_obj.get("html_url"),
            details.get("description"),
            details.get("location"),
            (details.get("created_at") or "")[:10] or None,
        ),
    )
    return gid


def link_owner_person(
    cur: sqlite3.Cursor, repo_id: str, owner_obj: dict, gh: GitHubClient,
    person_cache: dict[str, str],
) -> None:
    """Для владельца-человека пытается привязать его к persons_itmo."""
    login = owner_obj["login"]
    details = gh.user(login) or {}
    full_name = details.get("name") or ""
    person_id = find_itmo_person_by_name(person_cache, full_name) if full_name else None
    if not person_id:
        return
    # Проставляем github человеку (если ещё не стоит) и связь owner.
    cur.execute(
        "UPDATE persons_itmo SET github = ? WHERE id = ? AND (github IS NULL OR github = '')",
        (login, person_id),
    )
    cur.execute(
        """
        INSERT OR IGNORE INTO repository_persons (repository_id, person_id, role)
        VALUES (?, ?, 'owner')
        """,
        (repo_id, person_id),
    )


def link_contributor_persons(
    cur: sqlite3.Cursor, repo_id: str, contributors: list[str]
) -> None:
    """Связывает контрибьюторов-логинов с уже известными persons_itmo по github."""
    for login in contributors:
        cur.execute(
            "SELECT id FROM persons_itmo WHERE github = ? COLLATE NOCASE", (login,)
        )
        row = cur.fetchone()
        if row:
            cur.execute(
                """
                INSERT OR IGNORE INTO repository_persons (repository_id, person_id, role)
                VALUES (?, ?, 'contributor')
                """,
                (repo_id, row[0]),
            )


def save_repository(
    cur: sqlite3.Cursor,
    repo_id: str,
    url: str,
    name: str,
    owner: str | None,
    owner_type: str | None,
    ghdept_id: str | None,
    meta: dict | None,
    contributors: list[str],
    has_readme: bool,
) -> None:
    """Вставляет строку repositories (метаданные из GitHub API, если есть)."""
    meta = meta or {}
    license_obj = meta.get("license") or {}
    cur.execute(
        """
        INSERT OR IGNORE INTO repositories
            (id, name, url, description, access_date, has_publication,
             contributors, owner, owner_type, github_department_id,
             has_readme, stars_num, last_updated, license, created_at)
        VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            repo_id,
            name,
            url,
            meta.get("description"),
            date.today().isoformat(),
            json.dumps(contributors, ensure_ascii=False) if contributors else None,
            owner,
            owner_type,
            ghdept_id,
            1 if has_readme else 0,
            meta.get("stargazers_count"),
            (meta.get("updated_at") or "")[:10] or None,
            license_obj.get("spdx_id") or license_obj.get("key"),
            (meta.get("created_at") or "")[:10] or None,
        ),
    )


def link_repository_publications(
    cur: sqlite3.Cursor, repo_id: str, publication_ids: list[str]
) -> None:
    for pub_id in publication_ids:
        cur.execute(
            """
            INSERT OR IGNORE INTO repository_publications (repository_id, publication_id)
            VALUES (?, ?)
            """,
            (repo_id, pub_id),
        )


# --- Сбор кандидатов -----------------------------------------------------


def fetch_confirmed_repos(conn: sqlite3.Connection) -> list[tuple[str, list[str]]]:
    """(url, [publication_id, ...]) по подтверждённым github-ссылкам, ещё не в repositories."""
    cur = conn.cursor()
    cur.execute(
        """
        SELECT url, GROUP_CONCAT(DISTINCT publication_id)
        FROM repo_links
        WHERE is_relevant = 1
          AND url LIKE 'https://github.com/%'
          AND url NOT IN (SELECT url FROM repositories)
        GROUP BY url
        ORDER BY url
        """
    )
    return [(url, (pubs or "").split(",")) for url, pubs in cur.fetchall()]


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.execute("PRAGMA foreign_keys = ON")
    cur = conn.cursor()
    gh = GitHubClient()

    if not GITHUB_TOKEN:
        logger.warning("GITHUB_TOKEN не задан - лимит 60 запросов/час, большие объёмы упрутся в rate-limit.")

    try:
        repos = fetch_confirmed_repos(conn)
        if not repos:
            logger.info("Нет новых подтверждённых репозиториев для обработки.")
            return

        logger.info("Обрабатываю %d репозиториев", len(repos))
        person_cache = build_person_name_cache(cur)
        stats = {"created": 0, "orgs": 0, "users": 0, "owner_linked": 0}

        for index, (url, pub_ids) in enumerate(repos, 1):
            parsed = parse_owner_repo(url)
            if parsed is None:
                continue  # github.com/<только-owner> без репо - мусор, пропускаем
            owner, name = parsed
            repo_id = repo_id_for(url)

            logger.info("[%d/%d] %s/%s", index, len(repos), owner, name)
            meta = gh.repo(owner, name)
            if meta is None:
                # репо удалён/приватный - всё равно сохраняем минимальную строку
                save_repository(
                    cur, repo_id, url, name, owner, None, None, None, [], False
                )
                link_repository_publications(cur, repo_id, pub_ids)
                stats["created"] += 1
                conn.commit()
                time.sleep(REQUEST_DELAY)
                continue

            owner_obj = meta.get("owner") or {}
            is_org = owner_obj.get("type") == "Organization"
            owner_type = "org" if is_org else "user"
            ghdept_id = None

            if is_org:
                ghdept_id = upsert_github_department(cur, owner_obj, gh)
                stats["orgs"] += 1
            else:
                stats["users"] += 1

            has_readme = gh.has_readme(owner, name)
            contributors = gh.contributors(owner, name)

            save_repository(
                cur, repo_id, url, name, owner, owner_type, ghdept_id,
                meta, contributors, has_readme,
            )
            link_repository_publications(cur, repo_id, pub_ids)

            if not is_org:
                before = cur.execute(
                    "SELECT COUNT(*) FROM repository_persons WHERE repository_id=? AND role='owner'",
                    (repo_id,),
                ).fetchone()[0]
                link_owner_person(cur, repo_id, owner_obj, gh, person_cache)
                after = cur.execute(
                    "SELECT COUNT(*) FROM repository_persons WHERE repository_id=? AND role='owner'",
                    (repo_id,),
                ).fetchone()[0]
                if after > before:
                    stats["owner_linked"] += 1

            link_contributor_persons(cur, repo_id, contributors)

            stats["created"] += 1
            conn.commit()
            time.sleep(REQUEST_DELAY)

        logger.info(
            "Готово — создано: %d, орг: %d, user: %d, привязано к persons_itmo: %d",
            stats["created"], stats["orgs"], stats["users"], stats["owner_linked"],
        )
    except Exception:
        logger.exception("build_repositories упал с ошибкой")
        raise
    finally:
        conn.commit()
        conn.close()


if __name__ == "__main__":
    main()

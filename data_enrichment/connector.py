import json
import logging
import sqlite3
import threading

logger = logging.getLogger(__name__)

# Колонки openreview_profiles
OPENREVIEW_COLS = [
    "person_id", "openreview_id", "name_en", "matched_by", "names", "emails_masked",
    "affiliations", "relations", "homepage", "gscholar", "dblp", "orcid", "github", "linkedin",
]
OPENREVIEW_JSON_COLS = {"names", "emails_masked", "affiliations", "relations"}

# Колонки person_profiles в порядке вставки
PERSON_PROFILE_COLS = [
    "person_id", "name_en", "openalex_author_id", "openalex_url", "orcid",
    "scopus_id", "researcher_id", "twitter", "wikipedia", "linkedin", "country",
    "works_count", "cited_by_count", "h_index", "i10_index", "last_institution",
    "affiliations", "employments", "educations", "topics", "counts_by_year",
    "researcher_urls", "external_ids", "keywords", "biography",
    "emails", "other_names", "has_github", "github_urls", "status",
]

# DDL таблиц-выходов пайплайна.
SERVICE_SCHEMA = """
CREATE TABLE IF NOT EXISTS crossref_orcid (
    person_id TEXT PRIMARY KEY,
    orcid     TEXT,
    doi       TEXT,
    found_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS collected_emails (
    person_id TEXT,
    email     TEXT,
    source    TEXT,
    ref       TEXT,
    found_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (person_id, email)
);
CREATE INDEX IF NOT EXISTS idx_collemail_person ON collected_emails(person_id);
CREATE INDEX IF NOT EXISTS idx_collemail_source ON collected_emails(source);

CREATE TABLE IF NOT EXISTS person_profiles (
    person_id          TEXT PRIMARY KEY,
    name_en            TEXT,
    openalex_author_id TEXT,
    openalex_url       TEXT,
    orcid              TEXT,
    scopus_id          TEXT,
    researcher_id      TEXT,
    twitter            TEXT,
    wikipedia          TEXT,
    linkedin           TEXT,
    country            TEXT,
    works_count        INTEGER,
    cited_by_count     INTEGER,
    h_index            INTEGER,
    i10_index          INTEGER,
    last_institution   TEXT,
    affiliations       TEXT,
    employments        TEXT,
    educations         TEXT,
    topics             TEXT,
    counts_by_year     TEXT,
    researcher_urls    TEXT,
    external_ids       TEXT,
    keywords           TEXT,
    biography          TEXT,
    emails             TEXT,
    other_names        TEXT,
    has_github         INTEGER DEFAULT 0,
    github_urls        TEXT,
    status             TEXT,
    enriched_at        TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_profiles_status ON person_profiles(status);
CREATE INDEX IF NOT EXISTS idx_profiles_github ON person_profiles(has_github);

CREATE TABLE IF NOT EXISTS openreview_profiles (
    person_id     TEXT PRIMARY KEY,
    openreview_id TEXT,
    name_en       TEXT,
    matched_by    TEXT,
    names         TEXT,
    emails_masked TEXT,
    affiliations  TEXT,
    relations     TEXT,
    homepage      TEXT,
    gscholar      TEXT,
    dblp          TEXT,
    orcid         TEXT,
    github        TEXT,
    linkedin      TEXT,
    found_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_orp_oid ON openreview_profiles(openreview_id);

CREATE TABLE IF NOT EXISTS github_candidates (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    github_login    TEXT NOT NULL,
    github_url      TEXT,
    user_type       TEXT,
    source          TEXT,
    repo_url        TEXT,
    publication_ids TEXT,
    gh_name         TEXT,
    gh_email        TEXT,
    gh_company      TEXT,
    gh_location     TEXT,
    gh_bio          TEXT,
    gh_blog         TEXT,
    gh_twitter      TEXT,
    commit_emails   TEXT,
    commit_names    TEXT,
    harvested_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(github_login, repo_url)
);
CREATE INDEX IF NOT EXISTS idx_ghcand_login ON github_candidates(github_login);
CREATE INDEX IF NOT EXISTS idx_ghcand_repo  ON github_candidates(repo_url);

CREATE TABLE IF NOT EXISTS github_matches (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    person_id    TEXT,
    person_name  TEXT,
    github_login TEXT,
    github_url   TEXT,
    score        REAL,
    signals      TEXT,
    evidence     TEXT,
    decision     TEXT,
    confidence   TEXT,
    repos        TEXT,
    matched_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(person_id, github_login)
);
CREATE INDEX IF NOT EXISTS idx_ghmatch_login    ON github_matches(github_login);
CREATE INDEX IF NOT EXISTS idx_ghmatch_decision ON github_matches(decision);
"""


class SqliteConnector:
    def __init__(self, db_path: str, timeout: int = 30) -> None:
        self._conn = sqlite3.connect(db_path, timeout=timeout, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode = WAL")
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._write_lock = threading.Lock()

    # низкоуровневое, временно, пока не выделены именованные методы под операции

    def query(self, sql: str, params: tuple = ()) -> list:
        return self._conn.execute(sql, params).fetchall()

    def execute(self, sql: str, params: tuple = ()) -> None:
        with self._write_lock:
            self._conn.execute(sql, params)

    def commit(self) -> None:
        with self._write_lock:
            self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def ensure_schema(self) -> None:
        """Создаёт служебные таблицы-выходы пайплайна, если их ещё нет."""
        with self._write_lock:
            self._conn.executescript(SERVICE_SCHEMA)
            # merge_profiles складывает эти два поля в persons_itmo; в базовой схеме их
            # нет, а ADD COLUMN IF NOT EXISTS в SQLite не поддержан — отсюда try/except.
            for col in ("linkedin", "gitlab"):
                try:
                    self._conn.execute(f"ALTER TABLE persons_itmo ADD COLUMN {col} TEXT")
                except sqlite3.OperationalError:
                    pass
            self._conn.commit()

    # Пересчёт производных, sync_* из finalize.py
    def rebuild_derived(self) -> None:
        self._sync_publications_code()
        self._sync_publication_departments()
        self._sync_repository_departments()
        self.commit()

    def _sync_publications_code(self) -> None:
        """publications.has_code / code_url из размеченных repo_links."""
        self.execute("UPDATE publications SET has_code = 0, code_url = NULL")
        urls_per_pub: dict[str, list[str]] = {}
        for pub_id, url in self.query(
            "SELECT publication_id, url FROM repo_links WHERE is_relevant = 1 "
            "ORDER BY publication_id, COALESCE(llm_confidence, 0) DESC, id ASC"
        ):
            urls_per_pub.setdefault(pub_id, []).append(url)
        for pub_id, urls in urls_per_pub.items():
            self.execute("UPDATE publications SET has_code = 1, code_url = ? WHERE id = ?",
                         (json.dumps(urls, ensure_ascii=False), pub_id))
        logger.info("[derived] публикаций с кодом: %d", len(urls_per_pub))

    def _sync_publication_departments(self) -> None:
        """Раскрывает '; '-список persons_itmo.department в пары публикация-департамент."""
        self.execute("DELETE FROM publication_departments")
        valid = {row[0] for row in self.query("SELECT id FROM departments")}
        pairs = {
            (pub_id, dept_id)
            for pub_id, field in self.query(
                "SELECT pa.publication_id, pi.department FROM publication_authors pa "
                "JOIN persons_itmo pi ON pi.id = pa.person_id AND pa.person_type = 'itmo' "
                "WHERE pi.department IS NOT NULL AND pi.department != ''")
            for dept_id in (d.strip() for d in field.split(";"))
            if dept_id in valid
        }
        for pub_id, dept_id in pairs:
            self.execute("INSERT OR IGNORE INTO publication_departments "
                         "(publication_id, department_id) VALUES (?, ?)", (pub_id, dept_id))
        logger.info("[derived] пар публикация-департамент: %d", len(pairs))

    def _sync_repository_departments(self) -> None:
        self.execute("DELETE FROM repository_departments")
        self.execute(
            """
            INSERT OR IGNORE INTO repository_departments (repository_id, department_id)
            SELECT DISTINCT rp.repository_id, pd.department_id
            FROM repository_publications rp
            JOIN publication_departments pd ON pd.publication_id = rp.publication_id
            """
        )
        logger.info("[derived] пар репозиторий-департамент: %d",
                    self.query("SELECT COUNT(*) FROM repository_departments")[0][0])

    # Именованные методы под операции

    # enrich_persons_ru
    def persons_needing_ru_names(self) -> list:
        return self.query(
            "SELECT id, name_en, name_variants FROM persons_itmo "
            "WHERE surname_ru IS NULL OR surname_ru = '' ORDER BY id"
        )

    def save_ru_name(self, person_id: str, surname: str, first: str, second: str) -> None:
        self.execute(
            "UPDATE persons_itmo SET surname_ru = ?, first_name_ru = ?, second_name_ru = ? "
            "WHERE id = ?",
            (surname, first, second, person_id),
        )

    # crossref_orcid
    def itmo_authors_needing_orcid(self) -> list:
        """(publication_id, doi, person_id, name_en, name_variants) для ИТМО-авторов
        публикаций с DOI, у кого ещё нет ORCID и кого ещё не проверял crossref."""
        return self.query(
            """
            SELECT pa.publication_id, p.doi, pa.person_id, pi.name_en, pi.name_variants
            FROM publication_authors pa
            JOIN publications p   ON p.id = pa.publication_id
            JOIN persons_itmo pi  ON pi.id = pa.person_id
            WHERE pa.person_type = 'itmo' AND p.doi > '' AND pi.name_en > ''
              AND pa.person_id NOT IN (SELECT person_id FROM person_profiles WHERE orcid > '')
              AND pa.person_id NOT IN (SELECT person_id FROM crossref_orcid)
            """
        )

    def save_crossref_orcid(self, person_id: str, orcid: str, doi: str) -> None:
        # PRIMARY KEY по person_id + IGNORE -> первое присвоение выигрывает, гонок нет.
        self.execute(
            "INSERT OR IGNORE INTO crossref_orcid (person_id, orcid, doi) VALUES (?, ?, ?)",
            (person_id, orcid, doi),
        )

    # enrich_persons
    def persons_to_enrich(self) -> list:
        """(id, name_en) для персон с OpenAlex-id, которых ещё не собирали ИЛИ у кого
        статус no_orcid, но теперь есть ORCID из crossref (пере-обработка — фикс метки)."""
        return self.query(
            """
            SELECT pi.id, pi.name_en
            FROM persons_itmo pi
            LEFT JOIN person_profiles pp ON pp.person_id = pi.id
            WHERE pi.id LIKE 'itmo_A%'
              AND (pp.person_id IS NULL
                   OR (pp.status = 'no_orcid'
                       AND pi.id IN (SELECT person_id FROM crossref_orcid)))
            ORDER BY pi.id
            """
        )

    def crossref_orcid_map(self) -> list:
        return self.query("SELECT person_id, orcid FROM crossref_orcid")

    def save_person_profile(self, row: dict) -> None:
        cols = ", ".join(PERSON_PROFILE_COLS)
        placeholders = ", ".join(f":{c}" for c in PERSON_PROFILE_COLS)
        self.execute(
            f"INSERT OR REPLACE INTO person_profiles ({cols}, enriched_at) "
            f"VALUES ({placeholders}, CURRENT_TIMESTAMP)",
            row,
        )

    # collect_emails
    def save_collected_email(self, person_id: str, email: str, source: str, ref: str) -> None:
        self.execute(
            "INSERT OR IGNORE INTO collected_emails (person_id, email, source, ref) "
            "VALUES (?, ?, ?, ?)",
            (person_id, email, source, ref),
        )

    # collect_emails --source pdf
    def itmo_authors_for_pdf_emails(self) -> list:
        """(publication_id, person_id, name_en, name_variants) по ИТМО-авторам публикаций,
        которые ещё не разбирали на email из PDF."""
        return self.query(
            """
            SELECT pa.publication_id, p.id, p.name_en, p.name_variants
            FROM publication_authors pa
            JOIN persons_itmo p ON p.id = pa.person_id
            WHERE pa.person_type = 'itmo'
              AND pa.publication_id NOT IN
                  (SELECT DISTINCT ref FROM collected_emails WHERE source = 'pdf')
            """
        )

    # merge_profiles сборка из всех коллекторов
    def merge_person_profiles(self) -> list:
        return self.query(
            "SELECT person_id, emails, github_urls, researcher_urls, linkedin FROM person_profiles")

    def merge_collected_emails(self) -> list:
        return self.query("SELECT person_id, email, source FROM collected_emails")

    def merge_github_matched(self) -> list:
        return self.query(
            "SELECT DISTINCT person_id, github_login FROM github_matches WHERE decision = 'matched'")

    def github_candidate_email_fields(self, login: str) -> list:
        return self.query(
            "SELECT gh_email, commit_emails, gh_blog FROM github_candidates WHERE github_login = ?",
            (login,))

    def merge_openreview(self) -> list:
        return self.query(
            "SELECT person_id, openreview_id, github, gscholar, linkedin FROM openreview_profiles")

    def persons_merge_targets(self) -> list:
        return self.query(
            "SELECT id, email, github, google_scholar, openreview, linkedin, gitlab FROM persons_itmo")

    def update_person_fields(self, person_id: str, updates: dict) -> None:
        sets = ", ".join(f"{k} = ?" for k in updates)
        self.execute(f"UPDATE persons_itmo SET {sets} WHERE id = ?", (*updates.values(), person_id))

    # finalize_dedup департаментов
    def all_departments(self) -> list:
        return self.query("SELECT id, name_en, name_variants FROM departments")

    def update_department_variants(self, dept_id: str, variants_json: str) -> None:
        self.execute("UPDATE departments SET name_variants = ? WHERE id = ?", (variants_json, dept_id))

    def persons_with_department(self, loser_id: str) -> list:
        return self.query("SELECT id, department FROM persons_itmo WHERE department LIKE ?",
                          (f"%{loser_id}%",))

    def set_person_department(self, person_id: str, dept_str: str) -> None:
        self.execute("UPDATE persons_itmo SET department = ? WHERE id = ?", (dept_str, person_id))

    def repoint_department_junctions(self, loser: str, canonical: str) -> None:
        for table in ("publication_departments", "repository_departments"):
            self.execute(f"UPDATE OR IGNORE {table} SET department_id = ? WHERE department_id = ?",
                         (canonical, loser))
            self.execute(f"DELETE FROM {table} WHERE department_id = ?", (loser,))

    def delete_department(self, dept_id: str) -> None:
        self.execute("DELETE FROM departments WHERE id = ?", (dept_id,))

    # finalize_dedup персон
    def all_persons_for_dedup(self) -> list:
        return self.query("SELECT id, name_en, name_variants, github FROM persons_itmo")

    def repoint_person_junctions(self, loser: str, canonical: str) -> None:
        self.execute("UPDATE OR IGNORE publication_authors SET person_id = ? "
                     "WHERE person_id = ? AND person_type = 'itmo'", (canonical, loser))
        self.execute("DELETE FROM publication_authors WHERE person_id = ? AND person_type = 'itmo'",
                     (loser,))
        self.execute("UPDATE OR IGNORE repository_persons SET person_id = ? WHERE person_id = ?",
                     (canonical, loser))
        self.execute("DELETE FROM repository_persons WHERE person_id = ?", (loser,))

    def person_variants_github(self, person_id: str) -> tuple | None:
        rows = self.query("SELECT name_variants, github FROM persons_itmo WHERE id = ?", (person_id,))
        return rows[0] if rows else None

    def update_person_merged(self, person_id: str, variants_json: str, github: str | None) -> None:
        self.execute("UPDATE persons_itmo SET name_variants = ?, github = COALESCE(github, ?) "
                     "WHERE id = ?", (variants_json, github, person_id))

    def delete_person(self, person_id: str) -> None:
        self.execute("DELETE FROM persons_itmo WHERE id = ?", (person_id,))

    # collect_emails --source pages
    def persons_names_variants(self) -> list:
        return self.query("SELECT id, name_en, name_variants FROM persons_itmo WHERE name_en > ''")

    def researcher_urls_rows(self) -> list:
        return self.query(
            "SELECT person_id, researcher_urls FROM person_profiles WHERE researcher_urls IS NOT NULL")

    def openreview_homepages(self) -> list:
        return self.query("SELECT person_id, homepage FROM openreview_profiles WHERE homepage > ''")

    def pages_done(self) -> list:
        return self.query("SELECT DISTINCT person_id FROM collected_emails WHERE source = 'page'")

    # match_github
    def persons_for_matching(self) -> list:
        return self.query(
            "SELECT id, name_en, name_variants, email, github FROM persons_itmo")

    def profiles_for_matching(self) -> list:
        return self.query("SELECT person_id, emails, other_names FROM person_profiles")

    def itmo_publication_authors(self) -> list:
        return self.query(
            "SELECT publication_id, person_id FROM publication_authors WHERE person_type = 'itmo'")

    def itmo_github_orgs(self) -> set:
        return {login.lower() for (login,) in self.query(
            "SELECT github_login FROM github_departments") if login}

    def all_github_candidates(self) -> list:
        return self.query(
            """
            SELECT github_login, github_url, source, repo_url, publication_ids,
                   gh_name, gh_email, gh_company, gh_location, gh_bio, commit_emails, commit_names
            FROM github_candidates
            """
        )

    def repository_ids_by_url(self) -> dict:
        return dict(self.query("SELECT url, id FROM repositories"))

    def clear_github_matches(self) -> None:
        self.execute("DELETE FROM github_matches")

    def save_github_match(self, row: tuple) -> None:
        self.execute(
            """
            INSERT OR REPLACE INTO github_matches
                (person_id, person_name, github_login, github_url, score,
                 signals, evidence, decision, confidence, repos)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, row)

    def set_person_github(self, person_id: str, login: str) -> None:
        self.execute(
            "UPDATE persons_itmo SET github = ? WHERE id = ? AND (github IS NULL OR github = '')",
            (login, person_id))

    def link_repository_person(self, repository_id, person_id: str, role: str) -> None:
        self.execute(
            "INSERT OR IGNORE INTO repository_persons (repository_id, person_id, role) "
            "VALUES (?, ?, ?)", (repository_id, person_id, role))

    # build_repositories
    def confirmed_repos_not_built(self) -> list:
        """(url, [publication_id]) по подтверждённым github-ссылкам, которых ещё нет
        в repositories."""
        return [(url, (pubs or "").split(",")) for url, pubs in self.query(
            """
            SELECT url, GROUP_CONCAT(DISTINCT publication_id)
            FROM repo_links
            WHERE is_relevant = 1
              AND url LIKE 'https://github.com/%'
              AND url NOT IN (SELECT url FROM repositories)
            GROUP BY url ORDER BY url
            """
        )]

    def upsert_github_department(self, row: tuple) -> None:
        self.execute(
            """
            INSERT INTO github_departments
                (id, github_login, name, html_url, description, location, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(github_login) DO UPDATE SET
                name        = COALESCE(excluded.name, github_departments.name),
                description = COALESCE(excluded.description, github_departments.description),
                location    = COALESCE(excluded.location, github_departments.location)
            """, row)

    def save_repository(self, row: tuple) -> None:
        self.execute(
            """
            INSERT OR IGNORE INTO repositories
                (id, name, url, description, access_date, has_publication,
                 contributors, owner, owner_type, github_department_id,
                 has_readme, stars_num, last_updated, license, created_at)
            VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, row)

    def link_repository_publication(self, repository_id: str, publication_id: str) -> None:
        self.execute(
            "INSERT OR IGNORE INTO repository_publications (repository_id, publication_id) "
            "VALUES (?, ?)", (repository_id, publication_id))

    def person_id_by_github_login(self, login: str) -> str | None:
        rows = self.query(
            "SELECT id FROM persons_itmo WHERE github = ? COLLATE NOCASE", (login,))
        return rows[0][0] if rows else None

    # github_harvest
    def confirmed_repo_links(self) -> list:
        """(url, [publication_id]) по всем подтверждённым github-ссылкам."""
        by_url: dict[str, list] = {}
        for url, pub_id in self.query(
            "SELECT url, publication_id FROM repo_links "
            "WHERE is_relevant = 1 AND host = 'github.com' ORDER BY url"
        ):
            by_url.setdefault(url, [])
            if pub_id and pub_id not in by_url[url]:
                by_url[url].append(pub_id)
        return list(by_url.items())

    def harvested_repo_urls(self) -> set:
        return {r[0] for r in self.query("SELECT DISTINCT repo_url FROM github_candidates")}

    def social_graph_seeds(self) -> list:
        """Сиды соцграфа: ИТМО-организации, затем подтверждённые личные аккаунты."""
        orgs = [(r[0], "org") for r in self.query(
            "SELECT github_login FROM github_departments") if r[0]]
        users = [(r[0], "user") for r in self.query(
            "SELECT DISTINCT github FROM persons_itmo WHERE github > ''") if r[0]]
        seen, seeds = set(), []
        for login, kind in orgs + users:
            if login.lower() not in seen:
                seen.add(login.lower())
                seeds.append((login, kind))
        return seeds

    def replace_github_candidates(self, repo_url: str, candidates: list) -> None:
        self.execute("DELETE FROM github_candidates WHERE repo_url = ?", (repo_url,))
        for c in candidates:
            self.execute(
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
                """, c)

    # find_code_links: extract + classify
    def publications_for_link_extract(self) -> list:
        return self.query(
            """
            SELECT id, abstract FROM publications
            WHERE (pdf_url IS NOT NULL AND pdf_url != '')
               OR (abstract IS NOT NULL AND abstract != '')
            """
        )

    def merge_repo_links(self, publication_id: str, links: list) -> None:
        """Досыпает новые ссылки публикации, не трогая уже классифицированные"""
        if not links:
            return
        known = {u for (u,) in self.query(
            "SELECT url FROM repo_links WHERE publication_id = ?", (publication_id,))}
        for url, context, page in links:
            if url in known:
                continue
            self.execute(
                "INSERT INTO repo_links (publication_id, url, host, context, page_number) "
                "VALUES (?, ?, 'github.com', ?, ?)", (publication_id, url, context, page))

    def unclassified_repo_links(self) -> list:
        return self.query(
            """
            SELECT rl.id, rl.url, rl.context, rl.page_number, p.title, p.authors
            FROM repo_links rl JOIN publications p ON p.id = rl.publication_id
            WHERE rl.is_relevant IS NULL ORDER BY rl.id
            """
        )

    def save_link_classification(self, link_id, is_relevant, confidence, reason) -> None:
        self.execute(
            "UPDATE repo_links SET is_relevant = ?, llm_confidence = ?, llm_reason = ? WHERE id = ?",
            (1 if is_relevant else 0, confidence, reason, link_id))

    # enrich_departments
    def persons_needing_departments(self) -> list:
        """(id, affiliation) для персон с аффилиацией без департамента"""
        return self.query(
            """
            SELECT id, affiliation FROM persons_itmo
            WHERE affiliation IS NOT NULL AND affiliation != ''
              AND (department IS NULL OR department = '' OR department = '-')
            ORDER BY id
            """
        )

    def create_department_row(self, dept_id: str, name_en: str) -> bool:
        """Создаёт департамент без name_ru"""
        try:
            self.execute("INSERT INTO departments (id, name_en) VALUES (?, ?)", (dept_id, name_en))
            return True
        except sqlite3.IntegrityError:
            return False

    def department_id_by_name_en(self, name_en: str) -> str | None:
        rows = self.query("SELECT id FROM departments WHERE name_en = ?", (name_en,))
        return rows[0][0] if rows else None

    # enrich_openreview
    def openreview_person_data(self) -> list:
        """(person_id, orcid, other_names) — сигналы для верификации профиля."""
        return self.query("SELECT person_id, orcid, other_names FROM person_profiles")

    def openreview_done(self) -> list:
        return self.query("SELECT person_id FROM openreview_profiles")

    def save_openreview_profile(self, person_id: str, data: dict) -> None:
        row = {"person_id": person_id}
        for col in OPENREVIEW_COLS[1:]:
            value = data.get(col)
            row[col] = (json.dumps(value, ensure_ascii=False)
                        if col in OPENREVIEW_JSON_COLS else value)
        cols = ", ".join(OPENREVIEW_COLS)
        placeholders = ", ".join(f":{c}" for c in OPENREVIEW_COLS)
        self.execute(
            f"INSERT OR REPLACE INTO openreview_profiles ({cols}, found_at) "
            f"VALUES ({placeholders}, CURRENT_TIMESTAMP)",
            row,
        )
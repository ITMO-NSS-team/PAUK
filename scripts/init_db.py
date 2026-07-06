import logging
import sqlite3

from config import DB_PATH

logger = logging.getLogger(__name__)


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS publications (
    id               TEXT PRIMARY KEY,
    title            TEXT NOT NULL,
    journal          TEXT,
    doi              TEXT UNIQUE,
    publication_date DATE,
    year             INTEGER,
    has_code         BOOLEAN DEFAULT 0,
    code_url         TEXT,
    funding          TEXT,
    openalex_url     TEXT,
    authors          TEXT,
    affiliation      TEXT,
    pdf_url          TEXT,
    abstract         TEXT,
    created_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS persons_itmo (
    id              TEXT PRIMARY KEY,
    first_name_ru   TEXT,
    second_name_ru  TEXT,
    surname_ru      TEXT,
    name_en         TEXT,
    name_variants   TEXT,
    department      TEXT,
    affiliation     TEXT,
    degree          TEXT,
    email           TEXT,
    github          TEXT,
    google_scholar  TEXT,
    openreview      TEXT,
    thesis          TEXT,
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS persons_external (
    id            TEXT PRIMARY KEY,
    name_en       TEXT NOT NULL,
    name_variants TEXT,
    affiliation   TEXT,
    department    TEXT,
    email         TEXT
);

CREATE TABLE IF NOT EXISTS publication_authors (
    publication_id     TEXT,
    person_id          TEXT,
    person_type        TEXT CHECK(person_type IN ('itmo', 'external')),
    author_position    INTEGER,
    affiliation_string TEXT,
    is_corresponding   BOOLEAN DEFAULT 0,
    PRIMARY KEY (publication_id, person_id, person_type),
    FOREIGN KEY (publication_id) REFERENCES publications(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS departments (
    id            TEXT PRIMARY KEY,
    name_ru       TEXT,
    name_en       TEXT NOT NULL UNIQUE,
    name_variants TEXT
);

CREATE TABLE IF NOT EXISTS publication_departments (
    publication_id TEXT,
    department_id  TEXT,
    PRIMARY KEY (publication_id, department_id),
    FOREIGN KEY (publication_id) REFERENCES publications(id) ON DELETE CASCADE,
    FOREIGN KEY (department_id)  REFERENCES departments(id)  ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS github_departments (
    id            TEXT PRIMARY KEY,
    github_login  TEXT NOT NULL UNIQUE,
    name          TEXT,
    name_variants TEXT,
    html_url      TEXT,
    description   TEXT,
    location      TEXT,
    created_at    DATE
);

CREATE TABLE IF NOT EXISTS repositories (
    id                   TEXT PRIMARY KEY,
    name                 TEXT NOT NULL,
    url                  TEXT NOT NULL UNIQUE,
    description          TEXT,
    access_date          DATE,
    has_publication      BOOLEAN DEFAULT 0,
    contributors         TEXT,
    owner                TEXT,
    owner_type           TEXT CHECK(owner_type IN ('user', 'org')),
    github_department_id TEXT,
    has_readme           BOOLEAN DEFAULT 0,
    stars_num            INTEGER,
    last_updated         DATE,
    license              TEXT,
    created_at           DATE,
    FOREIGN KEY (github_department_id) REFERENCES github_departments(id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS repository_persons (
    repository_id TEXT,
    person_id     TEXT,
    role          TEXT,
    PRIMARY KEY (repository_id, person_id),
    FOREIGN KEY (repository_id) REFERENCES repositories(id) ON DELETE CASCADE,
    FOREIGN KEY (person_id)     REFERENCES persons_itmo(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS repository_departments (
    repository_id TEXT,
    department_id TEXT,
    PRIMARY KEY (repository_id, department_id),
    FOREIGN KEY (repository_id) REFERENCES repositories(id) ON DELETE CASCADE,
    FOREIGN KEY (department_id) REFERENCES departments(id)  ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS repository_publications (
    repository_id  TEXT,
    publication_id TEXT,
    PRIMARY KEY (repository_id, publication_id),
    FOREIGN KEY (repository_id)  REFERENCES repositories(id)  ON DELETE CASCADE,
    FOREIGN KEY (publication_id) REFERENCES publications(id)  ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS repo_links (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    publication_id TEXT NOT NULL,
    url            TEXT NOT NULL,
    host           TEXT,
    context        TEXT,
    page_number    INTEGER,
    is_relevant    BOOLEAN,
    llm_confidence REAL,
    llm_reason     TEXT,
    extracted_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (publication_id) REFERENCES publications(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_pub_date          ON publications(publication_date);
CREATE INDEX IF NOT EXISTS idx_pub_year          ON publications(year);
CREATE INDEX IF NOT EXISTS idx_pub_code          ON publications(has_code);
CREATE INDEX IF NOT EXISTS idx_authors_pub       ON publication_authors(publication_id);
CREATE INDEX IF NOT EXISTS idx_authors_person    ON publication_authors(person_id, person_type);
CREATE INDEX IF NOT EXISTS idx_itmo_affiliation  ON persons_itmo(affiliation);
CREATE INDEX IF NOT EXISTS idx_itmo_github       ON persons_itmo(github);
CREATE INDEX IF NOT EXISTS idx_external_name     ON persons_external(name_en);
CREATE INDEX IF NOT EXISTS idx_dept_name         ON departments(name_en);
CREATE INDEX IF NOT EXISTS idx_pub_dept_pub      ON publication_departments(publication_id);
CREATE INDEX IF NOT EXISTS idx_pub_dept_dept     ON publication_departments(department_id);
CREATE INDEX IF NOT EXISTS idx_ghdept_login      ON github_departments(github_login);
CREATE INDEX IF NOT EXISTS idx_repo_name         ON repositories(name);
CREATE INDEX IF NOT EXISTS idx_repo_owner        ON repositories(owner);
CREATE INDEX IF NOT EXISTS idx_repo_ghdept       ON repositories(github_department_id);
CREATE INDEX IF NOT EXISTS idx_repo_stars        ON repositories(stars_num);
CREATE INDEX IF NOT EXISTS idx_repo_persons_repo ON repository_persons(repository_id);
CREATE INDEX IF NOT EXISTS idx_repo_persons_pers ON repository_persons(person_id);
CREATE INDEX IF NOT EXISTS idx_repo_dept_repo    ON repository_departments(repository_id);
CREATE INDEX IF NOT EXISTS idx_repo_dept_dept    ON repository_departments(department_id);
CREATE INDEX IF NOT EXISTS idx_repo_pub_repo     ON repository_publications(repository_id);
CREATE INDEX IF NOT EXISTS idx_repo_pub_pub      ON repository_publications(publication_id);
CREATE INDEX IF NOT EXISTS idx_repo_links_pub    ON repo_links(publication_id);
CREATE INDEX IF NOT EXISTS idx_repo_links_host   ON repo_links(host);
"""


def create_schema(conn: sqlite3.Connection) -> None:
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA_SQL)
    conn.commit()


def table_summary(conn: sqlite3.Connection) -> list[tuple[str, int]]:
    cur = conn.cursor()
    cur.execute(
        """
        SELECT name FROM sqlite_master
        WHERE type='table' AND name NOT LIKE 'sqlite_%'
        ORDER BY name
        """
    )
    names = [row[0] for row in cur.fetchall()]
    summary: list[tuple[str, int]] = []
    for name in names:
        cur.execute(f"SELECT COUNT(*) FROM {name}")
        summary.append((name, cur.fetchone()[0]))
    return summary


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    try:
        create_schema(conn)
        rows = "\n".join(f"  {name:<24} {count:>10}" for name, count in table_summary(conn))
        logger.info("База данных готова: %s\n%s", DB_PATH, rows)
    finally:
        conn.close()


if __name__ == "__main__":
    main()

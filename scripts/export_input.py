"""Разовый экспорт входных данных из SQLite в JSONL для конвейера."""
import json
import logging
import sqlite3
from pathlib import Path

from data_enrichment.config import DATA_DIR, DB_PATH

logger = logging.getLogger(__name__)

INPUT_DIR = Path(DATA_DIR) / "input"


def export_persons(conn: sqlite3.Connection, out_path: Path) -> int:
    n = 0
    fh = out_path.open("w", encoding="utf-8")
    for (pid, name_en, variants, sur, first, second, email, github, scholar,
         openreview, department, affiliation, degree, thesis) in conn.execute(
        "SELECT id, name_en, name_variants, surname_ru, first_name_ru, second_name_ru, "
        "email, github, google_scholar, openreview, department, affiliation, degree, thesis "
        "FROM persons_itmo ORDER BY id"
    ):
        try:
            variants_list = json.loads(variants) if variants else []
        except ValueError:
            variants_list = []
        dept_ids = [d.strip() for d in (department or "").split(";")
                    if d.strip() and d.strip() != "-"]
        fh.write(json.dumps({
            "id": pid, "name_en": name_en, "name_variants": variants_list,
            "surname_ru": sur or None, "first_name_ru": first or None,
            "second_name_ru": second or None, "email": email or None,
            "github": github or None, "google_scholar": scholar or None,
            "openreview": openreview or None, "degree": degree or None,
            "thesis": thesis or None, "department_ids": dept_ids,
            "affiliation": affiliation or None,
        }, ensure_ascii=False) + "\n")
        n += 1
    fh.close()
    return n


def export_publications(conn: sqlite3.Connection, out_path: Path) -> int:
    # ИТМО авторы с фамилиями для привязки orcid/email в субконвейере
    by_pub: dict[str, list[dict]] = {}
    for pub_id, pid, name_en, variants in conn.execute(
        "SELECT pa.publication_id, p.id, p.name_en, p.name_variants "
        "FROM publication_authors pa JOIN persons_itmo p ON p.id = pa.person_id "
        "WHERE pa.person_type = 'itmo'"
    ):
        from data_enrichment.helpers import author_surnames
        surnames = sorted(author_surnames(name_en, variants))
        if surnames:
            by_pub.setdefault(pub_id, []).append({"person_id": pid, "surnames": surnames})

    n = 0
    fh = out_path.open("w", encoding="utf-8")
    for pub_id, doi, title, abstract, authors_str in conn.execute(
        "SELECT id, doi, title, abstract, authors FROM publications ORDER BY id"
    ):
        fh.write(json.dumps({
            "id": pub_id, "doi": doi or None, "title": title or None,
            "abstract": abstract or None, "authors_str": authors_str or None,
            "authors": by_pub.get(pub_id, []),
        }, ensure_ascii=False) + "\n")
        n += 1
    fh.close()
    return n


def export_departments(conn: sqlite3.Connection, out_path: Path) -> int:
    n = 0
    fh = out_path.open("w", encoding="utf-8")
    for did, name_ru, name_en, variants in conn.execute(
        "SELECT id, name_ru, name_en, name_variants FROM departments ORDER BY id"
    ):
        try:
            variants_list = json.loads(variants) if variants else []
        except ValueError:
            variants_list = []
        fh.write(json.dumps({
            "id": did, "name_en": name_en, "name_ru": name_ru or None,
            "name_variants": variants_list,
        }, ensure_ascii=False) + "\n")
        n += 1
    fh.close()
    return n


def export_github(conn: sqlite3.Connection, cand_path: Path, orgs_path: Path) -> tuple[int, int]:
    """Кандидаты аккаунты (вход match_github) и ИТМО организации на GitHub."""
    cols = [c[1] for c in conn.execute("PRAGMA table_info(github_candidates)")]
    n_c = 0
    with cand_path.open("w", encoding="utf-8") as fh:
        for row in conn.execute("SELECT * FROM github_candidates ORDER BY id"):
            d = dict(zip(cols, row))
            for k in ("publication_ids", "commit_emails", "commit_names"):
                try:
                    d[k] = json.loads(d[k]) if d[k] else []
                except ValueError:
                    d[k] = []
            fh.write(json.dumps(d, ensure_ascii=False) + "\n")
            n_c += 1
    n_o = 0
    with orgs_path.open("w", encoding="utf-8") as fh:
        for did, login in conn.execute("SELECT id, github_login FROM github_departments"):
            fh.write(json.dumps({"id": did, "github_login": login}, ensure_ascii=False) + "\n")
            n_o += 1
    return n_c, n_o


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    INPUT_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    try:
        n_p = export_persons(conn, INPUT_DIR / "persons.jsonl")
        n_pub = export_publications(conn, INPUT_DIR / "publications.jsonl")
        n_d = export_departments(conn, INPUT_DIR / "departments.jsonl")
        n_c, n_o = export_github(conn, INPUT_DIR / "github_candidates.jsonl", INPUT_DIR / "github_orgs.jsonl")
        logger.info("экспорт: персон %d, публикаций %d, департаментов %d, "
                    "кандидатов github %d, орг %d -> %s", n_p, n_pub, n_d, n_c, n_o, INPUT_DIR)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
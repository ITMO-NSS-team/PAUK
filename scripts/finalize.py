import argparse
import json
import logging
import re
import sqlite3

from catalog import load_catalog, official_name_en_set
from config import DB_PATH

logger = logging.getLogger(__name__)



# --- DEDUP ---------------------------------------------------

class UnionFind:
    def __init__(self) -> None:
        self.parent: dict[str, str] = {}

    def find(self, x: str) -> str:
        self.parent.setdefault(x, x)
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a: str, b: str) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[ra] = rb

    def groups(self) -> dict[str, list[str]]:
        out: dict[str, list[str]] = {}
        for node in self.parent:
            out.setdefault(self.find(node), []).append(node)
        return out


def normalize(s: str) -> str:
    if not s:
        return ""
    s = re.sub(r'["“”‘’«»]', "", s)
    return re.sub(r"\s+", " ", s).strip().lower()


def load_variants(raw: str | None) -> list[str]:
    if not raw:
        return []
    try:
        v = json.loads(raw)
        return v if isinstance(v, list) else []
    except (TypeError, ValueError):
        return []


def dedup_departments(conn: sqlite3.Connection, dry_run: bool) -> int:
    cur = conn.cursor()
    cur.execute("SELECT id, name_en, name_variants FROM departments")
    depts = {
        did: {"name_en": name, "variants": load_variants(var)}
        for did, name, var in cur.fetchall()
    }
    name_to_id = {normalize(info["name_en"]): did for did, info in depts.items()}
    official = official_name_en_set(load_catalog())

    uf = UnionFind()
    for did in depts:
        uf.find(did)
    for did, info in depts.items():
        for variant in info["variants"]:
            other = name_to_id.get(normalize(variant))
            if other and other != did:
                uf.union(did, other)

    merged = 0
    for members in uf.groups().values():
        if len(members) < 2:
            continue
        # Официальный деп (name_en ∈ каталог) выигрывает канон, чтобы не потерять
        # авторитетный name_ru; иначе — по числу вариантов, затем по id.
        canonical = max(members, key=lambda d: (depts[d]["name_en"] in official, len(depts[d]["variants"]), d))
        for loser in (d for d in members if d != canonical):
            logger.info("Слияние департаментов: «%s» → «%s»", depts[loser]["name_en"], depts[canonical]["name_en"])
            if not dry_run:
                merge_department(cur, loser, canonical, depts)
            merged += 1
    if not dry_run:
        conn.commit()
    return merged


def merge_department(cur: sqlite3.Cursor, loser: str, canonical: str, depts: dict) -> None:
    canon_variants = depts[canonical]["variants"]
    existing = {normalize(v) for v in canon_variants}
    existing.add(normalize(depts[canonical]["name_en"]))
    for cand in [depts[loser]["name_en"], *depts[loser]["variants"]]:
        if normalize(cand) not in existing:
            canon_variants.append(cand)
            existing.add(normalize(cand))
    cur.execute(
        "UPDATE departments SET name_variants = ? WHERE id = ?",
        (json.dumps(canon_variants, ensure_ascii=False), canonical),
    )
    cur.execute("SELECT id, department FROM persons_itmo WHERE department LIKE ?", (f"%{loser}%",))
    for pid, field in cur.fetchall():
        ids = [x.strip() for x in field.split(";") if x.strip()]
        ids = list(dict.fromkeys(canonical if x == loser else x for x in ids))
        cur.execute("UPDATE persons_itmo SET department = ? WHERE id = ?", ("; ".join(ids), pid))
    for table in ("publication_departments", "repository_departments"):
        cur.execute(f"UPDATE OR IGNORE {table} SET department_id = ? WHERE department_id = ?", (canonical, loser))
        cur.execute(f"DELETE FROM {table} WHERE department_id = ?", (loser,))
    cur.execute("DELETE FROM departments WHERE id = ?", (loser,))


def dedup_persons(conn: sqlite3.Connection, dry_run: bool) -> int:
    cur = conn.cursor()
    cur.execute("SELECT id, name_en, name_variants, github FROM persons_itmo")
    by_name: dict[str, list[tuple]] = {}
    for pid, name_en, variants, github in cur.fetchall():
        by_name.setdefault(normalize(name_en), []).append((pid, name_en, variants, github))

    merged = 0
    for norm_name, rows in by_name.items():
        if not norm_name or len(rows) < 2:
            continue
        canonical = max(rows, key=lambda r: (bool(r[3]), len(load_variants(r[2])), r[0]))
        canon_id = canonical[0]
        for loser in (r for r in rows if r[0] != canon_id):
            logger.info("Слияние персон «%s»: %s → %s", canonical[1], loser[0], canon_id)
            if not dry_run:
                merge_person(cur, loser[0], canon_id)
            merged += 1
    if not dry_run:
        conn.commit()
    return merged


def merge_person(cur: sqlite3.Cursor, loser: str, canonical: str) -> None:
    cur.execute(
        "UPDATE OR IGNORE publication_authors SET person_id = ? WHERE person_id = ? AND person_type = 'itmo'",
        (canonical, loser),
    )
    cur.execute("DELETE FROM publication_authors WHERE person_id = ? AND person_type = 'itmo'", (loser,))
    cur.execute("UPDATE OR IGNORE repository_persons SET person_id = ? WHERE person_id = ?", (canonical, loser))
    cur.execute("DELETE FROM repository_persons WHERE person_id = ?", (loser,))
    cur.execute("SELECT name_variants, github FROM persons_itmo WHERE id = ?", (loser,))
    loser_row = cur.fetchone()
    cur.execute("SELECT name_variants, github FROM persons_itmo WHERE id = ?", (canonical,))
    canon_row = cur.fetchone()
    if loser_row and canon_row:
        merged_variants = list(dict.fromkeys(load_variants(canon_row[0]) + load_variants(loser_row[0])))
        cur.execute(
            "UPDATE persons_itmo SET name_variants = ?, github = COALESCE(github, ?) WHERE id = ?",
            (json.dumps(merged_variants, ensure_ascii=False), canon_row[1] or loser_row[1], canonical),
        )
    cur.execute("DELETE FROM persons_itmo WHERE id = ?", (loser,))


def _repo_norm(url: str) -> str:
    return url.rstrip("/").replace("-", "").lower()


def dedup_repositories(conn: sqlite3.Connection, dry_run: bool) -> int:
    """Удаляет github-репо-«склейки»: 404-репо, чей путь — префикс валидного двойника."""
    cur = conn.cursor()
    rows = cur.execute(
        "SELECT id, url, stars_num FROM repositories WHERE url LIKE 'https://github.com/%'"
    ).fetchall()
    valid = [url.rstrip("/") for _id, url, stars in rows if stars is not None]
    valid_norm = [_repo_norm(v) for v in valid]

    removed = 0
    for rid, url, stars in rows:
        if stars is not None:
            continue
        u = url.rstrip("/")
        un = _repo_norm(u)
        twin = next((v for v, vn in zip(valid, valid_norm) if u != v and (u.startswith(v) or un.startswith(vn))), None)
        if not twin:
            continue
        logger.info("Удаляю репо-склейку %s (двойник %s)", url, twin)
        if not dry_run:
            cur.execute(
                "UPDATE repo_links SET is_relevant = 0, "
                "llm_reason = COALESCE(llm_reason,'') || ' [removed: glued extraction artifact]' WHERE url = ?",
                (url,),
            )
            cur.execute("DELETE FROM repositories WHERE id = ?", (rid,))
        removed += 1
    if not dry_run:
        conn.commit()
    return removed


# --- SYNC ----------------------------------------------------

def sync_publications_code(conn: sqlite3.Connection) -> int:
    cur = conn.cursor()
    cur.execute("UPDATE publications SET has_code = 0, code_url = NULL")
    cur.execute(
        """
        SELECT publication_id, url FROM repo_links WHERE is_relevant = 1
        ORDER BY publication_id, COALESCE(llm_confidence, 0) DESC, id ASC
        """
    )
    urls_per_pub: dict[str, list[str]] = {}
    for pub_id, url in cur.fetchall():
        urls_per_pub.setdefault(pub_id, []).append(url)
    for pub_id, urls in urls_per_pub.items():
        cur.execute(
            "UPDATE publications SET has_code = 1, code_url = ? WHERE id = ?",
            (json.dumps(urls, ensure_ascii=False), pub_id),
        )
    conn.commit()
    return len(urls_per_pub)


def sync_publication_departments(conn: sqlite3.Connection) -> int:
    cur = conn.cursor()
    cur.execute("DELETE FROM publication_departments")
    cur.execute(
        """
        SELECT pa.publication_id, pi.department
        FROM publication_authors pa
        JOIN persons_itmo pi ON pi.id = pa.person_id AND pa.person_type = 'itmo'
        WHERE pi.department IS NOT NULL AND pi.department != ''
        """
    )
    person_rows = cur.fetchall()
    valid = {row[0] for row in cur.execute("SELECT id FROM departments")}
    pairs = {
        (pub_id, dept_id)
        for pub_id, field in person_rows
        for dept_id in (d.strip() for d in field.split(";"))
        if dept_id in valid
    }
    cur.executemany(
        "INSERT OR IGNORE INTO publication_departments (publication_id, department_id) VALUES (?, ?)",
        list(pairs),
    )
    conn.commit()
    return len(pairs)


def sync_repository_departments(conn: sqlite3.Connection) -> int:
    cur = conn.cursor()
    cur.execute("DELETE FROM repository_departments")
    cur.execute(
        """
        INSERT OR IGNORE INTO repository_departments (repository_id, department_id)
        SELECT DISTINCT rp.repository_id, pd.department_id
        FROM repository_publications rp
        JOIN publication_departments pd ON pd.publication_id = rp.publication_id
        """
    )
    count = cur.rowcount
    conn.commit()
    return count


def print_summary(conn: sqlite3.Connection) -> None:
    cur = conn.cursor()
    total = cur.execute("SELECT COUNT(*) FROM publications").fetchone()[0]
    with_code = cur.execute("SELECT COUNT(*) FROM publications WHERE has_code = 1").fetchone()[0]
    repos = cur.execute("SELECT COUNT(*) FROM repositories").fetchone()[0]
    gh_depts = cur.execute("SELECT COUNT(*) FROM github_departments").fetchone()[0]
    depts = cur.execute("SELECT COUNT(*) FROM departments").fetchone()[0]
    code_pct = f" ({with_code / total * 100:.1f}%)" if total else ""
    logger.info(
        "Публикаций: %d, с репо: %d%s | Репозиториев: %d | GitHub-орг: %d | Департаментов: %d",
        total, with_code, code_pct, repos, gh_depts, depts,
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Чистка дублей + сборка производных связей.")
    parser.add_argument("--dry-run", action="store_true", help="Только показать, что слил бы dedup (без sync).")
    return parser.parse_args()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    args = parse_args()
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        logger.info("=== dedup ===")
        d = dedup_departments(conn, args.dry_run)
        p = dedup_persons(conn, args.dry_run)
        r = dedup_repositories(conn, args.dry_run)
        prefix = "[dry-run] " if args.dry_run else ""
        logger.info("%sдепартаментов слито: %d, персон: %d, репо-склеек удалено: %d", prefix, d, p, r)
        if args.dry_run:
            return
        logger.info("=== sync ===")
        logger.info("[1/3] has_code: %d публикаций", sync_publications_code(conn))
        logger.info("[2/3] publication_departments: %d связей", sync_publication_departments(conn))
        logger.info("[3/3] repository_departments: %d связей", sync_repository_departments(conn))
        print_summary(conn)
    except Exception:
        logger.exception("Финализация упала с ошибкой")
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    main()

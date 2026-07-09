import argparse
import json
import logging
import re
import sqlite3
import time
import unicodedata

import fitz
import requests
from config import (
    BROWSER_USER_AGENT,
    DB_PATH,
    PAGE_SCRAPE_REQUEST_DELAY,
    PAGE_SCRAPE_TIMEOUT,
    SQLITE_TIMEOUT,
    pdf_path_for,
)

logger = logging.getLogger(__name__)

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS collected_emails (
    person_id TEXT,
    email     TEXT,
    source    TEXT,   -- pdf | page
    ref       TEXT,   -- publication_id (pdf) или URL страницы (page)
    found_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (person_id, email)
);
CREATE INDEX IF NOT EXISTS idx_collemail_person ON collected_emails(person_id);
CREATE INDEX IF NOT EXISTS idx_collemail_source ON collected_emails(source);
"""

TLD = (r"(?:ru|com|org|net|edu|gov|io|info|biz|name|eu|de|fr|uk|us|cn|jp|kr"
       r"|in|it|es|nl|se|fi|no|ch|at|cz|pl|by|kz|ua)")
EMAIL_RE = re.compile(rf"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+?\.{TLD}(?![A-Za-z])", re.I)
BRACE_RE = re.compile(rf"\{{([^{{}}@]+)\}}@([A-Za-z0-9.\-]+?\.{TLD})(?![A-Za-z])", re.I)  # {a,b}@domain
MAILTO_RE = re.compile(r"mailto:([A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,})", re.I)

SKIP_HOSTS = ("github.com", "linkedin.com", "scholar.google", "researchgate",
              "orcid.org", "twitter.com", "x.com", "facebook", "youtube",
              "t.me", "vk.com", "semanticscholar", "dblp.org", "publons")


# --- Общее: нормализация имён, привязка по фамилии -----------------------


def alpha(s: str | None) -> str:
    s = "".join(c for c in unicodedata.normalize("NFKD", s or "") if not unicodedata.combining(c))
    return re.sub(r"[^a-z]", "", s.lower())


def jloads(s):
    try:
        return json.loads(s) if s else []
    except (TypeError, ValueError):
        return []


def author_surnames(name_en: str, variants) -> set[str]:
    """Фамилии (последний токен) из name_en + name_variants, длиной >= 4."""
    surnames = set()
    for n in [name_en] + jloads(variants):
        toks = [alpha(t) for t in (n or "").split()]
        toks = [t for t in toks if t]
        if len(toks) >= 2 and len(toks[-1]) >= 4:
            surnames.add(toks[-1])
    return surnames


def match_authors(local_part: str, authors: list[tuple[str, set[str]]]) -> list[str]:
    """person_id, чья фамилия встречается в локальной части адреса."""
    lp = alpha(local_part)
    if not lp:
        return []
    return [pid for pid, surnames in authors if any(s in lp for s in surnames)]


def save(out: sqlite3.Connection, pid: str, email: str, source: str, ref: str | None) -> None:
    out.execute(
        "INSERT OR IGNORE INTO collected_emails (person_id, email, source, ref) VALUES (?, ?, ?, ?)",
        (pid, email, source, ref),
    )


# --- Источник: PDF ---------------------------------------------------------


def emails_from_pdf_text(text: str) -> set[str]:
    found = set()
    for inside, dom in BRACE_RE.findall(text):
        for part in re.split(r"[;,]", inside):
            part = part.strip().strip(".")
            if part:
                found.add(f"{part}@{dom}".lower())
    for m in EMAIL_RE.findall(text):
        found.add(m.lower().rstrip("."))
    return found


def emails_from_pdf(path) -> set[str]:
    try:
        doc = fitz.open(path)
    except Exception:
        return set()
    out: set[str] = set()
    for page in doc:
        out |= emails_from_pdf_text(page.get_text() or "")
    doc.close()
    return out


def load_pub_authors(main: sqlite3.Connection) -> dict[str, list[tuple[str, set[str]]]]:
    """publication_id -> [(person_id, {surnames})] по ИТМО-авторам."""
    rows = main.execute(
        """
        SELECT pa.publication_id, p.id, p.name_en, p.name_variants
        FROM publication_authors pa
        JOIN persons_itmo p ON p.id = pa.person_id
        WHERE pa.person_type = 'itmo'
        """
    ).fetchall()
    by_pub: dict[str, list[tuple[str, set[str]]]] = {}
    for pub_id, pid, name_en, variants in rows:
        surn = author_surnames(name_en, variants)
        if surn:
            by_pub.setdefault(pub_id, []).append((pid, surn))
    return by_pub


def run_pdf(main: sqlite3.Connection, out: sqlite3.Connection) -> None:
    by_pub = load_pub_authors(main)
    pubs = [(pid, pdf_path_for(pid)) for pid in by_pub]
    pubs = [(pid, path) for pid, path in pubs if path.exists()]
    logger.info("Статей с PDF и ИТМО-авторами: %d", len(pubs))

    stats = {"pdfs": 0, "emails": 0, "attributed": 0, "ambiguous": 0, "unmatched": 0}
    for i, (pub_id, path) in enumerate(pubs, 1):
        stats["pdfs"] += 1
        emails = emails_from_pdf(path)
        authors = by_pub[pub_id]
        for email in emails:
            stats["emails"] += 1
            matched = match_authors(email.split("@")[0], authors)
            if len(matched) == 1:
                save(out, matched[0], email, "pdf", pub_id)
                stats["attributed"] += 1
            elif len(matched) > 1:
                stats["ambiguous"] += 1
            else:
                stats["unmatched"] += 1
        if stats["pdfs"] % 100 == 0:
            out.commit()
            logger.info("[%d/%d] обработано", i, len(pubs))
    out.commit()

    logger.info("PDF прочитано: %d, Email встречено: %d, Привязано (1 автор): %d, Неоднозначных: %d, Без совпадения ФИО: %d",
                 stats['pdfs'], stats['emails'], stats['attributed'], stats['ambiguous'], stats['unmatched'])

# --- Источник: личные/лаб-страницы -----------------------------------------


def is_page(u: str | None) -> bool:
    u = (u or "").lower()
    return u.startswith("http") and not any(s in u for s in SKIP_HOSTS)


def deobfuscate(t: str) -> str:
    t = re.sub(r"&#0*64;|&#x0*40;|&commat;", "@", t, flags=re.I)
    t = re.sub(r"\s*[\[\(\{<]\s*at\s*[\]\)\}>]\s*", "@", t, flags=re.I)
    t = re.sub(r"\s*[\[\(\{<]\s*dot\s*[\]\)\}>]\s*", ".", t, flags=re.I)
    t = re.sub(r"\s*@\s*", "@", t)
    return t


def emails_from_html(html: str) -> set[str]:
    found = {m.lower() for m in MAILTO_RE.findall(html)}
    found |= {m.lower() for m in EMAIL_RE.findall(deobfuscate(html))}
    return found


def load_person_urls(main: sqlite3.Connection, prof: sqlite3.Connection) -> dict[str, set[str]]:
    urls: dict[str, set[str]] = {}
    for pid, ru in prof.execute(
        "SELECT person_id, researcher_urls FROM person_profiles WHERE researcher_urls IS NOT NULL"
    ):
        for u in (x.get("url") for x in jloads(ru)):
            if is_page(u):
                urls.setdefault(pid, set()).add(u)
    for pid, hp in prof.execute(
        "SELECT person_id, homepage FROM openreview_profiles WHERE homepage > ''"
    ):
        if is_page(hp):
            urls.setdefault(pid, set()).add(hp)
    return urls


def run_pages(main: sqlite3.Connection, out: sqlite3.Connection, limit: int | None) -> None:
    surn = {pid: author_surnames(name_en, variants) for pid, name_en, variants in
            main.execute("SELECT id, name_en, name_variants FROM persons_itmo WHERE name_en > ''")}
    person_urls = load_person_urls(main, out)
    done = {r[0] for r in out.execute(
        "SELECT DISTINCT person_id FROM collected_emails WHERE source = 'page'")}

    people = [(pid, us) for pid, us in person_urls.items()
              if pid in surn and surn[pid] and pid not in done]
    if limit:
        people = people[:limit]
    logger.info("Персон к обходу: %d", len(people))

    session = requests.Session()
    session.headers["User-Agent"] = BROWSER_USER_AGENT
    stats = {"persons": 0, "urls": 0, "found": 0}
    for i, (pid, urls) in enumerate(people, 1):
        stats["persons"] += 1
        for url in urls:
            stats["urls"] += 1
            try:
                r = session.get(url, timeout=PAGE_SCRAPE_TIMEOUT, allow_redirects=True)
            except requests.RequestException:
                continue
            if r.status_code != 200 or "html" not in r.headers.get("Content-Type", "").lower():
                continue
            for email in emails_from_html(r.text):
                if any(s in alpha(email.split("@")[0]) for s in surn[pid]):
                    save(out, pid, email, "page", url)
                    stats["found"] += 1
            time.sleep(PAGE_SCRAPE_REQUEST_DELAY)
        if stats["persons"] % 25 == 0:
            out.commit()
            logger.info("[%d/%d] найдено: %d", i, len(people), stats['found'])
    out.commit()

    logger.info("Персон обойдено: %d, URL проверено: %d, Привязок email: %d", stats['persons'], stats['urls'], stats['found'])


# --- Драйвер ---------------------------------------------------------------


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description="Email из PDF/страниц -> collected_emails.")
    # Два источника (--source): pdf — полный текст скачанных PDF 
    # pages — страницы из ORCID и openreview c деобфусцированием
    parser.add_argument("--source", choices=("pdf", "pages"), required=True)
    parser.add_argument("--limit", type=int, default=None, help="[pages] Сколько персон обойти.")
    args = parser.parse_args()

    main_db = sqlite3.connect(DB_PATH, timeout=SQLITE_TIMEOUT)
    out = sqlite3.connect(DB_PATH, timeout=SQLITE_TIMEOUT)
    out.executescript(SCHEMA_SQL)
    try:
        if args.source == "pdf":
            run_pdf(main_db, out)
        else:
            run_pages(main_db, out, args.limit)
    except Exception:
        logger.exception("collect_emails упал с ошибкой")
        raise
    finally:
        out.close()
        main_db.close()


if __name__ == "__main__":
    main()

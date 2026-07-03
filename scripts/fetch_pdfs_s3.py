"""Дозагружает недостающие PDF из внешнего S3-зеркала препринтов.

Дополнительный источник PDF к OpenAlex: S3-совместимое хранилище с
зеркалами arXiv, bioRxiv и medRxiv (PDF + распарсенный md + картинки).
Для каждой публикации, у которой ещё НЕТ локального файла
data/pdfs/{id}.pdf, скрипт пытается найти PDF в хранилище по
идентификатору и скачать его:

* bioRxiv/medRxiv — по DOI ``10.1101/...`` (берётся последняя версия);
* arXiv — по arXiv-id, извлечённому из DOI ``10.48550/arXiv...`` или из
  ссылки ``arxiv.org/abs|pdf/...`` в pdf_url.

Если в хранилище файла нет — публикация просто пропускается, БД не
меняется. Найденный PDF кладётся в data/pdfs/{id}.pdf; если pdf_url был
пустым маркером «нет OA», он перезаписывается на ``s3://bucket/key``.

Все действия идемпотентны: уже скачанные файлы не трогаются.
Настройки хранилища (эндпоинт, креды, бакеты) — в config.py / .env.

Запускать из корня проекта:
    uv run python scripts/fetch_pdfs_s3.py --limit 500
    uv run python scripts/fetch_pdfs_s3.py --dry-run     # только отчёт о матчах
    uv run python scripts/fetch_pdfs_s3.py --coverage    # % корпуса в зеркале, по годам
"""

import argparse
import re
import sqlite3
from pathlib import Path

import boto3
from botocore.client import BaseClient, Config
from botocore.exceptions import BotoCoreError, ClientError

from config import (
    DB_PATH,
    PDF_DIR,
    S3_ACCESS_KEY,
    S3_ARXIV_BUCKET,
    S3_ARXIV_PREFIX,
    S3_BIORXIV_BUCKET,
    S3_BIORXIV_PREFIX,
    S3_ENDPOINT_URL,
    S3_MEDRXIV_BUCKET,
    S3_MEDRXIV_PREFIX,
    S3_REGION,
    S3_SECRET_KEY,
    pdf_path_for,
)

PDF_MAGIC = b"%PDF"

# Суффикс версии в ключах bioRxiv/medRxiv: '...v1.3.pdf' (минорная часть
# опциональна на случай ключей вида '...v1.pdf').
VERSION_RE = re.compile(r"v(\d+)(?:\.(\d+))?\.pdf$", re.IGNORECASE)

# arXiv-id из DOI DataCite: '10.48550/arXiv.2401.01234'.
ARXIV_FROM_DOI_RE = re.compile(r"^10\.48550/arxiv\.(.+)$", re.IGNORECASE)

# arXiv-id из ссылки: 'arxiv.org/abs/2401.01234v2' или '/pdf/hep-th/0701001'.
ARXIV_FROM_URL_RE = re.compile(
    r"arxiv\.org/(?:abs|pdf)/(.+?)(?:v\d+)?(?:\.pdf)?$", re.IGNORECASE
)


# ----------------------------- нормализация id ----------------------------


def normalize_doi(doi: str | None) -> str | None:
    """Приводит DOI к каноничному виду ``10.xxxx/...`` (без URL-префикса, lower)."""
    if not doi:
        return None
    value = doi.strip().lower()
    for prefix in ("https://doi.org/", "http://doi.org/", "http://dx.doi.org/", "doi:"):
        if value.startswith(prefix):
            value = value[len(prefix) :]
    value = value.strip("/")
    return value or None


def extract_arxiv_id(doi_norm: str | None, pdf_url: str | None) -> str | None:
    """Достаёт arXiv-id из DOI DataCite или из ссылки arxiv.org, иначе None."""
    if doi_norm:
        match = ARXIV_FROM_DOI_RE.match(doi_norm)
        if match:
            return match.group(1).strip().lower()
    if pdf_url:
        match = ARXIV_FROM_URL_RE.search(pdf_url.strip())
        if match:
            return match.group(1).strip().lower()
    return None


# ------------------------------- S3-клиент --------------------------------


def make_s3_client() -> BaseClient:
    """S3-клиент к внешнему зеркалу препринтов (подпись s3v4)."""
    return boto3.client(
        "s3",
        endpoint_url=S3_ENDPOINT_URL,
        aws_access_key_id=S3_ACCESS_KEY,
        aws_secret_access_key=S3_SECRET_KEY,
        config=Config(signature_version="s3v4"),
        region_name=S3_REGION,
    )


# ------------------------------- резолверы --------------------------------


def resolve_preprint(s3: BaseClient, doi_norm: str) -> tuple[str, str] | None:
    """Ищет DOI ``10.1101/...`` в bioRxiv, затем medRxiv.

    Возвращает (bucket, key) последней версии PDF или None. DOI-часть ключа
    сверяется точно, чтобы аккешн-номер не «залипал» на префиксе
    (``000042`` не должен матчить ``0004267``).
    """
    for bucket, prefix in (
        (S3_BIORXIV_BUCKET, S3_BIORXIV_PREFIX),
        (S3_MEDRXIV_BUCKET, S3_MEDRXIV_PREFIX),
    ):
        expected_base = f"{prefix}{doi_norm}"
        candidates: list[tuple[tuple[int, int], str]] = []
        paginator = s3.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=bucket, Prefix=expected_base):
            for obj in page.get("Contents", []):
                key = obj["Key"]
                match = VERSION_RE.search(key)
                if match:
                    base = key[: match.start()]
                    version = (int(match.group(1)), int(match.group(2) or 0))
                elif key.lower().endswith(".pdf"):
                    base = key[:-4]
                    version = (0, 0)
                else:
                    continue
                if base != expected_base:
                    continue
                candidates.append((version, key))
        if candidates:
            candidates.sort()
            return bucket, candidates[-1][1]
    return None


def resolve_arxiv(s3: BaseClient, arxiv_id: str) -> tuple[str, str] | None:
    """Проверяет наличие ``article/{arxiv_id}.pdf`` в бакете arXiv."""
    key = f"{S3_ARXIV_PREFIX}{arxiv_id}.pdf"
    try:
        s3.head_object(Bucket=S3_ARXIV_BUCKET, Key=key)
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code")
        if code in ("404", "NoSuchKey", "NotFound"):
            return None
        raise
    return S3_ARXIV_BUCKET, key


def resolve(
    s3: BaseClient, doi_norm: str | None, pdf_url: str | None
) -> tuple[str, str] | None:
    """Единая точка: (bucket, key) для публикации или None, если не найдено."""
    if doi_norm and doi_norm.startswith("10.1101/"):
        hit = resolve_preprint(s3, doi_norm)
        if hit:
            return hit
    arxiv_id = extract_arxiv_id(doi_norm, pdf_url)
    if arxiv_id:
        return resolve_arxiv(s3, arxiv_id)
    return None


# --------------------------------- БД -------------------------------------


def fetch_targets(conn: sqlite3.Connection) -> list[tuple[str, str | None, str | None]]:
    """(id, doi, pdf_url) для публикаций с идентификатором и без локального PDF."""
    cur = conn.cursor()
    cur.execute(
        """
        SELECT id, doi, pdf_url
        FROM publications
        WHERE (doi IS NOT NULL AND doi != '')
           OR (pdf_url LIKE '%arxiv.org%')
        """
    )
    return [
        (pid, doi, url) for pid, doi, url in cur.fetchall() if not pdf_path_for(pid).exists()
    ]


def fetch_addressable(
    conn: sqlite3.Connection,
) -> list[tuple[str, str | None, str | None, int | None]]:
    """(id, doi, pdf_url, year) для всех публикаций с DOI или arXiv-ссылкой.

    В отличие от fetch_targets, наличие локального PDF не учитывается — это
    срез для отчёта о покрытии корпуса зеркалом, а не для дозагрузки.
    """
    cur = conn.cursor()
    cur.execute(
        """
        SELECT id, doi, pdf_url,
               COALESCE(year, CAST(substr(publication_date, 1, 4) AS INTEGER)) AS yr
        FROM publications
        WHERE (doi IS NOT NULL AND doi != '')
           OR (pdf_url LIKE '%arxiv.org%')
        """
    )
    return cur.fetchall()


def set_pdf_url(conn: sqlite3.Connection, publication_id: str, value: str) -> None:
    """Проставляет источник PDF (s3://...) там, где раньше стоял маркер «нет OA»."""
    conn.execute(
        "UPDATE publications SET pdf_url = ? WHERE id = ?", (value, publication_id)
    )
    conn.commit()


# ------------------------------ скачивание --------------------------------


def download_s3(s3: BaseClient, bucket: str, key: str, dest: Path) -> bool:
    """Качает объект в ``dest``. True, если файл действительно PDF."""
    try:
        s3.download_file(bucket, key, str(dest))
    except (ClientError, BotoCoreError) as exc:
        print(f"  ошибка скачивания s3://{bucket}/{key}: {exc}")
        dest.unlink(missing_ok=True)
        return False
    with dest.open("rb") as handle:
        if handle.read(4) != PDF_MAGIC:
            print(f"  скачанный объект не PDF: s3://{bucket}/{key}")
            dest.unlink(missing_ok=True)
            return False
    return True


# --------------------------------- main -----------------------------------


def run(conn: sqlite3.Connection, limit: int, dry_run: bool) -> None:
    """Основной проход: резолвит и (если не dry-run) скачивает недостающие PDF."""
    s3 = make_s3_client()
    targets = fetch_targets(conn)
    print(f"Кандидатов без локального PDF (с DOI/arXiv-ссылкой): {len(targets)}")

    matched = downloaded = 0
    for index, (pub_id, doi, pdf_url) in enumerate(targets, 1):
        if matched >= limit:
            break
        doi_norm = normalize_doi(doi)
        hit = resolve(s3, doi_norm, pdf_url)
        if not hit:
            continue
        matched += 1
        bucket, key = hit
        print(f"[{index}] {pub_id} -> s3://{bucket}/{key}")
        if dry_run:
            continue
        dest = pdf_path_for(pub_id)
        if download_s3(s3, bucket, key, dest):
            downloaded += 1
            if not pdf_url:
                set_pdf_url(conn, pub_id, f"s3://{bucket}/{key}")

    print(
        f"\nИтого: найдено в хранилище {matched}"
        + ("" if dry_run else f", скачано {downloaded}")
        + (" (dry-run, БД не менялась)" if dry_run else "")
    )


def pct(part: int, whole: int) -> str:
    """Доля в процентах или '—', если знаменатель нулевой."""
    return f"{100 * part / whole:.1f}%" if whole else "—"


def run_coverage(conn: sqlite3.Connection) -> None:
    """Отчёт: какая доля корпуса покрыта S3-зеркалом, включая разбивку по годам.

    Корпус НЕ ограничивается охватом зеркала — покрытие просто измеряется как
    метрика. Реальные обращения к S3 идут только для препринтов (bioRxiv/
    medRxiv/arXiv); журнальные DOI отсеиваются без сетевых вызовов.
    """
    s3 = make_s3_client()
    total_pubs = conn.execute("SELECT COUNT(*) FROM publications").fetchone()[0]
    rows = fetch_addressable(conn)

    matched = 0
    by_year: dict[object, list[int]] = {}
    for _pid, doi, pdf_url, year in rows:
        slot = by_year.setdefault(year if year is not None else "—", [0, 0])
        slot[1] += 1
        if resolve(s3, normalize_doi(doi), pdf_url):
            matched += 1
            slot[0] += 1

    addressable = len(rows)
    print(f"Всего публикаций в БД:          {total_pubs}")
    print(
        f"С идентификатором (DOI/arXiv):  {addressable} ({pct(addressable, total_pubs)} корпуса)"
    )
    print(
        f"Найдено в S3-зеркале:           {matched}"
        f" ({pct(matched, total_pubs)} корпуса, {pct(matched, addressable)} адресуемых)"
    )

    print("\nПокрытие по годам (в S3 / с идентификатором):")
    print(f"  {'Год':>6} {'Идентиф.':>9} {'В S3':>6} {'Покрытие':>9}")
    for year in sorted(by_year, key=lambda k: (isinstance(k, str), k)):
        found, addr = by_year[year]
        print(f"  {str(year):>6} {addr:>9} {found:>6} {pct(found, addr):>9}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Дозагружает недостающие PDF из внешнего S3-зеркала препринтов."
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=100_000,
        help="Макс. число публикаций, найденных в хранилище, за один запуск.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Только отчёт о матчах, без скачивания и записи в БД.",
    )
    parser.add_argument(
        "--coverage",
        action="store_true",
        help="Отчёт о покрытии корпуса зеркалом (по годам), без скачивания.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if not S3_ENDPOINT_URL:
        print("S3_ENDPOINT_URL не задан в .env — источник отключён, выхожу.")
        return
    conn = sqlite3.connect(DB_PATH)
    try:
        if args.coverage:
            run_coverage(conn)
        else:
            PDF_DIR.mkdir(parents=True, exist_ok=True)
            run(conn, limit=args.limit, dry_run=args.dry_run)
    finally:
        conn.close()


if __name__ == "__main__":
    main()

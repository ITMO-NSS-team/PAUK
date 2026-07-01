"""Общая конфигурация проекта."""

import os
from pathlib import Path

from dotenv import load_dotenv

# --- Файловая система ---------------------------------------------------

ROOT_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT_DIR / "data"
DB_PATH = DATA_DIR / "itmo_research_opensource.db"
PDF_DIR = DATA_DIR / "pdfs"


def pdf_path_for(publication_id: str) -> Path:
    """Локальный путь до PDF этой публикации (соглашение: data/pdfs/{id}.pdf)."""
    return PDF_DIR / f"{publication_id}.pdf"

load_dotenv(ROOT_DIR / ".env")


# --- OpenAlex API --------------------------------------------------------

OPENALEX_WORKS_URL = "https://api.openalex.org/works"
OPENALEX_AUTHORS_URL = "https://api.openalex.org/authors"
OPENALEX_API_KEY = os.getenv("OPENALEX_API_KEY", "")

USER_AGENT_EMAIL = os.getenv("USER_AGENT_EMAIL", "anonymous@example.com")
USER_AGENT = f"ITMO-Research-Monitor/1.0 ({USER_AGENT_EMAIL})"

BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/130.0.0.0 Safari/537.36"
)

REQUEST_DELAY = 0.1


# --- ИТМО как организация -----------------------------------------------

# Используется populate_publications.py для фильтрации работ ИТМО.
ITMO_ROR_ID = "04txgxn49"
ITMO_NAMES = ["ITMO University", "ITMO"]


# --- Загрузка авторов ---------------------------------------------------

AUTHORS_CACHE_CAPACITY = 2000
AFFILIATION_SEPARATOR = " \n "


# --- Скачивание PDF ------------------------------------------------------

FETCH_BATCH_SIZE = 10
DOWNLOAD_TIMEOUT = 60


# --- LLM-классификация ссылок --------------------------------------------

OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"

LLM_MODEL = os.getenv("LLM_MODEL", "openai/gpt-4o-mini")

CLASSIFY_BATCH_SIZE = 50


# --- S3-источник PDF (внешнее зеркало препринтов) ------------------------

# Внешнее S3-совместимое хранилище (MinIO) с PDF/MD препринтов arXiv,
# bioRxiv и medRxiv. Используется fetch_pdfs_s3.py как дополнительный
# источник PDF для публикаций, которым не хватило файла из OpenAlex.
# Креды и эндпоинт берутся из .env (в репозиторий не коммитятся).
S3_ENDPOINT_URL = os.getenv("S3_ENDPOINT_URL", "")
S3_ACCESS_KEY = os.getenv("S3_ACCESS_KEY", "")
S3_SECRET_KEY = os.getenv("S3_SECRET_KEY", "")
S3_REGION = os.getenv("S3_REGION", "us-east-1")

# Раскладка ключей в хранилище (см. разведку структуры бакетов):
#   arXiv:    bucket 'articles',  key 'article/{arxiv_id}.pdf'
#   bioRxiv:  bucket 'biorxiv',   key 'article/biorxiv/{doi}v{ver}.pdf'
#   medRxiv:  bucket 'medrxiv',   key 'article/medrxiv/{doi}v{ver}.pdf'
S3_ARXIV_BUCKET = "articles"
S3_ARXIV_PREFIX = "article/"
S3_BIORXIV_BUCKET = "biorxiv"
S3_BIORXIV_PREFIX = "article/biorxiv/"
S3_MEDRXIV_BUCKET = "medrxiv"
S3_MEDRXIV_PREFIX = "article/medrxiv/"


# --- Извлечение ссылок на репозитории ------------------------------------

SUPPORTED_HOSTS = [
    "github.com",
    "gitlab.com",
    "bitbucket.org",
    "codeberg.org",
    "gitee.com",
    "huggingface.co",
    "zenodo.org",
    "figshare.com",
    "osf.io",
]

# Сколько символов с каждой стороны URL сохраняется в repo_links.context.
CONTEXT_RADIUS = 400

# Знаки препинания, которые часто стоят сразу после URL; срезаются перед
# сохранением, чтобы не таскать их внутри URL.
URL_TRAILING_PUNCT = ".,;:!?)]}>\"'"

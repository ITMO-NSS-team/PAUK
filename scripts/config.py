"""Общая конфигурация проекта."""

import os
from pathlib import Path

from dotenv import load_dotenv

# --- Файловая система ---------------------------------------------------

ROOT_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT_DIR / "data"
DB_PATH = DATA_DIR / "itmo_research_opensource.db"
PDF_DIR = DATA_DIR / "pdfs"

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
DOWNLOAD_TIMEOUT = 30


# --- LLM-классификация ссылок --------------------------------------------

OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"

LLM_MODEL = os.getenv("LLM_MODEL", "openai/gpt-4o-mini")

CLASSIFY_BATCH_SIZE = 50


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
CONTEXT_RADIUS = 200

# Знаки препинания, которые часто стоят сразу после URL; срезаются перед
# сохранением, чтобы не таскать их внутри URL.
URL_TRAILING_PUNCT = ".,;:!?)]}>\"'"

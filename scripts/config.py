"""Общая конфигурация проекта"""

from pathlib import Path

# --- Файловая система ---------------------------------------------------

ROOT_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT_DIR / "data"
DB_PATH = DATA_DIR / "itmo_research_opensource.db"
PDF_DIR = DATA_DIR / "pdfs"


# --- OpenAlex API --------------------------------------------------------

OPENALEX_WORKS_URL = "https://api.openalex.org/works"
OPENALEX_AUTHORS_URL = "https://api.openalex.org/authors"
OPENALEX_API_KEY = "JqvcQC7jsVmM3giPeVibBo"
USER_AGENT = "ITMO-Research-Monitor/1.0 (pyrolusilicate@gmail.com)"
REQUEST_DELAY = 0.1


# --- ИТМО как организация -----------------------------------------------

# Используется populate_publications.py для фильтрации работ, аффилированных с ИТМО.
ITMO_ROR_ID = "04txgxn49"
ITMO_NAMES = ["ITMO University", "ITMO"]


# --- Загрузка авторов ---------------------------------------------------

# Размер in-memory LRU-кэша для запросов к OpenAlex /authors.
AUTHORS_CACHE_CAPACITY = 2000
AFFILIATION_SEPARATOR = " \n "


# --- Скачивание PDF ------------------------------------------------------

# Сколько публикаций обрабатывать за один запуск fetch_papers.py.
FETCH_BATCH_SIZE = 10

# Таймаут на один HTTP-запрос (секунды).
DOWNLOAD_TIMEOUT = 30


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

CONTEXT_RADIUS = 200

# Знаки препинания, которые часто стоят сразу после URL в тексте; срезаются
# перед сохранением, чтобы не таскать их внутри URL.
URL_TRAILING_PUNCT = ".,;:!?)]}>\"'"

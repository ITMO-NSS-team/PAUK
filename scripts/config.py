import os
from pathlib import Path

from dotenv import load_dotenv

# --- Пути ---------------------------------------------------------------

ROOT_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT_DIR / "data"
DB_PATH = Path(os.environ.get("PAUK_DB_PATH", DATA_DIR / "itmo_opensource.db"))
PDF_DIR = DATA_DIR / "pdfs"


def pdf_path_for(publication_id: str) -> Path:
    return PDF_DIR / f"{publication_id}.pdf"


load_dotenv(ROOT_DIR / ".env")

# --- Ключи (.env) -------------------------------------------------------

OPENALEX_API_KEY = os.getenv("OPENALEX_API_KEY", "")
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "")

# --- Эндпоинты ----------------------------------------------------------

OPENALEX_WORKS_URL = "https://api.openalex.org/works"
OPENALEX_AUTHORS_URL = "https://api.openalex.org/authors"
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
GITHUB_API_URL = "https://api.github.com"
PDF_CRAWLER_URL = "http://localhost:8000/api/v1"

# --- HTTP ---------------------------------------------------------------

USER_AGENT = "ITMO-Research-Monitor/1.0 (cake@gmail.com)"
BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
)
REQUEST_DELAY = 0.1
DOWNLOAD_TIMEOUT = 60
CRAWLER_DOWNLOAD_TIMEOUT = 300

# --- ИТМО как организация ------------------------------------------------

ITMO_ROR_ID = "04txgxn49"
ITMO_NAMES = ["ITMO University", "ITMO"]
AUTHORS_CACHE_CAPACITY = 2000

# --- LLM-модели и батчи --------------------------------------------------

CLASSIFY_MODEL = "deepseek/deepseek-v4-pro"      # ссылка авторов или чужая

DEPT_MODEL = "deepseek/deepseek-v4-pro"          # сопоставление департаментов
DEPT_CHUNK_SIZE = 20
DEPT_SLEEP_BETWEEN_CHUNKS = 1.0
DEPT_TIMEOUT = 240                               # длинный контекст + reasoning

PERSONS_RU_MODEL = "openai/gpt-4o-mini"          # транслитерация ФИО
PERSONS_RU_CHUNK_SIZE = 50
PERSONS_RU_SLEEP_BETWEEN_CHUNKS = 0.3

LLM_RATE_LIMIT_SLEEP = 30                        # пауза при 429 от OpenRouter

# --- Извлечение ссылок ---------------------------------------------------

CONTEXT_RADIUS = 400         # символов вокруг URL в repo_links.context

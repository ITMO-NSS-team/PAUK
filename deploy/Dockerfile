# The PAUK image: the admin panel and the worker run from it with different
# commands. Built from the repository root (see compose.yml, `context: ..`).
FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.12.21 /uv /uvx /bin/

# git: OSA clones the repositories it reports on.
RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    PYTHONUNBUFFERED=1 \
    HOME=/home/pauk \
    PATH=/app/.venv/bin:$PATH

WORKDIR /app

# Dependencies first, so a code change does not reinstall them.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY . .
RUN uv sync --frozen --no-dev \
    && mkdir -p /home/pauk && chmod 777 /home/pauk

CMD ["pauk", "--help"]

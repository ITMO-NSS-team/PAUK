import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

DEFAULT_CONFIG = {"model": "openai/gpt-4o", "no_fork": True, "no_pull_request": True}
MAX_WORKERS = 5

def build_arguments(url: str, config: dict[str, str] | None = None):
    args = [sys.executable, "-m", "osa_tool.run", "-r", url]

    if config is not None:
        mapping = {
            "branch": "--branch",
            "output": "-o",
            "api": "--api",
            "base_url": "--base-url",
            "model": "--model",
            "attachment": "--attachment",
            "top_p": "--top_p",
            "temperature": "--temperature",
            "max_tokens": "--max_tokens",
            "context_window": "--context_window",
        }

        for key, flag in mapping.items():
            value = config.get(key)
            if value is not None:
                args += [flag, str(value)]

        if config.get("delete_dir"):
            args.append("--delete-dir")
        if config.get("no_fork"):
            args.append("--no-fork")
        if config.get("no_pull_request"):
            args.append("--no-pull-request")

    args += ["--docstring", "--mode", "basic", "--web-mode"]

    return args


def process_url(url: str):
    """Функция-обертка для запуска одного процесса."""
    try:
        subprocess.run(
            build_arguments(url=url.strip(), config=DEFAULT_CONFIG),
            check=True,
            text=True,
        )
    except subprocess.CalledProcessError as e:
        print(f"Ошибка при обработке {url}: {e}")

def main():
    with open("links.txt") as file:
        urls = [line.strip() for line in file if line.strip()]

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        executor.map(process_url, urls)


if __name__ == "__main__":
    main()

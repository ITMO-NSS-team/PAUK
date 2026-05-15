import subprocess
import sys

DEFAULT_CONFIG = {"model": "openai/gpt-4o", "no_fork": True, "no_pull_request": True}


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


def main():
    with open("links.txt") as file:
        line = file.readline()
        while line:
            subprocess.run(
                build_arguments(url=line.replace("\n", ""), config=DEFAULT_CONFIG),
                check=True,
                text=True,
            )
            line = file.readline()


if __name__ == "__main__":
    main()

"""Запускает ADK-клиент в отдельном окружении против fixture-сервера PAUK."""

from __future__ import annotations

import argparse
import importlib.util
import json
import logging
import os
import secrets
import socket
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
LOGGER = logging.getLogger("pauk_adk_gate")


def support_module() -> Any:
    """Подключает существующие фикстуры без копирования реализации сервера."""
    repo = ROOT.parents[2]
    sys.dont_write_bytecode = True
    sys.path.insert(0, str(repo))
    spec = importlib.util.spec_from_file_location("pauk_adk_fixture_support", repo / "tests" / "mcp_support.py")
    if spec is None or spec.loader is None:
        raise RuntimeError("Fixture module unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def invoke_client(python: Path, url: str, unavailable: str, token: str) -> dict[str, Any]:
    """Передаёт только синтетический токен и выводит обезличенный JSON-итог."""
    env = {key: os.environ[key] for key in ("SYSTEMROOT", "WINDIR", "TEMP", "TMP", "PATH") if key in os.environ}
    env.update(PAUK_MCP_TEST_TOKEN=token, GOOGLE_API_USE_CLIENT_CERTIFICATE="false", PYTHONIOENCODING="utf-8")
    command = [
        str(python.absolute()),
        str(ROOT / "client.py"),
        "--url",
        url,
        "--cases",
        str(ROOT / "cases.json"),
        "--unreachable-url",
        unavailable,
    ]
    result = subprocess.run(
        command, cwd=ROOT, env=env, capture_output=True, text=True, encoding="utf-8", timeout=180, check=False
    )
    combined = result.stdout + result.stderr
    if token in combined:
        raise RuntimeError("Child reflected token; output blocked")
    reports = [json.loads(line) for line in combined.splitlines() if line.startswith('{"status":')]
    if result.returncode != 0 or len(reports) != 1 or reports[0].get("status") != "passed":
        raise RuntimeError("ADK compatibility client failed; child output suppressed")
    return reports[0]


def compatibility(python: Path) -> dict[str, Any]:
    """Поднимает реальное приложение только с существующим SyntheticReader."""
    support = support_module()
    from pauk.mcp.server import create_app
    from pauk.mcp.service import Service

    token = secrets.token_urlsafe(40)
    app = create_app(Service(support.SyntheticReader(), secrets.token_bytes(32), safe_synthetic_text=True), token)
    with support.live_server(app) as url, socket.socket() as reserved:
        reserved.bind(("127.0.0.1", 0))
        unavailable = f"http://127.0.0.1:{reserved.getsockname()[1]}/mcp"
        return invoke_client(python, url, unavailable, token)


def main() -> int:
    """Требует явный Python отдельного клиента; секреты в CLI не принимает."""
    logging.basicConfig(level=logging.CRITICAL, format="%(message)s")
    LOGGER.setLevel(logging.INFO)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--client-python", required=True, type=Path)
    args = parser.parse_args()
    try:
        report = compatibility(args.client_python)
    except (Exception, KeyboardInterrupt) as error:
        LOGGER.error(
            json.dumps(
                {
                    "status": "failed",
                    "error_type": type(error).__name__,
                    "details": "suppressed to protect credentials and fixture data",
                }
            )
        )
        return 1
    LOGGER.info(json.dumps(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Live page for scripts/run_institutions.py: which university, which step, how long.

Serves one page on localhost that polls a JSON status built from what the
run leaves behind: `<instances>/state.json` (finished steps), the per-key
logs (step headers, warnings, the tail), the `cli_progress` record each
`pauk run`/`pauk enrich` keeps in the university's MongoDB database
(stage and row counts), and `ps` (is it alive, is it spinning).

Run from the project root, then open the printed address:
    uv run python scripts/watch_institutions.py ~/pauk-ufjf-data/scale/keys.txt
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import subprocess
import sys
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from pymongo import MongoClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pauk import institution as institutions  # noqa: E402
from pauk.pipeline.progress_record import COLLECTION  # noqa: E402
from pauk.pipeline.stages import ALL_STAGES  # noqa: E402

STEPS = ("run", "social_graph", "publish", "dedup_graph")
# How each step's command line starts in a key's log header.
STEP_COMMANDS = {"run": "run ", "social_graph": "enrich social_graph", "publish": "publish graph",
                 "dedup_graph": "dedup graph"}
RUN_STAGES = ("collect", "normalize", *(stage.name for stage in ALL_STAGES))
# PipelineRunner names its first two stages for the admin panel, in Russian.
STAGE_ALIASES = {"выгрузка из OpenAlex": "collect", "разбор выгруженного": "normalize"}
HEADER = re.compile(r"^=== (\d{4}-\d\d-\d\d \d\d:\d\d:\d\d) pauk (.*)$")
STAMP = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d),\d+ \[(\w+)\] (.*)$")
WAITING = re.compile(r"waiting (\d+)s until (\S+) before retrying (\w+) (\S+)")
NOISE = re.compile(r"MuPDF|resource_tracker|warnings\.warn|^\s*$")
TAIL_BYTES = 256 * 1024


def local(text: str) -> datetime:
    return datetime.strptime(text, "%Y-%m-%d %H:%M:%S").astimezone()


def iso(moment: datetime | None) -> str | None:
    return moment.astimezone(UTC).isoformat() if moment else None


class Watcher:
    def __init__(self, keys_file: Path, instances: Path, mongo_uri: str, sizes: list[Path]) -> None:
        self.keys_file, self.instances = keys_file, instances
        self.mongo = MongoClient(mongo_uri, serverSelectionTimeoutMS=1500)
        self.works = self._works(sizes)
        self._headers: dict[str, tuple[float, int, list[tuple[datetime, str]]]] = {}

    @staticmethod
    def _works(sizes: list[Path]) -> dict[str, int]:
        """Works per year by ROR, from planning lists (for the ETA only); later files win.

        JSON: a list of {ror, works}; CSV: columns ror (bare id or ror.org URL) and works.
        """
        works: dict[str, int] = {}
        for path in sizes:
            if not path.exists():
                continue
            if path.suffix == ".csv":
                rows = list(csv.DictReader(path.open(encoding="utf-8")))
            else:
                rows = json.loads(path.read_text())
            works |= {row["ror"].rsplit("/", 1)[-1]: int(row["works"]) for row in rows if row.get("ror")}
        return works

    def key_list(self) -> list[str]:
        return [line.split()[0] for line in self.keys_file.read_text().splitlines() if line.strip()]

    def headers(self, key: str) -> list[tuple[datetime, str]]:
        """(time, command) of every pauk command the orchestrator started for this key."""
        log = self.instances / f"{key}.log"
        if not log.exists():
            return []
        stat = log.stat()
        cached = self._headers.get(key)
        if cached and cached[:2] == (stat.st_mtime, stat.st_size):
            return cached[2]
        found = []
        with log.open(encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if line.startswith("=== ") and (match := HEADER.match(line.rstrip("\n"))):
                    found.append((local(match[1]), match[2]))
        self._headers[key] = (stat.st_mtime, stat.st_size, found)
        return found

    def tail(self, key: str, lines: int = 60) -> tuple[list[str], datetime | None]:
        log = self.instances / f"{key}.log"
        if not log.exists():
            return [], None
        with log.open("rb") as fh:
            fh.seek(max(0, log.stat().st_size - TAIL_BYTES))
            text = fh.read().decode("utf-8", errors="replace")
        kept = [line for line in text.splitlines() if not NOISE.search(line)]
        # Only the latest command: a resumed step's log still holds the failed attempt.
        starts = [i for i, line in enumerate(kept) if line.startswith("=== ")]
        if starts:
            kept = kept[starts[-1]:]
        return kept[-lines:], datetime.fromtimestamp(log.stat().st_mtime).astimezone()

    def processes(self) -> tuple[bool, dict[str, Any] | None]:
        out = subprocess.run(["ps", "-axo", "pid=,etime=,%cpu=,rss=,command="],
                             capture_output=True, text=True, check=False).stdout
        orchestrator, pauk = False, None
        for line in out.splitlines():
            parts = line.split(None, 4)
            if len(parts) < 5:
                continue
            pid, etime, cpu, rss, command = parts
            if "run_institutions.py" in command and "python" in command:
                orchestrator = True
            if ".venv/bin/pauk " in command or command.rstrip().endswith(".venv/bin/pauk"):
                pauk = {"pid": int(pid), "etime": etime, "cpu": float(cpu), "rss_mb": int(rss) // 1024,
                        "command": command.split(".venv/bin/pauk", 1)[-1].strip()}
        return orchestrator, pauk

    def progress(self, key: str) -> dict[str, Any] | None:
        try:
            db = self.mongo[institutions.get(key).mongo_db]
            row = db[COLLECTION].find_one({"finished_at": None}, sort=[("updated_at", -1)]) or \
                db[COLLECTION].find_one(sort=[("updated_at", -1)])
        except Exception:
            return None
        if not row:
            return None
        stage = row.get("stage")
        row["stage"] = STAGE_ALIASES.get(stage, stage)
        return {k: iso(v) if isinstance(v, datetime) else v for k, v in row.items()}

    def institution(self, key: str) -> dict[str, Any]:
        try:
            profile = institutions.get(key)
        except Exception:
            return {"name": key, "short": key, "works": None}
        return {"name": profile.name_en, "short": profile.short_name, "works": self.works.get(profile.ror_id)}

    def status(self) -> dict[str, Any]:
        state_path = self.instances / "state.json"
        state = json.loads(state_path.read_text()) if state_path.exists() else {}
        orchestrator, pauk = self.processes()
        keys = self.key_list()
        rows = []
        current = None
        for index, key in enumerate(keys):
            done = state.get(key, {})
            headers = self.headers(key)
            steps = {}
            for step in STEPS:
                starts = [moment for moment, command in headers if command.startswith(STEP_COMMANDS[step])]
                finished = local(done[step]) if done.get(step) else None
                started = max((m for m in starts if not finished or m <= finished), default=None)
                steps[step] = {"started": iso(started), "finished": iso(finished),
                               "seconds": (finished - started).total_seconds() if started and finished else None}
            complete = all(done.get(step) for step in STEPS)
            first = min((m for m, _ in headers), default=None)
            last_done = max((local(v) for v in done.values()), default=None)
            row = {"key": key, "index": index + 1, **self.institution(key), "steps": steps,
                   "complete": complete, "started": iso(first), "finished": iso(last_done) if complete else None,
                   "seconds": sum(s["seconds"] or 0 for s in steps.values()) if complete else None,
                   "status": "done" if complete else ("partial" if done else "pending")}
            rows.append(row)
            if current is None and not complete and headers and orchestrator:
                current = row
        if current is not None:
            current["status"] = "running"
            current.update(self.detail(current["key"], pauk))
        finished = [r for r in rows if r["complete"] and r["seconds"] and r["works"]]
        per_work = (sum(r["seconds"] for r in finished) / sum(r["works"] for r in finished)) if finished else None
        return {"now": iso(datetime.now(UTC)), "orchestrator": orchestrator, "process": pauk,
                "run_stages": RUN_STAGES, "steps": STEPS, "rows": rows,
                "current": current["key"] if current else None, "seconds_per_work": per_work}

    def detail(self, key: str, pauk: dict[str, Any] | None) -> dict[str, Any]:
        tail, log_mtime = self.tail(key)
        headers = self.headers(key)
        step = None
        if headers:
            command = headers[-1][1]
            step = next((s for s, prefix in STEP_COMMANDS.items() if command.startswith(prefix)), None)
        waiting = None
        for line in reversed(tail[-40:]):
            if match := WAITING.search(line):
                until = datetime.fromisoformat(match[2])
                if until > datetime.now().astimezone():
                    waiting = {"seconds": int(match[1]), "until": iso(until), "url": match[4]}
                break
        warnings = [line for line in tail if "[WARNING]" in line or "[ERROR]" in line or "Traceback" in line][-8:]
        return {"step": step, "step_started": iso(headers[-1][0]) if headers else None,
                "progress": self.progress(key), "log_updated": iso(log_mtime), "waiting": waiting,
                "warnings": warnings}


PAGE = (Path(__file__).resolve().parent / "watch_institutions.html")


def handler(watcher: Watcher) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args: Any) -> None:
            pass

        def _send(self, body: bytes, kind: str) -> None:
            self.send_response(200)
            self.send_header("Content-Type", kind)
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            url = urlparse(self.path)
            if url.path == "/api/status":
                self._send(json.dumps(watcher.status(), ensure_ascii=False).encode(), "application/json")
            elif url.path == "/api/log":
                key = parse_qs(url.query).get("key", [""])[0]
                if key not in watcher.key_list():
                    self.send_error(404)
                    return
                lines, _ = watcher.tail(key, 200)
                self._send(json.dumps(lines, ensure_ascii=False).encode(), "application/json")
            elif url.path == "/":
                self._send(PAGE.read_bytes(), "text/html; charset=utf-8")
            else:
                self.send_error(404)

    return Handler


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("keys", type=Path, help="the same list file run_institutions.py was given")
    parser.add_argument("--instances", type=Path, default=Path.home() / "pauk-ufjf-data" / "neo4j")
    parser.add_argument("--mongo-uri", default="mongodb://127.0.0.1:27117")
    parser.add_argument("--sizes", type=Path, nargs="+",
                        default=[Path.home() / "pauk-ufjf-data" / "scale" / "sizes2.json", ROOT / "universities.csv"],
                        help="planning lists with works per ROR (JSON or CSV), for the time-left estimate")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    watcher = Watcher(args.keys, args.instances, args.mongo_uri, args.sizes)
    server = ThreadingHTTPServer(("127.0.0.1", args.port), handler(watcher))
    print(f"http://127.0.0.1:{args.port}/", flush=True)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())

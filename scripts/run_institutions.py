"""Build one graph per university, one university after another, on this machine.

For each profile key: start the university's own Neo4j instance, run the
pipeline for the period, walk the GitHub social graph, publish the group,
deduplicate the graph, stop the instance. A step that finished is recorded in
`<instances>/state.json` and skipped on the next start, so an interrupted run
resumes where it stopped.

One after another rather than in parallel: the universities share this
machine's API quotas (ORCID's daily reads above all), and each stage already
runs its requests in parallel inside those limits.

Each university gets:
  - MongoDB database pauk_<key> on MONGO_URI (Institution.mongo_db);
  - a Neo4j Community instance in <instances>/<key>/ (Community has one
    database per instance), Bolt port 7710 + 2*i, HTTP 9710 + i (8710 + i before 2026-10-23; the monitor serves on 8765), where i is
    the key's position in the list file;
  - group name <key>-<from>-<to>.

Needs NEO4J_HOME (a Neo4j 5+/2026 tarball install) and JAVA_HOME for it.

Run from the project root:
    uv run python scripts/run_institutions.py keys.txt --from 2025-10-21 --to 2026-10-21
    uv run python scripts/run_institutions.py keys.txt --only mit,ethz
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

from neo4j import GraphDatabase

logger = logging.getLogger("run_institutions")

ROOT = Path(__file__).resolve().parents[1]
STEPS = ("run", "social_graph", "publish", "dedup_graph")
# Settings this script owns in every instance's neo4j.conf.
OWNED = ("server.directories.data", "server.directories.logs", "server.directories.run",
         "server.bolt.listen_address", "server.http.listen_address", "server.https.enabled",
         "server.default_listen_address", "server.memory.heap.initial_size", "server.memory.heap.max_size",
         "server.memory.pagecache.size")


class Instance:
    def __init__(self, home: Path, root: Path, key: str, index: int, password: str) -> None:
        self.home, self.dir, self.key, self.password = home, root / key, key, password
        self.bolt, self.http = 7710 + 2 * index, 9710 + index
        # An instance keeps the ports it was created with: editing the list
        # moves a key's position, and the next start would wait on the wrong port.
        conf = self.dir / "conf" / "neo4j.conf"
        if conf.exists():
            text = conf.read_text(encoding="utf-8")
            if bolt := re.search(r"^server\.bolt\.listen_address=:(\d+)$", text, re.M):
                self.bolt = int(bolt.group(1))
            if http := re.search(r"^server\.http\.listen_address=:(\d+)$", text, re.M):
                self.http = int(http.group(1))
        self.env = {**os.environ, "NEO4J_CONF": str(self.dir / "conf")}

    @property
    def uri(self) -> str:
        return f"bolt://127.0.0.1:{self.bolt}"

    def prepare(self) -> None:
        conf = self.dir / "conf"
        if (conf / "neo4j.conf").exists():
            return
        conf.mkdir(parents=True)
        base = (self.home / "conf" / "neo4j.conf").read_text(encoding="utf-8").splitlines()
        lines = [line for line in base if line.split("=", 1)[0].strip() not in OWNED]
        lines += [
            f"server.directories.data={self.dir / 'data'}",
            f"server.directories.logs={self.dir / 'logs'}",
            f"server.directories.run={self.dir / 'run'}",
            f"server.bolt.listen_address=:{self.bolt}",
            f"server.http.listen_address=:{self.http}",
            "server.https.enabled=false",
            "server.default_listen_address=127.0.0.1",
            "server.memory.heap.initial_size=512m",
            "server.memory.heap.max_size=2g",
            "server.memory.pagecache.size=1g",
        ]
        (conf / "neo4j.conf").write_text("\n".join(lines) + "\n", encoding="utf-8")
        for name in ("neo4j-admin.conf", "server-logs.xml", "user-logs.xml"):
            if (self.home / "conf" / name).exists():
                shutil.copy(self.home / "conf" / name, conf / name)
        subprocess.run([str(self.home / "bin" / "neo4j-admin"), "dbms", "set-initial-password", self.password],
                       env=self.env, check=True, capture_output=True)

    def start(self) -> None:
        self.prepare()
        subprocess.run([str(self.home / "bin" / "neo4j"), "start"], env=self.env, check=True, capture_output=True)
        driver = GraphDatabase.driver(self.uri, auth=("neo4j", self.password))
        try:
            for _ in range(90):
                try:
                    driver.verify_connectivity()
                    return
                except Exception:
                    time.sleep(2)
            raise RuntimeError(f"Neo4j for {self.key} did not come up on {self.uri}")
        finally:
            driver.close()

    def stop(self) -> None:
        subprocess.run([str(self.home / "bin" / "neo4j"), "stop"], env=self.env, check=False, capture_output=True)


def pauk(args: list[str], env: dict[str, str], log: Path) -> None:
    with log.open("a", encoding="utf-8") as fh:
        fh.write(f"\n=== {time.strftime('%Y-%m-%d %H:%M:%S')} pauk {' '.join(args)}\n")
        fh.flush()
        result = subprocess.run(["uv", "run", "pauk", *args], cwd=ROOT, env=env, stdout=fh, stderr=subprocess.STDOUT)
    if result.returncode:
        raise RuntimeError(f"pauk {' '.join(args)} failed ({result.returncode}), see {log}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("keys", type=Path, help="file with one profile key per line (first word of each line)")
    parser.add_argument("--from", dest="date_from", required=True)
    parser.add_argument("--to", dest="date_to", required=True)
    parser.add_argument("--instances", type=Path, default=Path.home() / "pauk-ufjf-data" / "neo4j")
    parser.add_argument("--neo4j-password", default=os.getenv("PAUK_INSTANCE_NEO4J_PASSWORD", "paukLocalTest1"))
    parser.add_argument("--only", default="", help="comma-separated keys to run, in list order")
    parser.add_argument("--mongo-uri", default="mongodb://127.0.0.1:27117",
                        help="MongoDB for every university (default: the local one, not .env's)")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

    home = Path(os.environ["NEO4J_HOME"])
    only = {key for key in args.only.split(",") if key}
    args.instances.mkdir(parents=True, exist_ok=True)
    state_path = args.instances / "state.json"
    state = json.loads(state_path.read_text()) if state_path.exists() else {}
    attempted: set[str] = set()

    while True:
        # Read the list again before every university, so editing the file
        # reorders the queue without stopping the run in progress.
        keys = [line.split()[0] for line in args.keys.read_text(encoding="utf-8").splitlines() if line.strip()]
        queue = [(index, key) for index, key in enumerate(keys)
                 if (not only or key in only) and key not in attempted
                 and not all(state.get(key, {}).get(step) for step in STEPS)]
        if not queue:
            break
        index, key = queue[0]
        # A university that failed waits for the next start, not for this loop.
        attempted.add(key)
        group = re.sub(r"[^A-Za-z0-9_.-]", "-", f"{key}-{args.date_from}-{args.date_to}")
        done = state.setdefault(key, {})
        instance = Instance(home, args.instances, key, index, args.neo4j_password)
        # The OpenRouter proxy in .env is reachable only on the lab VPN; from
        # here OpenRouter answers directly.
        env = {**os.environ, "PAUK_INSTITUTION": key, "MONGO_DB": "", "NEO4J_URI": instance.uri,
               "NEO4J_USER": "neo4j", "NEO4J_PASSWORD": args.neo4j_password, "OPENROUTER_PROXY_URL": "",
               "MONGO_URI": args.mongo_uri}
        log = args.instances / f"{key}.log"
        logger.info("%s: group %s, Neo4j %s, log %s", key, group, instance.uri, log)
        instance.start()
        try:
            commands = {
                "run": ["run", "--from", args.date_from, "--to", args.date_to, "--name", group],
                "social_graph": ["enrich", "social_graph", "--group", group],
                "publish": ["publish", "graph", "--group", group],
                "dedup_graph": ["dedup", "graph"],
            }
            for step in STEPS:
                if done.get(step):
                    continue
                started = time.time()
                pauk(commands[step], env, log)
                done[step] = time.strftime("%Y-%m-%d %H:%M:%S")
                state_path.write_text(json.dumps(state, indent=1), encoding="utf-8")
                logger.info("%s: %s done in %.0f min", key, step, (time.time() - started) / 60)
        except Exception:
            logger.exception("%s: stopped; the next start resumes from the failed step", key)
        finally:
            instance.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())

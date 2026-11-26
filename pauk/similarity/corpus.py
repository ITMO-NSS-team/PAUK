"""Build versioned, self-contained README corpora from published graph snapshots."""

import json
import re
from collections import Counter
from dataclasses import asdict
from hashlib import sha256
from pathlib import Path
from urllib.parse import urlsplit

from pauk.sources.base import HttpRequestError
from pauk.sources.github import GitHubClient, Readme
from pauk.storage.atomic import AtomicWriter, atomic_write_bytes
from pauk.storage.readme import cached_readme, store_readme
from pauk.urls import normalize_repo_url

from .documents import prepare_document


def _repositories(snapshot: dict) -> list[dict]:
    rows = snapshot.get("repositories") if isinstance(snapshot, dict) else None
    if not isinstance(rows, list):
        raise ValueError("snapshot must contain a repositories array")
    seen: set[str] = set()
    owners: dict[str, str] = {}
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("id"), str) or not row["id"].strip():
            raise ValueError("repository requires a nonempty id")
        rid = row["id"]
        if rid in seen:
            raise ValueError(f"duplicate repository id: {rid}")
        seen.add(rid)
        for field in ("url", "description", "name", "language"):
            if row.get(field) is not None and not isinstance(row[field], str):
                raise ValueError(f"repository {rid}: {field} must be a string or null")
        topics = row.get("topics")
        if topics is not None and (not isinstance(topics, list) or not all(isinstance(t, str) for t in topics)):
            raise ValueError(f"repository {rid}: topics must be a string array or null")
        if row.get("url"):
            try:
                key = normalize_repo_url(row["url"])
            except ValueError:
                continue
            if key in owners:
                raise ValueError(f"same repository URL for {owners[key]} and {rid}")
            owners[key] = rid
    return sorted(rows, key=lambda row: row["id"])


def _fetch(client: GitHubClient, cache: Path, url: str, refresh: bool) -> tuple[Readme | None, str]:
    try:
        parsed = urlsplit(normalize_repo_url(url))
    except ValueError:
        return None, "unsupported_url"
    parts = re.fullmatch(r"/([a-z0-9_.-]+)/([a-z0-9_.-]+)", parsed.path)
    if (parsed.scheme != "https" or parsed.netloc != "github.com" or not parts
            or any(part in {".", ".."} for part in parts.groups())):
        return None, "unsupported_url"
    readme = None if refresh else cached_readme(cache, url)
    if readme is None or readme.status in {"unavailable", "invalid"}:
        try:
            readme = client.get_readme(*parts.groups(), allow_blob=True)
        except (HttpRequestError, ValueError) as exc:
            code = exc.status_code if isinstance(exc, HttpRequestError) else None
            return None, f"http_{code}" if code else "fetch_error"
        store_readme(cache, url, readme)
    return readme, readme.status


def collect(snapshot: Path, out: Path, cache: Path, client: GitHubClient, *, refresh: bool = False) -> dict:
    payload = snapshot.read_bytes()
    snapshot_sha = sha256(payload).hexdigest()
    rows = _repositories(json.loads(payload))
    del payload
    records = []
    counts: Counter[str] = Counter()
    for row in rows:
        url = row.get("url") or ""
        readme, status = _fetch(client, cache, url, refresh)
        counts[status] += 1
        path = readme.path if readme else ""
        body = readme.body if readme else b""
        source = None
        if status in {"available", "empty"}:
            source = f"blobs/{sha256(body).hexdigest()}"
            if not (out / source).exists():
                atomic_write_bytes(out / source, body)
        description = row.get("description") or ""
        parts, warning = prepare_document(description, path, body)
        text = "\n".join(part.text for part in parts)
        records.append({
            "id": row["id"], "url": url, "name": row.get("name"), "description": description,
            "topics": row.get("topics") or [], "language": row.get("language"),
            "readme": {"status": status, "path": path, "sha": readme.sha if readme else "", "blob": source},
            "text": text, "text_sha256": sha256(text.encode()).hexdigest() if text else None,
            "fragments": [asdict(part) for part in parts], "warning": warning,
            "skip_reason": None if text else "no_descriptive_text",
        })
    manifest = {"schema_version": 1, "cleaner_version": 1, "snapshot_sha256": snapshot_sha,
                "counts": dict(counts), "repositories": records}
    encoded = json.dumps(manifest, ensure_ascii=False, separators=(",", ":")).encode()
    version = sha256(encoded).hexdigest()
    atomic_write_bytes(out / "versions" / f"{version}.json", encoded)
    with AtomicWriter(out / "manifest.json") as fh:
        fh.write(encoded.decode())
    return {"version": version, "repositories": len(records),
            "prepared": sum(bool(row["text"]) for row in records), **counts}

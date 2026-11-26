from __future__ import annotations

import re
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path

from pymongo.database import Database

from .atomic import atomic_write_bytes


class PdfStore:
    """PDF bytes on local disk (`pdf_dir`), keyed by source-record key.

    Mongo holds `{record_key, filename, fetched_at}` pointers (and optional
    source provenance), not the bytes.
    """

    def __init__(self, db: Database, pdf_dir: Path) -> None:
        self.pointers = db.pdfs
        self.pdf_dir = pdf_dir

    LEGACY_ID = re.compile(r"^[WA][A-Za-z0-9._-]*$")

    def _filename(self, record_key: str) -> str:
        # Legacy files stay where they are. Every source-record key goes
        # through a hash, so slashes, colons and Unicode cannot escape or
        # become invalid on Windows.
        if self.LEGACY_ID.fullmatch(record_key):
            return f"{record_key}.pdf"
        digest = sha256(record_key.encode("utf-8")).hexdigest()
        return f"{digest}.pdf"

    def _source_path(self, source_url: str) -> Path:
        """Keep URL-keyed PDFs outside the source-record key namespace."""
        return self.pdf_dir / "sources" / f"{sha256(source_url.encode()).hexdigest()}.pdf"

    def read_source(self, source_url: str) -> bytes:
        """Read a PDF cached by its source URL rather than a record key."""
        return self._source_path(source_url).read_bytes()

    def save_source(self, source_url: str, data: bytes) -> None:
        """Cache validated PDF bytes without creating a record pointer."""
        atomic_write_bytes(self._source_path(source_url), data)

    def _path(self, record_key: str) -> Path:
        pointer = self.pointers.find_one({"_id": record_key}, {"filename": 1})
        filename = (pointer or {}).get("filename") or self._filename(record_key)
        if not isinstance(filename, str) or Path(filename).name != filename:
            filename = self._filename(record_key)
        path = self.pdf_dir / filename
        if path.exists():
            return path
        # An OpenAlex source key may still have the transition-era W file.
        # Reading it is enough; migration adds a pointer without renaming it.
        if record_key.startswith("openalex:"):
            legacy = record_key.split(":", 1)[1]
            if self.LEGACY_ID.fullmatch(legacy):
                return self.pdf_dir / f"{legacy}.pdf"
        return path

    def exists(self, record_key: str) -> bool:
        return self._path(record_key).exists()

    def cache_identity(self, record_key: str) -> str:
        """Stable identity of the file a record key currently resolves to."""
        return str(self._path(record_key).resolve())

    def read(self, record_key: str) -> bytes:
        """Bytes for a key already known to exist (see `exists`)."""
        return self._path(record_key).read_bytes()

    def _pointer(self, record_key: str) -> dict:
        pointer = self.pointers.find_one({"_id": record_key})
        if pointer is None and record_key.startswith("openalex:"):
            legacy = record_key.split(":", 1)[1]
            if self.LEGACY_ID.fullmatch(legacy):
                pointer = self.pointers.find_one({"_id": legacy})
        return pointer or {}

    def read_with_source(self, record_key: str) -> tuple[bytes, str | None]:
        """Return bytes and their recorded source only if the recorded digest agrees."""
        metadata = self._pointer(record_key)
        data = self.read(record_key)
        source = metadata.get("source_url")
        if isinstance(source, str) and metadata.get("sha256") == sha256(data).hexdigest():
            return data, source
        return data, None

    def save(self, record_key: str, data: bytes, *, source_url: str | None = None) -> None:
        """Write the PDF atomically, then record the pointer and provenance digest.

        Writing bytes first leaves a readable file if the pointer write fails.
        Its source remains unverified until the matching digest is recorded.
        """
        filename = self._filename(record_key)
        atomic_write_bytes(self.pdf_dir / filename, data)
        self.pointers.update_one(
            {"_id": record_key},
            {"$set": {
                "record_key": record_key,
                "filename": filename,
                "fetched_at": datetime.now(UTC).isoformat(),
                "source_url": source_url,
                "sha256": sha256(data).hexdigest(),
            }},
            upsert=True,
        )

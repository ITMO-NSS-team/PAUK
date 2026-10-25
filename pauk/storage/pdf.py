from __future__ import annotations

import re
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path

from pymongo.database import Database

from .atomic import atomic_write_bytes


class PdfStore:
    """PDF bytes on local disk (`pdf_dir`), keyed by source-record key.

    Mongo holds `{record_key, filename, fetched_at}` pointers, not the bytes.
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

    def read(self, record_key: str) -> bytes:
        """Bytes for an id already known to exist (see `exists`)."""
        return self._path(record_key).read_bytes()

    def save(self, record_key: str, data: bytes) -> None:
        """Write the PDF atomically, then record the pointer.

        The order matters: if the process dies between the two steps, the next
        run sees a file without a pointer and re-saves it, instead of a pointer
        promising a file that was never written.
        """
        filename = self._filename(record_key)
        atomic_write_bytes(self.pdf_dir / filename, data)
        self.pointers.update_one(
            {"_id": record_key},
            {"$set": {
                "record_key": record_key,
                "filename": filename,
                "fetched_at": datetime.now(UTC).isoformat(),
            }},
            upsert=True,
        )

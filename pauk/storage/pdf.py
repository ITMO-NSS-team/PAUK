from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from pymongo.database import Database

from .atomic import atomic_write_bytes


class PdfStore:
    """PDF bytes on local disk (`pdf_dir`), keyed by publication id.
    Mongo holds only a pointer per id: `{fetched_at}`, not the bytes."""

    def __init__(self, db: Database, pdf_dir: Path) -> None:
        self.pointers = db.pdfs
        self.pdf_dir = pdf_dir

    def _path(self, publication_id: str) -> Path:
        return self.pdf_dir / f"{publication_id}.pdf"

    def exists(self, publication_id: str) -> bool:
        return self._path(publication_id).exists()

    def read(self, publication_id: str) -> bytes:
        """Bytes for an id already known to exist (see `exists`)."""
        return self._path(publication_id).read_bytes()

    def save(self, publication_id: str, data: bytes) -> None:
        """Write the PDF atomically, then record the pointer.

        Written in this order deliberately: if the process dies between the
        two, the next run just sees a file with no pointer yet and re-saves
        it (cheap - the file write is what mattered) rather than a pointer
        promising a file that was never actually written.
        """
        atomic_write_bytes(self._path(publication_id), data)
        self.pointers.update_one(
            {"_id": publication_id},
            {"$set": {"fetched_at": datetime.now(UTC).isoformat()}},
            upsert=True,
        )

from __future__ import annotations

from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path

from pymongo.database import Database

from .atomic import atomic_write_bytes


class PdfStore:
    """PDF bytes on local disk (`pdf_dir`), keyed by publication id.

    Mongo holds the pointer and optional source provenance, not the bytes.
    """

    def __init__(self, db: Database, pdf_dir: Path) -> None:
        self.pointers = db.pdfs
        self.pdf_dir = pdf_dir

    def _path(self, publication_id: str) -> Path:
        return self.pdf_dir / f"{publication_id}.pdf"

    def _source_path(self, source_url: str) -> Path:
        """Keep URL-keyed PDFs outside the publication ID namespace."""
        return self.pdf_dir / "sources" / f"{sha256(source_url.encode()).hexdigest()}.pdf"

    def read_source(self, source_url: str) -> bytes:
        """Read a PDF cached by its source URL, rather than as a publication's PDF.

        Args:
            source_url: The URL from which the PDF was downloaded.

        Returns:
            The cached PDF bytes.
        """
        return self._source_path(source_url).read_bytes()

    def save_source(self, source_url: str, data: bytes) -> None:
        """Cache validated PDF bytes without creating a publication pointer.

        Args:
            source_url: The URL from which the PDF was downloaded.
            data: Successfully parsed PDF bytes.
        """
        atomic_write_bytes(self._source_path(source_url), data)

    def exists(self, publication_id: str) -> bool:
        return self._path(publication_id).exists()

    def read(self, publication_id: str) -> bytes:
        """Bytes for an id already known to exist (see `exists`)."""
        return self._path(publication_id).read_bytes()

    def read_with_source(self, publication_id: str) -> tuple[bytes, str | None]:
        """Return bytes and their recorded source only if the recorded digest agrees."""
        metadata = self.pointers.find_one({"_id": publication_id}) or {}
        data = self.read(publication_id)
        source = metadata.get("source_url")
        if isinstance(source, str) and metadata.get("sha256") == sha256(data).hexdigest():
            return data, source
        return data, None

    def save(self, publication_id: str, data: bytes, *, source_url: str | None = None) -> None:
        """Write the bytes atomically, then record provenance tied to their digest."""
        atomic_write_bytes(self._path(publication_id), data)
        self.pointers.update_one(
            {"_id": publication_id},
            {"$set": {"fetched_at": datetime.now(UTC).isoformat(),
                      "source_url": source_url, "sha256": sha256(data).hexdigest()}},
            upsert=True,
        )

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import date
from typing import Any

from pydantic import BaseModel, Field, model_validator

from pauk.models import ExternalIdentifier, Funding


class RawEnvelope(BaseModel):
    """Versioned wrapper around a source response stored in Mongo."""

    provider: str
    record_type: str = "publication"
    record_key: str
    schema_version: int = Field(default=2, ge=1)
    payload: dict[str, Any]
    request: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def read_legacy(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        values = dict(data)
        source = values.get("source")
        if not values.get("provider") and source == "openalex_works":
            values["provider"] = "openalex"
            values["record_type"] = "publication"
            source_id = str((values.get("payload") or {}).get("id") or "").rstrip("/").rsplit("/", 1)[-1]
            values["record_key"] = f"openalex:{source_id.upper()}"
            values["schema_version"] = 1
        for field in ("provider", "record_key"):
            if not str(values.get(field) or "").strip():
                raise ValueError(f"raw envelope {field} must be non-empty")
        return values


class IncomingAuthor(BaseModel):
    """A source author before PAUK assigns an internal person id."""

    source_author_id: str | None = None
    name: str | None = None
    orcid: str | None = None
    identifiers: list[ExternalIdentifier] = Field(default_factory=list)
    position: int = Field(ge=1)
    affiliation: str | None = None
    is_itmo: bool = False
    is_corresponding: bool = False
    name_variants: list[str] = Field(default_factory=list)


class IncomingPublication(BaseModel):
    """Source-neutral publication returned by an adapter."""

    source: str
    source_id: str
    record_key: str
    source_url: str | None = None
    title: str | None = None
    type: str | None = None
    fields: list[str] = Field(default_factory=list)
    journal: str | None = None
    doi: str | None = None
    publication_date: date | None = None
    year: int | None = None
    pdf_urls: list[str] = Field(default_factory=list)
    abstract: str | None = None
    funding: list[Funding] = Field(default_factory=list)
    authors: list[IncomingAuthor] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_identity(self) -> IncomingPublication:
        self.source = self.source.strip().casefold()
        self.source_id = self.source_id.strip()
        self.record_key = self.record_key.strip()
        if not self.source or not self.source_id or not self.record_key:
            raise ValueError("incoming publication source, source_id and record_key must be non-empty")
        if not self.record_key.startswith(f"{self.source}:"):
            raise ValueError("incoming publication record_key must be namespaced by source")
        return self


class PublicationAdapter(ABC):
    """Stable boundary between one provider payload and PAUK's domain."""

    source: str

    @abstractmethod
    def adapt(self, envelope: RawEnvelope) -> IncomingPublication:
        raise NotImplementedError

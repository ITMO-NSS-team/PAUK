from __future__ import annotations

from datetime import date
from typing import Any

from pydantic import BaseModel, Field, model_validator

DOI_PREFIXES = (
    "https://doi.org/",
    "http://doi.org/",
    "https://dx.doi.org/",
    "http://dx.doi.org/",
    "doi:",
)


def normalize_external_value(scheme: str, value: str) -> str:
    """Return the stable comparison value for one external identifier."""
    scheme = scheme.strip().casefold()
    cleaned = value.strip()
    if scheme == "doi":
        cleaned = cleaned.casefold()
        for prefix in DOI_PREFIXES:
            cleaned = cleaned.removeprefix(prefix)
        return cleaned.rstrip("/")
    if scheme == "openalex":
        return cleaned.rstrip("/").rsplit("/", 1)[-1].upper()
    if scheme == "orcid":
        return cleaned.rstrip("/").rsplit("/", 1)[-1]
    return cleaned


class ExternalIdentifier(BaseModel):
    """An identifier owned by a system outside PAUK."""

    scheme: str
    value: str
    normalized_key: str = ""
    url: str | None = None

    @model_validator(mode="before")
    @classmethod
    def normalize(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        values = dict(data)
        scheme = str(values.get("scheme") or "").strip().casefold()
        value = normalize_external_value(scheme, str(values.get("value") or ""))
        if not scheme or not value:
            raise ValueError("external identifier scheme and value must be non-empty")
        values["scheme"] = scheme
        values["value"] = value
        values["normalized_key"] = values.get("normalized_key") or f"{scheme}:{value}"
        return values


class SourceAuthor(BaseModel):
    """An author as one source record stated it, after identity resolution."""

    person_id: str
    name: str | None = None
    position: int | None = None
    identifiers: list[ExternalIdentifier] = Field(default_factory=list)
    affiliation: str | None = None
    is_corresponding: bool = False


class PublicationSourceRecord(BaseModel):
    """One lossless, source-owned description of a publication."""

    key: str = ""
    source: str
    source_id: str
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
    authors: list[SourceAuthor] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def normalize_key(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        values = dict(data)
        source = str(values.get("source") or "").strip().casefold()
        source_id = normalize_external_value(source, str(values.get("source_id") or ""))
        if not source or not source_id:
            raise ValueError("source record source and source_id must be non-empty")
        values["source"] = source
        values["source_id"] = source_id
        values["key"] = values.get("key") or f"{source}:{source_id}"
        return values

from datetime import date
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .identity import ExternalIdentifier, PublicationSourceRecord, SourceAuthor
from .processing import ProcessingState
from .relations import MentionsLink


class Funding(BaseModel):
    funder: str | None = None
    grant_id: str | None = None


class VersionAuthor(BaseModel):
    """One author of one version, as that record listed them."""

    person_id: str
    name: str | None = None
    position: int | None = None


class PublicationVersion(BaseModel):
    """One place a work appeared: a preprint, dataset deposit, journal version or duplicate record.

    The dedup stage folds such records into a single Publication and keeps
    one entry here per record, including the surviving one, so that no
    venue, DOI, abstract or author list is lost by merging. This is a
    ledger, not the graph: nodes and AUTHORED edges are always drawn from
    the merged state of the surviving publication.
    """

    openalex_id: str
    title: str | None = None
    doi: str | None = None
    journal: str | None = None
    publication_date: date | None = None
    year: int | None = None
    openalex_url: str | None = None
    pdf_urls: list[str] = Field(default_factory=list)
    abstract: str | None = None
    authors: list[VersionAuthor] = Field(default_factory=list)


class Publication(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    id: str
    title: str
    # Internal ids are opaque. Existing OpenAlex W ids remain valid legacy
    # ids, while newly ingested entities use a PAUK-generated id.
    schema_version: int = 1
    identifiers: list[ExternalIdentifier] = Field(default_factory=list)
    source_records: list[PublicationSourceRecord] = Field(default_factory=list)
    sources: list[str] = Field(default_factory=list)
    source_keys: list[str] = Field(default_factory=list)
    # OpenAlex work type: "article", "preprint", "software", "dataset", ...
    # Not every work is a paper (e.g. a software release archived on Zenodo),
    # and the code_links stage treats those differently.
    type: str | None = None
    # OpenAlex topic fields ("Computer Science", "Chemistry"): what the work
    # is about, coarse enough to be stable. Dedup uses an overlap between
    # two persons' fields as evidence that they are one researcher.
    fields: list[str] = Field(default_factory=list)
    journal: str | None = None
    doi: str | None = None
    publication_date: date | None = None
    year: int | None = None
    has_code: bool = False
    # JSON text with every author-produced repository URL. Kept as a string
    # because the GUI and existing Neo4j data rely on that format.
    code_url: str | None = None
    funding: list[Funding] = Field(default_factory=list)
    openalex_url: str | None = None
    pdf_urls: list[str] = Field(default_factory=list)
    abstract: str | None = None
    full_text: str | None = None
    department_ids: list[str] = Field(default_factory=list)
    mentions_links: list[MentionsLink] = Field(default_factory=list)
    # Every record folded into this publication (see PublicationVersion) and
    # the OpenAlex work ids those records were keyed by.
    versions: list[PublicationVersion] = Field(default_factory=list)
    merged_ids: list[str] = Field(default_factory=list)
    processing: dict[str, ProcessingState] = Field(default_factory=dict, alias="_processing")

    @model_validator(mode="before")
    @classmethod
    def read_legacy_sources(cls, data: Any) -> Any:
        """Accept v1 rows and expose them through the source-neutral ledger."""
        if not isinstance(data, dict):
            return data
        values = dict(data)
        records = [
            record.model_dump(mode="python") if isinstance(record, BaseModel) else record
            for record in (values.get("source_records") or [])
        ]
        if not records:
            for version in values.get("versions") or []:
                source_id = version.get("openalex_id") if isinstance(version, dict) else None
                if not source_id:
                    continue
                records.append({
                    "key": f"openalex:{source_id}",
                    "source": "openalex",
                    "source_id": source_id,
                    "source_url": version.get("openalex_url"),
                    "title": version.get("title"),
                    "type": values.get("type"),
                    "fields": values.get("fields") or [],
                    "journal": version.get("journal"),
                    "doi": version.get("doi"),
                    "publication_date": version.get("publication_date"),
                    "year": version.get("year"),
                    "pdf_urls": version.get("pdf_urls") or [],
                    "abstract": version.get("abstract"),
                    "authors": version.get("authors") or [],
                })
        values["source_records"] = records
        if records:
            values.setdefault("sources", list(dict.fromkeys(r.get("source") for r in records if r.get("source"))))
            values.setdefault("source_keys", list(dict.fromkeys(r.get("key") for r in records if r.get("key"))))
        identifiers = list(values.get("identifiers") or [])
        identifiers = [
            item.model_dump(mode="python") if isinstance(item, BaseModel) else item
            for item in identifiers
        ]
        known = {i.get("normalized_key") for i in identifiers if isinstance(i, dict)}
        for record in records:
            key = record.get("key")
            if key and key not in known:
                identifiers.append({
                    "scheme": record.get("source"),
                    "value": record.get("source_id"),
                    "normalized_key": key,
                    "url": record.get("source_url"),
                })
                known.add(key)
            doi = record.get("doi")
            if doi:
                identifier = ExternalIdentifier(scheme="doi", value=doi)
                if identifier.normalized_key not in known:
                    identifiers.append(identifier.model_dump(mode="python"))
                    known.add(identifier.normalized_key)
        if values.get("doi"):
            identifier = ExternalIdentifier(scheme="doi", value=values["doi"])
            if identifier.normalized_key not in known:
                identifiers.append(identifier.model_dump(mode="python"))
        values["identifiers"] = identifiers
        return values

    @model_validator(mode="after")
    def write_legacy_sources(self) -> "Publication":
        """Keep old consumers alive during the dual-write migration window."""
        self.sources = list(dict.fromkeys([*self.sources, *(r.source for r in self.source_records)]))
        self.source_keys = list(dict.fromkeys([*self.source_keys, *(r.key for r in self.source_records)]))
        known_identifiers = {identifier.normalized_key for identifier in self.identifiers}
        for record in self.source_records:
            if record.key not in known_identifiers:
                self.identifiers.append(ExternalIdentifier(
                    scheme=record.source,
                    value=record.source_id,
                    normalized_key=record.key,
                    url=record.source_url,
                ))
                known_identifiers.add(record.key)
        # V2 source records are authoritative. Legacy rows can still rebuild
        # their version ledger from raw OpenAlex payloads during dedup, so an
        # existing v1 ledger must not be overwritten by its compatibility
        # source-record projection on the next model read.
        if self.schema_version >= 2 or not self.versions:
            self.versions = [
                PublicationVersion(
                    openalex_id=record.source_id,
                    title=record.title,
                    doi=record.doi,
                    journal=record.journal,
                    publication_date=record.publication_date,
                    year=record.year,
                    openalex_url=record.source_url,
                    pdf_urls=record.pdf_urls,
                    abstract=record.abstract,
                    authors=[VersionAuthor(
                        person_id=author.person_id,
                        name=author.name,
                        position=author.position,
                    ) for author in record.authors],
                )
                for record in self.source_records
                if record.source == "openalex"
            ]
        if self.openalex_url is None:
            self.openalex_url = next(
                (record.source_url for record in self.source_records if record.source == "openalex"),
                None,
            )
        return self


__all__ = [
    "ExternalIdentifier",
    "Funding",
    "Publication",
    "PublicationSourceRecord",
    "PublicationVersion",
    "SourceAuthor",
    "VersionAuthor",
]

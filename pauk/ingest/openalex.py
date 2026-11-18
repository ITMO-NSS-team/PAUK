from __future__ import annotations

from datetime import date

from pauk.models import ExternalIdentifier

from .contracts import IncomingAuthor, IncomingPublication, PublicationAdapter, RawEnvelope


class OpenAlexAdapter(PublicationAdapter):
    source = "openalex"

    def adapt(self, envelope: RawEnvelope) -> IncomingPublication:
        if envelope.record_type != "publication":
            raise ValueError(f"OpenAlex adapter cannot read record type {envelope.record_type!r}")
        # Kept in normalize.py during the compatibility window so old imports
        # and the existing normalization tests keep exercising the same parser.
        from pauk.pipeline.membership import authorship_institution_rors
        from pauk.pipeline.normalize import (
            EXTERNAL_AUTHORS_LIMIT,
            ITMO_ROR_ID,
            NOT_A_PERSON_NAME,
            _abstract,
            _authorship_person_id,
            _clean_markup,
            _fields,
            _funding,
            _short_id,
        )

        work = envelope.payload
        source_id = _short_id(work.get("id")) or envelope.record_key.split(":", 1)[-1]
        publication_date = work.get("publication_date")
        venue = (work.get("primary_location") or {}).get("source") or {}
        authors: list[IncomingAuthor] = []
        authorship_snapshot = []
        external_kept = 0
        for position, authorship in enumerate(work.get("authorships") or [], start=1):
            author = authorship.get("author") or {}
            source_owner_id = _authorship_person_id(author)
            institution_rors = authorship_institution_rors(authorship)
            if source_owner_id:
                authorship_snapshot.append({
                    "source_owner_id": source_owner_id,
                    "position": position,
                    "institution_rors": institution_rors,
                })
            name = author.get("display_name") or ""
            if NOT_A_PERSON_NAME.search(name):
                continue
            is_itmo = ITMO_ROR_ID in (institution_rors or [])
            if not is_itmo:
                external_kept += 1
                if external_kept > EXTERNAL_AUTHORS_LIMIT:
                    continue
            author_id = _short_id(author.get("id"))
            orcid = _short_id(author.get("orcid"))
            if not name and not author_id and not orcid:
                continue
            identifiers = []
            if author_id:
                identifiers.append(ExternalIdentifier(
                    scheme="openalex", value=author_id, url=author.get("id"),
                ))
            if orcid:
                identifiers.append(ExternalIdentifier(
                    scheme="orcid", value=orcid, url=author.get("orcid"),
                ))
            authors.append(IncomingAuthor(
                source_author_id=author_id,
                source_owner_id=source_owner_id,
                name=name or None,
                orcid=orcid,
                identifiers=identifiers,
                position=position,
                affiliation="; ".join(authorship.get("raw_affiliation_strings") or []) or None,
                institution_rors=institution_rors,
                is_itmo=is_itmo,
                is_corresponding=bool(authorship.get("is_corresponding")),
                name_variants=author.get("display_name_alternatives") or [],
            ))
        return IncomingPublication(
            source=self.source,
            source_id=source_id,
            record_key=envelope.record_key,
            source_url=work.get("id"),
            title=_clean_markup(work.get("title")) or "Untitled",
            type=work.get("type"),
            fields=_fields(work),
            doi=work.get("doi"),
            publication_date=date.fromisoformat(publication_date) if publication_date else None,
            year=int(publication_date[:4]) if publication_date else None,
            journal=venue.get("display_name"),
            venue_type=venue.get("type"),
            pdf_urls=list(dict.fromkeys(
                url
                for location in [work.get("best_oa_location"), *(work.get("locations") or [])]
                if (url := (location or {}).get("pdf_url"))
            )),
            abstract=_abstract(work),
            funding=_funding(work),
            authors=authors,
            metadata={
                "authorship_snapshot": authorship_snapshot,
                "authorship_snapshot_complete": (
                    isinstance(work.get("authorships"), list)
                    and not work.get("is_authors_truncated")
                    and len(authorship_snapshot) == len(work["authorships"])
                    and (
                        work.get("authors_count") is None
                        or isinstance(work.get("authors_count"), int)
                        and work["authors_count"] <= len(work["authorships"])
                    )
                ),
            },
        )

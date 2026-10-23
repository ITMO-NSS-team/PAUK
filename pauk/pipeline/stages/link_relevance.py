from __future__ import annotations

import json
import logging
import threading
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime

from pauk.models import ClassificationStatus, CodeLink, LinkOccurrence, Publication, RepoLink
from pauk.models.processing import ProcessingState, ProcessingStatus
from pauk.sources import OpenRouterClient
from pauk.storage import LlmLogStore
from pauk.urls import deduplicate_repo_urls

from .base import EnrichmentStage
from .code_links import ARCHIVED_DEPOSIT_REASON, CONTEXT_WINDOW

logger = logging.getLogger(__name__)

# A context spans up to 500 characters on either side. Twelve keeps the
# evidence near 12,000 characters while leaving room for all six source
# types and repeated confirmations without making every LLM call unbounded.
MODEL_CONTEXT_LIMIT = 12
CONTEXT_OVERLAP_THRESHOLD = 0.8
AMBIGUOUS_URL_REASON = "ambiguous_url_extraction"

PROMPT_TEMPLATE = """You help analyze scientific publications.

Publication:
  Title: {title}

A link was found in its materials:
  URL: {url}

Contexts of this link selected for analysis:
{contexts}

Question: is this a repository/model/dataset RELEASED BY THE AUTHORS of this
paper as accompanying material, or is it a mention of someone else's tool?

Signs of an authors' artifact: "our code is available at", "we release",
"our code is available"; the username/organization in the URL resembles an
author's name or affiliation.
Signs of an external tool: a link in the reference list; a well-known external
library (PyTorch, BERT, Llama); phrases such as "we use", "based on", "following [N]".

Consider all provided contexts together. If the evidence is insufficient or the
contexts contradict each other, return null in the is_authors_artifact field.

Return STRICTLY valid JSON without markdown:
{{"is_authors_artifact": null, "confidence": 0.0, "reason": "one short sentence"}}
"""


def _source_hint(occurrence: LinkOccurrence) -> str:
    if occurrence.source == "deposit_title":
        return "archived deposit title"
    if occurrence.source == "citation":
        return f"in-text citation [{occurrence.reference_label}], page {occurrence.page_number}"
    if occurrence.source == "reference":
        label = f" [{occurrence.reference_label}]" if occurrence.reference_label else ""
        return f"reference list{label}, page {occurrence.page_number}"
    if occurrence.source == "pdf_annotation":
        return f"PDF clickable region, page {occurrence.page_number}"
    if occurrence.page_number is None:
        return "OpenAlex abstract (limited context)"
    return f"PDF visible text, page {occurrence.page_number}"


def _unique_context_occurrences(occurrences: list[LinkOccurrence]) -> list[LinkOccurrence]:
    unique: list[LinkOccurrence] = []
    seen: set[tuple] = set()
    for occurrence in occurrences:
        context = " ".join((occurrence.context or "").split()) or "(empty context)"
        key = (
            occurrence.pdf_id,
            occurrence.page_number,
            context,
            occurrence.source,
            occurrence.reference_label,
        )
        if key not in seen:
            seen.add(key)
            unique.append(occurrence)
    return unique


def _model_occurrences(
    occurrences: list[LinkOccurrence], limit: int = MODEL_CONTEXT_LIMIT
) -> list[LinkOccurrence]:
    unique: list[LinkOccurrence] = []
    for occurrence in _unique_context_occurrences(occurrences):
        if any(_contexts_overlap(occurrence, kept) for kept in unique):
            continue
        unique.append(occurrence)
    if len(unique) <= limit:
        return unique

    selected: set[int] = set()
    sources: set[str | None] = set()
    for index, occurrence in enumerate(unique):
        if len(selected) >= limit:
            break
        if occurrence.source not in sources:
            sources.add(occurrence.source)
            selected.add(index)
    for index in range(len(unique)):
        if len(selected) >= limit:
            break
        occurrence = unique[index]
        if not any(
            (unique[kept].pdf_id, unique[kept].page_number) == (occurrence.pdf_id, occurrence.page_number)
            for kept in selected
        ):
            selected.add(index)
    for index in range(len(unique)):
        if len(selected) >= limit:
            break
        selected.add(index)
    return [occurrence for index, occurrence in enumerate(unique) if index in selected]


def _contexts_overlap(first: LinkOccurrence, second: LinkOccurrence) -> bool:
    # Split link annotations must not exhaust the budget with nearly identical windows.
    if (first.pdf_id, first.page_number, first.source, first.reference_label) != (
        second.pdf_id, second.page_number, second.source, second.reference_label
    ) or any(value is None for value in (first.text_start, first.text_end, second.text_start, second.text_end)):
        return False
    first_start, second_start = max(0, first.text_start - CONTEXT_WINDOW), max(0, second.text_start - CONTEXT_WINDOW)
    first_end, second_end = first.text_end + CONTEXT_WINDOW, second.text_end + CONTEXT_WINDOW
    overlap = min(first_end, second_end) - max(first_start, second_start)
    return overlap >= CONTEXT_OVERLAP_THRESHOLD * min(first_end - first_start, second_end - second_start)


def _format_contexts(
    occurrences: list[LinkOccurrence], limit: int | None = MODEL_CONTEXT_LIMIT
) -> str:
    if not occurrences:
        return "[1] Unknown source\n\"\"\"\n(empty context)\n\"\"\""

    unique = _unique_context_occurrences(occurrences)
    selected = unique if limit is None else _model_occurrences(unique, limit)
    blocks: list[str] = []
    for occurrence in selected:
        context = " ".join((occurrence.context or "").split()) or "(empty context)"
        source_hint = _source_hint(occurrence)
        if occurrence.pdf_id:
            source_hint += f", PDF {occurrence.pdf_id}"
        blocks.append(
            f"[{len(blocks) + 1}] Source: {source_hint}\n"
            f'\"\"\"\n{context}\n\"\"\"'
        )
    if len(selected) < len(unique):
        blocks.append(
            f"Another {len(unique) - len(selected)} context(s) were saved "
            "but omitted due to the request size limit or overlapping windows."
        )
    return "\n\n".join(blocks)


def _build_prompt(title: str | None, url: str, occurrences: list[LinkOccurrence]) -> str:
    return PROMPT_TEMPLATE.format(
        title=title or "(untitled)",
        url=url,
        contexts=_format_contexts(occurrences),
    )


def _parse_verdict(result: dict) -> tuple[bool | None, float, str | None]:
    if "is_authors_artifact" not in result:
        raise ValueError("model response has no is_authors_artifact field")
    verdict = result["is_authors_artifact"]
    if verdict is not True and verdict is not False and verdict is not None:
        raise ValueError("is_authors_artifact must be true, false, or null")

    confidence = result.get("confidence")
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
        raise ValueError("confidence must be a number between 0 and 1")
    confidence = float(confidence)
    if not 0.0 <= confidence <= 1.0:
        raise ValueError("confidence must be a number between 0 and 1")

    reason = result.get("reason")
    if reason is not None and not isinstance(reason, str):
        raise ValueError("reason must be a string or null")
    normalized_reason = reason.strip() or None if reason is not None else None
    return verdict, confidence, normalized_reason


def _update_publication_code(publication: Publication, links: list[CodeLink]) -> None:
    urls = deduplicate_repo_urls(
        link.url for link in links
        if link.is_relevant is True and not link.url_ambiguous
    )
    publication.has_code = bool(urls)
    if not urls:
        publication.code_url = None
        return
    # JSON text, kept for the GUI, which displays every author-produced artifact of a paper.
    publication.code_url = json.dumps(urls, ensure_ascii=False)


class LinkRelevanceStage(EnrichmentStage):
    """Judges whether a CodeLink is the paper's own artifact or a mention of
    someone else's tool. Runs after code_links, which leaves is_relevant
    unset (None) for every link except a deposit's own archived repository -
    a deterministic case that needs no judgment call.
    """

    name = "link_relevance"
    progress_label = "Code links: assessing whether they are authors' artifacts"

    def _judge(self, client: OpenRouterClient, llm_log: LlmLogStore, row: RepoLink, pub: Publication,
               pending: list[CodeLink]) -> None:
        """Ask the model about each pending link of one publication and record the publication's state."""
        state = pub.processing.get(self.name)
        error = None
        for link in pending:
            prompt = _build_prompt(pub.title, link.url, link.occurrences)
            result = client.chat_json(prompt)
            llm_error = None
            if result is None:
                llm_error = client.last_error if isinstance(client.last_error, str) else "no response"
            else:
                try:
                    verdict, confidence, reason = _parse_verdict(result)
                except ValueError as exc:
                    llm_error = str(exc)
            llm_log.record(
                group=self.prepared.group,
                model=self.config.llm_model,
                prompt=prompt,
                raw_response=client.last_response,
                parsed=result,
                usage=client.last_usage,
                error=llm_error,
                context={"publication_id": pub.id, "url": link.url},
            )
            if llm_error is not None:
                link.classification_status = ClassificationStatus.FAILED
                link.is_relevant = None
                link.llm_confidence = None
                link.llm_reason = None
                error = "llm request failed"
                logger.warning(
                    "link_relevance: %s -> %s: %s (%s)",
                    pub.id,
                    link.url,
                    llm_error,
                    self.config.llm_model,
                )
                continue
            link.classification_status = ClassificationStatus.CLASSIFIED
            link.is_relevant = verdict
            link.llm_confidence = confidence
            link.llm_reason = reason
            logger.info(
                "link_relevance: %s -> %s: %s (confidence=%.2f) %s",
                pub.id,
                link.url,
                (
                    "authors' own"
                    if link.is_relevant is True
                    else ("not authors' own" if link.is_relevant is False else "uncertain")
                ),
                link.llm_confidence,
                link.llm_reason or "",
            )
        # A failed batch can contain a mix of new verdicts and links whose
        # previous verdict was cleared for retry. Keep the last complete
        # publication-level state until every link has been classified.
        if error is None:
            _update_publication_code(pub, row.links)
        resolved = sum(
            link.classification_status == ClassificationStatus.CLASSIFIED
            for link in row.links
        )
        pub.processing[self.name] = ProcessingState(
            status=ProcessingStatus.FAILED
            if error
            else (ProcessingStatus.COMPLETED if row.links else ProcessingStatus.COMPLETED_EMPTY),
            attempts=(state.attempts if state else 0) + 1,
            finished_at=datetime.now(UTC),
            result_count=resolved,
            error=error,
        )

    def run(self) -> dict[str, int]:
        publications = {pub.id: pub for pub in self.prepared.read_models("publications", Publication)}
        links_by_publication = list(self.prepared.read_models("repo_links", RepoLink))
        llm_log = LlmLogStore(self.prepared.db, "llm_logs_link_relevance")
        logger.info(
            "link_relevance: model=%s, %d publication(s) with links to consider",
            self.config.llm_model,
            len(links_by_publication),
        )
        candidates: list[tuple[RepoLink, Publication, list[CodeLink]]] = []
        for row in links_by_publication:
            pub = publications.get(row.publication_id)
            if pub is None or not self.selected("publications", pub.id):
                continue
            state = pub.processing.get(self.name)
            if not self.needs_attempt(state):
                continue
            for link in row.links:
                if link.url_ambiguous:
                    link.classification_status = ClassificationStatus.CLASSIFIED
                    link.is_relevant = None
                    link.llm_confidence = None
                    link.llm_reason = AMBIGUOUS_URL_REASON
            # Under --force, re-judge only verdicts that came from the model.
            # Extraction ambiguity cannot be resolved from the same contexts.
            pending = [
                link
                for link in row.links
                if not link.url_ambiguous
                and (
                    link.classification_status != ClassificationStatus.CLASSIFIED
                    or (self.force and link.llm_reason != ARCHIVED_DEPOSIT_REASON)
                )
            ]
            candidates.append((row, pub, pending))
        local = threading.local()

        def client_for_thread() -> OpenRouterClient:
            # One per thread: chat_json leaves the response, usage and error
            # on the instance.
            if not hasattr(local, "client"):
                local.client = OpenRouterClient(
                    self.config.request_timeout,
                    self.config.openrouter_api_key,
                    self.config.llm_model,
                    self.config.openrouter_proxy_url,
                )
            return local.client

        def judge(item: tuple[RepoLink, Publication, list[CodeLink]]) -> None:
            row, pub, pending = item
            self._judge(client_for_thread(), llm_log, row, pub, pending)

        workers = max(1, self.config.link_relevance_concurrency)

        def done() -> Iterator[None]:
            if workers == 1:
                yield from (judge(item) for item in candidates)
                return
            for start in range(0, len(candidates), workers * 8):
                block = candidates[start:start + workers * 8]
                with ThreadPoolExecutor(max_workers=min(workers, len(block))) as pool:
                    futures = [pool.submit(judge, item) for item in block]
                    try:
                        for future in as_completed(futures):
                            yield future.result()
                    except BaseException:
                        pool.shutdown(wait=False, cancel_futures=True)
                        raise

        changed = sum(1 for _ in self.progress(done(), total=len(candidates), unit="publication"))
        logger.info("link_relevance: done, %d publication(s) processed", changed)
        self.prepared.write_models("publications", publications.values())
        self.prepared.write_models("repo_links", links_by_publication)
        return {"publications": changed}

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime

from pauk.models import CodeLink, LinkOccurrence, Publication, RepoLink
from pauk.models.processing import ProcessingState, ProcessingStatus
from pauk.sources import OpenRouterClient
from pauk.storage import LlmLogStore

from .base import EnrichmentStage
from .code_links import ARCHIVED_DEPOSIT_REASON

logger = logging.getLogger(__name__)

PROMPT_TEMPLATE = """Ты помогаешь анализировать научные публикации.

Публикация:
  Название: {title}

В её материалах найдена ссылка:
  URL: {url}

Все найденные контексты этой ссылки:
{contexts}

Вопрос: это репозиторий/модель/датасет, который ВЫЛОЖИЛИ САМИ АВТОРЫ этой
статьи как сопроводительный материал - или это упоминание чужого инструмента?

Признаки авторского: "our code is available at", "we release", "наш код
доступен"; имя пользователя/организации в URL похоже на автора или его
аффилиацию.
Признаки чужого: ссылка в списке литературы; известная чужая библиотека
(PyTorch, BERT, Llama); формулировки "we use", "based on", "following [N]".

Учитывай все контексты вместе. Если данных недостаточно или контексты
противоречат друг другу, верни null в поле is_authors_artifact.

Ответь СТРОГО валидным JSON без markdown:
{{"is_authors_artifact": null, "confidence": 0.0, "reason": "одно короткое предложение"}}
"""


def _source_hint(page_number: int | None) -> str:
    if page_number is None:
        return "абстракт OpenAlex (контекст ограничен)"
    return f"видимый текст PDF, страница {page_number}"


def _format_contexts(occurrences: list[LinkOccurrence]) -> str:
    if not occurrences:
        return "[1] Источник неизвестен\n\"\"\"\n(контекст пустой)\n\"\"\""

    blocks: list[str] = []
    seen: set[tuple[int | None, str]] = set()
    for occurrence in occurrences:
        context = " ".join((occurrence.context or "").split()) or "(контекст пустой)"
        key = (occurrence.page_number, context)
        if key in seen:
            continue
        seen.add(key)
        blocks.append(
            f"[{len(blocks) + 1}] Источник: {_source_hint(occurrence.page_number)}\n"
            f'\"\"\"\n{context}\n\"\"\"'
        )
    return "\n\n".join(blocks)


def _build_prompt(title: str | None, url: str, occurrences: list[LinkOccurrence]) -> str:
    return PROMPT_TEMPLATE.format(
        title=title or "(без названия)",
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
    relevant = [link for link in links if link.is_relevant is True]
    publication.has_code = bool(relevant)
    if not relevant:
        publication.code_url = None
        return
    # code_url predates the graph model and remains JSON text for compatibility
    # with the GUI, which displays every author-produced artifact of a paper.
    publication.code_url = json.dumps([link.url for link in relevant], ensure_ascii=False)


class LinkRelevanceStage(EnrichmentStage):
    """Judges whether a CodeLink is the paper's own artifact or a mention of
    someone else's tool. Runs after code_links, which leaves is_relevant
    unset (None) for every link except a deposit's own archived repository -
    a deterministic case that needs no judgment call.
    """

    name = "link_relevance"
    progress_label = "Code links: assessing whether they are authors' artifacts"

    def run(self) -> dict[str, int]:
        publications = {pub.id: pub for pub in self.prepared.read_models("publications", Publication)}
        links_by_publication = list(self.prepared.read_models("repo_links", RepoLink))
        client = OpenRouterClient(
            self.config.request_timeout,
            self.config.openrouter_api_key,
            self.config.llm_model,
            self.config.openrouter_proxy_url,
        )
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
            # Under --force, re-judge everything except the one deterministic
            # verdict code_links sets itself (not an LLM call, nothing to
            # re-judge) - e.g. to re-classify with a newly configured model.
            pending = [
                link
                for link in row.links
                if (link.is_relevant is None and link.llm_confidence is None)
                or (self.force and link.llm_reason != ARCHIVED_DEPOSIT_REASON)
            ]
            candidates.append((row, pub, pending))
        changed = 0
        for row, pub, pending in self.progress(candidates, total=len(candidates), unit="publication"):
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
            resolved = sum(link.is_relevant is not None for link in row.links)
            pub.processing[self.name] = ProcessingState(
                status=ProcessingStatus.FAILED
                if error
                else (ProcessingStatus.COMPLETED if row.links else ProcessingStatus.COMPLETED_EMPTY),
                attempts=(state.attempts if state else 0) + 1,
                finished_at=datetime.now(UTC),
                result_count=resolved,
                error=error,
            )
            changed += 1
        logger.info("link_relevance: done, %d publication(s) processed", changed)
        self.prepared.write_models("publications", publications.values())
        self.prepared.write_models("repo_links", links_by_publication)
        return {"publications": changed}

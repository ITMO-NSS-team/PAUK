from __future__ import annotations

import logging
from datetime import UTC, datetime

from pauk.models import Publication, RepoLink
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
  Источник: {source_hint}

Окружающий текст:
\"\"\"
{context}
\"\"\"

Вопрос: это репозиторий/модель/датасет, который ВЫЛОЖИЛИ САМИ АВТОРЫ этой
статьи как сопроводительный материал - или это упоминание чужого инструмента?

Признаки авторского: "our code is available at", "we release", "наш код
доступен"; имя пользователя/организации в URL похоже на автора или его
аффилиацию.
Признаки чужого: ссылка в списке литературы; известная чужая библиотека
(PyTorch, BERT, Llama); формулировки "we use", "based on", "following [N]".

Ответь СТРОГО валидным JSON без markdown:
{{"is_authors_artifact": true, "confidence": 0.0, "reason": "одно короткое предложение"}}
"""


def _build_prompt(title: str | None, url: str, context: str | None, page_number: int | None) -> str:
    hint = (
        f"видимый текст PDF, страница {page_number}."
        if page_number is not None
        else "абстракт OpenAlex (контекст ограничен)."
    )
    return PROMPT_TEMPLATE.format(
        title=title or "(без названия)",
        url=url,
        source_hint=hint,
        context=context or "(контекст пустой)",
    )


class LinkRelevanceStage(EnrichmentStage):
    """Judges whether a CodeLink is the paper's own artifact or a mention of
    someone else's tool. Runs after code_links, which leaves is_relevant
    unset (None) for every link except a deposit's own archived repository -
    a deterministic case that needs no judgment call.
    """

    name = "link_relevance"

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
        changed = 0
        for row in links_by_publication:
            pub = publications.get(row.publication_id)
            if pub is None:
                continue
            if self.selection is not None and (
                self.selection.entity != "publications" or pub.id not in self.selection.ids
            ):
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
                if link.is_relevant is None or (self.force and link.llm_reason != ARCHIVED_DEPOSIT_REASON)
            ]
            if not pending:
                continue
            error = None
            classified = 0
            for link in pending:
                # ponytail: only the first recorded occurrence goes into the
                # prompt (matches the old pre-reform prototype); weighing all
                # occurrences is a real improvement, do it as its own issue.
                occurrence = link.occurrences[0] if link.occurrences else None
                context = occurrence.context if occurrence else None
                page_number = occurrence.page_number if occurrence else None
                prompt = _build_prompt(pub.title, link.url, context, page_number)
                result = client.chat_json(prompt)
                llm_log.record(
                    group=self.prepared.group,
                    model=self.config.llm_model,
                    prompt=prompt,
                    raw_response=client.last_response,
                    parsed=result,
                    usage=client.last_usage,
                    error=None if result is not None else "no response",
                    context={"publication_id": pub.id, "url": link.url},
                )
                if result is None:
                    error = "llm request failed"
                    logger.warning(
                        "link_relevance: %s -> %s: no response from %s", pub.id, link.url, self.config.llm_model
                    )
                    continue
                link.is_relevant = bool(result.get("is_authors_artifact"))
                try:
                    link.llm_confidence = float(result.get("confidence") or 0.0)
                except (TypeError, ValueError):
                    link.llm_confidence = 0.0
                link.llm_reason = str(result.get("reason") or "").strip() or None
                logger.info(
                    "link_relevance: %s -> %s: %s (confidence=%.2f) %s",
                    pub.id,
                    link.url,
                    "authors' own" if link.is_relevant else "not authors' own",
                    link.llm_confidence,
                    link.llm_reason or "",
                )
                classified += 1
            pub.processing[self.name] = ProcessingState(
                status=ProcessingStatus.FAILED
                if error
                else (ProcessingStatus.COMPLETED if classified else ProcessingStatus.COMPLETED_EMPTY),
                attempts=(state.attempts if state else 0) + 1,
                finished_at=datetime.now(UTC),
                result_count=classified,
                error=error,
            )
            changed += 1
        logger.info("link_relevance: done, %d publication(s) processed", changed)
        self.prepared.write_models("publications", publications.values())
        self.prepared.write_models("repo_links", links_by_publication)
        return {"publications": changed}

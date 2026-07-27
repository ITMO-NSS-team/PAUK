from __future__ import annotations

import logging
import os

from .config import CLASSIFY_MODEL, PERSONS_RU_MODEL, pdf_path_for
from .conveyor import PipelinePerson, PubLinks, PubStage, PubUnit, RepoLink, Stage
from .llm import chat_json

# Переходно тянем протестированные хелперы из старого модуля; когда operations.py
# уйдёт, переедут сюда/в общий util.
from .operations import (
    CLASSIFY_PROMPT,
    OpenReviewClient,
    crossref_authors,
    deduplicate,
    emails_from_pdf,
    extract,
    extract_from_abstract,
    extract_from_pdf,
    extract_from_person,
    fetch_openalex_author,
    fetch_orcid_record,
    match_authors,
    norm,
    parse_openalex_author,
    parse_orcid_record,
    search_terms,
    surname_match,
    tail,
    tail_id,
    verify,
)

logger = logging.getLogger(__name__)


RU_SYSTEM_PROMPT = """\
Ты транслитерируешь имя сотрудника ИТМО с английского на русский.
Верни русские ФИО:
  - surname_ru — фамилия,
  - first_name_ru — имя,
  - second_name_ru — отчество (только если явно видно из имени; иначе "").
Не выдумывай отчество. Если транслитерировать однозначно нельзя — оставь поле пустым.
Ответь СТРОГО валидным JSON без markdown:
{"surname_ru":"...","first_name_ru":"...","second_name_ru":""}
"""


class TranslateNames(Stage):
    """Русские ФИО из name_en и варианты. Self-contained: нужен только сам объект."""

    name = "translate_names"

    def apply(self, p: PipelinePerson) -> None:
        if p.surname_ru:                       # уже переведено
            return
        if not p.name_en:
            return
        variants = "; ".join(p.name_variants) if p.name_variants else "—"
        data = chat_json(PERSONS_RU_MODEL, [
            {"role": "system", "content": RU_SYSTEM_PROMPT},
            {"role": "user", "content": f"name_en: {p.name_en}\nварианты: {variants}"},
        ])
        if not data:
            return
        p.surname_ru = (data.get("surname_ru") or "").strip() or None
        p.first_name_ru = (data.get("first_name_ru") or "").strip() or None
        p.second_name_ru = (data.get("second_name_ru") or "").strip() or None


class EnrichOpenreview(Stage):
    """Профиль OpenReview по имени, подтверждение по orcid / @itmo.ru / аффилиации.
    Клиент логинится один раз. Полный профиль в p.profile['openreview'],
    плюс плоские граф-поля github/scholar и рабочий orcid."""

    name = "enrich_openreview"

    def __init__(self) -> None:
        self._client: OpenReviewClient | None = None

    def _api(self) -> OpenReviewClient:
        if self._client is None:
            self._client = OpenReviewClient()      # логин один раз
        return self._client

    def apply(self, p: PipelinePerson) -> None:
        if p.openreview or not p.name_en:
            return
        norms = {n for n in ({norm(p.name_en)} | {norm(v) for v in p.name_variants}) if n}
        our_orcid = p.orcid                        # рабочее поле (exclude=True)
        extra = [v for v in p.name_variants if norm(v) and norm(v) != norm(p.name_en)]
        for term in list(search_terms(p.name_en)) + extra:
            for profile in self._api().search(term):
                content = profile.get("content", {})
                matched_by = verify(content, norms, our_orcid)
                if not matched_by:
                    continue
                p.profile = {**(p.profile or {}), "openreview": extract(profile, matched_by, p.name_en)}
                p.openreview = profile.get("id")
                if not p.github and content.get("github"):
                    p.github = tail(content["github"])
                if not p.google_scholar and content.get("gscholar"):
                    p.google_scholar = content["gscholar"]
                if not p.orcid and content.get("orcid"):      # сигнал для enrich_persons/crossref
                    p.orcid = tail(content["orcid"])
                return


class EnrichPersons(Stage):
    """OpenAlex /authors + ORCID /record."""

    name = "enrich_persons"

    def apply(self, p: PipelinePerson) -> None:
        if not p.id.startswith("itmo_A"):        # только OpenAlex-персоны
            return
        if p.profile and "openalex" in p.profile:  # свой источник уже собран
            return
        author_id = p.id[len("itmo_"):]
        author = fetch_openalex_author(author_id)
        oa = parse_openalex_author(author) if author else {}
        orcid = tail_id((author or {}).get("orcid")) or p.orcid
        orc, github, status = {}, [], "no_orcid"
        if orcid:
            record = fetch_orcid_record(orcid)
            if record is None:
                status = "orcid_error"
            else:
                orc = parse_orcid_record(record)
                orc["orcid"] = orcid
                github, _ = extract_from_person(record.get("person") or {})
                status = "enriched"
 
        p.profile = {**(p.profile or {}), "openalex_author_id": author_id, "status": status,
                     "openalex": oa, "orcid": orc,
                     "github_urls": [g["url"] for g in github]}

        # Плоские графовые поля
        if not p.email and orc.get("emails"):
            p.email = orc["emails"][0]
        if not p.github and github:
            p.github = tail(github[0]["url"])
        if not p.google_scholar:
            for u in orc.get("researcher_urls") or []:
                if "scholar.google" in (u.get("url") or ""):
                    p.google_scholar = u["url"]
                    break


# --- Субконвейер по публикациям --------------


class CrossrefOrcid(PubStage):
    """DOI - ORCID авторов/Crossref - рабочее поле orcid ИТМО автору по фамилии.
    Только однозначное совпадение."""

    name = "crossref_orcid"

    def apply(self, pub: PubUnit, out: dict[str, PipelinePerson]) -> None:
        if not pub.doi:
            return
        for family, orcid in crossref_authors(pub.doi):
            cand = [a.person_id for a in pub.authors if surname_match(family, set(a.surnames))]
            if len(cand) == 1:
                part = out.setdefault(cand[0], PipelinePerson(id=cand[0]))
                if not part.orcid:
                    part.orcid = orcid


class CollectEmailsPdf(PubStage):
    """PDF публикации - email - ИТМО автор по фамилии в локальной части адреса."""

    name = "collect_emails_pdf"

    def apply(self, pub: PubUnit, out: dict[str, PipelinePerson]) -> None:
        path = pdf_path_for(pub.id)
        if not os.path.exists(path):
            return
        authors = [(a.person_id, set(a.surnames)) for a in pub.authors]
        for email in emails_from_pdf(path):
            cand = match_authors(email.split("@")[0], authors)
            if len(cand) == 1:
                part = out.setdefault(cand[0], PipelinePerson(id=cand[0]))
                if not part.email:
                    part.email = email


class ExtractRepoLinks(PubStage):
    """Github ссылки из PDF и абстракта публикации. Канонизация URL и
    дедуп по приоритету источника."""

    name = "extract_repo_links"

    def apply(self, pub: PubUnit, out: dict[str, PubLinks]) -> None:
        if pub.id in out:                    # уже извлечено
            return
        path = pdf_path_for(pub.id)
        links = extract_from_pdf(path) if os.path.exists(path) else []
        if not links and pub.abstract:
            links = extract_from_abstract(pub.abstract)
        found = [RepoLink(url=url, context=context, page_number=page)
                 for url, context, page in deduplicate(links)]
        if found:
            out[pub.id] = PubLinks(publication_id=pub.id, links=found)


class ClassifyRepoLinks(PubStage):
    """LLM решает, авторская ли ссылка. Дописывает is_relevant/confidence/reason
    в ссылки, извлечённые ExtractRepoLinks."""

    name = "classify_repo_links"
    model = CLASSIFY_MODEL

    def apply(self, pub: PubUnit, out: dict[str, PubLinks]) -> None:
        entry = out.get(pub.id)
        if entry is None:
            return
        for link in entry.links:
            if link.is_relevant is not None:     # уже классифицирована
                continue
            hint = (f"Источник: видимый текст PDF, страница {link.page_number}."
                    if link.page_number is not None
                    else "Источник: абстракт из OpenAlex (контекст ограничен).")
            data = chat_json(self.model, [{"role": "user", "content": CLASSIFY_PROMPT.format(
                title=pub.title or "(без названия)",
                authors=pub.authors_str or "(авторы не указаны)",
                url=link.url, source_hint=hint,
                context=link.context or "(контекст пустой)")}])
            if not data:
                continue
            link.is_relevant = bool(data.get("is_authors_artifact"))
            try:
                link.llm_confidence = float(data.get("confidence") or 0.0)
            except (TypeError, ValueError):
                link.llm_confidence = 0.0
            link.llm_reason = str(data.get("reason") or "").strip() or None

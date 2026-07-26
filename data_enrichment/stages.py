from __future__ import annotations

import logging

from .config import PERSONS_RU_MODEL
from .conveyor import PipelinePerson, Stage
from .llm import chat_json

# Переходно тянем протестированные хелперы из старого модуля; когда operations.py
# уйдёт, переедут сюда/в общий util.
from .operations import (
    OpenReviewClient,
    extract_from_person,
    fetch_openalex_author,
    fetch_orcid_record,
    norm,
    search_terms,
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
    """Русские ФИО из name_en (+ варианты). Self-contained: нужен только сам объект."""

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
    Клиент логинится один раз (ленивая инициализация, этап живёт весь поток).
    Пишет только граф-поля: openreview + github/scholar как запасной источник."""

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
                if not verify(content, norms, our_orcid):
                    continue
                p.openreview = profile.get("id")
                if not p.github and content.get("github"):
                    p.github = tail(content["github"])
                if not p.google_scholar and content.get("gscholar"):
                    p.google_scholar = content["gscholar"]
                return


class EnrichPersons(Stage):
    """ORCID → email/github/scholar (граф-поля). OpenAlex-id из person.id даёт orcid,
    ORCID /record — контакты. Библиометрию OpenAlex в граф не кладём, потому не тянем.
    По данным ORCID-email — крупнейший уникальный источник email (238 персон)."""

    name = "enrich_persons"

    def apply(self, p: PipelinePerson) -> None:
        if not p.id.startswith("itmo_A"):              # только OpenAlex-персоны
            return
        if p.email and p.github and p.google_scholar:  # добирать нечего
            return
        author = fetch_openalex_author(p.id[len("itmo_"):])
        orcid = tail_id((author or {}).get("orcid")) or p.orcid
        if not orcid:
            return
        record = fetch_orcid_record(orcid)
        if not record:
            return
        person = record.get("person") or {}
        github_findings, researcher_urls = extract_from_person(person)
        emails = [e.get("email") for e in (person.get("emails") or {}).get("email", [])
                  if e.get("email")]
        scholar = next((u["url"] for u in researcher_urls
                        if "scholar.google" in (u.get("url") or "")), None)
        if not p.email and emails:
            p.email = emails[0]
        if not p.github and github_findings:
            p.github = tail(github_findings[0]["url"])
        if not p.google_scholar and scholar:
            p.google_scholar = scholar
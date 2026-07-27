from __future__ import annotations

import logging
import os
import uuid

from .config import (
    BROWSER_USER_AGENT,
    CLASSIFY_MODEL,
    DEPT_MODEL,
    DEPT_TIMEOUT,
    PAGE_SCRAPE_TIMEOUT,
    PERSONS_RU_MODEL,
    pdf_path_for,
)
from .conveyor import PipelinePerson, PubLinks, PubStage, PubUnit, RepoLink, Stage
from .helpers import (
    CLASSIFY_PROMPT,
    OpenReviewClient,
    alpha,
    author_surnames,
    crossref_authors,
    deduplicate,
    emails_from_html,
    emails_from_pdf,
    extract,
    extract_from_abstract,
    extract_from_pdf,
    extract_from_person,
    fetch_openalex_author,
    fetch_orcid_record,
    is_page,
    match_authors,
    norm,
    normalize,
    parse_openalex_author,
    parse_orcid_record,
    search_terms,
    surname_match,
    tail,
    tail_id,
    usable_email,
    verify,
)
from .llm import chat_json
from .models import Department

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
                u = usable_email(email)
                if u and ("pdf", u) not in part.email_candidates:
                    part.email_candidates.append(("pdf", u))


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


# --- Департаменты: реестр + шортлист ------------------------------------------

DEPT_SHORTLIST_PROMPT = """\
Ты — эксперт по оргструктуре университета ИТМО. Сопоставь аффилиацию человека
с подразделениями ИТМО.

Ты получишь СПИСОК КАНДИДАТОВ (предварительно отобранные похожие подразделения:
name_en и variants) и сырую строку affiliation.

ДЕЙСТВИЯ:
1. Выдели в аффилиации упоминания подразделений ИТМО.
   - Голый университет без подразделения (ITMO University, ITMO) — НЕ извлекай.
   - Несколько подразделений в одной строке — извлеки КАЖДОЕ.
   - Нормализуй: убери хвостовое "ITMO University", кавычки, двойные пробелы.
2. Сравни каждое извлечённое название с кандидатами:
   - Мелкие различия (регистр, Center/Centre, Lab/Laboratory, перестановка слов)
     — ОДНО И ТО ЖЕ → matched=true, existing_name_en из списка; если написание
     слегка иное — add_variant_to_existing=true.
   - Название лишь часть другого, более длинного — РАЗНЫЕ (сомневаешься — разные).
   - Подходящего кандидата нет → matched=false, is_new=true, new_name_en.

Верни СТРОГО валидный JSON без markdown:
{"departments":[{"extracted_name":"...","matched":true,"existing_name_en":"...",
"is_new":false,"new_name_en":"","add_variant_to_existing":false}]}
"""


class DepartmentRegistry:
    """Реестр департаментов в памяти: общее растущее состояние этапа. Следующая
    персона видит созданное предыдущей — дубликаты не плодятся (поток и так
    последователен). В конце прогона to_models() уходит в сток."""

    def __init__(self, departments: list[Department] | None = None) -> None:
        self.departments: dict[str, Department] = {d.id: d for d in (departments or [])}

    def _names(self, d: Department) -> list[str]:
        return [n for n in [d.name_en, d.name_ru, *d.name_variants] if n]

    def resolve(self, name: str) -> str | None:
        """id по точному (нормализованному) совпадению имени/варианта."""
        target = normalize(name)
        if not target:
            return None
        for did, d in self.departments.items():
            if any(normalize(n) == target for n in self._names(d)):
                return did
        return None

    def create(self, name_en: str) -> str:
        existing = self.resolve(name_en)
        if existing:
            return existing
        did = f"dept_{uuid.uuid4().hex[:12]}"
        self.departments[did] = Department(id=did, name_en=name_en)
        return did

    def add_variant(self, dept_id: str, variant: str) -> None:
        d = self.departments.get(dept_id)
        if not d or not variant:
            return
        known = {normalize(n) for n in self._names(d)}
        if normalize(variant) not in known:
            d.name_variants.append(variant)

    def shortlist(self, affiliation: str, k: int = 8) -> list[Department]:
        """Кандидаты по строковому сходству: подстрока → 1.0, иначе доля токенов
        названия, найденных в аффилиации. Замена полного списка в промпте."""
        aff_norm = normalize(affiliation)
        aff_tokens = set(aff_norm.split())
        scored = []
        for d in self.departments.values():
            best = 0.0
            for n in self._names(d):
                nn = normalize(n)
                if not nn:
                    continue
                if nn in aff_norm:
                    best = 1.0
                    break
                toks = set(nn.split())
                if toks:
                    best = max(best, len(toks & aff_tokens) / len(toks))
            if best >= 0.34:
                scored.append((best, d))
        scored.sort(key=lambda x: -x[0])
        return [d for _, d in scored[:k]]

    def to_models(self) -> list[Department]:
        return list(self.departments.values())


# Голый университет — не департамент. Guard против главной ошибки LLM на этом
# этапе (живой прогон: модель завела "ITMO University" как департамент).
NOT_A_DEPARTMENT = {
    "itmo", "itmo university", "university itmo", "national research university itmo",
    "itmo national research university", "university of itmo",
    "saint petersburg national research university of information technologies mechanics and optics",
}


def is_bare_university(name: str) -> bool:
    return normalize(name) in NOT_A_DEPARTMENT


class EnrichDepartments(Stage):
    """Аффилиация (рабочее поле) → department_ids через LLM. В промпт идёт шортлист
    кандидатов, не весь реестр — режет контекст на порядок. Новые департаменты
    заводятся в реестре; сомнительное 'создать' сперва проверяется по имени."""

    name = "enrich_departments"
    model = DEPT_MODEL

    def __init__(self, registry: DepartmentRegistry) -> None:
        self.registry = registry

    def apply(self, p: PipelinePerson) -> None:
        if p.department_ids or not p.affiliation:
            return
        cands = self.registry.shortlist(p.affiliation)
        cand_block = "\n".join(
            f'- name_en: {d.name_en}\n  variants: [{", ".join(d.name_variants) or "—"}]'
            for d in cands) or "(похожих не найдено — всё в аффилиации будет новым)"
        data = chat_json(self.model, [
            {"role": "system", "content": DEPT_SHORTLIST_PROMPT},
            {"role": "user", "content": f"КАНДИДАТЫ:\n{cand_block}\n\naffiliation: {p.affiliation}"},
        ], timeout=DEPT_TIMEOUT)
        if not data:
            return
        ids: list[str] = []
        for dep in data.get("departments", []):
            if dep.get("is_new"):
                name = (dep.get("new_name_en") or dep.get("extracted_name") or "").strip()
                if name and not is_bare_university(name):
                    ids.append(self.registry.create(name))
                continue
            name = (dep.get("existing_name_en") or "").strip()
            did = self.registry.resolve(name) if name else None
            if did is None:
                fallback = name or (dep.get("extracted_name") or "").strip()
                if fallback and not is_bare_university(fallback):
                    did = self.registry.create(fallback)
            if did:
                ids.append(did)
                if dep.get("add_variant_to_existing"):
                    self.registry.add_variant(did, (dep.get("extracted_name") or "").strip())
        p.department_ids = list(dict.fromkeys(ids))


class CollectEmailsPages(Stage):
    """Email с личных/лаб-страниц: адреса берутся из собранного профиля
    (researcher_urls ORCID + homepage OpenReview), почта привязывается по фамилии
    в локальной части. Идёт после EnrichPersons/EnrichOpenreview — нужен p.profile.
    Кандидаты с источником "page" — выбор в finalize_emails."""

    name = "collect_emails_pages"

    def __init__(self) -> None:
        import requests
        self._session = requests.Session()
        self._session.headers["User-Agent"] = BROWSER_USER_AGENT

    def _urls(self, p: PipelinePerson) -> list[str]:
        prof = p.profile or {}
        urls = [u.get("url") for u in (prof.get("orcid") or {}).get("researcher_urls") or []]
        urls.append((prof.get("openreview") or {}).get("homepage"))
        return [u for u in dict.fromkeys(urls) if is_page(u)]

    def apply(self, p: PipelinePerson) -> None:
        if any(src == "page" for src, _ in p.email_candidates):   # уже обходили
            return
        surnames = author_surnames(p.name_en or "", None) | {
            s for v in p.name_variants for s in author_surnames(v, None)}
        if not surnames:
            return
        for url in self._urls(p):
            try:
                r = self._session.get(url, timeout=PAGE_SCRAPE_TIMEOUT, allow_redirects=True)
            except Exception:
                continue
            if r.status_code != 200 or "html" not in r.headers.get("Content-Type", "").lower():
                continue
            for email in emails_from_html(r.text):
                if any(s in alpha(email.split("@")[0]) for s in surnames):
                    u = usable_email(email)
                    if u and ("page", u) not in p.email_candidates:
                        p.email_candidates.append(("page", u))

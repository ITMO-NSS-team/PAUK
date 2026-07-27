from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterable

from pydantic import BaseModel, Field

from .models import ItmoPerson

logger = logging.getLogger(__name__)


class PipelinePerson(ItmoPerson):
    """Объект, текущий по конвейеру: граф-модель и рабочие поля.

    Рабочие поля — поля, которые не идут в (orcid сигнал матчинга, affiliation для классификации департамента).
    Помечены exclude=True: текут по пайплайну, но model_dump/JSON их выкидывает.
    """

    orcid: str | None = Field(default=None, exclude=True)
    affiliation: str | None = Field(default=None, exclude=True)
    profile: dict | None = None


class Stage(ABC):
    """Этап конвейера. Обогащает объект своими полями, не затирая чужие."""

    name: str = "stage"

    @abstractmethod
    def apply(self, person: PipelinePerson) -> None:
        raise NotImplementedError


class Conveyor:
    """Гонит объекты из источника через этапы в сток."""

    def __init__(self, *stages: Stage) -> None:
        self.stages = stages

    def run(self, source: Iterable[PipelinePerson], sink: Callable[[PipelinePerson], None]) -> int:
        done = 0
        for person in source:
            for stage in self.stages:
                try:
                    stage.apply(person)
                except Exception:
                    logger.exception("[%s] упал на %s", stage.name, person.id)
            sink(person)
            done += 1
        logger.info("конвейер: обработано %d объектов", done)
        return done


def to_json(person: PipelinePerson) -> str:
    """Объект -> JSON только с граф-полями: exclude_none убирает пустые, а рабочие
    поля отсеиваются сами (exclude=True). Так виден вклад этапов и JSON лёгкий."""
    return person.model_dump_json(exclude_none=True)


def merge_by_id(main: dict[str, PipelinePerson], partial: PipelinePerson) -> None:
    """Мёрдж результата субконвейера в основной объект по person_id: переносит
    заполненные поля partial, включая рабочие, не затирая уже проставленные в main.
    Идём по полям модели, а не по model_dump иначе рабочие exclude поля выпадут."""
    target = main.get(partial.id)
    if target is None:
        main[partial.id] = partial
        return
    for field in type(partial).model_fields:
        value = getattr(partial, field, None)
        if field != "id" and value and not getattr(target, field, None):
            setattr(target, field, value)


# --- Субконвейер по публикациям -----------------------------------
# Этапы, где на вход публикация, а не персона.


class PubAuthor(BaseModel):
    person_id: str
    surnames: list[str] = []          # фамилии для привязки email/orcid


class PubUnit(BaseModel):
    """Единица субконвейера. Поля по необходимости этапов: doi/authors — для привязки
    orcid/email к персонам; title/abstract — для extract/classify ссылок."""
    id: str
    doi: str | None = None
    title: str | None = None
    abstract: str | None = None
    authors_str: str | None = None    # имена авторов строкой — для промпта classify
    authors: list[PubAuthor] = []


class RepoLink(BaseModel):
    url: str
    context: str | None = None
    page_number: int | None = None
    is_relevant: bool | None = None
    llm_confidence: float | None = None
    llm_reason: str | None = None


class PubLinks(BaseModel):
    """Выход ветки код-ссылок: ссылки публикации. Ключ publication_id, не person_id
    Ссылки привязаны к статье, к людям выходят позже через github ветку."""
    publication_id: str
    links: list[RepoLink] = []


class PubStage(ABC):
    """Этап субконвейера: из публикации извлекает данные в аккумулятор out. Значение
    out зависит от набора этапов - партиалы персон (crossref/pdf) или ссылки (extract/
    classify); в одном прогоне их не смешивают."""

    name: str = "pub_stage"

    @abstractmethod
    def apply(self, pub: PubUnit, out: dict) -> None:
        raise NotImplementedError


def run_sub(source: Iterable[PubUnit], stages: Iterable[PubStage]) -> dict:
    """Гонит публикации через pub этапы, копит результат по ключу (person_id или
    publication_id - зависит от этапов)."""
    stages = tuple(stages)
    out: dict = {}
    n = 0
    for pub in source:
        for stage in stages:
            try:
                stage.apply(pub, out)
            except Exception:
                logger.exception("[%s] упал на публикации %s", stage.name, pub.id)
        n += 1
    logger.info("субконвейер: публикаций %d, результатов %d", n, len(out))
    return out
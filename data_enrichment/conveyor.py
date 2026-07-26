from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterable

from pydantic import Field

from .models import ItmoPerson

logger = logging.getLogger(__name__)


class PipelinePerson(ItmoPerson):
    """Объект, текущий по конвейеру: граф-модель и рабочие поля.

    Рабочие поля — поля, которые не идут в (orcid сигнал матчинга, affiliation для классификации департамента).
    Помечены exclude=True: текут по пайплайну, но model_dump/JSON их выкидывает.
    """

    orcid: str | None = Field(default=None, exclude=True)
    affiliation: str | None = Field(default=None, exclude=True)


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
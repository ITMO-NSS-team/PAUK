"""Задел на будущее: publication-rooted агрегат.

Сегодня пайплайн (data_enrichment/run_conveyor.py) не пишет publications.jsonl
и repositories.jsonl — только persons.jsonl/departments.jsonl/repo_links.jsonl
(person-rooted). PublicationAggregate здесь никем не используется — это цель
для того момента, когда пайплайн начнёт эмитить публикации как корневой
объект. pauk/graph/ загружает то, что реально есть, через NODE_REGISTRY в
extract.py, а не через этот класс.
"""

from pydantic import BaseModel

from .publication import Publication


class PublicationAuthorLink(BaseModel):
    person_id: str
    position: int | None = None
    affiliation: str | None = None
    is_corresponding: bool = False


class PublicationAggregate(BaseModel):
    publication: Publication
    authors: list[PublicationAuthorLink] = []
    repository_ids: list[str] = []  # -> IMPLEMENTS, когда появятся repositories.jsonl


__all__ = ["PublicationAggregate", "PublicationAuthorLink"]

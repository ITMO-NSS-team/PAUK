"""Декларативное отображение JSONL-строк (обычных dict) в узлы/рёбра Neo4j.

Работает на plain dict, не на pydantic-инстансах — не тянет
data_enrichment.conveyor.PipelinePerson сюда. Это заодно и решает проблему
"рабочих" полей PipelinePerson (orcid/affiliation/email_candidates/profile,
которые из-за бага в conveyor.py::to_json() всё же попадают в persons.jsonl):
у extract_node — БЕЛЫЙ список нужных ключей (prop_fields), а не чёрный список
лишних, так что любые случайные поля молча игнорируются.

Реестр покрывает все типы узлов/рёбер из data_enrichment/models.py, не только
те два (Department/ItmoPerson), для которых пайплайн сегодня реально пишет
JSONL — остальное зарегистрировано заранее и просто не сработает, пока файлов
нет (см. jsonl_loader.py::FILE_SPECS).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field


@dataclass(frozen=True)
class RelSpec:
    field: str  # ключ в исходном dict с данными связи
    rel_type: str  # тип связи в Cypher, напр. "BELONGS_TO"
    tgt_label: str
    tgt_id_field: str | None  # None = элементы списка сами есть id (напр. department_ids)
    prop_fields: tuple[str, ...] = ()
    tgt_match_field: str = "id"  # по какому свойству искать целевой узел
    scalar: bool = False  # True = field хранит одно значение (str|None), не список
    guard: Callable[[dict], bool] | None = None  # фильтр на элемент, для discriminated union


@dataclass(frozen=True)
class NodeSpec:
    labels: str  # "Person:Itmo"
    id_field: str = "id"
    prop_fields: tuple[str, ...] = ()
    relationships: tuple[RelSpec, ...] = field(default_factory=tuple)


NODE_REGISTRY: dict[str, NodeSpec] = {
    "department": NodeSpec(
        labels="Department",
        prop_fields=("name_en", "name_ru", "name_variants"),
    ),
    "itmo_person": NodeSpec(
        labels="Person:Itmo",
        prop_fields=(
            "name_en", "name_variants", "email", "first_name_ru",
            "second_name_ru", "surname_ru", "degree", "github",
            "google_scholar", "openreview", "thesis", "created_at",
        ),
        relationships=(
            RelSpec("department_ids", "BELONGS_TO", "Department", None),
            RelSpec(
                "authored", "AUTHORED", "Publication", "publication_id",
                ("position", "affiliation", "is_corresponding"),
            ),
            RelSpec(
                "contributed_to", "CONTRIBUTED_TO", "Repository", "repository_id",
                ("role",),
            ),
        ),
    ),
    "publication": NodeSpec(
        labels="Publication",
        prop_fields=(
            "title", "journal", "doi", "publication_date", "year", "has_code",
            "code_url", "funding", "openalex_url", "pdf_url", "abstract",
        ),
        relationships=(
            RelSpec("department_ids", "PRODUCED_BY", "Department", None),
            RelSpec(
                "mentions_links", "MENTIONS_LINK", "Repository", "repository_url",
                ("context", "page_number", "is_relevant", "llm_confidence", "llm_reason"),
                tgt_match_field="url",
                guard=lambda item: item.get("target_kind") == "repository",
            ),
            RelSpec(
                "mentions_links", "MENTIONS_LINK", "LinkCandidate", "candidate_id",
                ("context", "page_number", "is_relevant", "llm_confidence", "llm_reason"),
                guard=lambda item: item.get("target_kind") == "candidate",
            ),
        ),
    ),
    "repository": NodeSpec(
        labels="Repository",
        prop_fields=(
            "name", "url", "description", "access_date", "has_readme",
            "stars_num", "last_updated", "license", "contributors",
        ),
        relationships=(
            RelSpec("department_ids", "DEVELOPED_BY", "Department", None),
            RelSpec("publication_ids", "IMPLEMENTS", "Publication", None),
            RelSpec(
                "owner_login", "OWNED_BY", "GitHubProfile", None,
                tgt_match_field="login", scalar=True,
            ),
        ),
    ),
    "github_profile": NodeSpec(
        labels="GitHubProfile",
        prop_fields=("login", "name", "html_url", "description", "location", "type"),
    ),
    "link_candidate": NodeSpec(
        labels="LinkCandidate",
        prop_fields=("url", "host"),
    ),
    # "external_person" пока не регистрируем — такие строки нигде не
    # эмитятся сегодня (нет отдельного файла/дискриминатора в persons.jsonl).
}


def extract_node(row: dict, spec: NodeSpec) -> tuple[str, tuple[str, dict]]:
    """-> (labels, (node_id, properties)), под Neo4jClient.upsert_nodes_batch."""
    node_id = row[spec.id_field]
    props = {k: row[k] for k in spec.prop_fields if row.get(k) is not None}
    return spec.labels, (node_id, props)


def extract_relationships(
    row: dict, spec: NodeSpec
) -> dict[tuple[str, str, str, str], list[tuple[str, str, dict]]]:
    """-> {(src_label, tgt_label, rel_type, tgt_match_field): [(src_id, tgt_id, rel_props), ...]}.

    Ключ включает tgt_match_field, потому что один и тот же rel_type
    (MENTIONS_LINK) может матчить цель по разным свойствам в зависимости от
    RelSpec (url для Repository, id для LinkCandidate) — их нельзя валить
    в один батч.
    """
    src_id = row[spec.id_field]
    out: dict[tuple[str, str, str, str], list[tuple[str, str, dict]]] = {}

    for rel in spec.relationships:
        key = (spec.labels, rel.tgt_label, rel.rel_type, rel.tgt_match_field)

        if rel.scalar:
            value = row.get(rel.field)
            if value is None:
                continue
            out.setdefault(key, []).append((src_id, value, {}))
            continue

        for item in row.get(rel.field) or []:
            if rel.tgt_id_field is None:
                out.setdefault(key, []).append((src_id, item, {}))
                continue
            if rel.guard is not None and not rel.guard(item):
                continue
            tgt_id = item.get(rel.tgt_id_field)
            if tgt_id is None:
                continue
            props = {k: item[k] for k in rel.prop_fields if item.get(k) is not None}
            out.setdefault(key, []).append((src_id, tgt_id, props))

    return out

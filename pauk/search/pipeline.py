"""One question end to end: plan → entry points → subgraph → ranking → answer."""

from __future__ import annotations

from datetime import datetime
import json
import logging
import os
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field, replace
from typing import Any

from pauk.settings import Settings, settings

from . import templates
from .entities import Resolution, load_department_index, load_person_index
from .llm import LLMError, compose_answer, make_client, parse_question
from .plan import QueryPlan, fallback_plan
from .ranking import preset, rank, trim
from .reader import GraphReader
from .retrieval import Collection, Embedder, Hit, Mode

logger = logging.getLogger(__name__)
# log_file_path = os.path.join("pauk/search/logs", f"pipeline.log")
timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
log_file_path = os.path.join("pauk/search/logs", f"{timestamp}-pipeline.log")

@dataclass
class AskOptions:
    retrieval_mode: Mode = "hybrid"
    ranking: str = "graph"  # "graph" or "authorship", see ranking.PRESETS
    llm_parse: bool = True
    llm_answer: bool = True
    top_k_publications: int = 100
    top_k_repositories: int = 30
    min_score: float = 0.0  # drop entry points below this relevance
    top_n: int = 10
    plan: QueryPlan | None = None  # a ready plan skips LLM №1 (manual runs, evaluation)


@dataclass
class _Timer:
    marks: dict[str, float] = field(default_factory=dict)
    start: float = field(default_factory=time.perf_counter)

    def mark(self, name: str) -> None:
        now = time.perf_counter()
        self.marks[name] = round(now - self.start, 3)
        self.start = now


class Engine:
    def __init__(self, config: Settings | None = None) -> None:
        self.config = config or settings
        data_dir = self.config.search_dir
        if not (data_dir / "manifest.json").exists():
            raise FileNotFoundError(f"no search index in {data_dir}; build it first: pauk search build-index")
        self.manifest = json.loads((data_dir / "manifest.json").read_text(encoding="utf-8"))
        self.graph = GraphReader(self.config)
        self.publications = Collection.load(data_dir, "publications")
        self.repositories = Collection.load(data_dir, "repositories")
        self.pub_titles = {
            row["id"]: row.get("title")
            for row in json.loads((data_dir / "publications.json").read_text(encoding="utf-8"))
        }
        self.persons = load_person_index(data_dir)
        self.departments = load_department_index(data_dir)
        self.embedder = Embedder(self.manifest["embedding_model"])
        self.llm = make_client(self.config)

    def close(self) -> None:
        self.graph.close()

    def ask(self, question: str, expected_template, options: AskOptions | None = None) -> dict[str, Any]:
        options = options or AskOptions()
        timer = _Timer()
        notes: list[str] = []
        trace: dict[str, Any] = {}

        if options.plan is not None:
            plan = options.plan
        elif options.llm_parse:
            plan, trace["parse"] = parse_question(self.llm, question)
            if "fallback" in trace["parse"]:
                notes.append("LLM не разобрала вопрос, выполнен поиск по теме всего вопроса.")
        else:
            plan = fallback_plan(question)
        timer.mark("parse")

        resolutions = [self._resolve(entity.kind, entity.name) for entity in plan.entities]
        anchor = next((r for r in resolutions if r.best), None)
        for resolution in resolutions:
            if not resolution.best:
                notes.append(f"Не найдено в графе: «{resolution.query}».")
            elif resolution.ambiguous:
                options_text = "; ".join(c.label for c in resolution.candidates[:5])
                notes.append(f"Имя «{resolution.query}» неоднозначно, выбран «{resolution.best.label}». "
                             f"Другие варианты: {options_text}.")
        timer.mark("entities")

        counts: dict[str, Any] = {}
        hits: list[Hit] = []
        data: dict[str, Any] = {}
        pub_rel: dict[str, float] = {}
        repo_rel: dict[str, float] = {}

        logger.info("Resolved entities: %s",  resolutions)
        if any(not res.candidates for res in resolutions):
            logger.error("One or more resolutions have empty candidates list" )
            raise ValueError("One or more resolutions have empty candidates list.")

        logger.info("Complete parsed plan: %s", plan)
        if plan.template !=expected_template:
            logger.error("Parse to not expected template")
            notes.append(f"Parsed not to expected template={expected_template}, LLM suggested template={plan.template}")
            raise ValueError(f"Parsed not to expected template={expected_template}, LLM suggested template={plan.template}")


        if plan.template in ("1", "2", "3", "4", "5"):
            # logger.info(plan)
            timer.mark("graph")
            answer_data, counts = self._execute_graph_template(plan, resolutions)
            timer.mark("ranking")

            answer = answer_data if not counts else counts
            # if options.llm_answer:
            #     try:
            #         answer = compose_answer(self.llm, question, sections, notes)
            #     except LLMError as exc:
            #         notes.append(f"Текстовый ответ не получен: {exc}")
            timer.mark("answer")
            return self._result(question, plan, options, resolutions, hits, counts, answer, notes, trace,
                                timer)
        else:
            # Шаблоны 6–10 и "none": задействуем семантический поиск и профили
            if plan.type in ("graph", "mixed") and anchor is None:
                return self._result(question, plan, options, resolutions, [], {}, {}, None, notes, trace, timer)

            if plan.type == "graph":
                data, pub_rel, repo_rel, counts = self._profile(anchor, plan)
            else:
                if options.retrieval_mode != "bm25" and not self.embedder.available:
                    notes.append("Модель эмбеддингов не установлена (pip extra `search`), поиск выполнен только BM25.")
                    options = replace(options, retrieval_mode="bm25")
                allowed = self._anchor_publications(anchor) if plan.type == "mixed" else None
                hits = self.publications.search(options.retrieval_mode, plan.core, self.embedder,
                                                options.top_k_publications, allowed)
                repo_hits = [] if allowed is not None else self.repositories.search(
                    options.retrieval_mode, plan.core, self.embedder, options.top_k_repositories)
                pub_rel = {h.id: h.score for h in hits if h.score >= options.min_score}
                repo_rel = {h.id: h.score for h in repo_hits if h.score >= options.min_score}
                timer.mark("retrieval")
                trace["repository_hits"] = [{"id": h.id, "score": round(h.score, 4)} for h in repo_hits[:10]]
                data = templates.topic_subgraph(self.graph, list(pub_rel), list(repo_rel))
            timer.mark("graph")

        ranking = preset(options.ranking)
        sections = trim(rank(data, pub_rel, repo_rel, plan, ranking), plan, options.top_n)
        if counts and (
                "counts" in plan.expected_values or plan.template in ("1", "2", "3", "4", "5") or plan.type == "graph"):
            sections["counts"] = counts
        timer.mark("ranking")

        # 5. Генерация текстового ответа через LLM №2[cite: 5, 11]
        answer = None
        if options.llm_answer:
            try:
                answer = compose_answer(self.llm, question, sections, notes)
            except LLMError as exc:
                notes.append(f"Текстовый ответ не получен: {exc}")
            timer.mark("answer")

        return self._result(question, plan, options, resolutions, hits, counts, answer, notes, trace, timer)

    # resolves only 1-5 templates (only graph search)
    def _execute_graph_template(self, plan: QueryPlan, resolutions: list[Resolution]) -> tuple[
        dict[str, Any], int | dict| None]:
        logger.info("Executing graph template %s (subtype %s) with params %s", plan.template, plan.subtype,
                    plan.params)

        # 1. Получаем шаблон или словарь шаблонов из TEMPLATE_QUERIES
        template_def = templates.TEMPLATE_QUERIES.get((plan.template, plan.subtype))
        # if isinstance(template_def, dict): template_def = template_def.get([0])
        dept_resolution = next((res for res in resolutions if res.kind == 'department'), None)
        dept_flag = False

        person_resolution = next((res for res in resolutions if res.kind == 'person'), None)
        person_flag = True
        if (plan.template in {"1", "2", "4", "5"} or
                (plan.template == "3" and plan.subtype == "3e")):
            if ('by_dept' in plan.params or 'by_dept' in plan.params.values()) or dept_resolution:
                dept_flag = True
                template_def = template_def.get("by_dept")
                # logger.info("We're here, template_def: %s", template_def)
            else:
                template_def = template_def.get("itmo")

        logger.info("Template resolved: %s", template_def)
        if not template_def:
            logger.error("couldn't resolve template")
            raise ValueError(f"No cypher template found for template={plan.template}, subtype={plan.subtype}")

        # 3. Выбираем конкретный Cypher-запрос (by_dept или itmo)
        # if isinstance(template_def, dict):
        #     if dept_resolution and "by_dept" in template_def:
        #         cypher_query = template_def["by_dept"]
        #     else:
        #         cypher_query = template_def.get("itmo") or list(template_def.values())[0]
        # else:
        #     cypher_query = template_def
        # 4. Формируем словарь параметров для Cypher-запроса, учитывая фильтры и ID сущностей
        query_params: dict[str, Any] = {}
        # Прокидываем фильтры дат из плана
        if plan.filter:
            query_params["year_from"] = plan.filter.year_from
            query_params["year_to"] = plan.filter.year_to
        # Резолв департамента
        # dept_resolution = next((res for res in resolutions if res.kind =='department'), None)
        if dept_flag and dept_resolution.candidates:
            best_candidate = max(dept_resolution.candidates, key=lambda c: c.score)
            query_params["dept_id"] = best_candidate.id
        if person_resolution and person_resolution.candidates:
            best_candidate = max(person_resolution.candidates, key=lambda c: c.score)
            query_params["person_id"] = best_candidate.id

        # 5. Выполняем запрос через GraphReader
        # logger.info(f"template_def type = {type(template_def)}, value = {template_def}")
        logger.info("Running Cypher query:\n%s\nwith params: %s", template_def, query_params)
        raw_results = self.graph.read(template_def, **query_params)
        logger.info("Obtained raw results:\n%s\n", raw_results)

        data = {
            "persons": {},
            "publications": {},
            "repositories": {},
            "departments": {}
        }

        for row in raw_results:
            match row:
                case {"person_id": p_id, **args}:
                    data["persons"][p_id] = row

                case {"publication_id": p_id, **args}:
                    data["publications"][p_id] = row

                case {"department_id": d_id, **arfs}:
                    data["departments"][d_id] = row

                case _:
                    pass

        # applying parsed by hand
        filters = plan.params.get('filters', [])
        if filters:
            self._apply_filters(data, filters)

        logger.info("Structured data: \n%s\n", data)

        # counts = {
        #     "entity": dept_resolution.best.label if dept_resolution and dept_resolution.best else "ИТМО",
        #     "persons": len(raw_results),
        # }
        groupby_field = plan.params.get('group_by')
        if groupby_field:
            self._apply_group_by(data, groupby_field)


        counts = None
        if plan.params.get('metric') and plan.params.get('metric') == 'count':
            target = plan.params.get('target')
            match target:
                case 'persons' | 'publications' | 'repositories' | 'departments':
                    coll = data.get(target)
                    if isinstance(coll, list):
                       counts = len(coll)
                    elif isinstance(coll, dict):
                        if coll and all(isinstance(v, list) for v in coll.values()):
                            counts = {group_key: len(items) for group_key, items in coll.items()}
                        else:
                            counts = len(coll)
                    else:
                        counts = 0

                    logger.debug("Counts: %s", counts)
                case _:
                    counts = None
        if counts:
            logger.info("Counts: %s\n", counts)

        # logger.info("Data: %s\n", data)
        return data, counts

    def _apply_filters(self, data: dict[str, dict[Any, Any]], filters):
        for entity_type in ['persons', 'publications', 'departments', 'repositories']:
            if entity_type in filters:
                filtered_persons = {}
                for person_id, person_data in data[entity_type].items():
                    match_all = True
                    for f in filters:
                        entity_field = f.get('field')
                        op = f.get('op')
                        val = f.get('value')
                        person_val = person_data.get(entity_field)

                        if op == 'eq':
                            if person_val != val:
                                match_all = False
                                break
                        elif op == 'not_null':
                            if person_val is None:
                                match_all = False
                                break
                        elif op == 'is_null':
                            if person_val is not None:
                                match_all = False
                                break
                        elif op == 'neq':
                            if person_val == val:
                                match_all = False
                                break
                    if match_all:
                        filtered_persons[person_id] = person_data

                data[entity_type] = filtered_persons

    def _apply_group_by(self, data: dict[str, dict[Any, Any]], groupby_field: Any | None):
        for collection_name, collection_data in data.items():
            if collection_data:  # коллекция не пуста
                grouped = defaultdict(list)

                # Определяем элементы в зависимости от структуры (словарь или список)
                items = collection_data.values() if isinstance(collection_data, dict) else collection_data

                for item in items:
                    # Определяем ключ для группировки в зависимости от параметров
                    if groupby_field == 'dept':
                        # У публикаций departments — это список, элемент может относиться к нескольким отделам
                        depts = item.get('departments', [])
                        if isinstance(depts, list):
                            for dept in depts:
                                grouped[dept].append(item)
                        else:
                            grouped[depts].append(item)

                    elif groupby_field == 'year':
                        key = item.get('year', None)
                        grouped[key].append(item)

                    elif groupby_field == 'quarter':
                        # Если квартала нет явно, можно извлечь из даты или использовать поле
                        quarter = item.get('quarter', None)
                        grouped[quarter].append(item)

                    elif groupby_field == 'type':
                        key = item.get('type', None)
                        grouped[key].append(item)

                    elif groupby_field == 'degree':
                        key = item.get('degree', None)
                        grouped[key].append(item)

                    else:
                        key = item.get(groupby_field, None)
                        grouped[key].append(item)

                data[collection_name] = dict(grouped)
                break

    def _resolve(self, kind: str, name: str) -> Resolution:
        index = self.persons if kind == "person" else self.departments
        return index.resolve(name)

    def _anchor_publications(self, anchor: Resolution) -> set[str]:
        if anchor.kind == "person":
            return set(templates.person_publication_ids(self.graph, anchor.best.id))
        units = templates.department_units(self.graph, anchor.best.id)
        return set(templates.department_publication_ids(self.graph, units))

    def _profile(self, anchor: Resolution, plan: QueryPlan):
        if anchor.kind == "person":
            data = templates.person_profile(self.graph, anchor.best.id)
        else:
            data = templates.department_profile(self.graph, anchor.best.id)
        pub_rel = {row["id"]: 1.0 for row in data["publications"]}
        repo_rel = {row["id"]: 1.0 for row in data["repositories"]}
        years = [row.get("year") for row in data["publications"]]
        in_range = [
            y for y in years
            if (plan.filter.year_from is None or (y and y >= plan.filter.year_from))
            and (plan.filter.year_to is None or (y and y <= plan.filter.year_to))
        ]
        counts = {
            "entity": anchor.best.label,
            "publications": len(in_range),
            "publications_total": len(years),
            "by_year": dict(sorted(Counter(y for y in in_range if y).items())),
            "repositories": len(data["repositories"]),
        }
        if anchor.kind == "person":
            counts["coauthors"] = len({row["person"] for row in data["authorship"]} - {anchor.best.id})
        else:
            counts["members"] = len(data.get("members") or [])
            counts["units"] = len(data.get("units") or [])
        return data, pub_rel, repo_rel, counts

    def _result(self, question, plan, options, resolutions, hits, counts, answer, notes, trace, timer):
        return {
            "question": question,
            "plan": plan,
            "options": {
                "retrieval_mode": options.retrieval_mode, "ranking": options.ranking,
                "llm_parse": options.llm_parse, "llm_answer": options.llm_answer, "top_n": options.top_n,
            },
            "entities": [
                {"query": r.query, "kind": r.kind, "ambiguous": r.ambiguous,
                 "candidates": [c.__dict__ for c in r.candidates[:5]]}
                for r in resolutions
            ],
            "entry_points": [
                {"id": h.id, "title": self.pub_titles.get(h.id), "score": round(h.score, 4),
                 "dense": None if h.dense is None else round(h.dense, 4),
                 "bm25": None if h.bm25 is None else round(h.bm25, 3)}
                for h in hits[:20]
            ],
            "answer": answer,
            "counts":counts,
            "notes": notes,
            "trace": trace,
            "timings": timer.marks,
        }

"""Валидатор разметки test_questions.csv (схема из labeling_instruction.md)"""
from __future__ import annotations

import argparse
import csv
import json
import re
import signal
import sys
import zipfile
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from pathlib import Path

# ---------------------------------------------------------------- схема

COLUMNS = ["id", "split", "section", "question", "type", "subtype",
           "type_alt", "params", "behavior", "tags", "note"]

TYPES = {str(i) for i in range(1, 11)} | {"none"}
SUBTYPES = {"3": {"3a", "3b", "3c", "3d"}}
SPLITS = {"dev", "test"}
BEHAVIORS = {"answer", "clarify", "refuse"}
TAGS = {"case_form", "abbrev", "two_entities", "years", "relative_period",
        "negation", "group_by", "ranking", "ambiguous_name", "no_data",
        "composite", "check_value", "needs_review"}

METRICS = {"list", "count", "exists", "ratio", "avg_per_year", "avg_per_person"}
METRICS_BY_TYPE = {
    "1": {"list", "count", "exists"},
    "2": {"list", "count", "exists", "ratio", "avg_per_year", "avg_per_person"},
    "3": {"list", "count", "exists"},
    "4": {"list", "count", "exists"},
}
for _t in "5 6 7 8 9 10".split():
    METRICS_BY_TYPE[_t] = {"list", "count", "exists"}

TARGETS_BY_TYPE = {
    "1": {"persons"},
    "2": {"publications"},
    "3": {"persons", "publications"},
    "4": {"persons", "repositories"},
    "5": {"persons"},
    "6": {"persons"},
    "7": {"publications"},
    "8": {"persons", "publications"},
    "9": {"publications", "repositories"},
    "10": {"persons", "publications"},
}

_PERIOD = {"year_from", "year_to", "last_n_years"}
ALLOWED_KEYS = {
    "1": {"target", "metric", "dept_id", "include_subunits", "filters",
          "group_by", "order", "limit", "no_dept"},
    "2": {"target", "metric", "dept_id", "include_subunits", "filters",
          "ratio_filters", "group_by", "order", "limit"} | _PERIOD,
    "3": {"target", "metric", "person_ids", "dept_ids","dept_id", "include_subunits",
          "coauthor_is_itmo", "order", "limit"} | _PERIOD,
    "4": {"target", "metric", "dept_id", "include_subunits", "person_ids",
          "filters", "group_by", "order", "limit", "is_itmo"},
    "5": {"target", "pattern", "dept_id", "include_subunits", "filters", "is_itmo"},
}
for _t in "6 7 8 9 10".split():
    ALLOWED_KEYS[_t] = {"target", "topic", "metric", "dept_id",
                        "include_subunits", "require_code", "link",
                        "repo_filters", "is_itmo"} | _PERIOD
REQUIRED_KEYS = {
    "1": {"target", "metric"}, "2": {"target", "metric"},
    "3": {"target", "metric"}, "4": {"target", "metric"},
    "5": {"target", "pattern"},
    "6": {"target", "topic"}, "7": {"target", "topic"},
    "8": {"target", "topic", "dept_id"}, "9": {"target", "topic", "require_code"},
    "10": {"target", "topic"},
}

GROUP_BY_BY_TYPE = {
    "1": {"dept", "degree"},
    "2": {"dept", "year", "quarter", "type"},
    "4": {"dept"},
}
PATTERNS = {"author_contributor", "author_not_contributor", "author_with_repo"}
LINKS = {"mentions", "implements"}

PERSON_FIELDS = {"degree", "github", "google_scholar", "linkedin", "orcid",
                 "homepage", "other_names", "is_itmo"}
PUB_FIELDS = {"type", "doi", "funding", "pdf_urls", "journal"}
REPO_FIELDS = {"license", "stars_num", "forks_num", "archived", "is_fork",
               "has_readme", "language", "last_updated", "contributors_count",
               "owner_type", "role"}
FIELDS_BY_TYPE = {"1": PERSON_FIELDS, "2": PUB_FIELDS, "4": REPO_FIELDS,
                  "5": REPO_FIELDS}
PRESENCE_ONLY = {"github", "google_scholar", "linkedin", "orcid", "homepage",
                 "other_names", "doi", "funding", "pdf_urls"}
NUMERIC_FIELDS = {"stars_num", "forks_num", "contributors_count"}
BOOL_FIELDS = {"archived", "is_fork", "has_readme", "is_itmo"}
OPS = {"eq", "ne", "gt", "gte", "lt", "lte", "in", "is_null", "not_null"}
NO_VALUE_OPS = {"is_null", "not_null"}
ORDERS = {"asc", "desc"}

DEGREE_CATEGORIES = {"candidate", "doctor"}
DEGREE_LITERAL = re.compile(r"^[кд]\.[а-яё.\-]+$", re.IGNORECASE)
PLACEHOLDER = re.compile(r"^<id\s+\S.*>$")
ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
ID_RE = re.compile(r"^(?:t(\d{1,2})-(\d{2,3})|c-(\d{2,3}))$")
STUB_RE = re.compile(r"(?<![A-Za-zА-Яа-яЁё])[XYZW](?![A-Za-zА-Яа-яЁё])")
TOPIC_NOISE = re.compile(r"\b(кто|какие|какой|где|есть ли|занимается|занимаются)\b",
                         re.IGNORECASE)


# ---------------------------------------------------------------- отчёт

class Report:
    def __init__(self) -> None:
        self.items: list[tuple[str, str, str, str]] = []

    def add(self, level: str, rid: str, where: str, msg: str) -> None:
        self.items.append((level, rid, where, msg))

    def error(self, rid, where, msg): self.add("ERROR", rid, where, msg)
    def warn(self, rid, where, msg): self.add("WARN", rid, where, msg)
    def info(self, rid, where, msg): self.add("INFO", rid, where, msg)

    def count(self, level: str) -> int:
        return sum(1 for it in self.items if it[0] == level)


def norm_text(s: str) -> str:
    s = s.lower().replace("ё", "е")
    s = re.sub(r"^\s*\d+[.)]\s*", "", s)
    s = re.sub(r"\s+", " ", s).strip(" \t.?!:;")
    return s


def find_nulls(obj, path="params"):
    if obj is None:
        yield path
    elif isinstance(obj, dict):
        for k, v in obj.items():
            yield from find_nulls(v, f"{path}.{k}")
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            yield from find_nulls(v, f"{path}[{i}]")


def is_int(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


# ---------------------------------------------------------------- params

def check_entity(rep, rid, key, value, resolved_required):
    def one(v, where):
        if not isinstance(v, str) or not v.strip():
            rep.error(rid, where, "идентификатор должен быть непустой строкой")
        elif PLACEHOLDER.match(v):
            if resolved_required:
                rep.error(rid, where, f"заглушка {v!r} не заменена на id")
        elif "<" in v or ">" in v:
            rep.error(rid, where, f"неверная заглушка {v!r}; формат: <id название>")

    if key == "dept_id":
        one(value, key)
    else:
        if not isinstance(value, list) or not value:
            rep.error(rid, key, "ожидается непустой список строк")
            return
        for i, v in enumerate(value):
            one(v, f"{key}[{i}]")
        if len({str(v) for v in value}) != len(value):
            rep.error(rid, key, "повторяющиеся значения в списке")


def check_degree_value(rep, rid, where, v):
    if not isinstance(v, str) or not (v in DEGREE_CATEGORIES
                                      or DEGREE_LITERAL.match(v)):
        rep.error(rid, where,
                  f"degree: {v!r} — нужна категория candidate/doctor "
                  "или значение из графа вида 'к.т.н.', 'д.ф.-м.н.'")


def check_filter_value(rep, rid, where, field, op, value):
    def scalar(v, w):
        if field == "degree":
            check_degree_value(rep, rid, w, v)
        elif field in NUMERIC_FIELDS:
            if isinstance(v, bool) or not isinstance(v, (int, float)):
                rep.error(rid, w, f"{field}: нужно число, получено {v!r}")
        elif field in BOOL_FIELDS:
            if not isinstance(v, bool):
                rep.error(rid, w, f"{field}: нужно true/false, получено {v!r}")
        elif field == "owner_type":
            if v not in {"user", "organization"}:
                rep.error(rid, w, "owner_type: user или organization")
        elif field == "last_updated":
            if isinstance(v, dict):
                if set(v) != {"years_ago"} or isinstance(v["years_ago"], bool) \
                        or not isinstance(v["years_ago"], (int, float)) \
                        or v["years_ago"] <= 0:
                    rep.error(rid, w, 'last_updated: {"years_ago": N>0} или дата YYYY-MM-DD')
            elif not (isinstance(v, str) and ISO_DATE.match(v)):
                rep.error(rid, w, 'last_updated: {"years_ago": N} или дата YYYY-MM-DD')
        else:
            if not isinstance(v, str) or not v.strip():
                rep.error(rid, w, f"{field}: нужна непустая строка")

    if op in NO_VALUE_OPS:
        return
    if op == "in":
        if not isinstance(value, list) or not value:
            rep.error(rid, where, "op=in требует непустой список value")
            return
        for i, v in enumerate(value):
            scalar(v, f"{where}.value[{i}]")
    else:
        if isinstance(value, list):
            rep.error(rid, where, f"op={op} требует одно значение, не список")
            return
        scalar(value, f"{where}.value")


def check_filters(rep, rid, key, filters, allowed_fields, ttype):
    if not isinstance(filters, list) or not filters:
        rep.error(rid, key, "ожидается непустой список условий")
        return
    for i, f in enumerate(filters):
        where = f"{key}[{i}]"
        if not isinstance(f, dict):
            rep.error(rid, where, "условие должно быть объектом")
            continue
        extra = set(f) - {"field", "op", "value"}
        if extra:
            rep.error(rid, where, f"лишние ключи: {sorted(extra)}")
        field, op = f.get("field"), f.get("op")
        if field not in allowed_fields:
            rep.error(rid, where,
                      f"поле {field!r} недопустимо для типа {ttype}; "
                      f"разрешены: {sorted(allowed_fields)}")
            continue
        if op not in OPS:
            rep.error(rid, where, f"op {op!r} не из {sorted(OPS)}")
            continue
        if field in PRESENCE_ONLY and op not in NO_VALUE_OPS:
            rep.error(rid, where, f"для {field} допустимы только is_null/not_null")
        if field in BOOL_FIELDS and op not in {"eq", "ne"} | NO_VALUE_OPS:
            rep.error(rid, where, f"для булева поля {field} допустимы eq/ne")
        if field == "last_updated" and op in {"eq", "ne", "in"}:
            rep.error(rid, where, "для last_updated допустимы lt/lte/gt/gte")
        if op in NO_VALUE_OPS and "value" in f:
            rep.error(rid, where, f"op={op} не должен содержать value")
        if op not in NO_VALUE_OPS and "value" not in f:
            rep.error(rid, where, f"op={op} требует value")
            continue
        check_filter_value(rep, rid, where, field, op, f.get("value"))


def check_params(rep, rid, ttype, subtype, params, resolved_required):
    if ttype == "none":
        if params != {}:
            rep.error(rid, "params", "для type=none params должен быть {}")
        return

    allowed = ALLOWED_KEYS[ttype]
    for k in sorted(set(params) - allowed):
        rep.error(rid, "params", f"ключ {k!r} недопустим для типа {ttype}")
    for k in sorted(REQUIRED_KEYS[ttype] - set(params)):
        rep.error(rid, "params", f"отсутствует обязательный ключ {k!r}")

    target = params.get("target")
    if target is not None and target not in TARGETS_BY_TYPE[ttype]:
        rep.error(rid, "target",
                  f"{target!r}; для типа {ttype} разрешены "
                  f"{sorted(TARGETS_BY_TYPE[ttype])}")

    metric = params.get("metric")
    if metric is not None and metric not in METRICS_BY_TYPE[ttype]:
        rep.error(rid, "metric",
                  f"{metric!r}; для типа {ttype} разрешены "
                  f"{sorted(METRICS_BY_TYPE[ttype])}")
    if metric == "ratio" and "ratio_filters" not in params:
        rep.error(rid, "metric", "ratio требует ratio_filters")
    if "ratio_filters" in params and metric != "ratio":
        rep.error(rid, "ratio_filters", "ratio_filters допустим только при metric=ratio")

    for key in ("dept_id", "dept_ids", "person_ids"):
        if key in params:
            check_entity(rep, rid, key, params[key], resolved_required)
    if "dept_id" in params and "dept_ids" in params:
        rep.error(rid, "params", "dept_id и dept_ids вместе не используются")

    if params.get("include_subunits") is True:
        rep.warn(rid, "include_subunits", "true — значение по умолчанию, ключ лишний")
    elif "include_subunits" in params and params["include_subunits"] is not False:
        rep.error(rid, "include_subunits", "допустимо только false")

    # периоды
    for k in ("year_from", "year_to"):
        if k in params and (not is_int(params[k]) or not 1900 <= params[k] <= 2100):
            rep.error(rid, k, f"нужен год (целое число), получено {params[k]!r}")
    if "last_n_years" in params:
        v = params["last_n_years"]
        if not is_int(v) or not 1 <= v <= 50:
            rep.error(rid, "last_n_years", f"нужно целое 1..50, получено {v!r}")
        if {"year_from", "year_to"} & set(params):
            rep.error(rid, "params", "last_n_years нельзя сочетать с year_from/year_to")
    if is_int(params.get("year_from")) and is_int(params.get("year_to")) \
            and params["year_from"] > params["year_to"]:
        rep.error(rid, "params", "year_from больше year_to")

    # order / limit / group_by
    if "order" in params and params["order"] not in ORDERS:
        rep.error(rid, "order", "asc или desc")
    if "limit" in params and (not is_int(params["limit"]) or params["limit"] < 1):
        rep.error(rid, "limit", "целое число >= 1")
    if "limit" in params and "order" not in params:
        rep.warn(rid, "limit", "limit без order: по какому порядку берётся топ?")
    if "group_by" in params:
        allowed_gb = GROUP_BY_BY_TYPE.get(ttype, set())
        if params["group_by"] not in allowed_gb:
            rep.error(rid, "group_by",
                      f"{params['group_by']!r}; для типа {ttype} разрешены "
                      f"{sorted(allowed_gb)}")
        if metric == "list":
            rep.warn(rid, "group_by", "group_by вместе с metric=list")

    # фильтры
    if "filters" in params:
        fields = FIELDS_BY_TYPE.get(ttype)
        if fields is None:
            rep.error(rid, "filters", f"в типе {ttype} filters не используются")
        else:
            check_filters(rep, rid, "filters", params["filters"], fields, ttype)
    if "ratio_filters" in params and ttype == "2":
        check_filters(rep, rid, "ratio_filters", params["ratio_filters"],
                      PUB_FIELDS, ttype)
    if "repo_filters" in params:
        check_filters(rep, rid, "repo_filters", params["repo_filters"],
                      REPO_FIELDS, ttype)
        if params.get("require_code") is not True:
            rep.error(rid, "repo_filters", "repo_filters допустим только при require_code=true")

    # тип 1
    if "no_dept" in params:
        if params["no_dept"] is not True:
            rep.error(rid, "no_dept", "допустимо только true")
        if {"dept_id", "dept_ids"} & set(params):
            rep.error(rid, "no_dept", "no_dept несовместим с dept_id")

    # тип 3
    if ttype == "3":
        pc = len(params.get("person_ids", []) or [])
        has_dept = "dept_id" in params
        has_partner = "partner_dept_id" in params
        rules = {
            "3a": (2 <= pc <= 3 and not has_dept and not has_partner,
                   "3a: person_ids из 2–3 людей, без dept_id и partner_dept_id"),
            "3b": (pc == 1 and not has_dept and not has_partner,
                   "3b: ровно один person_ids, без dept_id и partner_dept_id"),
            "3c": (pc == 0 and has_dept and has_partner,
                   "3c: dept_id и partner_dept_id, без person_ids"),
            "3d": (pc == 1 and not has_dept and has_partner,
                   "3d: один person_ids и partner_dept_id, без dept_id"),
            "3e": (pc == 0 and not has_partner,
                   "3e: без person_ids и partner_dept_id; dept_id необязателен "
                   "(нет — значит весь ИТМО)"),
        }
        if subtype in rules and not rules[subtype][0]:
            rep.error(rid, "subtype",
                      f"параметры не соответствуют подтипу — {rules[subtype][1]}")
        if subtype == "3e" and params.get("target") not in {None, "persons"}:
            rep.error(rid, "target", "для 3e target должен быть persons")
        if "coauthor_is_itmo" in params and not isinstance(params["coauthor_is_itmo"], bool):
            rep.error(rid, "coauthor_is_itmo", "нужно true/false")
        if "rank_by" in params:
            if params["rank_by"] not in {"distinct_coauthors", "joint_publications"}:
                rep.error(rid, "rank_by",
                          "distinct_coauthors или joint_publications")
            if "order" not in params:
                rep.error(rid, "rank_by", "rank_by без order")

    # тип 5
    if ttype == "5" and params.get("pattern") not in PATTERNS | {None}:
        rep.error(rid, "pattern", f"{params['pattern']!r}; разрешены {sorted(PATTERNS)}")

    # типы 6–10
    if ttype in {"6", "7", "8", "9", "10"}:
        topic = params.get("topic")
        if topic is not None:
            if not isinstance(topic, str) or not topic.strip():
                rep.error(rid, "topic", "нужна непустая строка")
            elif TOPIC_NOISE.search(topic):
                rep.warn(rid, "topic", f"в теме остались служебные слова: {topic!r}")
        if "require_code" in params and params["require_code"] is not True:
            rep.error(rid, "require_code", "допустимо только true")
        if "link" in params:
            if params["link"] not in LINKS:
                rep.error(rid, "link", f"{params['link']!r}; разрешены {sorted(LINKS)}")
            if params.get("require_code") is not True:
                rep.error(rid, "link", "link допустим только при require_code=true")
        if ttype == "10" and not (_PERIOD & set(params)):
            rep.error(rid, "params", "для типа 10 нужен период "
                                     "(year_from/year_to/last_n_years)")


# ---------------------------------------------------------------- docx

def docx_paragraphs(path: Path) -> list[str]:
    w = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    with zipfile.ZipFile(path) as z:
        root = ET.fromstring(z.read("word/document.xml"))
    out = []
    for p in root.iter(f"{{{w}}}p"):
        text = "".join(t.text or "" for t in p.iter(f"{{{w}}}t")).strip()
        if text:
            out.append(text)
    return out


# ---------------------------------------------------------------- главная

def validate(csv_path: Path, docx_path: Path | None, resolved_required: bool):
    rep = Report()
    rows: list[dict] = []

    with csv_path.open(newline="", encoding="utf-8-sig") as fh:
        reader = csv.reader(fh)
        try:
            header = next(reader)
        except StopIteration:
            rep.error("-", "file", "файл пуст")
            return rep, rows
        if header != COLUMNS:
            rep.error("-", "header",
                      f"заголовок должен быть: {','.join(COLUMNS)}; получен: {','.join(header)}")
            return rep, rows
        for rec in reader:
            if not rec or all(not c.strip() for c in rec):
                continue
            line = reader.line_num
            rid_guess = rec[0].strip() if rec else f"line{line}"
            if len(rec) != len(COLUMNS):
                rep.error(rid_guess, f"line {line}",
                          f"{len(rec)} полей вместо {len(COLUMNS)} "
                          "(пропущена или лишняя запятая; кавычки внутри JSON удваиваются)")
                continue
            row = dict(zip(COLUMNS, (c.strip() for c in rec)))
            row["_line"] = line
            rows.append(row)

    seen_ids: dict[str, int] = {}
    seen_q: dict[str, str] = {}

    for row in rows:
        rid = row["id"] or f"line{row['_line']}"
        ttype, subtype = row["type"], row["subtype"]

        # id
        m = ID_RE.match(row["id"])
        if not m:
            rep.error(rid, "id", "формат t{раздел}-{NN} или c-{NN}")
        else:
            if m.group(1) is not None and row["section"] != str(int(m.group(1))):
                rep.error(rid, "section", f"section={row['section']!r} не совпадает с id")
            if m.group(3) is not None and row["section"] != "0":
                rep.error(rid, "section", "для c-NN section должен быть 0")
        if row["id"] in seen_ids:
            rep.error(rid, "id", f"дубликат id (строка {seen_ids[row['id']]})")
        seen_ids[row["id"]] = row["_line"]

        # простые колонки
        if row["split"] not in SPLITS:
            rep.error(rid, "split", f"{row['split']!r}; нужно dev или test")
        if not (row["section"].isdigit() and 0 <= int(row["section"]) <= 10):
            rep.error(rid, "section", "число 0..10")
        if row["behavior"] not in BEHAVIORS:
            rep.error(rid, "behavior", f"{row['behavior']!r}; нужно {sorted(BEHAVIORS)}")
        if ttype not in TYPES:
            rep.error(rid, "type", f"{ttype!r}; нужно 1..10 или none")

        # вопрос
        q = row["question"]
        if not q:
            rep.error(rid, "question", "пустой вопрос")
        else:
            nq = norm_text(q)
            if nq in seen_q:
                rep.error(rid, "question", f"дубликат вопроса из {seen_q[nq]}")
            seen_q[nq] = rid
            if STUB_RE.search(q):
                rep.warn(rid, "question", "похоже на заглушку (X/Y/Z/W) вместо реального названия")

        # subtype / type_alt
        if ttype in SUBTYPES:
            if subtype not in SUBTYPES[ttype]:
                rep.error(rid, "subtype", f"для типа {ttype} нужен подтип из {sorted(SUBTYPES[ttype])}")
        elif subtype:
            rep.error(rid, "subtype", f"для типа {ttype} подтипа быть не должно")
        alts = [a.strip() for a in row["type_alt"].split(";") if a.strip()]
        for a in alts:
            if a not in TYPES:
                rep.error(rid, "type_alt", f"{a!r} — неизвестный тип")
        if ttype in alts:
            rep.error(rid, "type_alt", "совпадает с type")

        # теги
        tags = [t.strip() for t in row["tags"].split(";") if t.strip()]
        for t in tags:
            if t not in TAGS:
                rep.error(rid, "tags", f"неизвестный тег {t!r}")
        if len(set(tags)) != len(tags):
            rep.warn(rid, "tags", "повторяющиеся теги")
        if "needs_review" in tags:
            rep.info(rid, "tags", "needs_review: " + (row["note"] or "без заметки"))

        # behavior ↔ type/tags
        if row["behavior"] == "refuse" and ttype != "none":
            rep.error(rid, "behavior", "refuse допустим только при type=none")
        if row["behavior"] == "refuse" and "no_data" not in tags:
            rep.warn(rid, "tags", "behavior=refuse, но нет тега no_data")
        if "no_data" in tags and row["behavior"] != "refuse":
            rep.warn(rid, "tags", "тег no_data при behavior != refuse: данные в графе есть?")

        # note
        if ttype == "none" and not row["note"]:
            rep.warn(rid, "note", "для type=none нужна заметка: почему шаблон не подходит")
        if ttype in TYPES and ttype != "none" and row["section"].isdigit() \
                and row["section"] not in {ttype} and not row["note"]:
            rep.warn(rid, "note", f"type={ttype} отличается от section={row['section']}: объясни в note")

        # params
        raw = row["params"]
        try:
            params = json.loads(raw)
        except json.JSONDecodeError as e:
            rep.error(rid, "params", f"невалидный JSON: {e}")
            continue
        if not isinstance(params, dict):
            rep.error(rid, "params", "params должен быть JSON-объектом")
            continue
        for p in find_nulls(params):
            rep.error(rid, p, "null не допускается: отсутствующий ключ просто не пишется")
        row["_params"], row["_tags"] = params, tags
        if ttype not in TYPES:
            continue
        check_params(rep, rid, ttype, subtype, params, resolved_required)

        # согласованность тегов и params
        if ttype != "none":
            has_period = bool(_PERIOD & set(params))
            if "years" in tags and not ({"year_from", "year_to"} & set(params)):
                rep.warn(rid, "tags", "тег years, но нет year_from/year_to")
            if "relative_period" in tags and "last_n_years" not in params:
                rep.warn(rid, "tags", "тег relative_period, но нет last_n_years")
            if "last_n_years" in params and "relative_period" not in tags:
                rep.warn(rid, "tags", "есть last_n_years, но нет тега relative_period")
            if has_period and "years" not in tags and "relative_period" not in tags:
                rep.warn(rid, "tags", "в params период, но нет тегов years/relative_period")
            if "group_by" in tags and "group_by" not in params:
                rep.warn(rid, "tags", "тег group_by, но в params нет group_by")
            if "ranking" in tags and "order" not in params:
                rep.warn(rid, "tags", "тег ranking, но в params нет order")
            if "two_entities" in tags:
                n = len(params.get("person_ids", [])) + len(params.get("dept_ids", []))
                if n < 2:
                    rep.warn(rid, "tags", "тег two_entities, но в params меньше двух сущностей")

    # ---- сплит
    by_split = Counter(r["split"] for r in rows)
    total = len(rows)
    if total:
        share = by_split.get("test", 0) / total
        if not 0.15 <= share <= 0.35:
            rep.warn("-", "split", f"доля test = {share:.0%} (ожидается 15–35%)")
    by_type_split: dict[str, Counter] = defaultdict(Counter)
    for r in rows:
        by_type_split[r["type"]][r["split"]] += 1
    for t, c in sorted(by_type_split.items()):
        if t != "none" and total >= 40 and not (c.get("dev") and c.get("test")):
            rep.warn("-", "split", f"в типе {t} нет строк одного из сплитов: {dict(c)}")

    # пары 6/7 по теме должны быть в одном сплите
    topics: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        if r["type"] in {"6", "7"} and isinstance(r.get("_params"), dict) \
                and isinstance(r["_params"].get("topic"), str):
            topics[norm_text(r["_params"]["topic"])].append(r)
    for tp, rs in topics.items():
        if len({x["split"] for x in rs}) > 1:
            ids = ", ".join(x["id"] for x in rs)
            rep.warn("-", "split", f"тема {tp!r} разнесена по сплитам ({ids}): пары 6/7 держим вместе")

    # ---- сверка с docx
    if docx_path is not None:
        paras = docx_paragraphs(docx_path)
        pnorm = {norm_text(p) for p in paras}
        for r in rows:
            if r["question"] and norm_text(r["question"]) not in pnorm:
                rep.error(r["id"], "question", "текст вопроса не найден в docx дословно")
        csv_norm = {norm_text(r["question"]) for r in rows}
        missing = []
        for p in paras:
            if p.rstrip().endswith("?") and norm_text(p) not in csv_norm \
                    and norm_text(p) not in {norm_text(m) for m in missing}:
                missing.append(p)
        for p in missing:
            rep.warn("-", "docx", f"вопрос из docx без разметки: {p[:110]}")

    return rep, rows


def print_report(rep: Report, rows: list[dict], verbose_info: bool) -> None:
    order = {"ERROR": 0, "WARN": 1, "INFO": 2}
    for level, rid, where, msg in sorted(rep.items, key=lambda x: order[x[0]]):
        if level == "INFO" and not verbose_info:
            continue
        print(f"{level:5} {rid:8} {where}: {msg}")
    print()
    print(f"Строк: {len(rows)}")
    c = Counter((r["type"], r["split"]) for r in rows)
    types = sorted({t for t, _ in c}, key=lambda x: (x == "none", int(x) if x.isdigit() else 99))
    print("тип   dev  test")
    for t in types:
        print(f"{t:5} {c.get((t, 'dev'), 0):4} {c.get((t, 'test'), 0):5}")
    print(f"\nОшибок: {rep.count('ERROR')}, предупреждений: {rep.count('WARN')}, "
          f"needs_review: {rep.count('INFO')}")


def main() -> int:
    if hasattr(signal, "SIGPIPE"):  # `| head` не должен ронять скрипт
        signal.signal(signal.SIGPIPE, signal.SIG_DFL)
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("csv", type=Path, help="test_questions.csv")
    ap.add_argument("--docx", type=Path, help="docx с вопросами для сверки текстов")
    ap.add_argument("--require-resolved", action="store_true",
                    help="считать ошибкой неразрешённые заглушки <id ...>")
    ap.add_argument("--strict", action="store_true",
                    help="предупреждения тоже дают ненулевой код возврата")
    ap.add_argument("--info", action="store_true", help="показать INFO (needs_review)")
    args = ap.parse_args()

    rep, rows = validate(args.csv, args.docx, args.require_resolved)
    print_report(rep, rows, args.info)
    if rep.count("ERROR") or (args.strict and rep.count("WARN")):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Graph integrity check definitions.

All queries are deliberately cheap: counts, IS NULL / empty-string checks,
group-by duplicates, and a few regexes. No LLM, no morphology, no graph
algorithms.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Check:
    """One graph integrity check.

    Attributes:
        id: Stable identifier, used by the frontend in /api/check.
        group: UI group heading (Russian).
        title: Check heading (Russian).
        title_en: Check heading (English).
        count: Cypher returning a single number.
        warn: Threshold above which the check is "warn".
        fail: Threshold above which the check is "fail".
        of: Cypher for the denominator, or None — then warn/fail compare
            against the raw count instead of a share.
        hint: User-facing explanation (Russian), or None.
        hint_en: User-facing explanation (English), or None.
        examples: Cypher returning example rows, taking a $lim parameter,
            or None if this check has no drill-down. Column aliases stay
            Russian — the examples table/CSV isn't translated yet.
    """

    id: str
    group: str
    title: str
    title_en: str
    count: str
    warn: float
    fail: float
    of: str | None = None
    hint: str | None = None
    hint_en: str | None = None
    examples: str | None = None


# UI group heading, Russian -> English.
GROUP_EN = {
    "Пропуски": "Gaps",
    "Имена": "Names",
    "Дубликаты": "Duplicates",
    "Противоречия": "Contradictions",
}

CYR, LAT = r"\\p{IsCyrillic}", r"\\p{IsLatin}"
RU_NAME_FIELDS = "[p.surname_ru, p.first_name_ru, p.second_name_ru]"

# is_itmo:Itmo/External label migration - #150.
_ITMO_TOTAL = "MATCH (p:Person:Itmo) RETURN count(p)"
_PUB_TOTAL = "MATCH (p:Publication) RETURN count(p)"
_DEPT_TOTAL = "MATCH (d:Department) RETURN count(d)"

_FIO = (
    "trim(coalesce(p.surname_ru,'') + ' ' + coalesce(p.first_name_ru,'') "
    "+ ' ' + coalesce(p.second_name_ru,''))"
)

_PUB_YEAR = "toInteger(left(toString(p.publication_date), 4))"

CHECKS = [
    # ---------------- gaps ----------------
    Check(
        id="itmo_no_dept",
        group="Пропуски",
        title="Сотрудники без департамента",
        title_en="Staff without a department",
        count="MATCH (p:Person:Itmo) WHERE NOT (p)-[:BELONGS_TO]->() RETURN count(p)",
        of=_ITMO_TOTAL,
        warn=0.05,
        fail=0.25,
        hint="Итоги по департаментам занижены.",
        hint_en="Department totals are undercounted.",
        examples=f"""MATCH (p:Person:Itmo) WHERE NOT (p)-[:BELONGS_TO]->()
            OPTIONAL MATCH (p)-[:AUTHORED]->(pub:Publication)
            WITH p, count(pub) AS pubs, max(pub.year) AS last_year
            RETURN p.id AS id, p.name_raw AS `Имя (лат.)`, {_FIO} AS `ФИО`,
                   pubs AS `Публикаций`, last_year AS `Последняя публикация`
            ORDER BY pubs DESC LIMIT $lim""",
    ),
    Check(
        id="pub_no_authors",
        group="Пропуски",
        title="Публикации без единого автора",
        title_en="Publications with no author at all",
        count="MATCH (p:Publication) WHERE NOT (p)<-[:AUTHORED]-() RETURN count(p)",
        of=_PUB_TOTAL,
        warn=0.001,
        fail=0.01,
        hint="Ни с кем не связаны и не видны на карте.",
        hint_en="Not linked to anyone and invisible on the map.",
        examples="""MATCH (p:Publication) WHERE NOT (p)<-[:AUTHORED]-()
            RETURN p.id AS id, p.title AS `Заголовок`, p.year AS `Год`,
                   p.doi AS `DOI`, p.journal AS `Журнал`
            ORDER BY p.year DESC LIMIT $lim""",
    ),
    Check(
        id="pub_no_itmo_author",
        group="Пропуски",
        title="Публикации без автора из ИТМО",
        title_en="Publications with no ITMO author",
        count="MATCH (p:Publication) WHERE NOT (p)<-[:AUTHORED]-(:Person:Itmo) RETURN count(p)",
        of=_PUB_TOTAL,
        warn=0.01,
        fail=0.05,
        hint="Не попадают на карту.",
        hint_en="Don't make it onto the map.",
        examples="""MATCH (p:Publication) WHERE NOT (p)<-[:AUTHORED]-(:Person:Itmo)
            OPTIONAL MATCH (p)<-[:AUTHORED]-(a:Person)
            WITH p, count(a) AS authors
            RETURN p.id AS id, p.title AS `Заголовок`, p.year AS `Год`,
                   authors AS `Всего авторов`, p.doi AS `DOI`
            ORDER BY p.year DESC LIMIT $lim""",
    ),
    Check(
        id="pub_no_abstract",
        group="Пропуски",
        title="Публикации без аннотации",
        title_en="Publications without an abstract",
        count="MATCH (p:Publication) WHERE p.abstract IS NULL OR p.abstract = '' RETURN count(p)",
        of=_PUB_TOTAL,
        warn=0.05,
        fail=0.15,
        hint="По аннотациям ищутся ссылки на код — часть репозиториев не находится.",
        hint_en="Code links are found by searching abstracts — some repositories go undiscovered.",
        examples="""MATCH (p:Publication) WHERE p.abstract IS NULL OR p.abstract = ''
            RETURN p.id AS id, p.title AS `Заголовок`, p.year AS `Год`,
                   p.doi AS `DOI`, p.pdf_url AS `PDF`
            ORDER BY p.year DESC LIMIT $lim""",
    ),
    Check(
        id="itmo_no_ru_name",
        group="Пропуски",
        title="Сотрудники без русского ФИО",
        title_en="Staff without a Russian full name",
        count="MATCH (p:Person:Itmo) WHERE p.name_ru IS NULL "
        "OR trim(p.name_ru) = '' RETURN count(p)",
        of=_ITMO_TOTAL,
        warn=0.02,
        fail=0.10,
        hint="Показываются латиницей. Этап author_names не запускался "
        "для группы, из которой пришли эти люди.",
        hint_en="Shown in Latin script. The author_names stage hasn't run "
        "for the batch these people came from.",
        examples="""MATCH (p:Person:Itmo)
            WHERE p.name_ru IS NULL OR trim(p.name_ru) = ''
            OPTIONAL MATCH (p)-[:AUTHORED]->(pub:Publication)
            WITH p, count(pub) AS pubs
            RETURN p.id AS id, p.name_raw AS `Имя (лат.)`,
                   coalesce(p.surname_ru,'—') AS `Фамилия (рус.)`, pubs AS `Публикаций`
            ORDER BY pubs DESC LIMIT $lim""",
    ),
    Check(
        id="itmo_no_patronymic",
        group="Пропуски",
        title="Сотрудники без отчества",
        title_en="Staff without a patronymic",
        count="MATCH (p:Person:Itmo) WHERE p.second_name_ru IS NULL "
        "OR trim(p.second_name_ru) = '' RETURN count(p)",
        of=_ITMO_TOTAL,
        warn=0.25,
        fail=0.60,
        hint="Подписываются как «Фамилия Имя» — сокращать до «Фамилия И.О.» нечем. "
        "Отчество приходит только из справочника сотрудников.",
        hint_en='Signed as "Surname Given name" — nothing to shorten to '
        '"Surname G.P." with. The patronymic comes only from the staff directory.',
        examples="""MATCH (p:Person:Itmo)
            WHERE p.second_name_ru IS NULL OR trim(p.second_name_ru) = ''
            OPTIONAL MATCH (p)-[:AUTHORED]->(pub:Publication)
            WITH p, count(pub) AS pubs
            RETURN p.id AS id, p.name_raw AS `Имя (лат.)`,
                   coalesce(p.name_ru, '—') AS `ФИО (рус.)`,
                   coalesce(p.surname_ru,'—') AS `Фамилия`, pubs AS `Публикаций`
            ORDER BY pubs DESC LIMIT $lim""",
    ),
    Check(
        id="dept_no_ru_name",
        group="Пропуски",
        title="Департаменты без русского названия",
        title_en="Departments without a Russian name",
        count="MATCH (d:Department) WHERE d.name_ru IS NULL OR trim(d.name_ru) = '' "
        "RETURN count(d)",
        of=_DEPT_TOTAL,
        warn=0.05,
        fail=0.20,
        hint="Подписи получаются на смеси языков.",
        hint_en="Labels end up mixing languages.",
        examples="""MATCH (d:Department)
            WHERE d.name_ru IS NULL OR trim(d.name_ru) = ''
            OPTIONAL MATCH (d)<-[:BELONGS_TO]-(p:Person:Itmo)
            WITH d, count(p) AS people
            RETURN d.id AS id, d.name_en AS `Название (лат.)`, people AS `Сотрудников`
            ORDER BY people DESC LIMIT $lim""",
    ),
    Check(
        id="pub_no_corresponding",
        group="Пропуски",
        title="Публикации без контактного автора",
        title_en="Publications without a corresponding author",
        count="""MATCH (p:Publication) WHERE NOT EXISTS {
                      (p)<-[a:AUTHORED]-() WHERE a.is_corresponding } RETURN count(p)""",
        of=_PUB_TOTAL,
        warn=0.10,
        fail=0.30,
        hint=None,
        examples="""MATCH (p:Publication) WHERE NOT EXISTS {
              (p)<-[a:AUTHORED]-() WHERE a.is_corresponding }
            OPTIONAL MATCH (p)<-[:AUTHORED]-(a:Person)
            WITH p, count(a) AS authors
            RETURN p.id AS id, p.title AS `Заголовок`, p.year AS `Год`,
                   authors AS `Авторов`
            ORDER BY p.year DESC LIMIT $lim""",
    ),
    Check(
        id="authored_no_affiliation",
        group="Пропуски",
        title="Авторства без указания места работы",
        title_en="Authorships without an affiliation",
        count="MATCH ()-[a:AUTHORED]->() WHERE a.affiliation IS NULL "
        "OR a.affiliation = '' RETURN count(a)",
        of="MATCH ()-[a:AUTHORED]->() RETURN count(a)",
        warn=0.01,
        fail=0.05,
        hint="Такого автора нельзя отнести ни к ИТМО, ни к внешним.",
        hint_en="Such an author can't be classified as ITMO or external.",
        examples="""MATCH (p:Person)-[a:AUTHORED]->(pub:Publication)
            WHERE a.affiliation IS NULL OR a.affiliation = ''
            RETURN p.id AS id, p.name_raw AS `Автор`, pub.id AS `id публикации`,
                   pub.title AS `Публикация`, pub.year AS `Год`
            ORDER BY pub.year DESC LIMIT $lim""",
    ),
    Check(
        id="dept_no_variants",
        group="Пропуски",
        title="Департаменты без вариантов написания",
        title_en="Departments without name variants",
        count="MATCH (d:Department) WHERE size(coalesce(d.name_variants, [])) = 0 RETURN count(d)",
        of=_DEPT_TOTAL,
        warn=0.10,
        fail=0.40,
        hint="По ним департамент опознаётся в тексте статьи.",
        hint_en="These are how a department gets recognized in a paper's text.",
        examples="""MATCH (d:Department) WHERE size(coalesce(d.name_variants, [])) = 0
            OPTIONAL MATCH (d)<-[:BELONGS_TO]-(p:Person:Itmo)
            WITH d, count(p) AS people
            RETURN d.id AS id, coalesce(d.name_ru, d.name_en) AS `Департамент`,
                   people AS `Сотрудников`
            ORDER BY people DESC LIMIT $lim""",
    ),
    # ---------------- names ----------------
    Check(
        id="name_mixed_script",
        group="Имена",
        title="Кириллица и латиница внутри одного слова",
        title_en="Cyrillic and Latin mixed within one word",
        count=f"""MATCH (p:Person:Itmo) WHERE any(v IN {RU_NAME_FIELDS}
                       WHERE v IS NOT NULL AND v =~ '.*({CYR}{LAT}|{LAT}{CYR}).*')
                     RETURN count(p)""",
        of=_ITMO_TOTAL,
        warn=1e-9,
        fail=0.005,
        hint="Сбой транслитерации: «Вершиinin», «Полевaя», «Аkhмеров».",
        hint_en='Transliteration glitch: "Вершиinin", "Полевaя", "Аkhмеров".',
        examples=f"""MATCH (p:Person:Itmo) WHERE any(v IN {RU_NAME_FIELDS}
              WHERE v IS NOT NULL AND v =~ '.*({CYR}{LAT}|{LAT}{CYR}).*')
            RETURN p.id AS id, {_FIO} AS `ФИО (рус.)`, p.name_raw AS `Имя (лат.)`,
                   coalesce(p.surname_ru,'') AS `Фамилия`,
                   coalesce(p.first_name_ru,'') AS `Имя`,
                   coalesce(p.second_name_ru,'') AS `Отчество`
            ORDER BY p.name_raw LIMIT $lim""",
    ),
    Check(
        id="name_accent",
        group="Имена",
        title="Знак ударения внутри имени",
        title_en="Stress mark inside a name",
        count=f"""MATCH (p:Person:Itmo) WHERE any(v IN {RU_NAME_FIELDS}
                       WHERE v IS NOT NULL AND v =~ '.*[\\\\u0300-\\\\u036F].*')
                     RETURN count(p)""",
        of=_ITMO_TOTAL,
        warn=1e-9,
        fail=0.005,
        hint="«Смоля́нская» — поиск по такому имени не найдёт человека.",
        hint_en='"Смоля́нская" — searching for this name won\'t find the person.',
        examples=f"""MATCH (p:Person:Itmo) WHERE any(v IN {RU_NAME_FIELDS}
              WHERE v IS NOT NULL AND v =~ '.*[\\\\u0300-\\\\u036F].*')
            RETURN p.id AS id, {_FIO} AS `ФИО (рус.)`, p.name_raw AS `Имя (лат.)`
            ORDER BY p.name_raw LIMIT $lim""",
    ),
    Check(
        id="surname_one_letter",
        group="Имена",
        title="Фамилия из одной буквы",
        title_en="One-letter surname",
        count="""MATCH (p:Person:Itmo) WHERE p.surname_ru IS NOT NULL
                    AND size(trim(replace(p.surname_ru, '.', ''))) = 1 RETURN count(p)""",
        of=_ITMO_TOTAL,
        warn=1e-9,
        fail=0.005,
        hint="Короткие фамилии схлопнулись до инициала.",
        hint_en="Short surnames collapsed down to an initial.",
        examples="""MATCH (p:Person:Itmo) WHERE p.surname_ru IS NOT NULL
              AND size(trim(replace(p.surname_ru, '.', ''))) = 1
            RETURN p.id AS id, p.surname_ru AS `Фамилия (рус.)`,
                   p.name_raw AS `Имя (лат.)`
            ORDER BY p.name_raw LIMIT $lim""",
    ),
    Check(
        id="name_latin_only",
        group="Имена",
        title="Русское ФИО записано латиницей",
        title_en="Russian name written in Latin script",
        count=f"""MATCH (p:Person:Itmo)
                     WHERE p.surname_ru IS NOT NULL AND trim(p.surname_ru) <> ''
                       AND p.surname_ru =~ '[{LAT}\\\\s.-]+'
                     RETURN count(p)""",
        of=_ITMO_TOTAL,
        warn=1e-9,
        fail=0.005,
        hint="Транслитерация не отработала.",
        hint_en="Transliteration didn't run.",
        examples=f"""MATCH (p:Person:Itmo)
              WHERE p.surname_ru IS NOT NULL AND trim(p.surname_ru) <> ''
                AND p.surname_ru =~ '[{LAT}\\\\s.-]+'
            RETURN p.id AS id, {_FIO} AS `ФИО (рус.)`, p.name_raw AS `Имя (лат.)`
            ORDER BY p.name_raw LIMIT $lim""",
    ),
    Check(
        id="first_name_initials",
        group="Имена",
        title="Вместо имени только инициалы",
        title_en="Initials instead of a given name",
        count="""MATCH (p:Person:Itmo) WHERE p.first_name_ru IS NOT NULL
                    AND trim(p.first_name_ru) <> ''
                    AND size(trim(replace(replace(p.first_name_ru,'.',''),' ',''))) <= 2
                    RETURN count(p)""",
        of=_ITMO_TOTAL,
        warn=0.05,
        fail=0.15,
        hint="В источнике не было полного имени.",
        hint_en="The source didn't have a full name.",
        examples="""MATCH (p:Person:Itmo) WHERE p.first_name_ru IS NOT NULL
              AND trim(p.first_name_ru) <> ''
              AND size(trim(replace(replace(p.first_name_ru,'.',''),' ',''))) <= 2
            RETURN p.id AS id, coalesce(p.surname_ru,'') AS `Фамилия`,
                   p.first_name_ru AS `Имя`, p.name_raw AS `Имя (лат.)`
            ORDER BY p.surname_ru LIMIT $lim""",
    ),
    # ---------------- duplicates ----------------
    Check(
        id="full_namesakes",
        group="Дубликаты",
        title="Полные тёзки среди сотрудников",
        title_en="Complete namesakes among staff",
        count="""MATCH (p:Person:Itmo)
            WHERE p.surname_ru IS NOT NULL AND size(trim(p.surname_ru)) > 1
            WITH toLower(trim(p.surname_ru)) + '|' +
                 toLower(trim(coalesce(p.first_name_ru,''))) + '|' +
                 toLower(trim(coalesce(p.second_name_ru,''))) AS k, count(*) AS c
            WHERE c > 1 RETURN coalesce(sum(c - 1), 0)""",
        of=_ITMO_TOTAL,
        warn=0.01,
        fail=0.05,
        hint="Совпадает всё ФИО целиком — либо однофамильцы, либо один человек дважды.",
        hint_en="The entire full name matches — either same-name coincidences, "
        "or one person entered twice.",
        examples=f"""MATCH (p:Person:Itmo)
            WHERE p.surname_ru IS NOT NULL AND size(trim(p.surname_ru)) > 1
            WITH toLower(trim(p.surname_ru)) + '|' +
                 toLower(trim(coalesce(p.first_name_ru,''))) + '|' +
                 toLower(trim(coalesce(p.second_name_ru,''))) AS k,
                 collect(p) AS ps, collect({_FIO})[0] AS fio
            WHERE size(ps) > 1
            RETURN fio AS `ФИО`, size(ps) AS `Записей`,
                   [x IN ps | x.id] AS `Идентификаторы`,
                   [x IN ps | x.name_raw] AS `Имена (лат.)`
            ORDER BY size(ps) DESC, fio LIMIT $lim""",
    ),
    Check(
        id="short_signature_dup",
        group="Дубликаты",
        title="Одинаковая подпись «Фамилия И.»",
        title_en='Same short signature "Surname I."',
        count="""MATCH (p:Person:Itmo)
            WHERE p.surname_ru IS NOT NULL AND size(trim(p.surname_ru)) > 1
              AND p.first_name_ru IS NOT NULL AND trim(p.first_name_ru) <> ''
            WITH toLower(trim(p.surname_ru)) + ' ' +
                 toLower(left(trim(p.first_name_ru),1)) AS k, count(*) AS c
            WHERE c > 1 RETURN coalesce(sum(c - 1), 0)""",
        of=_ITMO_TOTAL,
        warn=0.05,
        fail=0.12,
        hint="Именно так люди подписаны на карте — этих не различить визуально.",
        hint_en="This is exactly how people are labeled on the map — "
        "these can't be told apart visually.",
        examples="""MATCH (p:Person:Itmo)
            WHERE p.surname_ru IS NOT NULL AND size(trim(p.surname_ru)) > 1
              AND p.first_name_ru IS NOT NULL AND trim(p.first_name_ru) <> ''
            WITH trim(p.surname_ru) + ' ' + left(trim(p.first_name_ru),1) + '.' AS sig,
                 collect(p) AS ps
            WHERE size(ps) > 1
            RETURN sig AS `Подпись на карте`, size(ps) AS `Человек`,
                   [x IN ps | x.name_raw] AS `Имена (лат.)`,
                   [x IN ps | x.id] AS `Идентификаторы`
            ORDER BY size(ps) DESC, sig LIMIT $lim""",
    ),
    Check(
        id="same_name_raw_diff_id",
        group="Дубликаты",
        title="Одинаковое имя латиницей у разных людей",
        title_en="Same Latin-script name for different people",
        count="""MATCH (p:Person) WHERE p.name_raw IS NOT NULL AND trim(p.name_raw) <> ''
            WITH toLower(trim(p.name_raw)) AS k, collect(p.id) AS ids WHERE size(ids) > 1
            WITH [x IN ids | CASE WHEN x STARTS WITH 'itmo_' THEN substring(x,5)
                                  WHEN x STARTS WITH 'ext_'  THEN substring(x,4)
                                  ELSE x END] AS sfx
            WITH reduce(a = [], s IN sfx | CASE WHEN s IN a THEN a ELSE a + s END) AS uniq
            WHERE size(uniq) > 1 RETURN coalesce(sum(size(uniq) - 1), 0)""",
        of="MATCH (p:Person) RETURN count(p)",
        warn=0.001,
        fail=0.01,
        hint="Один автор заведён под разными идентификаторами.",
        hint_en="One author entered under different identifiers.",
        examples="""MATCH (p:Person) WHERE p.name_raw IS NOT NULL AND trim(p.name_raw) <> ''
            WITH toLower(trim(p.name_raw)) AS k, collect(p.id) AS ids,
                 collect(p.name_raw)[0] AS nm
            WHERE size(ids) > 1
            WITH k, ids, nm,
                 [x IN ids | CASE WHEN x STARTS WITH 'itmo_' THEN substring(x,5)
                                  WHEN x STARTS WITH 'ext_'  THEN substring(x,4)
                                  ELSE x END] AS sfx
            WITH ids, nm, reduce(a = [], s IN sfx |
                 CASE WHEN s IN a THEN a ELSE a + s END) AS uniq
            WHERE size(uniq) > 1
            RETURN nm AS `Имя (лат.)`, size(uniq) AS `Разных id`,
                   ids AS `Идентификаторы`
            ORDER BY size(uniq) DESC, nm LIMIT $lim""",
    ),
    Check(
        id="dup_title",
        group="Дубликаты",
        title="Лишние публикации с тем же заголовком",
        title_en="Extra publications with the same title",
        count="""MATCH (p:Publication) WHERE p.title <> ''
            WITH toLower(trim(p.title)) AS k, count(*) AS c WHERE c > 1
            RETURN coalesce(sum(c - 1), 0)""",
        of=_PUB_TOTAL,
        warn=0.005,
        fail=0.02,
        hint="Обычно препринт и журнальная версия одной работы.",
        hint_en="Usually a preprint and the journal version of the same paper.",
        examples="""MATCH (p:Publication) WHERE p.title <> ''
            WITH toLower(trim(p.title)) AS k, collect(p) AS ps
            WHERE size(ps) > 1
            RETURN collect(ps[0].title)[0] AS `Заголовок`, size(ps) AS `Записей`,
                   [x IN ps | x.doi] AS `DOI`, [x IN ps | x.id] AS `Идентификаторы`
            ORDER BY size(ps) DESC LIMIT $lim""",
    ),
    Check(
        id="dup_doi",
        group="Дубликаты",
        title="Публикации с одинаковым DOI",
        title_en="Publications with the same DOI",
        count="""MATCH (p:Publication) WHERE p.doi <> ''
            WITH toLower(p.doi) AS k, count(*) AS c WHERE c > 1
            RETURN coalesce(sum(c - 1), 0)""",
        of=None,
        warn=1,
        fail=10,
        hint=None,
        examples="""MATCH (p:Publication) WHERE p.doi <> ''
            WITH toLower(p.doi) AS k, collect(p) AS ps WHERE size(ps) > 1
            RETURN k AS `DOI`, size(ps) AS `Записей`,
                   [x IN ps | x.id] AS `Идентификаторы`,
                   [x IN ps | x.title] AS `Заголовки`
            ORDER BY size(ps) DESC LIMIT $lim""",
    ),
    Check(
        id="itmo_ext_pair",
        group="Дубликаты",
        title="Человек заведён и как сотрудник, и как внешний",
        title_en="Person entered as both staff and external",
        count="""MATCH (i:Person:Itmo) WHERE i.id STARTS WITH 'itmo_'
            WITH i, 'ext_' + substring(i.id, 5) AS e
            MATCH (:Person:External {id: e}) RETURN count(*)""",
        of=_ITMO_TOTAL,
        warn=0.01,
        fail=0.05,
        hint="Соавторство теряется, если на статье он подписан не от ИТМО.",
        hint_en="Co-authorship is lost if a paper credits them outside ITMO.",
        examples="""MATCH (i:Person:Itmo) WHERE i.id STARTS WITH 'itmo_'
            WITH i, 'ext_' + substring(i.id, 5) AS eid
            MATCH (e:Person:External {id: eid})
            OPTIONAL MATCH (i)-[:AUTHORED]->(pi:Publication)
            WITH i, e, count(pi) AS itmo_pubs
            OPTIONAL MATCH (e)-[:AUTHORED]->(pe:Publication)
            RETURN i.name_raw AS `Имя`, i.id AS `id (ИТМО)`, e.id AS `id (внешний)`,
                   itmo_pubs AS `Публикаций от ИТМО`,
                   count(pe) AS `Публикаций вне ИТМО`
            ORDER BY count(pe) DESC LIMIT $lim""",
    ),
    Check(
        id="repo_url_case",
        group="Дубликаты",
        title="Репозитории, различающиеся лишь регистром ссылки",
        title_en="Repositories that differ only in URL case",
        count="""MATCH (r:Repository)
            WITH toLower(rtrim(r.url, '/')) AS k, count(*) AS c WHERE c > 1
            RETURN coalesce(sum(c - 1), 0)""",
        of=None,
        warn=1,
        fail=5,
        hint=None,
        examples="""MATCH (r:Repository)
            WITH toLower(rtrim(r.url, '/')) AS k, collect(r) AS rs WHERE size(rs) > 1
            RETURN k AS `Ссылка`, size(rs) AS `Записей`,
                   [x IN rs | x.url] AS `Варианты`, [x IN rs | x.id] AS `Идентификаторы`
            ORDER BY size(rs) DESC LIMIT $lim""",
    ),
    Check(
        id="dept_same_name",
        group="Дубликаты",
        title="Департаменты с одинаковым названием",
        title_en="Departments with the same name",
        count="""MATCH (d:Department) WHERE trim(coalesce(d.name_ru, d.name_en, '')) <> ''
            WITH toLower(trim(coalesce(d.name_ru, d.name_en))) AS k, count(*) AS c
            WHERE c > 1 RETURN coalesce(sum(c - 1), 0)""",
        of=None,
        warn=1,
        fail=5,
        hint=None,
        examples="""MATCH (d:Department) WHERE trim(coalesce(d.name_ru, d.name_en, '')) <> ''
            WITH toLower(trim(coalesce(d.name_ru, d.name_en))) AS k, collect(d) AS ds
            WHERE size(ds) > 1
            RETURN k AS `Название`, size(ds) AS `Записей`,
                   [x IN ds | x.id] AS `Идентификаторы`
            ORDER BY size(ds) DESC LIMIT $lim""",
    ),
    Check(
        id="dup_authored",
        group="Дубликаты",
        title="Повторяющиеся авторства",
        title_en="Duplicate authorships",
        count="""MATCH (p:Person)-[a:AUTHORED]->(pub:Publication)
            WITH p, pub, count(a) AS c WHERE c > 1 RETURN count(*)""",
        of=None,
        warn=1,
        fail=10,
        hint="Один человек указан автором одной статьи дважды.",
        hint_en="One person listed as an author of one paper twice.",
        examples="""MATCH (p:Person)-[a:AUTHORED]->(pub:Publication)
            WITH p, pub, count(a) AS c WHERE c > 1
            RETURN p.name_raw AS `Автор`, p.id AS `id автора`,
                   pub.title AS `Публикация`, pub.id AS `id публикации`, c AS `Связей`
            ORDER BY c DESC LIMIT $lim""",
    ),
    # ---------------- contradictions ----------------
    Check(
        id="implements_no_has_code",
        group="Противоречия",
        title="Репозиторий привязан, но статья помечена как без кода",
        title_en="Repository linked, but the paper is flagged as code-free",
        count="MATCH (r:Repository)-[:IMPLEMENTS]->(p:Publication) "
        "WHERE p.has_code = false RETURN count(DISTINCT p)",
        of=None,
        warn=1,
        fail=20,
        hint=None,
        examples="""MATCH (r:Repository)-[:IMPLEMENTS]->(p:Publication)
            WHERE p.has_code = false
            RETURN p.id AS id, p.title AS `Публикация`, p.year AS `Год`,
                   r.url AS `Репозиторий`, coalesce(p.code_url,'—') AS `code_url`
            ORDER BY p.year DESC LIMIT $lim""",
    ),
    Check(
        id="has_code_no_repo",
        group="Противоречия",
        title="Статья помечена как с кодом, но репозитория нет",
        title_en="Paper flagged as having code, but no repository",
        count="MATCH (p:Publication) WHERE p.has_code = true "
        "AND NOT (p)<-[:IMPLEMENTS]-() RETURN count(p)",
        of=None,
        warn=1,
        fail=20,
        hint=None,
        examples="""MATCH (p:Publication)
            WHERE p.has_code = true AND NOT (p)<-[:IMPLEMENTS]-()
            RETURN p.id AS id, p.title AS `Публикация`, p.year AS `Год`,
                   coalesce(p.code_url,'—') AS `code_url`
            ORDER BY p.year DESC LIMIT $lim""",
    ),
    Check(
        id="repo_bad_url",
        group="Противоречия",
        title="Репозитории с испорченной ссылкой",
        title_en="Repositories with a broken URL",
        count=r"""MATCH (r:Repository)
            WHERE r.url =~ '.*[^\x00-\x7F].*'
               OR NOT r.url =~ 'https?://[^/]+/[^/]+/[^/]+.*'
            RETURN count(r)""",
        of="MATCH (r:Repository) RETURN count(r)",
        warn=0.005,
        fail=0.02,
        hint="Мусор, вытащенный из PDF вместе с адресом.",
        hint_en="Junk pulled out of the PDF along with the address.",
        examples=r"""MATCH (r:Repository)
            WHERE r.url =~ '.*[^\x00-\x7F].*'
               OR NOT r.url =~ 'https?://[^/]+/[^/]+/[^/]+.*'
            OPTIONAL MATCH (r)-[:IMPLEMENTS]->(p:Publication)
            RETURN r.id AS id, r.url AS `Ссылка`, r.name AS `Имя`,
                   collect(p.title)[0] AS `Из публикации`
            ORDER BY r.url LIMIT $lim""",
    ),
    Check(
        id="relevant_link_no_repo",
        group="Противоречия",
        title="Ссылка признана рабочей, но репозиторий не заведён",
        title_en="Link marked relevant, but no repository was created",
        count="""MATCH (:Publication)-[m:MENTIONS_LINK]->(l:LinkCandidate)
            WHERE m.is_relevant = true AND NOT EXISTS {
              (r:Repository) WHERE toLower(rtrim(r.url,'/')) = toLower(rtrim(l.url,'/')) }
            RETURN count(*)""",
        of=None,
        warn=1,
        fail=20,
        hint=None,
        examples="""MATCH (p:Publication)-[m:MENTIONS_LINK]->(l:LinkCandidate)
            WHERE m.is_relevant = true AND NOT EXISTS {
              (r:Repository) WHERE toLower(rtrim(r.url,'/')) = toLower(rtrim(l.url,'/')) }
            RETURN l.url AS `Ссылка`, p.id AS `id публикации`, p.title AS `Публикация`,
                   m.llm_confidence AS `Уверенность`, m.llm_reason AS `Обоснование`
            ORDER BY m.llm_confidence DESC LIMIT $lim""",
    ),
    Check(
        id="link_no_verdict",
        group="Противоречия",
        title="Ссылки без решения о релевантности",
        title_en="Links without a relevance verdict",
        count="MATCH ()-[m:MENTIONS_LINK]->() WHERE m.is_relevant IS NULL RETURN count(m)",
        of=None,
        warn=1,
        fail=20,
        hint=None,
        examples="""MATCH (p:Publication)-[m:MENTIONS_LINK]->(l:LinkCandidate)
            WHERE m.is_relevant IS NULL
            RETURN l.url AS `Ссылка`, p.id AS `id публикации`, p.title AS `Публикация`,
                   l.host AS `Хост`
            LIMIT $lim""",
    ),
    Check(
        id="repo_no_owner",
        group="Противоречия",
        title="Репозитории без владельца",
        title_en="Repositories without an owner",
        count="MATCH (r:Repository) WHERE NOT (r)-[:OWNED_BY]->() RETURN count(r)",
        of=None,
        warn=1,
        fail=10,
        hint=None,
        examples="""MATCH (r:Repository) WHERE NOT (r)-[:OWNED_BY]->()
            RETURN r.id AS id, r.url AS `Ссылка`, r.name AS `Имя`,
                   r.stars_num AS `Звёзды` LIMIT $lim""",
    ),
    Check(
        id="ghprofile_no_repo",
        group="Противоречия",
        title="Профили GitHub без репозиториев",
        title_en="GitHub profiles without repositories",
        count="MATCH (g:GitHubProfile) WHERE NOT (g)<-[:OWNED_BY]-() RETURN count(g)",
        of=None,
        warn=1,
        fail=20,
        hint=None,
        examples="""MATCH (g:GitHubProfile) WHERE NOT (g)<-[:OWNED_BY]-()
            RETURN g.login AS `Логин`, g.name AS `Имя`, g.type AS `Тип`,
                   g.html_url AS `Ссылка` LIMIT $lim""",
    ),
    Check(
        id="year_mismatch",
        group="Противоречия",
        title="Год не совпадает с датой публикации",
        title_en="Year doesn't match the publication date",
        count=f"MATCH (p:Publication) WHERE p.publication_date IS NOT NULL "
        f"AND p.year <> {_PUB_YEAR} RETURN count(p)",
        of=None,
        warn=1,
        fail=50,
        hint=None,
        examples=f"""MATCH (p:Publication) WHERE p.publication_date IS NOT NULL
            AND p.year <> {_PUB_YEAR}
            RETURN p.id AS id, p.title AS `Публикация`, p.year AS `Поле year`,
                   toString(p.publication_date) AS `Дата публикации` LIMIT $lim""",
    ),
    Check(
        id="self_loop",
        group="Противоречия",
        title="Узел связан сам с собой",
        title_en="Node linked to itself",
        count="MATCH (n)-[e]->(n) RETURN count(e)",
        of=None,
        warn=1,
        fail=10,
        hint=None,
        examples="""MATCH (n)-[e]->(n)
            RETURN labels(n)[0] AS `Тип узла`, coalesce(n.id, n.login) AS id,
                   type(e) AS `Связь` LIMIT $lim""",
    ),
]

BY_ID = {c.id: c for c in CHECKS}

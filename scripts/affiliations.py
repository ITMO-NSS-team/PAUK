"""Очистка/дедуп поля persons_itmo.affiliation до списка подразделений ИТМО.

Сырое поле — union строк-аффилиаций по всем публикациям автора: одна организация в
десятках написаний (разные адреса/индексы/формат) плюс со-аффилиации других вузов.
Для матчинга департаментов нужны только РАЗЛИЧНЫЕ подразделения ИТМО. Здесь —
детерминированная очистка (без LLM): фильтр по ИТМО, извлечение деп-сегментов,
срезание вуза/адреса, дедуп по нормализованной форме.
"""

import re

AFFILIATION_SEPARATOR = " \n "

# Маркер принадлежности под-аффилиации к ИТМО (в т.ч. старое официальное имя).
_ITMO_MARK = re.compile(
    r"\bitmo\b|итмо|information technologies,?\s*mechanics,?\s*and optics|universitet\s+itmo",
    re.I,
)
# Ключевое слово подразделения.
_DEPT_KW = re.compile(r"\b(faculty|institute|department|school|centre|center|laborator\w*|cluster|hub)\b", re.I)
# Маркер ЧУЖОЙ (не ИТМО) организации в сегменте — такие сегменты не относим к ИТМО.
_FOREIGN_INST = re.compile(
    r"\b(universit\w*|polytechnic\w*|politekhn\w*|academ(?:y|ies)|akadem\w*|ras|ran|ран|ioffe|sheffield|helmholtz"
    r"|karlsruhe|menoufia|hamburg|kurnakov|magneton|electrotechnical|geesthacht)\b", re.I,
)
# Упоминание подразделения (англ. + кириллица) — чтобы отличить «есть, но не извлеклось»
# от «голый вуз без подразделения».
_DEPT_MENTION = re.compile(
    r"\b(?:faculty|institute|department|school|centre|center|laborator\w*|cluster)\b"
    r"|\b(?:факультет|институт|кафедр\w*|лаборатори\w*|школа|департамент|мегафакультет|центр|кластер)", re.I,
)
# Позиция начала нового подразделения — чтобы разбить склеенные в одном сегменте
# («School of ... Faculty of ...» без запятой).
_UNIT_SPLIT = re.compile(
    r"(?=\b(?:school|faculty|institute|department|centre|center|laborator\w*)\b\s+(?:of|for)\b)", re.I
)
# Вырезаемое: сам вуз (ИТМО в любом виде).
_ITMO_STRIP = re.compile(
    r"national research university[^,;]*optics|universitet\s+itmo|\bitmo\s+university\b|\bitmo\b", re.I
)
# Адресные/географические токены и транслит-обрывки — выкидываем.
_ADDR = re.compile(
    r"\b(saint|st|petersburg|peterburg|leningrad|region|russia|russian|federation|moscow|minsk|germany|uk"
    r"|china|kronverksk\w*|lomonosov\w*|khlopina|politekhn\w*|polytechnic\w*|prospekt|prospect|birjevaja"
    r"|birzhevaja|avenue|street|str|pr|av|line|bldg|building|megafakul\w*|fotoniki|universitet)\b",
    re.I,
)
_GENERIC_KEYS = {"university", "russianacademyofsciences"}
# Родовые слова — если после их удаления ничего не осталось, это фрагмент («Center of»).
_GENERIC_WORDS = re.compile(
    r"\b(department|faculty|institute|school|centre|center|laborator\w*|cluster|hub|of|for|and|the"
    r"|research|educational|education|national|international|scientific|higher|joint)\b", re.I)


def _canonical_unit(segment: str) -> str:
    """Вырезает из сегмента вуз/адрес/ведущий мусор, оставляя читаемое название подразделения."""
    s = _ITMO_STRIP.sub(" ", segment)
    s = re.sub(r"^[^A-Za-zА-Яа-яё]+", "", s)      # ведущие «1)», цифры, кавычки
    s = re.split(r"\d+(?![A-Za-zА-Яа-яё])", s, maxsplit=1)[0]  # адрес: число не перед буквой («3D» цел)
    s = _ADDR.sub(" ", s)
    s = re.sub(r"[^0-9A-Za-zА-Яа-яё ]", " ", s)   # пунктуация/дефисы
    s = re.sub(r"^\s*the\s+", "", s, flags=re.I)
    return " ".join(s.split())


def _key(name: str) -> str:
    """Ключ дедупа: буквы/цифры в нижнем регистре, орфо-вариант centre→center сведён."""
    s = name.lower().replace("centre", "center")
    return re.sub(r"[^0-9a-zа-яё]", "", s)


def clean_affiliation(raw: str | None) -> list[str]:
    """Различные подразделения ИТМО из сырого поля affiliation.

    Args:
        raw: сырое поле persons_itmo.affiliation (строки через AFFILIATION_SEPARATOR).

    Returns:
        Список читаемых названий подразделений ИТМО, по одному на каждое различное
        (порядок появления). Не-ИТМО организации и адресный шум отфильтрованы.
    """
    reps: dict[str, str] = {}
    for line in (raw or "").split(AFFILIATION_SEPARATOR):
        line_itmo = bool(_ITMO_MARK.search(line))
        for sub in line.split(";"):                       # разные организации в одной строке
            if not _ITMO_MARK.search(sub):
                # сегмент без маркера ИТМО берём, только если это подразделение без
                # собственного вуза, а маркер ИТМО есть в этой же строке («Lab; Lab; ИТМО»);
                # сегмент с собственным чужим вузом — пропускаем (Альферов/Политех/…).
                if not line_itmo or _FOREIGN_INST.search(sub):
                    continue
            for segment in sub.split(","):
                if not _DEPT_KW.search(segment):
                    continue
                if _FOREIGN_INST.search(segment) and not _ITMO_MARK.search(segment):
                    continue                              # чужая организация как сегмент ITMO-строки
                for piece in _UNIT_SPLIT.split(segment):  # склеенные юниты → по отдельности
                    if not _DEPT_KW.search(piece):
                        continue
                    name = _canonical_unit(piece)
                    key = _key(name)
                    # отбрасываем фрагменты: после удаления родовых слов должно остаться содержимое
                    content = _GENERIC_WORDS.sub(" ", name).strip()
                    if len(key) >= 4 and key not in _GENERIC_KEYS and content:
                        reps.setdefault(key, name)
    return list(reps.values())


def has_department_mention(raw: str | None) -> bool:
    """Есть ли на ИТМО-строке упоминание подразделения (англ./кириллица).

    Нужно, чтобы отличить «подразделение ИТМО есть, но не извлеклось» (тогда — в LLM)
    от «голый вуз без подразделения» или «только чужие организации» (тогда — без
    департамента, минуя LLM, чтобы не гонять шумный сырой блоб зря).
    """
    for line in (raw or "").split(AFFILIATION_SEPARATOR):
        if _ITMO_MARK.search(line) and _DEPT_MENTION.search(line):
            return True
    return False

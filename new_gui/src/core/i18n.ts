// i18n — два независимых механизма под одной крышей:
// 1) localize() — выбор языка в ДАННЫХ, которые генератор уже отдаёт
//    билингвально парами полей (author.label/label_en, dept.name/name_en).
// 2) t()/LOCALES — статичные строки интерфейса (подписи кнопок, полей
//    карточки и т.п.), которых в самих данных нет вообще.
// Это два разных источника текста, поэтому и функции разные — смешивать
// их в одну было бы удобно на вид, но означало бы держать текст интерфейса
// внутри объектов данных, что не имеет смысла.

import type { NodeKind } from "../contracts/graph";

export type Lang = "ru" | "en";

/**
 * Выбирает нужный языковой вариант из пары ru/en для одного поля ДАННЫХ
 * (не статичного текста интерфейса — для этого ниже есть `t()`).
 *
 * `en` может отсутствовать (не у каждого поля есть свой `_en`, например у
 * `RepoNode.label` — имя репозитория не переводится) — тогда, даже при
 * `lang === "en"`, функция остаётся на ru-варианте, а не возвращает пустую
 * строку и не падает.
 *
 * Сознательно принимает уже готовые строки (`localize(author.label,
 * author.label_en, lang)`), а не объект с именем поля-"базы" (например,
 * `localize(author, "label", lang)`). Вариант с именем поля потребовал бы
 * либо небезопасного приведения типов при сборке ключа `"label" + "_en"`,
 * либо жёсткой привязки к форме конкретного объекта (что делать с
 * `RepoNode`, у которого `_en`-варианта нет вообще?). Два явных строковых
 * аргумента компилятор проверяет полностью, без `as`.
 *
 * @param ru - значение на русском — оно же дефолт, если для языка "en" перевода нет.
 * @param en - значение на английском, если оно вообще существует у этого поля данных.
 * @param lang - язык интерфейса, на который нужно переключиться.
 * @returns `en`, если `lang === "en"` и `en` реально задан; иначе — `ru`.
 *
 * @example
 * localize("Иванов И.И.", "Ivanov I.I.", "ru"); // "Иванов И.И."
 * localize("Иванов И.И.", "Ivanov I.I.", "en"); // "Ivanov I.I."
 * localize("graph-toolkit", undefined, "en");   // "graph-toolkit" — en нет, остаёмся на ru
 */
export function localize(ru: string, en: string | undefined, lang: Lang): string {
  return lang === "en" && en ? en : ru;
}

/** Ключи статичных строк интерфейса — по одному на каждый видимый текст, который нужно показывать на двух языках. */
export type LocaleKey =
  | "tab.authors"
  | "tab.repos"
  | "tab.pubs"
  | "tab.search"
  // Шаблонный литеральный тип вместо четырёх записей вручную — так
  // `kind.${node.kind}` (node.kind: NodeKind) проверяется компилятором
  // по-настоящему, без приведения типов через as в местах вызова.
  | `kind.${NodeKind | "dept"}`
  | "kind.edge"
  | "field.key"
  | "field.kind"
  | "field.dept"
  | "field.pubsCount"
  | "field.degree"
  | "field.nameVariants"
  | "field.github"
  | "field.orcid"
  | "field.stars"
  | "field.description"
  | "field.owner"
  | "field.year"
  | "field.yearUnknown"
  | "field.unknownDept"
  | "field.edgeFrom"
  | "field.edgeTo"
  | "field.edgeWeight"
  | "field.sharedPubs"
  | "field.sharedAuthors"
  | "field.topCoauthors"
  | "field.relatedDepts"
  | "field.contributors"
  | "field.authorsCount"
  | "field.reposCount"
  | "field.deptsCount"
  | "field.total"
  | "overview.title"
  | "field.doi"
  | "field.code"
  | "search.placeholder"
  | "search.pubsCountShort"
  | "lang.toggle"
  | "filter.coauth"
  | "filter.sharedAuthors"
  | "filter.yearMax";

const LOCALES: Record<Lang, Record<LocaleKey, string>> = {
  ru: {
    "tab.authors": "Авторы",
    "tab.repos": "Репозитории",
    "tab.pubs": "Публикации",
    "tab.search": "Поиск",
    "kind.author": "Автор",
    "kind.repo": "Репозиторий",
    "kind.pub": "Публикация",
    "kind.dept": "Департамент",
    "kind.edge": "Связь",
    "field.key": "Ключ",
    "field.kind": "Тип",
    "field.dept": "Департамент",
    "field.pubsCount": "Публикаций",
    "field.degree": "Учёная степень",
    "field.nameVariants": "Варианты имени",
    "field.github": "GitHub",
    "field.orcid": "ORCID",
    "field.stars": "Звёзд",
    "field.description": "Описание",
    "field.owner": "Владелец",
    "field.year": "Год",
    "field.yearUnknown": "неизвестен",
    "field.unknownDept": "—",
    "field.edgeFrom": "От",
    "field.edgeTo": "К",
    "field.edgeWeight": "Вес",
    "field.sharedPubs": "Общие публикации",
    "field.sharedAuthors": "Общие авторы",
    "field.topCoauthors": "Топ соавторов",
    "field.relatedDepts": "Связанные департаменты",
    "field.contributors": "Участники",
    "field.authorsCount": "Авторов",
    "field.reposCount": "Репозиториев",
    "field.deptsCount": "Департаментов",
    "field.total": "Всего",
    "field.doi": "DOI",
    "field.code": "Код",
    "overview.title": "Обзор",
    "search.placeholder": "Поиск по авторам, репозиториям, публикациям, департаментам…",
    "search.pubsCountShort": "публ.",
    "lang.toggle": "EN",
    "filter.coauth": "Мин. соавторство",
    "filter.sharedAuthors": "Мин. общих авторов",
    "filter.yearMax": "До года",
  },
  en: {
    "tab.authors": "Authors",
    "tab.repos": "Repositories",
    "tab.pubs": "Publications",
    "tab.search": "Search",
    "kind.author": "Author",
    "kind.repo": "Repository",
    "kind.pub": "Publication",
    "kind.dept": "Department",
    "kind.edge": "Link",
    "field.key": "Key",
    "field.kind": "Type",
    "field.dept": "Department",
    "field.pubsCount": "Publications",
    "field.degree": "Degree",
    "field.nameVariants": "Other spellings",
    "field.github": "GitHub",
    "field.orcid": "ORCID",
    "field.stars": "Stars",
    "field.description": "Description",
    "field.owner": "Owner",
    "field.year": "Year",
    "field.yearUnknown": "unknown",
    "field.unknownDept": "—",
    "field.edgeFrom": "From",
    "field.edgeTo": "To",
    "field.edgeWeight": "Weight",
    "field.sharedPubs": "Shared publications",
    "field.sharedAuthors": "Shared authors",
    "field.topCoauthors": "Top co-authors",
    "field.relatedDepts": "Related departments",
    "field.contributors": "Contributors",
    "field.authorsCount": "Authors",
    "field.reposCount": "Repositories",
    "field.deptsCount": "Departments",
    "field.total": "Total",
    "field.doi": "DOI",
    "field.code": "Code",
    "overview.title": "Overview",
    "search.placeholder": "Search authors, repositories, publications, departments…",
    "search.pubsCountShort": "pubs",
    "lang.toggle": "RU",
    "filter.coauth": "Min. co-authorship",
    "filter.sharedAuthors": "Min. shared authors",
    "filter.yearMax": "Up to year",
  },
};

/**
 * Возвращает статичную строку интерфейса на нужном языке — единственный
 * способ получить текст кнопки/подписи поля/заголовка карточки и т.п. в
 * этом приложении. В отличие от `localize()` выше, работает не с полем
 * данных, а с фиксированным ключом из словаря {@link LOCALES}.
 *
 * @param key - ключ строки из {@link LocaleKey} — компилятор не даст передать несуществующий ключ.
 * @param lang - язык интерфейса.
 * @returns Готовая строка на нужном языке, всегда определена (для каждого
 *   ключа `LocaleKey` в обоих словарях `LOCALES.ru`/`LOCALES.en` обязательно
 *   есть значение — это гарантирует сам тип `Record<Lang, Record<LocaleKey, string>>`).
 *
 * @example
 * t("tab.authors", "ru"); // "Авторы"
 * t("tab.authors", "en"); // "Authors"
 */
export function t(key: LocaleKey, lang: Lang): string {
  return LOCALES[lang][key];
}

/**
 * Подпись вида узла или департамента ("Автор"/"Author", "Департамент" и
 * т.п.) — тонкая типобезопасная обёртка над `t()` под конкретный шаблонный
 * ключ `kind.*` из {@link LocaleKey}. Существует отдельно от `t()`, чтобы
 * вызывающему коду не нужно было руками собирать строку `` `kind.${kind}` ``
 * и приводить её к типу `LocaleKey` через `as`.
 *
 * @param kind - вид сущности: один из видов узла графа (`NodeKind`) либо `"dept"` для департамента.
 * @param lang - язык интерфейса.
 * @returns Подпись вида на нужном языке.
 *
 * @example
 * kindLabel("author", "ru"); // "Автор"
 * kindLabel("dept", "en");   // "Department"
 */
export function kindLabel(kind: NodeKind | "dept", lang: Lang): string {
  return t(`kind.${kind}`, lang);
}

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
 * Выбирает нужный вариант из пары ru/en. en может отсутствовать (не у
 * каждого поля есть свой _en, например у RepoNode.label) — тогда, даже
 * при lang === "en", остаёмся на ru-варианте, а не показываем пустоту.
 *
 * Сознательно принимает уже готовые строки (localize(author.label,
 * author.label_en, lang)), а не объект с именем поля-"базы" — вариант с
 * именем поля потребовал бы либо небезопасного приведения типов при
 * сборке ключа "поле_en", либо жёсткой привязки к форме конкретного
 * объекта. Два явных аргумента компилятор проверяет полностью.
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
  | "field.stars"
  | "field.owner"
  | "field.year"
  | "field.yearUnknown"
  | "field.unknownDept"
  | "field.edgeFrom"
  | "field.edgeTo"
  | "field.edgeWeight"
  | "field.authorsCount"
  | "field.reposCount"
  | "field.total"
  | "search.placeholder"
  | "search.pubsCountShort"
  | "lang.toggle";

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
    "field.stars": "Звёзд",
    "field.owner": "Владелец",
    "field.year": "Год",
    "field.yearUnknown": "неизвестен",
    "field.unknownDept": "—",
    "field.edgeFrom": "От",
    "field.edgeTo": "К",
    "field.edgeWeight": "Вес",
    "field.authorsCount": "Авторов",
    "field.reposCount": "Репозиториев",
    "field.total": "Всего",
    "search.placeholder": "Поиск по авторам, репозиториям, публикациям, департаментам…",
    "search.pubsCountShort": "публ.",
    "lang.toggle": "EN",
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
    "field.stars": "Stars",
    "field.owner": "Owner",
    "field.year": "Year",
    "field.yearUnknown": "unknown",
    "field.unknownDept": "—",
    "field.edgeFrom": "From",
    "field.edgeTo": "To",
    "field.edgeWeight": "Weight",
    "field.authorsCount": "Authors",
    "field.reposCount": "Repositories",
    "field.total": "Total",
    "search.placeholder": "Search authors, repositories, publications, departments…",
    "search.pubsCountShort": "pubs",
    "lang.toggle": "RU",
  },
};

/** Возвращает статичную строку интерфейса на нужном языке. */
export function t(key: LocaleKey, lang: Lang): string {
  return LOCALES[lang][key];
}

/** Подпись вида узла ("Автор"/"Author" и т.п.) — тонкая обёртка над t() под конкретный шаблонный ключ kind.*. */
export function kindLabel(kind: NodeKind | "dept", lang: Lang): string {
  return t(`kind.${kind}`, lang);
}

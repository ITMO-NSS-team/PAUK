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
  | "field.nameVariantsOpenalex"
  | "field.nameVariantsOrcid"
  | "field.github"
  | "field.orcid"
  | "field.stars"
  | "field.description"
  | "field.ownerType"
  | "field.license"
  | "field.hasReadme"
  | "field.openalexId"
  | "field.googleScholar"
  | "field.openreview"
  | "field.email"
  | "field.affiliations"
  | "field.pubType"
  | "field.pubFields"
  | "field.abstract"
  | "field.openalexUrl"
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
  | "field.loadingDetails"
  | "field.authorsCount"
  | "field.reposCount"
  | "field.deptsCount"
  | "field.total"
  | "overview.title"
  | "overview.avgPubsPerAuthor"
  | "overview.knownYear"
  | "chart.authorsByDept"
  | "chart.pubsByYear"
  | "chart.reposByStars"
  | "field.doi"
  | "field.code"
  | "field.createdAt"
  | "field.updatedAt"
  | "field.corresponding"
  | "field.authorPosition"
  | "section.general"
  | "section.private"
  | "section.service"
  | "panel.showMore"
  | "panel.showLess"
  | "search.placeholder"
  | "search.pubsCountShort"
  | "search.trigger"
  | "search.browseDepts"
  | "tab.searchPlaceholder"
  | "tab.noResults"
  | "tab.prevPage"
  | "tab.nextPage"
  | "filter.coauth"
  | "filter.sharedAuthors"
  | "filter.yearMax"
  | "filter.showNoDept"
  | "filter.edgeZoom"
  | "filter.showRegions"
  | "filter.regionZoom"
  | "filter.regionMinNodes"
  | "brand.backToMenu"
  | "section.filters"
  | "section.quickSearch"
  | "start.badge"
  | "start.title"
  | "start.subtitle"
  | "start.cta"
  | "start.loading"
  | "start.rendering"
  | "start.error"
  | "start.errorFetch"
  | "start.errorFetchHint"
  | "start.errorRender";

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
    "field.nameVariantsOpenalex": "Варианты написания (OpenAlex)",
    "field.nameVariantsOrcid": "Варианты написания (ORCID)",
    "field.github": "GitHub",
    "field.orcid": "ORCID",
    "field.stars": "Звёзд",
    "field.description": "Описание",
    "field.ownerType": "Тип владельца",
    "field.license": "Лицензия",
    "field.hasReadme": "Есть README",
    "field.openalexId": "OpenAlex",
    "field.googleScholar": "Google Scholar",
    "field.openreview": "OpenReview",
    "field.email": "Email",
    "field.affiliations": "Аффилиации",
    "field.pubType": "Тип публикации",
    "field.pubFields": "Направления",
    "field.abstract": "Аннотация",
    "field.openalexUrl": "OpenAlex",
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
    "field.loadingDetails": "Подробнее",
    "field.authorsCount": "Авторов",
    "field.reposCount": "Репозиториев",
    "field.deptsCount": "Департаментов",
    "field.total": "Всего",
    "field.doi": "DOI",
    "field.code": "Код",
    "field.createdAt": "Создан",
    "field.updatedAt": "Обновлён",
    "field.corresponding": "автор для переписки",
    "field.authorPosition": "{n}-й автор",
    "section.general": "Общее",
    "section.private": "Приватное",
    "section.service": "Служебное",
    "panel.showMore": "+ ещё {n}",
    "panel.showLess": "− свернуть",
    "overview.title": "Обзор",
    "overview.avgPubsPerAuthor": "Публикаций на автора (среднее)",
    "overview.knownYear": "Известен год",
    "chart.authorsByDept": "Авторы по департаментам",
    "chart.pubsByYear": "Публикации по годам",
    "chart.reposByStars": "Репозитории по звёздам",
    "search.placeholder": "Поиск по авторам, репозиториям, публикациям, департаментам…",
    "search.pubsCountShort": "публ.",
    "search.trigger": "Поиск по всему",
    "search.browseDepts": "Департаменты",
    "tab.searchPlaceholder": "Поиск…",
    "tab.noResults": "Ничего не найдено",
    "tab.prevPage": "Предыдущая страница",
    "tab.nextPage": "Следующая страница",
    "filter.coauth": "Мин. соавторство",
    "filter.sharedAuthors": "Мин. общих авторов",
    "filter.yearMax": "До года",
    "filter.showNoDept": "Показывать без департамента",
    "filter.edgeZoom": "Порог показа рёбер",
    "filter.showRegions": "Регионы департаментов",
    "filter.regionZoom": "Порог показа регионов",
    "filter.regionMinNodes": "Мин. узлов в регионе",
    "brand.backToMenu": "← Меню",
    "section.filters": "Фильтры",
    "section.quickSearch": "Быстрый поиск",
    "start.badge": "Открытый проект ИТМО",
    "start.title": "Карта соавторства и открытого кода ИТМО",
    "start.subtitle":
      "Публикации, авторы и департаменты ИТМО — и связанные с ними open-source репозитории на GitHub.",
    "start.cta": "Смотреть карту",
    "start.loading": "Загрузка данных…",
    "start.rendering": "Отрисовка графа…",
    // Статус-текст под прогресс-баром при любой ошибке — специально
    // нейтральный: причин две совсем разные (не удалось загрузить
    // graph-data.json ИЛИ данные загрузились, но сломалась отрисовка), а
    // сам boot-экран прячется сразу же (features/start.ts::hideBootOnError)
    // ради видимого баннера с точной причиной ниже — этот текст никто не
    // должен реально увидеть, но он не должен врать, если всё-таки увидит.
    "start.error": "Ошибка загрузки.",
    "start.errorFetch": "Не удалось загрузить данные графа",
    "start.errorFetchHint":
      'Проверьте, что "python -m new_generate.graph_builder" сгенерировал файлы в data/gui/private.',
    "start.errorRender":
      "Данные графа загрузились, но при отрисовке произошла ошибка. Подробности — в консоли браузера (F12).",
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
    "field.nameVariantsOpenalex": "Other spellings (OpenAlex)",
    "field.nameVariantsOrcid": "Other spellings (ORCID)",
    "field.github": "GitHub",
    "field.orcid": "ORCID",
    "field.stars": "Stars",
    "field.description": "Description",
    "field.ownerType": "Owner type",
    "field.license": "License",
    "field.hasReadme": "Has README",
    "field.openalexId": "OpenAlex",
    "field.googleScholar": "Google Scholar",
    "field.openreview": "OpenReview",
    "field.email": "Email",
    "field.affiliations": "Affiliations",
    "field.pubType": "Publication type",
    "field.pubFields": "Fields",
    "field.abstract": "Abstract",
    "field.openalexUrl": "OpenAlex",
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
    "field.loadingDetails": "More info",
    "field.authorsCount": "Authors",
    "field.reposCount": "Repositories",
    "field.deptsCount": "Departments",
    "field.total": "Total",
    "field.doi": "DOI",
    "field.code": "Code",
    "field.createdAt": "Created",
    "field.updatedAt": "Updated",
    "field.corresponding": "corresponding",
    "field.authorPosition": "author #{n}",
    "section.general": "General",
    "section.private": "Private",
    "section.service": "Service",
    "panel.showMore": "+ {n} more",
    "panel.showLess": "− show less",
    "overview.title": "Overview",
    "overview.avgPubsPerAuthor": "Publications per author (avg.)",
    "overview.knownYear": "Known year",
    "chart.authorsByDept": "Authors by department",
    "chart.pubsByYear": "Publications by year",
    "chart.reposByStars": "Repositories by stars",
    "search.placeholder": "Search authors, repositories, publications, departments…",
    "search.pubsCountShort": "pubs",
    "search.trigger": "Search everything",
    "search.browseDepts": "Departments",
    "tab.searchPlaceholder": "Search…",
    "tab.noResults": "No results found",
    "tab.prevPage": "Previous page",
    "tab.nextPage": "Next page",
    "filter.coauth": "Min. co-authorship",
    "filter.sharedAuthors": "Min. shared authors",
    "filter.yearMax": "Up to year",
    "filter.showNoDept": "Show without department",
    "filter.edgeZoom": "Edge visibility threshold",
    "filter.showRegions": "Department regions",
    "filter.regionZoom": "Region visibility threshold",
    "filter.regionMinNodes": "Min. nodes per region",
    "brand.backToMenu": "← Menu",
    "section.filters": "Filters",
    "section.quickSearch": "Quick Search",
    "start.badge": "Open ITMO project",
    "start.title": "ITMO co-authorship and open-source code map",
    "start.subtitle":
      "Publications, authors and departments of ITMO — and the open-source repositories linked to them on GitHub.",
    "start.cta": "View the map",
    "start.loading": "Loading data…",
    "start.rendering": "Rendering the graph…",
    "start.error": "Loading error.",
    "start.errorFetch": "Failed to load the graph data",
    "start.errorFetchHint":
      'Make sure "python -m new_generate.graph_builder" has generated the files in data/gui/private.',
    "start.errorRender":
      "The graph data loaded, but something failed while rendering it. See the browser console (F12) for details.",
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

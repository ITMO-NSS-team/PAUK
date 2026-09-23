// Слой "features" — панель с информацией о том, что сейчас выбрано.
// Умеет показывать карточку узла (автор/репозиторий/публикация), ребра
// (кто с кем связан и с каким весом), департамента (сводные числа из
// самого Department) и карточку "Обзор" по умолчанию, когда вообще ничего
// не выбрано — сводка ЗАВИСИТ от активной вкладки (числа сущностей,
// заполняемость её detail-полей), не один общий набор чисел на все три
// графа, см. {@link renderOverview}.

import type {
  Affiliation,
  AuthorDetail,
  AuthorNode,
  GraphData,
  PubDetail,
  PubNode,
  RepoDetail,
  RepoNode,
} from "../contracts/graph";
import { DATA_CONFIG, PANEL_CONFIG } from "../core/config";
import {
  buildAuthorPubIndex,
  buildAuthorRepoIndex,
  buildCoauthIndex,
  buildDeptEdgeIndex,
  buildRepoAuthorIndex,
  buildRepoPubIndex,
  githubProfileUrl,
  githubShortPath,
  groupsById,
  indexByKey,
  nodeLabel,
} from "../core/data";
import { createLoadingIndicator, requireElement } from "../core/dom";
import { kindLabel, localize, t } from "../core/i18n";
import { TAB_FOR_KIND, type AppState, type Selection, type Store } from "../core/state";

/** Строка карточки ещё не может показать значение — соответствующий
 * `*Detail`-файл (см. {@link AuthorDetail}/{@link RepoDetail}) не домержился
 * в карту, переданную {@link mountPanel}. Отдельный маркер, а не пустая
 * строка — пустая строка означала бы "поле реально пустое", а это "мы пока
 * не знаем, что там". */
const LOADING: unique symbol = Symbol("panel-row-loading");

/** Значение строки карточки — обычный текст, список кликабельных ВНЕШНИХ
 * ссылок ({@link PanelLink}, DOI/GitHub/ORCID/код — открываются в новой
 * вкладке), список кликабельных ссылок на ДРУГИЕ СУЩНОСТИ ГРАФА
 * ({@link PanelEntityRef} — соавторы, публикации, департаменты и т.п.:
 * клик делает эту сущность новым store.selection, не открывает вкладку),
 * либо {@link LOADING}, пока detail ещё не пришёл. */
type PanelRowValue = string | PanelLink[] | PanelEntityRef[] | PanelList | typeof LOADING;
/** Одна кликабельная ВНЕШНЯЯ ссылка в строке карточки — всегда открывается в новой вкладке ({@link buildCard}). */
interface PanelLink {
  kind: "link";
  href: string;
  text: string;
  /** Необязательный некликабельный суффикс, например годы аффилиации. */
  meta?: string;
}
/**
 * Длинное поле-список: подпись во всю ширину, под ней по элементу на строку,
 * первые {@link PANEL_CONFIG.listLimit} и кнопка "ещё N" для остальных.
 */
interface PanelList {
  kind: "list";
  items: (string | PanelText | PanelLink | PanelEntityRef)[];
}
/** Некликабельный пункт {@link PanelList} с серым суффиксом — оформлен так же, как {@link PanelLink}, только без ссылки. */
interface PanelText {
  kind: "text";
  text: string;
  meta?: string;
}
/**
 * Кликабельная ссылка на ДРУГУЮ сущность ЭТОГО ЖЕ графа (не внешний URL) —
 * узел или департамент, клик по которой делает её новым `store.selection`
 * (карта подлетает к ней, панель показывает уже её карточку — та же
 * механика, что у клика по узлу на карте или по строке списка вкладки, см.
 * map/build.ts::flyToSelection). Ключевая часть "прослеживать связи": не
 * просто СКАЗАТЬ, кто с кем связан, а дать перейти по этой связи одним кликом.
 */
interface PanelEntityRef {
  kind: "ref";
  /** Куда положить как `store.selection` по клику. */
  selection: Extract<Selection, { kind: "node" } | { kind: "dept" }>;
  /** Кликабельная подпись — сама ссылка. */
  label: string;
  /** Необязательный некликабельный суффикс справа от подписи, например роль в репозитории ("(maintainer)"). */
  meta?: string;
}
/** Одна строка карточки: `[подпись, значение]`. */
type PanelRow = [label: string, value: PanelRowValue];
/** Раздел карточки: заголовок (`null` — без заголовка) и его строки. */
interface PanelSection {
  title: string | null;
  rows: PanelRow[];
}

/** Карточка из одного раздела без заголовка — для карточек, которые на разделы не делятся. */
function untitled(rows: PanelRow[]): PanelSection[] {
  return [{ title: null, rows }];
}

/**
 * Строит ссылку на DOI публикации — как и в старом GUI (`search.js`): если
 * `doi` уже пришёл полным URL вида `https://doi.org/...`, префикс не
 * задваивается.
 *
 * Отдельной проверки схемы (как у {@link codeLink}) не требует: схема
 * `"https://doi.org/"` всегда захардкожена нами, значение подставляется
 * только в путь — оно физически не может подменить схему ссылки.
 *
 * @param doi - DOI публикации, с префиксом `https://doi.org/` или без него.
 * @returns Ссылка с полным `https://doi.org/...` в `href` и исходным `doi` в `text`.
 *
 * @example
 * doiLink("10.1000/xyz123"); // { href: "https://doi.org/10.1000/xyz123", text: "10.1000/xyz123" }
 * doiLink("https://doi.org/10.1000/xyz123"); // тот же результат — префикс не задвоился
 */
function doiLink(doi: string): PanelLink {
  return {
    kind: "link",
    href: `https://doi.org/${doi.replace(/^https?:\/\/doi\.org\//, "")}`,
    text: doi,
  };
}

/**
 * Строит ссылку на GitHub-профиль автора по его логину. Использует общий
 * {@link githubProfileUrl} из `core/data.ts`, а не собственный литерал
 * `"https://github.com/"` — так сборка ссылки (здесь) и укорачивание уже
 * готовой ссылки (см. {@link codeLink}) не могут разойтись между собой.
 *
 * @param username - логин автора на GitHub (`AuthorDetail.github`).
 * @returns Ссылка на профиль с логином в `text`.
 *
 * @example
 * githubLink("ivanov-ii"); // { href: "https://github.com/ivanov-ii", text: "ivanov-ii" }
 */
function githubLink(username: string): PanelLink {
  return { kind: "link", href: githubProfileUrl(username), text: username };
}

/**
 * Строит ссылку на ORCID автора по его id. Схема `"https://orcid.org/"`
 * захардкожена нами — та же логика безопасности, что и у {@link doiLink}.
 *
 * @param id - ORCID id автора (`AuthorDetail.orcid`), формата `"0000-0001-2345-6789"`.
 * @returns Ссылка на страницу ORCID с id в `text`.
 *
 * @example
 * orcidLink("0000-0001-2345-6789"); // { href: "https://orcid.org/0000-0001-2345-6789", text: "0000-0001-2345-6789" }
 */
function orcidLink(id: string): PanelLink {
  return { kind: "link", href: `https://orcid.org/${id}`, text: id };
}

/**
 * Проверяет схему произвольного URL, пришедшего из данных (не построенного
 * нами самими), и возвращает его как есть, если схема безопасна.
 *
 * Общая часть {@link codeLink} и {@link googleScholarLink}/{@link
 * openalexUrlLink} — все три поля (`code_url`, `google_scholar`,
 * `openalex_url`) приходят из внешнего харвестинга (GitHub/Google
 * Scholar/OpenAlex), а не собираются нами из проверенных частей, как
 * {@link doiLink}/{@link githubLink}/{@link orcidLink} — без проверки
 * схемы значение вроде `"javascript:alert(1)"` привело бы к выполнению
 * произвольного кода по клику (XSS). Если схема не `http:`/`https:`, или
 * `url` вообще не парсится как URL, возвращается безопасный `"about:blank"`,
 * а в консоль пишется предупреждение — не тихо, чтобы проблема с данными
 * была заметна разработчику.
 *
 * @param url - произвольная ссылка из внешних данных.
 * @param context - имя вызывающей функции, для текста предупреждения в консоли.
 * @returns `url` как есть, если схема `http:`/`https:`, иначе `"about:blank"`.
 */
function safeHref(url: string, context: string): string {
  try {
    const parsed = new URL(url);
    if (parsed.protocol === "http:" || parsed.protocol === "https:") return parsed.toString();
    console.warn(`${context}: недопустимая схема, ссылка заменена на "about:blank": ${url}`);
  } catch {
    console.warn(
      `${context}: значение не распознано как URL, ссылка заменена на "about:blank": ${url}`,
    );
  }
  return "about:blank";
}

/**
 * Строит ссылку на код публикации из `PubDetail.code_url`, с проверкой
 * схемы (см. {@link safeHref}).
 *
 * @param url - произвольная ссылка на код из `PubDetail.code_url`.
 * @returns Ссылка с проверенной схемой в `href` и коротким путём без
 *   `"https://github.com/"` в `text` (см. {@link githubShortPath}).
 *
 * @example
 * codeLink("https://github.com/example-org/graph-toolkit");
 * // { href: "https://github.com/example-org/graph-toolkit", text: "example-org/graph-toolkit" }
 */
function codeLink(url: string): PanelLink {
  return { kind: "link", href: safeHref(url, "codeLink"), text: githubShortPath(url) };
}

/**
 * Строит ссылку на профиль Google Scholar из `AuthorDetail.google_scholar`
 * (уже полный URL, в отличие от `orcid`/`openalex_id`, которые
 * приходят голыми id) — с проверкой схемы (см. {@link safeHref}). Текст
 * ссылки — фиксированное "Google Scholar", а не сам URL: он длинный и с
 * query-параметрами, нечитаем в узкой карточке.
 *
 * @param url - `AuthorDetail.google_scholar`.
 */
function googleScholarLink(url: string): PanelLink {
  return { kind: "link", href: safeHref(url, "googleScholarLink"), text: "Google Scholar" };
}

/**
 * Строит ссылку на страницу публикации на OpenAlex из `PubDetail.openalex_url`
 * (уже полный URL) — та же схема-проверка, что и у {@link googleScholarLink},
 * той же причине (внешний харвестинг, не наша сборка ссылки).
 *
 * @param url - `PubDetail.openalex_url`.
 */
function openalexUrlLink(url: string): PanelLink {
  return { kind: "link", href: safeHref(url, "openalexUrlLink"), text: "OpenAlex" };
}

/**
 * Строит ссылку на профиль автора на OpenAlex по его id
 * (`AuthorDetail.openalex_id`, например `"A5120308655"`) — схема
 * `"https://openalex.org/"` захардкожена нами, как и у {@link orcidLink},
 * проверка не нужна.
 *
 * @param id - `AuthorDetail.openalex_id`.
 */
function openalexIdLink(id: string): PanelLink {
  return { kind: "link", href: `https://openalex.org/${id}`, text: id };
}

/**
 * Строит ссылку `mailto:` из `AuthorDetail.email` — схема захардкожена
 * нами, проверка не нужна.
 *
 * @param email - адрес почты.
 */
function emailLink(email: string): PanelLink {
  return { kind: "link", href: `mailto:${email}`, text: email };
}

const AFFILIATION_SOURCE_LABELS: Record<string, string> = { openalex: "OpenAlex", orcid: "ORCID" };

/**
 * Склеивает записи аффилиаций с одинаковым названием (одна и та же
 * организация приходит отдельно от OpenAlex и от ORCID) в один пункт списка:
 * название (ссылка на ror.org, если ROR известен), диапазон лет и источники.
 * Сверху — самые недавние.
 *
 * @param affiliations - `AuthorDetail.affiliations`.
 * @returns Пункты для {@link PanelList}.
 *
 * @example
 * // [{name: "ITMO", ror: "04txgxn49", years: [2023, 2024], source: "openalex"},
 * //  {name: "ITMO", ror: "04txgxn49", years: [2019], source: "orcid"}]
 * // -> [{ kind: "link", href: "https://ror.org/04txgxn49", text: "ITMO", meta: "2019–2024 · OpenAlex, ORCID" }]
 */
function affiliationItems(affiliations: Affiliation[]): (PanelLink | PanelText)[] {
  const byName = new Map<string, { ror: string; years: number[]; sources: Set<string> }>();
  for (const aff of affiliations) {
    const entry = byName.get(aff.name) ?? { ror: "", years: [], sources: new Set<string>() };
    entry.ror ||= aff.ror;
    entry.years.push(...aff.years);
    entry.sources.add(AFFILIATION_SOURCE_LABELS[aff.source] ?? aff.source);
    byName.set(aff.name, entry);
  }

  return [...byName.entries()]
    .map(([name, { ror, years, sources }]) => ({
      name,
      ror,
      first: years.length > 0 ? Math.min(...years) : null,
      last: years.length > 0 ? Math.max(...years) : null,
      sources: [...sources],
    }))
    .sort((a, b) => (b.last ?? -Infinity) - (a.last ?? -Infinity))
    .map(({ name, ror, first, last, sources }): PanelLink | PanelText => {
      const yearsText = first === null ? "" : first === last ? String(first) : `${first}–${last}`;
      const meta = [yearsText, sources.join(", ")].filter(Boolean).join(" · ");
      return ror
        ? { kind: "link", href: `https://ror.org/${encodeURIComponent(ror)}`, text: name, meta }
        : { kind: "text", text: name, meta };
    });
}

/**
 * Форматирует служебную ISO-метку времени (`created_at`/`updated_at`) под
 * язык интерфейса. Нераспознанная строка возвращается как есть.
 *
 * @param value - ISO-строка, например `"2026-08-14T10:23:45.123Z"`.
 * @param lang - язык интерфейса.
 */
function formatTimestamp(value: string, lang: AppState["lang"]): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString(lang === "ru" ? "ru-RU" : "en-GB", {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  });
}

/**
 * Подключает панель информации: подписывается на Store и перерисовывает
 * содержимое каждый раз, когда меняется `state.selection` ИЛИ
 * `state.lang` — оба поля читаются в одном `render()`, поэтому одной
 * подписки достаточно, без отдельной логики "что именно изменилось".
 *
 * При монтировании один раз строит все нужные обратные индексы
 * (`indexByKey`, `buildAuthorPubIndex` и т.д.) — они не меняются, пока не
 * поменялись сами `data`, поэтому пересчитывать их на каждый рендер не
 * нужно, только на каждый клик искать в уже готовых структурах.
 *
 * @param store - Store приложения.
 * @param data - данные графа.
 * @param pubDetails - карта деталей публикаций (настоящие названия/DOI/код публикаций).
 * @param authorDetails - карта личных данных авторов (степень, GitHub, ORCID, варианты имени) — отдельно от `AuthorNode`, см. `contracts/graph.ts::AuthorDetail`.
 * @param repoDetails - карта описаний/лицензий/типа владельца репозиториев — отдельно от `RepoNode`, см. `contracts/graph.ts::RepoDetail`.
 * @returns Функция отписки (unmount) от Store.
 */
export function mountPanel(
  store: Store<AppState>,
  data: GraphData,
  pubDetails: Map<string, PubDetail>,
  authorDetails: Map<string, AuthorDetail>,
  repoDetails: Map<string, RepoDetail>,
): () => void {
  const container = requireElement("panel");

  // ponytail: static PDFs + file list next to the site data, move into repos-detail.json if this stays
  const reportFiles = new Map<string, string>();
  fetch(DATA_CONFIG.reportsIndexUrl)
    .then((response) => (response.ok ? (response.json() as Promise<string[]>) : []))
    .then((files) => {
      for (const file of files) reportFiles.set(file.toLowerCase(), file);
      store.notify();
    })
    .catch((error: unknown) => console.warn("reports/index.json:", error));

  function reportLinksOf(repoUrl: string, lang: AppState["lang"]): PanelLink[] {
    const name = repoUrl.replace(/\/+$/, "").split("/").pop()?.toLowerCase() ?? "";
    const links: PanelLink[] = [];
    for (const [suffix, label] of [
      ["_work_summary.pdf", t("field.workSummary", lang)],
      ["_report.pdf", t("field.report", lang)],
    ] as const) {
      const file = reportFiles.get(name + suffix);
      if (file)
        links.push({ kind: "link", href: `/reports/${encodeURIComponent(file)}`, text: label });
    }
    return links;
  }

  // Строится один раз при монтировании, а не на каждый рендер — поиск по
  // ключу должен быть мгновенным, а не пересчитывать индекс на каждый клик.
  const index = indexByKey(data);
  const deptById = new Map(data.departments.map((dept) => [dept.id, dept]));
  const groups = groupsById(data);
  const repoGroupById = new Map((data.repo_groups ?? []).map((group) => [group.id, group]));
  const repoEdgeVia = new Map(
    data.repo_edges.flatMap((edge) => [
      [`${edge.s}\u0000${edge.t}`, edge.via ?? []],
      [`${edge.t}\u0000${edge.s}`, edge.via ?? []],
    ]),
  );
  const { authorPubs, pubAuthors } = buildAuthorPubIndex(data);
  const coauthIndex = buildCoauthIndex(data);
  const authorRepoIndex = buildAuthorRepoIndex(data);
  const deptEdgeIndex = buildDeptEdgeIndex(data);
  const repoAuthorIndex = buildRepoAuthorIndex(data);
  const { repoPubs: repoPubIndex, pubRepos: pubRepoIndex } = buildRepoPubIndex(data);

  /**
   * Строит кликабельные ссылки на департаменты по списку их id — для строки
   * "связанные департаменты" в карточке департамента. Клик по любому из них
   * делает этот департамент новым `store.selection` — "прослеживать связи"
   * между департаментами так же просто, как между узлами.
   *
   * @param ids - список id департаментов.
   * @param lang - язык интерфейса.
   * @returns Ссылки в том же порядке, что и `ids`.
   */
  function deptRefsOf(ids: number[], lang: AppState["lang"]): PanelEntityRef[] {
    return ids.map((id) => {
      const dept = groups.get(id);
      const label = dept ? localize(dept.name, dept.name_en, lang) : String(id);
      return { kind: "ref", selection: { kind: "dept", id }, label };
    });
  }

  /**
   * Строит кликабельные ссылки на узлы графа по списку их ключей — общая
   * функция для строк "общие публикации"/"общие авторы" в карточке ребра и
   * всех похожих списков в карточках автора/репозитория/публикации/обзора.
   * Клик по любой из них делает этот узел новым `store.selection` — камера
   * подлетает к нему, а панель показывает уже его карточку (та же механика,
   * что у клика по узлу на карте, см. map/build.ts::flyToSelection) —
   * ключевая часть "прослеживать связи одним кликом", а не просто видеть
   * список имён.
   *
   * @param keys - список ключей узлов (авторов, репозиториев или публикаций).
   * @param lang - язык интерфейса.
   * @returns Ссылки в том же порядке, что и `keys`. Ключ, которого нет в
   *   `index` (не должно случаться на согласованных данных), используется
   *   как подпись как есть, а не отбрасывается — но тогда клик по нему
   *   ни к чему не приведёт (mountReactiveGraph сам обнулит несуществующий
   *   выбор, см. map/build.ts::selectionExistsIn).
   */
  function entityRefsOf(keys: string[], lang: AppState["lang"]): PanelEntityRef[] {
    return keys.map((key) => {
      const node = index.get(key);
      const label = node ? nodeLabel(node, lang, pubDetails) : key;
      return { kind: "ref", selection: { kind: "node", key }, label };
    });
  }

  /**
   * Отбирает из списка ключей только те, что резолвятся в публикацию, и
   * возвращает их недавние сверху (год по убыванию), обрезано до
   * {@link PANEL_CONFIG.listLimit} — без этого список на реальных данных
   * (у активного автора/репозитория может быть сотни публикаций) не
   * поместился бы в небольшую карточку. Общая часть {@link recentPubKeysOf}
   * и {@link repoPubKeysOf} — отличаются только тем, откуда берут исходный
   * список ключей (публикации автора vs публикации репозитория).
   *
   * @param pubKeys - произвольный список ключей (не обязательно только публикаций).
   * @returns До `PANEL_CONFIG.listLimit` ключей публикаций из `pubKeys`, от новых к старым.
   */
  function recentPubKeysFrom(pubKeys: string[]): string[] {
    return pubKeysByYear(pubKeys).slice(0, PANEL_CONFIG.listLimit);
  }

  /**
   * Все ключи публикаций из `pubKeys`, от новых к старым, без обрезки —
   * для списков с кнопкой "ещё N" ({@link PanelList}).
   *
   * @param pubKeys - произвольный список ключей (не обязательно только публикаций).
   */
  function pubKeysByYear(pubKeys: string[]): string[] {
    return pubKeys
      .map((key) => index.get(key))
      .filter((node): node is PubNode => node?.kind === "pub")
      .sort((a, b) => (b.year ?? -Infinity) - (a.year ?? -Infinity))
      .map((node) => node.key);
  }

  /**
   * Возвращает ключи соавторов автора, по убыванию суммарного веса связи
   * (числа совместных публикаций).
   *
   * @param authorKey - ключ автора.
   * @returns Ключи всех соавторов, от самых частых к редким.
   */
  function topCoauthorKeys(authorKey: string): string[] {
    return [...(coauthIndex.get(authorKey) ?? new Map<string, number>()).entries()]
      .sort(([, weightA], [, weightB]) => weightB - weightA)
      .map(([key]) => key);
  }

  /**
   * Возвращает ключи репозиториев автора, по убыванию звёзд (как и в
   * старом GUI).
   *
   * @param authorKey - ключ автора.
   * @returns Ключи всех репозиториев автора, от самых популярных к менее популярным.
   */
  function authorRepoKeysOf(authorKey: string): string[] {
    return (authorRepoIndex.get(authorKey) ?? [])
      .map((key) => index.get(key))
      .filter((node): node is RepoNode => node?.kind === "repo")
      .sort((a, b) => b.stars - a.stars)
      .map((node) => node.key);
  }

  /**
   * Строит кликабельные ссылки на участников репозитория, с ролью
   * некликабельным суффиксом (например, `"Иванов И.И. (maintainer)"` —
   * кликабельно только "Иванов И.И."), обрезано до {@link PANEL_CONFIG.listLimit}.
   * Отдельная функция, а не {@link entityRefsOf}: нужно дописать роль после
   * имени, а не только саму подпись узла.
   *
   * @param repoKey - ключ репозитория.
   * @param lang - язык интерфейса.
   * @returns Ссылки участников с ролями в `meta`.
   */
  function repoContributorRefsOf(repoKey: string, lang: AppState["lang"]): PanelEntityRef[] {
    return (repoAuthorIndex.get(repoKey) ?? []).slice(0, PANEL_CONFIG.listLimit).map((edge) => {
      const author = index.get(edge.t);
      const label = author ? nodeLabel(author, lang, pubDetails) : edge.t;
      return {
        kind: "ref",
        selection: { kind: "node", key: edge.t },
        label,
        meta: `(${edge.role})`,
      };
    });
  }

  /**
   * Возвращает ключи публикаций, связанных с репозиторием, недавние
   * сверху, обрезано до {@link PANEL_CONFIG.listLimit}.
   *
   * @param repoKey - ключ репозитория.
   * @returns До `PANEL_CONFIG.listLimit` ключей публикаций, от новых к старым.
   */
  function repoPubKeysOf(repoKey: string): string[] {
    return recentPubKeysFrom(repoPubIndex.get(repoKey) ?? []);
  }

  /** Скрывает панель и очищает её содержимое — для случая рассинхрона данных (см. `render()`) или отсутствующего selection. */
  function hide(): void {
    container.hidden = true;
    container.replaceChildren();
  }

  /**
   * Показывает панель с готовой карточкой.
   *
   * @param title - заголовок карточки (`<h3>`, например имя автора или "Обзор").
   * @param kind - короткий бейдж вида сущности рядом с заголовком (например
   *   "Автор"/"Департамент"/"Публикации" для "Обзора" текущей вкладки) —
   *   после того, как почти любая сущность стала кликабельной ссылкой на
   *   другую (см. {@link PanelEntityRef}), легко потерять, на карточку
   *   КАКОГО вида сущности только что перепрыгнули.
   * @param sections - разделы карточки со строками, см. {@link PanelSection}.
   * @param showBack - показывать ли кнопку "← Обзор" — не для самого
   *   "Обзора" (там уже некуда возвращаться), для всех остальных карточек.
   * @param subtitle - см. {@link PanelCardOptions.subtitle} — имя того же
   *   автора на ВТОРОМ языке, мельче и серым под заголовком; сейчас передаёт
   *   только карточка автора, `null`/не задано — подзаголовка нет.
   * @param extra - см. {@link PanelCardOptions.extra} — сейчас используется
   *   только "Обзором" для графиков ({@link buildBarChart}), поэтому
   *   необязательный: карточки узла/ребра/департамента его не передают.
   */
  function show(
    title: string,
    kind: string,
    sections: PanelSection[],
    showBack: boolean,
    subtitle?: string | null,
    extra?: HTMLElement | null,
  ): void {
    container.hidden = false;
    container.replaceChildren(
      buildCard({
        title,
        kind,
        sections,
        lang: store.get().lang,
        // "← Обзор"/"← Overview" — null для самого "Обзора" (там уже
        // некуда возвращаться), готовая локализованная строка для всех
        // остальных карточек.
        backLabel: showBack ? `← ${t("overview.title", store.get().lang)}` : null,
        // Клик по PanelEntityRef внутри карточки пишет новую сущность прямо
        // в store.selection, точно так же, как клик по узлу на карте
        // (features/selection.ts) или по строке списка вкладки. Ссылки на
        // сущность ДРУГОГО вида (например, "публикации" на карточке
        // автора — это узлы-публикации, а не узлы-авторы) требуют ЕЩЁ и
        // сменить tab — иначе selection указывал бы на узел, которого нет
        // в графе ТЕКУЩЕЙ вкладки, и flyToSelection() тихо не находил бы
        // координаты (renderer.getNodeDisplayData() возвращает undefined
        // для несуществующего узла) — камера не подлетала бы вовсе, хотя
        // сама карточка новой сущности показывалась бы нормально: та же
        // логика, что и в features/globalSearch.ts (TAB_FOR_KIND). У
        // "dept" смены вкладки не нужно — якоря департаментов есть в
        // графе каждой из трёх вкладок.
        onSelectRef: (selection) => {
          if (selection?.kind === "node") {
            const node = index.get(selection.key);
            if (node) {
              store.set({ tab: TAB_FOR_KIND[node.kind], selection });
              return;
            }
          }
          store.set({ selection });
        },
        onBack: () => store.set({ selection: null }),
        subtitle,
        extra,
      }),
    );
  }

  /**
   * Строит текст строки-процента заполненности поля. {@link LOADING}, пока
   * detail-файл ещё вообще не начал приходить (`total === 0`) — денежным
   * является число сущностей, чей detail УЖЕ пришёл, а не общее число
   * сущностей вкладки: до того, как соответствующий `*-detail.json`
   * домержился целиком (см. `app/main.ts::loadDetailsInto`), знаменатель
   * "общее число" давал бы искусственно растущий с каждым fetch процент, а
   * не реальную заполненность поля среди уже известных записей.
   *
   * @param count - число сущностей, у которых поле реально заполнено.
   * @param total - число сущностей, чей detail уже пришёл (знаменатель).
   */
  function completionRow(count: number, total: number): PanelRowValue {
    return total === 0 ? LOADING : `${Math.round((count / total) * 100)}%`;
  }

  /**
   * Рисует карточку "Обзор" по умолчанию, когда ничего не выбрано — сводка
   * по текущей активной ВКЛАДКЕ (в отличие от старой версии, где обзор был
   * один общий на все три графа): топ-10 сущностей вкладки и заполняемость
   * её detail-полей — то, что реально интересно про "авторов" отличается
   * от того, что интересно про "публикации" или "репозитории", общая
   * сводка на четыре числа не показывала ничего специфичного ни для одной
   * из вкладок.
   *
   * @param state - текущее состояние приложения (`tab` выбирает вид сводки, `lang` — язык).
   */
  function renderOverview(state: AppState): void {
    const { tab, lang } = state;
    // Какую вкладку резюмирует "Обзор" — в бейдж рядом с заголовком, а не
    // только в подсветку кнопки вкладки в сайдбаре: панель может быть
    // единственным, на что смотрят в моменте (например, после долгой серии
    // переходов по кликабельным ссылкам).
    const tabKind =
      tab === 1 ? t("tab.authors", lang) : tab === 2 ? t("tab.repos", lang) : t("tab.pubs", lang);

    if (tab === 1) {
      // Все авторы, ИТМО и внешние, независимо от фильтра на карте.
      const authors: AuthorNode[] = data.authors;
      const external = authors.filter((a) => a.is_itmo === false).length;
      const avgPubs =
        authors.length > 0
          ? (authors.reduce((sum, a) => sum + a.pubs_count, 0) / authors.length).toFixed(1)
          : "0";

      let withDetail = 0;
      let withOrcid = 0;
      let withGithub = 0;
      let withEmail = 0;
      for (const author of authors) {
        const detail = authorDetails.get(author.key);
        if (!detail) continue;
        withDetail++;
        if (detail.orcid) withOrcid++;
        if (detail.github) withGithub++;
        if (detail.email) withEmail++;
      }

      return show(
        t("overview.title", lang),
        tabKind,
        untitled([
          [t("field.authorsCount", lang), String(authors.length)],
          [t("overview.itmoAuthors", lang), String(authors.length - external)],
          [t("overview.externalAuthors", lang), String(external)],
          [t("field.deptsCount", lang), String(data.departments.length)],
          [t("overview.avgPubsPerAuthor", lang), avgPubs],
          [t("field.orcid", lang), completionRow(withOrcid, withDetail)],
          [t("field.github", lang), completionRow(withGithub, withDetail)],
          [t("field.email", lang), completionRow(withEmail, withDetail)],
        ]),
        false,
        null,
        authorsByDeptChart(authors, lang),
      );
    }

    if (tab === 2) {
      const repos: RepoNode[] = data.repos;

      let withReadme = 0;
      let withLicense = 0;
      for (const detail of repoDetails.values()) {
        if (detail.has_readme) withReadme++;
        if (detail.license) withLicense++;
      }

      return show(
        t("overview.title", lang),
        tabKind,
        untitled([
          [t("field.reposCount", lang), String(repos.length)],
          [t("field.hasReadme", lang), completionRow(withReadme, repoDetails.size)],
          [t("field.license", lang), completionRow(withLicense, repoDetails.size)],
        ]),
        false,
        null,
        reposByStarsChart(repos, lang),
      );
    }

    // tab === 3
    const pubs: PubNode[] = data.pubs;
    const withKnownYear = pubs.filter((p) => p.year !== null).length;

    let withDoi = 0;
    let withAbstract = 0;
    for (const detail of pubDetails.values()) {
      if (detail.doi) withDoi++;
      if (detail.abstract) withAbstract++;
    }

    return show(
      t("overview.title", lang),
      tabKind,
      untitled([
        [t("field.pubsCount", lang), String(pubs.length)],
        [t("overview.knownYear", lang), completionRow(withKnownYear, pubs.length)],
        [t("field.doi", lang), completionRow(withDoi, pubDetails.size)],
        [t("field.abstract", lang), completionRow(withAbstract, pubDetails.size)],
      ]),
      false,
      null,
      pubsByYearChart(pubs, lang),
    );
  }

  /**
   * График "Обзора" вкладки авторов — число авторов по департаментам, топ
   * {@link PANEL_CONFIG.chartBars} по величине. НЕ то же самое, что убранный
   * топ-10 конкретных авторов (прямая просьба его убрать) — здесь ось
   * категорий это ДЕПАРТАМЕНТЫ, а не имена людей, распределение, а не рейтинг сущностей.
   *
   * @param authors - узлы-авторы текущих данных.
   * @param lang - язык для названия департамента.
   */
  function authorsByDeptChart(authors: AuthorNode[], lang: AppState["lang"]): HTMLElement | null {
    const deptById = new Map(data.departments.map((dept) => [dept.id, dept]));
    const countByDept = new Map<number, number>();
    for (const author of authors) {
      countByDept.set(author.dept, (countByDept.get(author.dept) ?? 0) + 1);
    }

    const bars = [...countByDept.entries()]
      .sort((a, b) => b[1] - a[1])
      .slice(0, PANEL_CONFIG.chartBars)
      .map(([deptId, count]) => {
        const dept = deptById.get(deptId);
        return {
          label: dept ? localize(dept.name, dept.name_en, lang) : t("field.unknownDept", lang),
          value: count,
        };
      });

    return buildBarChart(t("chart.authorsByDept", lang), bars);
  }

  /**
   * График "Обзора" вкладки публикаций — число публикаций по году, в
   * ХРОНОЛОГИЧЕСКОМ порядке (не по величине и без обрезки {@link
   * PANEL_CONFIG.chartBars} — это временной ряд, а не рейтинг: обрезать
   * его означало бы выкинуть часть истории, а не "менее важные" столбцы).
   * Публикации с неизвестным годом (`year === null`) в график не попадают —
   * см. {@link field.yearUnknown} рядом в тех же полях "Обзора".
   *
   * @param pubs - узлы-публикации текущих данных.
   * @param lang - язык заголовка графика.
   */
  function pubsByYearChart(pubs: PubNode[], lang: AppState["lang"]): HTMLElement | null {
    const countByYear = new Map<number, number>();
    for (const pub of pubs) {
      if (pub.year === null) continue;
      countByYear.set(pub.year, (countByYear.get(pub.year) ?? 0) + 1);
    }

    const bars = [...countByYear.entries()]
      .sort((a, b) => a[0] - b[0])
      .map(([year, count]) => ({ label: String(year), value: count }));

    return buildBarChart(t("chart.pubsByYear", lang), bars);
  }

  /**
   * Границы корзин {@link reposByStarsChart} — не равномерный шаг, а
   * примерно логарифмический: звёзды на реальных данных распределены очень
   * неравномерно (пара репозиториев с десятками тысяч звёзд, основная масса
   * — единицы), равномерные корзины оставили бы почти всё в одной "0".
   */
  const STAR_BUCKETS: { max: number; label: string }[] = [
    { max: 0, label: "0" },
    { max: 9, label: "1–9" },
    { max: 99, label: "10–99" },
    { max: 999, label: "100–999" },
    { max: Infinity, label: "1000+" },
  ];

  /**
   * График "Обзора" вкладки репозиториев — число репозиториев по корзинам
   * звёзд ({@link STAR_BUCKETS}), не топ конкретных репозиториев по
   * звёздам (то как раз и был убранный топ-10) — распределение, а не рейтинг.
   *
   * @param repos - узлы-репозитории текущих данных.
   * @param lang - язык заголовка графика.
   */
  function reposByStarsChart(repos: RepoNode[], lang: AppState["lang"]): HTMLElement | null {
    const countByBucket = new Map<string, number>(STAR_BUCKETS.map((bucket) => [bucket.label, 0]));
    for (const repo of repos) {
      const bucket = STAR_BUCKETS.find((b) => repo.stars <= b.max);
      if (bucket) countByBucket.set(bucket.label, (countByBucket.get(bucket.label) ?? 0) + 1);
    }

    const bars = STAR_BUCKETS.map((bucket) => ({
      label: bucket.label,
      value: countByBucket.get(bucket.label) ?? 0,
    }));
    return buildBarChart(t("chart.reposByStars", lang), bars);
  }

  /**
   * Пересобирает содержимое панели под текущее состояние — вызывается
   * сразу при монтировании и на каждое изменение Store. Показывает один
   * из четырёх видов карточки:
   * - "Обзор" (см. {@link renderOverview}), если `selection === null`;
   * - карточку узла (автор/репозиторий/публикация), со своим набором
   *   дополнительных строк для каждого вида;
   * - карточку ребра, с общими публикациями/авторами, если оба конца
   *   ребра одного вида (автор-автор или публикация-публикация);
   * - карточку департамента, со связанными департаментами.
   *
   * Если выбранного узла/ребра/департамента вдруг нет в текущих `data`
   * (рассинхрон, которого не должно случаться на согласованных данных —
   * клик по карте или списку берёт ключ прямо из тех же `data`), панель
   * молча скрывается через {@link hide} вместо показа пустой карточки.
   *
   * @param state - текущее состояние приложения.
   */
  function render(state: AppState): void {
    const { selection, lang } = state;
    if (selection === null) return renderOverview(state);

    if (selection.kind === "node") {
      const node = index.get(selection.key);
      // Ключ выбран, но узла с таким ключом нет в текущих данных — такое
      // не должно происходить (клик по карте берёт key прямо из тех же
      // данных), но если вдруг случится рассинхрон, лучше молча спрятать
      // панель, чем показать пустую карточку.
      if (!node) return hide();

      const dept = deptById.get(node.dept);
      // Для автора заголовок карточки могут поменять ниже (полное имя
      // вместо сокращённой подписи) — для остальных видов узлов остаётся
      // как есть. subtitle — имя на ВТОРОМ языке под заголовком (прямая
      // просьба), тоже только для автора — у репозитория нет `_en`-варианта
      // имени вовсе, у публикации заголовок в принципе на одном языке.
      let title = nodeLabel(node, lang, pubDetails);
      let subtitle: string | null = null;
      const keyRow: PanelRow = [t("field.key", lang), node.key];
      const kindRow: PanelRow = [t("field.kind", lang), kindLabel(node.kind, lang)];
      const deptRow: PanelRow = [
        t("field.dept", lang),
        dept ? localize(dept.name, dept.name_en, lang) : t("field.unknownDept", lang),
      ];
      const rows: PanelRow[] = [keyRow, kindRow, deptRow];
      if (node.kind === "author") {
        // Разделы — по пометкам полей в pauk/cache/export.py: public/связи
        // графа — "Общее", private — "Приватное", id и метки времени — "Служебное".
        const general: PanelRow[] = [
          deptRow,
          [t("field.pubsCount", lang), String(node.pubs_count)],
        ];
        const privateRows: PanelRow[] = [];
        const service: PanelRow[] = [keyRow, kindRow];

        // pauk/gui пишет запись в authors-detail.json для КАЖДОГО автора,
        // даже с пустыми полями, поэтому "записи нет" значит ровно одно:
        // файл ещё не домержился (см. app/main.ts).
        const authorDetail = authorDetails.get(node.key);
        if (authorDetail) {
          // Заголовок — полное имя на нужном языке, если оно известно; под
          // ним имя на втором языке, если оно отличается.
          const fullName = localize(authorDetail.name_ru, authorDetail.name_en, lang);
          if (fullName) {
            title = fullName;
            const otherName = localize(authorDetail.name_en, authorDetail.name_ru, lang);
            if (otherName && otherName !== fullName) subtitle = otherName;
          }
          if (authorDetail.openalex_id)
            general.push([t("field.openalexId", lang), [openalexIdLink(authorDetail.openalex_id)]]);

          if (authorDetail.degree) privateRows.push([t("field.degree", lang), authorDetail.degree]);
          if (authorDetail.github)
            privateRows.push([t("field.github", lang), [githubLink(authorDetail.github)]]);
          if (authorDetail.orcid)
            privateRows.push([t("field.orcid", lang), [orcidLink(authorDetail.orcid)]]);
          if (authorDetail.google_scholar) {
            privateRows.push([
              t("field.googleScholar", lang),
              [googleScholarLink(authorDetail.google_scholar)],
            ]);
          }
          if (authorDetail.email)
            privateRows.push([t("field.email", lang), [emailLink(authorDetail.email)]]);
          if (authorDetail.affiliations.length > 0) {
            privateRows.push([
              t("field.affiliations", lang),
              { kind: "list", items: affiliationItems(authorDetail.affiliations) },
            ]);
          }
          // Раздельно по источнику — см. author_variants() в pauk/gui/graph_builder/nodes.py.
          if (authorDetail.name_variants.openalex.length > 0) {
            privateRows.push([
              t("field.nameVariantsOpenalex", lang),
              { kind: "list", items: authorDetail.name_variants.openalex },
            ]);
          }
          if (authorDetail.name_variants.orcid.length > 0) {
            privateRows.push([
              t("field.nameVariantsOrcid", lang),
              { kind: "list", items: authorDetail.name_variants.orcid },
            ]);
          }

          if (authorDetail.created_at)
            service.push([
              t("field.createdAt", lang),
              formatTimestamp(authorDetail.created_at, lang),
            ]);
          if (authorDetail.updated_at)
            service.push([
              t("field.updatedAt", lang),
              formatTimestamp(authorDetail.updated_at, lang),
            ]);
        } else {
          privateRows.push([t("field.loadingDetails", lang), LOADING]);
        }

        const pubKeys = pubKeysByYear(authorPubs.get(node.key) ?? []);
        if (pubKeys.length > 0) {
          const pubRefs = entityRefsOf(pubKeys, lang).map((ref, i) => {
            const role = authorDetail?.pub_roles?.[pubKeys[i] ?? ""];
            const meta = role
              ? [
                  role.position === null
                    ? ""
                    : t("field.authorPosition", lang).replace("{n}", String(role.position)),
                  role.corresponding ? t("field.corresponding", lang) : "",
                ]
                  .filter(Boolean)
                  .join(" · ")
              : "";
            return meta ? { ...ref, meta } : ref;
          });
          general.push([t("tab.pubs", lang), { kind: "list", items: pubRefs }]);
        }

        const coauthors = topCoauthorKeys(node.key);
        if (coauthors.length > 0) {
          general.push([
            t("field.topCoauthors", lang),
            { kind: "list", items: entityRefsOf(coauthors, lang) },
          ]);
        }

        const authorRepos = authorRepoKeysOf(node.key);
        if (authorRepos.length > 0) {
          general.push([
            t("tab.repos", lang),
            { kind: "list", items: entityRefsOf(authorRepos, lang) },
          ]);
        }

        return show(
          title,
          kindLabel(node.kind, lang),
          [
            { title: t("section.general", lang), rows: general },
            { title: t("section.private", lang), rows: privateRows },
            { title: t("section.service", lang), rows: service },
          ],
          true,
          subtitle,
        );
      }
      if (node.kind === "repo") {
        rows.push([t("field.stars", lang), String(node.stars)]);
        const group = node.group === undefined ? undefined : repoGroupById.get(node.group);
        if (group) rows.push([t(`group.kind.${group.kind}`, lang), deptRefsOf([group.id], lang)]);
        // Тот же приём, что и у автора выше: .has(), потому что запись в
        // repos-detail.json есть у каждого репозитория без исключений.
        if (repoDetails.has(node.key)) {
          const repoDetail = repoDetails.get(node.key);
          if (repoDetail?.description)
            rows.push([t("field.description", lang), repoDetail.description]);
          if (repoDetail?.owner_type)
            rows.push([t("field.ownerType", lang), repoDetail.owner_type]);
          if (repoDetail?.license) rows.push([t("field.license", lang), repoDetail.license]);
          if (repoDetail?.has_readme) rows.push([t("field.hasReadme", lang), "✓"]);
          const reportLinks = repoDetail ? reportLinksOf(repoDetail.url, lang) : [];
          if (reportLinks.length > 0) rows.push([t("field.documents", lang), reportLinks]);
        } else {
          rows.push([t("field.loadingDetails", lang), LOADING]);
        }

        const contributors = repoContributorRefsOf(node.key, lang);
        if (contributors.length > 0)
          rows.push([t("field.contributors", lang), { kind: "list", items: contributors }]);

        const repoPubs = repoPubKeysOf(node.key);
        if (repoPubs.length > 0)
          rows.push([t("tab.pubs", lang), { kind: "list", items: entityRefsOf(repoPubs, lang) }]);
      }
      if (node.kind === "pub") {
        rows.push([
          t("field.year", lang),
          node.year === null ? t("field.yearUnknown", lang) : String(node.year),
        ]);

        const detail = pubDetails.get(node.key);
        if (detail?.doi) rows.push([t("field.doi", lang), [doiLink(detail.doi)]]);
        if (detail?.type) rows.push([t("field.pubType", lang), detail.type]);
        if (detail && detail.fields.length > 0)
          rows.push([t("field.pubFields", lang), detail.fields.join(", ")]);
        if (detail?.abstract) rows.push([t("field.abstract", lang), detail.abstract]);
        if (detail?.openalex_url)
          rows.push([t("field.openalexUrl", lang), [openalexUrlLink(detail.openalex_url)]]);

        // Как и в старом showPubCard(): если публикация связана с нашим
        // собственным репозиторием (repo_pub_edges), показываем ссылку на
        // него ВМЕСТО голого code_url — связь через собственные данные
        // надёжнее внешнего харвестинга, а раз она есть, дублировать её
        // ещё и code_url незачем.
        const pubRepoKeys = (pubRepoIndex.get(node.key) ?? []).slice(0, PANEL_CONFIG.listLimit);
        if (pubRepoKeys.length > 0) {
          rows.push([t("tab.repos", lang), entityRefsOf(pubRepoKeys, lang)]);
        } else if (detail?.has_code && detail.code_url.length > 0) {
          rows.push([t("field.code", lang), detail.code_url.map(codeLink)]);
        }

        const pubAuthorKeys = (pubAuthors.get(node.key) ?? []).slice(0, PANEL_CONFIG.listLimit);
        if (pubAuthorKeys.length > 0)
          rows.push([t("tab.authors", lang), entityRefsOf(pubAuthorKeys, lang)]);
      }

      return show(title, kindLabel(node.kind, lang), untitled(rows), true, subtitle);
    }

    if (selection.kind === "edge") {
      const from = index.get(selection.s);
      const to = index.get(selection.t);
      if (!from || !to) return hide();

      const rows: PanelRow[] = [
        [t("field.edgeFrom", lang), entityRefsOf([from.key], lang)],
        [t("field.edgeTo", lang), entityRefsOf([to.key], lang)],
        [t("field.edgeWeight", lang), String(selection.w)],
      ];

      // Сам вес — это только число; что конкретно за ним стоит, видно только
      // через all_edges. Показываем список, только если он не пуст — как и
      // в старом showEdgeCard(), у ребра без общих публикаций/авторов (или
      // между узлами другого вида, например репозиториями) этой строки нет.
      // Так же, как и у остальных списков в этом файле, режем до
      // PANEL_CONFIG.listLimit — у активных соавторов общих публикаций
      // может быть больше, чем поместится в карточку.
      const via = repoEdgeVia.get(`${from.key}\u0000${to.key}`) ?? [];
      if (via.length > 0)
        rows.push([
          t("field.repoVia", lang),
          via.map((signal) => t(`via.${signal}`, lang)).join(", "),
        ]);
      if (from.kind === "author" && to.kind === "author") {
        const shared = (authorPubs.get(from.key) ?? [])
          .filter((pub) => (authorPubs.get(to.key) ?? []).includes(pub))
          .slice(0, PANEL_CONFIG.listLimit);
        if (shared.length > 0) rows.push([t("field.sharedPubs", lang), entityRefsOf(shared, lang)]);
      } else if (from.kind === "pub" && to.kind === "pub") {
        const shared = (pubAuthors.get(from.key) ?? [])
          .filter((author) => (pubAuthors.get(to.key) ?? []).includes(author))
          .slice(0, PANEL_CONFIG.listLimit);
        if (shared.length > 0)
          rows.push([t("field.sharedAuthors", lang), entityRefsOf(shared, lang)]);
      }

      return show(t("kind.edge", lang), t("kind.edge", lang), untitled(rows), true);
    }

    // selection.kind === "dept"
    const group = repoGroupById.get(selection.id);
    if (group) {
      const members = data.repos
        .filter((repo) => repo.group === group.id)
        .sort((a, b) => b.stars - a.stars)
        .map((repo) => repo.key);
      return show(
        localize(group.name, group.name_en, lang),
        t(`group.kind.${group.kind}`, lang),
        untitled([
          [t("field.reposCount", lang), String(members.length)],
          [t("field.groupWhy", lang), t(`group.why.${group.kind}`, lang)],
          [t("tab.repos", lang), { kind: "list", items: entityRefsOf(members, lang) }],
        ]),
        true,
      );
    }
    const dept = deptById.get(selection.id);
    if (!dept) return hide();

    const rows: PanelRow[] = [
      [t("field.authorsCount", lang), String(dept.n_authors)],
      [t("field.pubsCount", lang), String(dept.n_pubs)],
      [t("field.reposCount", lang), String(dept.n_repos)],
      [t("field.total", lang), String(dept.n)],
    ];

    const relatedIds = [...(deptEdgeIndex.get(dept.id) ?? new Map<number, number>()).entries()]
      .sort(([, weightA], [, weightB]) => weightB - weightA)
      .map(([id]) => id);
    if (relatedIds.length > 0) {
      rows.push([
        t("field.relatedDepts", lang),
        { kind: "list", items: deptRefsOf(relatedIds, lang) },
      ]);
    }

    return show(
      localize(dept.name, dept.name_en, lang),
      kindLabel("dept", lang),
      untitled(rows),
      true,
    );
  }

  render(store.get());
  const unsubscribe = store.subscribe(render);
  return unsubscribe;
}

/** Параметры одной карточки — вход {@link buildCard}. */
interface PanelCardOptions {
  /** Заголовок карточки (например, имя автора или "Обзор"). */
  title: string;
  /**
   * Короткий бейдж вида сущности рядом с заголовком (например
   * "Автор"/"Департамент"/название вкладки для "Обзора") — после того, как
   * почти любая сущность в карточке стала кликабельной ссылкой на другую
   * (см. {@link PanelEntityRef}), легко потерять, на карточку КАКОГО вида
   * сущности только что перепрыгнули, глядя только на список полей.
   */
  kind: string;
  /** Разделы карточки в порядке отображения; пустые разделы не рисуются. */
  sections: PanelSection[];
  /** Язык интерфейса — для подписи кнопки "ещё N" у {@link PanelList}. */
  lang: AppState["lang"];
  /**
   * Текст кнопки "назад к обзору" (например `"← Обзор"`), уже
   * локализованный вызывающим кодом — `null`, если кнопку показывать не
   * нужно (у самого "Обзора" — там уже некуда возвращаться).
   */
  backLabel: string | null;
  /** Вызывается с `PanelEntityRef.selection`, когда кликают по ссылке на другую сущность графа — пишет её в `store.selection` ("прослеживать связи" одним кликом). */
  onSelectRef: (selection: Selection) => void;
  /** Вызывается по клику на кнопку "назад к обзору" (см. `backLabel`). */
  onBack: () => void;
  /**
   * Имя той же сущности на ВТОРОМ языке — рисуется мельче и серым сразу
   * под заголовком (прямая просьба: для EN-интерфейса сверху английское
   * имя, под ним русское, и наоборот). Сейчас передаёт только карточка
   * автора (`name_ru`/`name_en` есть только у {@link AuthorDetail} —
   * у {@link RepoNode} нет `_en`-варианта вовсе, у {@link PubDetail}
   * заголовок в принципе на одном языке). `null`/не задано — подзаголовка нет.
   */
  subtitle?: string | null;
  /**
   * Произвольный DOM-узел, вставляемый ПОСЛЕ списка полей (`<dl>`) — сейчас
   * единственный потребитель это простые графики {@link buildBarChart} в
   * карточке "Обзор" (см. {@link renderOverview}): они не пара "подпись —
   * значение", как остальные строки, поэтому не встроены в {@link PanelRow},
   * а идут отдельным блоком. `null`/не задано — ничего не добавляется.
   */
  extra?: HTMLElement | null;
}

/**
 * Собирает DOM-карточку: (необязательная) кнопка "назад к обзору",
 * заголовок с бейджем вида сущности, список пар "подпись — значение".
 * Только `textContent` для обычного текста и явные `<a>`/`<button>` с
 * фиксированными атрибутами для ссылок — никакого `innerHTML`, данные из
 * графа не должны интерпретироваться как разметка.
 *
 * @param options - см. {@link PanelCardOptions}.
 * @returns `<div class="panel-card">`, ещё не вставленный в DOM.
 */
function buildCard(options: PanelCardOptions): HTMLElement {
  const { title, kind, sections, lang, backLabel, onSelectRef, onBack, subtitle, extra } = options;
  const card = document.createElement("div");
  card.className = "panel-card";

  if (backLabel !== null) {
    const back = document.createElement("button");
    back.type = "button";
    back.className = "panel-back";
    back.textContent = backLabel;
    back.addEventListener("click", onBack);
    card.appendChild(back);
  }

  // Заголовок и имя на втором языке — одним блоком, чтобы подзаголовок стоял
  // вплотную к имени, а не после общего отступа шапки.
  const head = document.createElement("div");
  head.className = "panel-card__head";
  const titles = document.createElement("div");
  titles.className = "panel-card__titles";
  const heading = document.createElement("h3");
  heading.textContent = title;
  titles.appendChild(heading);
  if (subtitle) {
    const sub = document.createElement("div");
    sub.className = "panel-card__subtitle";
    sub.textContent = subtitle;
    titles.appendChild(sub);
  }
  const kindBadge = document.createElement("span");
  kindBadge.className = "panel-kind";
  kindBadge.textContent = kind;
  head.append(titles, kindBadge);
  card.appendChild(head);

  function linkElement(link: PanelLink): HTMLAnchorElement {
    const a = document.createElement("a");
    a.href = link.href;
    a.target = "_blank";
    a.rel = "noopener noreferrer";
    a.textContent = link.text;
    return a;
  }

  // Ссылки на другие сущности графа — <button>, не <a>: клик не открывает
  // вкладку, а меняет store.selection (см. onSelectRef).
  function refElement(ref: PanelEntityRef): HTMLButtonElement {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "panel-entity-ref";
    button.textContent = ref.label;
    button.addEventListener("click", () => onSelectRef(ref.selection));
    return button;
  }

  function listItemElement(item: PanelList["items"][number]): HTMLLIElement {
    const li = document.createElement("li");
    if (typeof item === "string") {
      li.textContent = item;
      return li;
    }
    if (item.kind === "text") li.append(item.text);
    else li.appendChild(item.kind === "link" ? linkElement(item) : refElement(item));
    if (item.meta) {
      const meta = document.createElement("span");
      meta.className = "panel-list__meta";
      meta.textContent = item.meta;
      li.append(" ", meta);
    }
    return li;
  }

  function listElement(value: PanelList): HTMLUListElement {
    const ul = document.createElement("ul");
    ul.className = "panel-list";
    const limit = PANEL_CONFIG.listLimit;
    const hidden = value.items.length - limit;
    let expanded = false;

    // Кнопка переключает список между первыми `limit` пунктами и всеми.
    function renderItems(): void {
      ul.replaceChildren(
        ...(expanded ? value.items : value.items.slice(0, limit)).map(listItemElement),
      );
      if (hidden <= 0) return;

      const toggle = document.createElement("button");
      toggle.type = "button";
      toggle.className = "panel-list__more";
      toggle.textContent = expanded
        ? t("panel.showLess", lang)
        : t("panel.showMore", lang).replace("{n}", String(hidden));
      toggle.addEventListener("click", () => {
        expanded = !expanded;
        renderItems();
      });
      const toggleItem = document.createElement("li");
      toggleItem.appendChild(toggle);
      ul.appendChild(toggleItem);
    }

    renderItems();
    return ul;
  }

  for (const section of sections) {
    if (section.rows.length === 0) continue;

    const sectionEl = document.createElement("section");
    sectionEl.className = "panel-section";
    if (section.title) {
      const sectionTitle = document.createElement("h4");
      sectionTitle.className = "panel-section__title";
      sectionTitle.textContent = section.title;
      sectionEl.appendChild(sectionTitle);
    }

    const list = document.createElement("dl");
    for (const [label, value] of section.rows) {
      const dt = document.createElement("dt");
      dt.textContent = label;

      const dd = document.createElement("dd");
      if (typeof value === "string") {
        dd.textContent = value;
      } else if (value === LOADING) {
        dd.appendChild(createLoadingIndicator());
      } else if (!Array.isArray(value)) {
        // Длинный список — подпись и значения во всю ширину, по элементу на строку.
        dt.classList.add("panel-row--block");
        dd.classList.add("panel-row--block");
        dd.appendChild(listElement(value));
      } else if (value[0]?.kind === "link") {
        // Несколько внешних ссылок в одной строке — через запятую, как и в старом GUI.
        (value as PanelLink[]).forEach((link, i) => {
          if (i > 0) dd.append(", ");
          dd.appendChild(linkElement(link));
        });
      } else {
        (value as PanelEntityRef[]).forEach((ref, i) => {
          if (i > 0) dd.append(", ");
          dd.appendChild(refElement(ref));
          if (ref.meta) dd.append(` ${ref.meta}`);
        });
      }

      list.append(dt, dd);
    }
    sectionEl.appendChild(list);
    card.appendChild(sectionEl);
  }

  if (extra) card.appendChild(extra);

  return card;
}

/**
 * Собирает простой горизонтальный bar-chart из подписанных чисел — DOM +
 * CSS (ширина `<div>` в процентах от максимума ряда), без canvas/SVG и без
 * графической библиотеки: для "прикинуть соотношение на глаз" в карточке
 * "Обзор" (см. {@link renderOverview}) точная координатная система не
 * нужна, а точное число и так подписано рядом текстом.
 *
 * @param title - заголовок раздела над графиком (например, "Публикации по годам").
 * @param bars - пары "подпись — число", В ПОРЯДКЕ ОТОБРАЖЕНИЯ — сортировка
 *   (по величине, по году и т.п.) и обрезка длинных хвостов ({@link
 *   PANEL_CONFIG.chartBars}) — забота вызывающего кода, эта функция просто рисует, что дали.
 * @returns `<div class="panel-chart">`, ещё не вставленный в DOM; `null`,
 *   если `bars` пуст — не показывать пустой график лучше, чем показать его без единого столбца.
 */
function buildBarChart(
  title: string,
  bars: { label: string; value: number }[],
): HTMLElement | null {
  if (bars.length === 0) return null;
  // Math.max(..., 1) — подстраховка от деления на 0, если ВСЕ столбцы
  // нулевые (например, ни одна публикация ещё не набрала общих авторов
  // выше текущего порога фильтра) — тогда все столбцы просто рисуются пустыми.
  const max = Math.max(...bars.map((bar) => bar.value), 1);

  const container = document.createElement("div");
  container.className = "panel-chart";

  const heading = document.createElement("h4");
  heading.textContent = title;
  container.appendChild(heading);

  for (const bar of bars) {
    const row = document.createElement("div");
    row.className = "chart-bar-row";

    const label = document.createElement("span");
    label.className = "chart-bar-row__label";
    label.textContent = bar.label;

    const track = document.createElement("div");
    track.className = "chart-bar-row__track";
    const fill = document.createElement("div");
    fill.className = "chart-bar-row__fill";
    fill.style.width = `${(bar.value / max) * 100}%`;
    track.appendChild(fill);

    const value = document.createElement("span");
    value.className = "chart-bar-row__value";
    value.textContent = String(bar.value);

    row.append(label, track, value);
    container.appendChild(row);
  }

  return container;
}

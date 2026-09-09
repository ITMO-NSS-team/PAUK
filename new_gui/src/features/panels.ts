// Слой "features" — панель с информацией о том, что сейчас выбрано.
// Умеет показывать карточку узла (автор/репозиторий/публикация), ребра
// (кто с кем связан и с каким весом), департамента (сводные числа из
// самого Department) и карточку "Обзор" по умолчанию, когда вообще
// ничего не выбрано (сводные числа по всему графу, а не по одному узлу).

import type { AuthorDetail, GraphData, PubDetail, PubNode, RepoDetail, RepoNode } from "../contracts/graph";
import { PANEL_CONFIG } from "../core/config";
import {
  buildAuthorPubIndex,
  buildAuthorRepoIndex,
  buildCoauthIndex,
  buildDeptEdgeIndex,
  buildRepoAuthorIndex,
  buildRepoPubIndex,
  githubProfileUrl,
  githubShortPath,
  indexByKey,
  nodeLabel,
} from "../core/data";
import { createLoadingIndicator, requireElement } from "../core/dom";
import { kindLabel, localize, t } from "../core/i18n";
import type { AppState, Store } from "../core/state";

/** Строка карточки ещё не может показать значение — соответствующий
 * `*Detail`-файл (см. {@link AuthorDetail}/{@link RepoDetail}) не домержился
 * в карту, переданную {@link mountPanel}. Отдельный маркер, а не пустая
 * строка — пустая строка означала бы "поле реально пустое", а это "мы пока
 * не знаем, что там". */
const LOADING: unique symbol = Symbol("panel-row-loading");

/** Значение строки карточки — обычный текст, один или несколько кликабельных
 * ссылок (DOI, GitHub/ORCID, ссылка(и) на код), либо {@link LOADING}, пока
 * detail ещё не пришёл. */
type PanelRowValue = string | PanelLink[] | typeof LOADING;
/** Одна кликабельная ссылка в строке карточки — всегда открывается в новой вкладке ({@link buildCard}). */
interface PanelLink {
  href: string;
  text: string;
}
/** Одна строка карточки: `[подпись, значение]`. */
type PanelRow = [label: string, value: PanelRowValue];

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
  return { href: `https://doi.org/${doi.replace(/^https?:\/\/doi\.org\//, "")}`, text: doi };
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
  return { href: githubProfileUrl(username), text: username };
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
  return { href: `https://orcid.org/${id}`, text: id };
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
    console.warn(`${context}: значение не распознано как URL, ссылка заменена на "about:blank": ${url}`);
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
  return { href: safeHref(url, "codeLink"), text: githubShortPath(url) };
}

/**
 * Строит ссылку на профиль Google Scholar из `AuthorDetail.google_scholar`
 * (уже полный URL, в отличие от `orcid`/`openreview`/`openalex_id`, которые
 * приходят голыми id) — с проверкой схемы (см. {@link safeHref}). Текст
 * ссылки — фиксированное "Google Scholar", а не сам URL: он длинный и с
 * query-параметрами, нечитаем в узкой карточке.
 *
 * @param url - `AuthorDetail.google_scholar`.
 */
function googleScholarLink(url: string): PanelLink {
  return { href: safeHref(url, "googleScholarLink"), text: "Google Scholar" };
}

/**
 * Строит ссылку на страницу публикации на OpenAlex из `PubDetail.openalex_url`
 * (уже полный URL) — та же схема-проверка, что и у {@link googleScholarLink},
 * той же причине (внешний харвестинг, не наша сборка ссылки).
 *
 * @param url - `PubDetail.openalex_url`.
 */
function openalexUrlLink(url: string): PanelLink {
  return { href: safeHref(url, "openalexUrlLink"), text: "OpenAlex" };
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
  return { href: `https://openalex.org/${id}`, text: id };
}

/**
 * Строит ссылку на профиль автора на OpenReview по его id
 * (`AuthorDetail.openreview`, например `"~Ivan_Ivanov1"`) — схема
 * захардкожена нами, как и у {@link orcidLink}, проверка не нужна.
 *
 * @param id - `AuthorDetail.openreview`.
 */
function openreviewLink(id: string): PanelLink {
  return { href: `https://openreview.net/profile?id=${id}`, text: id };
}

/**
 * Строит ссылку `mailto:` из `AuthorDetail.email`/одного значения из
 * `AuthorDetail.emails` — схема захардкожена нами, проверка не нужна.
 *
 * @param email - адрес почты.
 */
function emailLink(email: string): PanelLink {
  return { href: `mailto:${email}`, text: email };
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

  // Строится один раз при монтировании, а не на каждый рендер — поиск по
  // ключу должен быть мгновенным, а не пересчитывать индекс на каждый клик.
  const index = indexByKey(data);
  const deptById = new Map(data.departments.map((dept) => [dept.id, dept]));
  const { authorPubs, pubAuthors } = buildAuthorPubIndex(data);
  const coauthIndex = buildCoauthIndex(data);
  const authorRepoIndex = buildAuthorRepoIndex(data);
  const deptEdgeIndex = buildDeptEdgeIndex(data);
  const repoAuthorIndex = buildRepoAuthorIndex(data);
  const { repoPubs: repoPubIndex, pubRepos: pubRepoIndex } = buildRepoPubIndex(data);

  /**
   * Строит подписи департаментов по списку их id, через запятую — для
   * строки "связанные департаменты" в карточке департамента.
   *
   * @param ids - список id департаментов.
   * @param lang - язык интерфейса.
   * @returns Подписи через `", "`, в том же порядке, что и `ids`.
   */
  function deptLabelsOf(ids: number[], lang: AppState["lang"]): string {
    return ids
      .map((id) => {
        const dept = deptById.get(id);
        return dept ? localize(dept.name, dept.name_en, lang) : String(id);
      })
      .join(", ");
  }

  /**
   * Строит подписи узлов графа по списку их ключей, через запятую — общая
   * функция для строк "общие публикации"/"общие авторы" в карточке ребра
   * и всех похожих списков в карточках автора/репозитория/публикации.
   *
   * @param keys - список ключей узлов (авторов, репозиториев или публикаций).
   * @param lang - язык интерфейса.
   * @returns Подписи через `", "`, в том же порядке, что и `keys`. Ключ,
   *   которого нет в `index` (не должно случаться на согласованных
   *   данных), используется как есть, а не отбрасывается.
   */
  function labelsOf(keys: string[], lang: AppState["lang"]): string {
    return keys
      .map((key) => {
        const node = index.get(key);
        return node ? nodeLabel(node, lang, pubDetails) : key;
      })
      .join(", ");
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
    return pubKeys
      .map((key) => index.get(key))
      .filter((node): node is PubNode => node?.kind === "pub")
      .sort((a, b) => (b.year ?? -Infinity) - (a.year ?? -Infinity))
      .slice(0, PANEL_CONFIG.listLimit)
      .map((node) => node.key);
  }

  /**
   * Возвращает ключи публикаций автора, недавние сверху, обрезано до
   * {@link PANEL_CONFIG.listLimit}.
   *
   * @param authorKey - ключ автора.
   * @returns До `PANEL_CONFIG.listLimit` ключей публикаций, от новых к старым.
   */
  function recentPubKeysOf(authorKey: string): string[] {
    return recentPubKeysFrom(authorPubs.get(authorKey) ?? []);
  }

  /**
   * Возвращает ключи соавторов автора, по убыванию суммарного веса связи
   * (числа совместных публикаций), обрезано до {@link PANEL_CONFIG.listLimit}.
   *
   * @param authorKey - ключ автора.
   * @returns До `PANEL_CONFIG.listLimit` ключей соавторов, от самых частых к редким.
   */
  function topCoauthorKeys(authorKey: string): string[] {
    return [...(coauthIndex.get(authorKey) ?? new Map<string, number>()).entries()]
      .sort(([, weightA], [, weightB]) => weightB - weightA)
      .slice(0, PANEL_CONFIG.listLimit)
      .map(([key]) => key);
  }

  /**
   * Возвращает ключи репозиториев автора, по убыванию звёзд (как и в
   * старом GUI), обрезано до {@link PANEL_CONFIG.listLimit}.
   *
   * @param authorKey - ключ автора.
   * @returns До `PANEL_CONFIG.listLimit` ключей репозиториев, от самых популярных к менее популярным.
   */
  function authorRepoKeysOf(authorKey: string): string[] {
    return (authorRepoIndex.get(authorKey) ?? [])
      .map((key) => index.get(key))
      .filter((node): node is RepoNode => node?.kind === "repo")
      .sort((a, b) => b.stars - a.stars)
      .slice(0, PANEL_CONFIG.listLimit)
      .map((node) => node.key);
  }

  /**
   * Строит подпись участников репозитория с ролью в формате `"Имя (роль)"`,
   * через запятую, обрезано до {@link PANEL_CONFIG.listLimit}. Отдельная
   * функция, а не {@link labelsOf}: нужно дописать роль после имени, а не
   * только саму подпись узла.
   *
   * @param repoKey - ключ репозитория.
   * @param lang - язык интерфейса.
   * @returns Подписи участников с ролями через `", "` (например, `"Иванов И.И. (maintainer)"`).
   */
  function repoContributorsOf(repoKey: string, lang: AppState["lang"]): string {
    return (repoAuthorIndex.get(repoKey) ?? [])
      .slice(0, PANEL_CONFIG.listLimit)
      .map((edge) => {
        const author = index.get(edge.t);
        const label = author ? nodeLabel(author, lang, pubDetails) : edge.t;
        return `${label} (${edge.role})`;
      })
      .join(", ");
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
   * @param title - заголовок карточки (`<h3>`).
   * @param rows - строки карточки, см. {@link PanelRow}.
   */
  function show(title: string, rows: PanelRow[]): void {
    container.hidden = false;
    container.replaceChildren(buildCard(title, rows));
  }

  /**
   * Рисует карточку "Обзор" по умолчанию, когда ничего не выбрано —
   * сводные числа по всему текущему набору данных, не зависят от активной
   * вкладки (в отличие от того, что рисует карта, которая всегда
   * показывает только граф активной вкладки).
   *
   * @param lang - язык интерфейса.
   */
  function renderOverview(lang: AppState["lang"]): void {
    const rows: PanelRow[] = [
      [t("field.authorsCount", lang), String(data.authors.length)],
      [t("field.reposCount", lang), String(data.repos.length)],
      [t("field.pubsCount", lang), String(data.pubs.length)],
      [t("field.deptsCount", lang), String(data.departments.length)],
    ];
    show(t("overview.title", lang), rows);
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
    if (selection === null) return renderOverview(lang);

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
      // как есть.
      let title = nodeLabel(node, lang, pubDetails);
      const rows: PanelRow[] = [
        [t("field.key", lang), node.key],
        [t("field.kind", lang), kindLabel(node.kind, lang)],
        [t("field.dept", lang), dept ? localize(dept.name, dept.name_en, lang) : t("field.unknownDept", lang)],
      ];
      if (node.kind === "author") {
        rows.push([t("field.pubsCount", lang), String(node.pubs_count)]);
        // authorDetails.has(), не просто .get() — new_generate строит
        // authors-detail.json так, что запись есть у КАЖДОГО автора, даже
        // если все поля в ней пустые (см. new_generate/generate_data.py).
        // Значит "записи нет" означает ровно одно: файл ещё не домержился
        // (см. app/main.ts) — а не то, что у автора реально нет данных.
        if (authorDetails.has(node.key)) {
          const authorDetail = authorDetails.get(node.key);
          // Заголовок — полное имя на нужном языке, если оно вообще
          // известно ("Фамилия Имя Отчество", не "Фамилия И.О."); если
          // пусто (редкий случай) — остаётся сокращённая подпись узла.
          const fullName = authorDetail ? localize(authorDetail.name_ru, authorDetail.name_en, lang) : "";
          if (fullName) title = fullName;
          if (authorDetail?.degree) rows.push([t("field.degree", lang), authorDetail.degree]);
          if (authorDetail?.github) rows.push([t("field.github", lang), [githubLink(authorDetail.github)]]);
          if (authorDetail?.orcid) rows.push([t("field.orcid", lang), [orcidLink(authorDetail.orcid)]]);
          if (authorDetail?.openalex_id) rows.push([t("field.openalexId", lang), [openalexIdLink(authorDetail.openalex_id)]]);
          if (authorDetail?.google_scholar) {
            rows.push([t("field.googleScholar", lang), [googleScholarLink(authorDetail.google_scholar)]]);
          }
          if (authorDetail?.openreview) rows.push([t("field.openreview", lang), [openreviewLink(authorDetail.openreview)]]);
          // email/emails — одна строка на оба, mailto-ссылками, без
          // дублирования: emails может содержать email ещё раз, поэтому
          // объединяем через Set по значению, а не просто конкатенируем.
          if (authorDetail) {
            const allEmails = [...new Set([authorDetail.email, ...authorDetail.emails].filter(Boolean))];
            if (allEmails.length > 0) rows.push([t("field.email", lang), allEmails.map(emailLink)]);
          }
          if (authorDetail && authorDetail.affiliations.length > 0) {
            const names = [...new Set(authorDetail.affiliations.map((a) => a.name))];
            rows.push([t("field.affiliations", lang), names.join(", ")]);
          }
          // Раздельно по источнику — OpenAlex (варианты по публикациям) и
          // ORCID (имя, под которым автор сам просит его указывать) — это
          // разные по происхождению вещи, см. author_variants() в
          // new_generate/generate_data.py.
          if (authorDetail && authorDetail.name_variants.openalex.length > 0) {
            rows.push([t("field.nameVariantsOpenalex", lang), authorDetail.name_variants.openalex.join(", ")]);
          }
          if (authorDetail && authorDetail.name_variants.orcid.length > 0) {
            rows.push([t("field.nameVariantsOrcid", lang), authorDetail.name_variants.orcid.join(", ")]);
          }
        } else {
          rows.push([t("field.loadingDetails", lang), LOADING]);
        }

        // Сами счётчики выше не говорят, КАКИЕ именно публикации/соавторы —
        // строки ниже показывают список, только когда он не пуст (как и у
        // общих публикаций/авторов в карточке ребра).
        const recentPubs = recentPubKeysOf(node.key);
        if (recentPubs.length > 0) rows.push([t("tab.pubs", lang), labelsOf(recentPubs, lang)]);

        const topCoauthors = topCoauthorKeys(node.key);
        if (topCoauthors.length > 0) rows.push([t("field.topCoauthors", lang), labelsOf(topCoauthors, lang)]);

        const authorRepos = authorRepoKeysOf(node.key);
        if (authorRepos.length > 0) rows.push([t("tab.repos", lang), labelsOf(authorRepos, lang)]);
      }
      if (node.kind === "repo") {
        rows.push([t("field.stars", lang), String(node.stars)]);
        // Тот же приём, что и у автора выше: .has(), потому что запись в
        // repos-detail.json есть у каждого репозитория без исключений.
        if (repoDetails.has(node.key)) {
          const repoDetail = repoDetails.get(node.key);
          if (repoDetail?.description) rows.push([t("field.description", lang), repoDetail.description]);
          if (repoDetail?.owner_type) rows.push([t("field.ownerType", lang), repoDetail.owner_type]);
          if (repoDetail?.license) rows.push([t("field.license", lang), repoDetail.license]);
          if (repoDetail?.has_readme) rows.push([t("field.hasReadme", lang), "✓"]);
        } else {
          rows.push([t("field.loadingDetails", lang), LOADING]);
        }

        const contributors = repoContributorsOf(node.key, lang);
        if (contributors.length > 0) rows.push([t("field.contributors", lang), contributors]);

        const repoPubs = repoPubKeysOf(node.key);
        if (repoPubs.length > 0) rows.push([t("tab.pubs", lang), labelsOf(repoPubs, lang)]);
      }
      if (node.kind === "pub") {
        rows.push([t("field.year", lang), node.year === null ? t("field.yearUnknown", lang) : String(node.year)]);

        const detail = pubDetails.get(node.key);
        if (detail?.doi) rows.push([t("field.doi", lang), [doiLink(detail.doi)]]);
        if (detail?.type) rows.push([t("field.pubType", lang), detail.type]);
        if (detail && detail.fields.length > 0) rows.push([t("field.pubFields", lang), detail.fields.join(", ")]);
        if (detail?.abstract) rows.push([t("field.abstract", lang), detail.abstract]);
        if (detail?.openalex_url) rows.push([t("field.openalexUrl", lang), [openalexUrlLink(detail.openalex_url)]]);

        // Как и в старом showPubCard(): если публикация связана с нашим
        // собственным репозиторием (repo_pub_edges), показываем ссылку на
        // него ВМЕСТО голого code_url — связь через собственные данные
        // надёжнее внешнего харвестинга, а раз она есть, дублировать её
        // ещё и code_url незачем.
        const pubRepoKeys = (pubRepoIndex.get(node.key) ?? []).slice(0, PANEL_CONFIG.listLimit);
        if (pubRepoKeys.length > 0) {
          rows.push([t("tab.repos", lang), labelsOf(pubRepoKeys, lang)]);
        } else if (detail?.has_code && detail.code_url.length > 0) {
          rows.push([t("field.code", lang), detail.code_url.map(codeLink)]);
        }

        const pubAuthorKeys = (pubAuthors.get(node.key) ?? []).slice(0, PANEL_CONFIG.listLimit);
        if (pubAuthorKeys.length > 0) rows.push([t("tab.authors", lang), labelsOf(pubAuthorKeys, lang)]);
      }

      return show(title, rows);
    }

    if (selection.kind === "edge") {
      const from = index.get(selection.s);
      const to = index.get(selection.t);
      if (!from || !to) return hide();

      const rows: PanelRow[] = [
        [t("field.edgeFrom", lang), nodeLabel(from, lang, pubDetails)],
        [t("field.edgeTo", lang), nodeLabel(to, lang, pubDetails)],
        [t("field.edgeWeight", lang), String(selection.w)],
      ];

      // Сам вес — это только число; что конкретно за ним стоит, видно только
      // через all_edges. Показываем список, только если он не пуст — как и
      // в старом showEdgeCard(), у ребра без общих публикаций/авторов (или
      // между узлами другого вида, например репозиториями) этой строки нет.
      // Так же, как и у остальных списков в этом файле, режем до
      // PANEL_CONFIG.listLimit — у активных соавторов общих публикаций
      // может быть больше, чем поместится в карточку.
      if (from.kind === "author" && to.kind === "author") {
        const shared = (authorPubs.get(from.key) ?? [])
          .filter((pub) => (authorPubs.get(to.key) ?? []).includes(pub))
          .slice(0, PANEL_CONFIG.listLimit);
        if (shared.length > 0) rows.push([t("field.sharedPubs", lang), labelsOf(shared, lang)]);
      } else if (from.kind === "pub" && to.kind === "pub") {
        const shared = (pubAuthors.get(from.key) ?? [])
          .filter((author) => (pubAuthors.get(to.key) ?? []).includes(author))
          .slice(0, PANEL_CONFIG.listLimit);
        if (shared.length > 0) rows.push([t("field.sharedAuthors", lang), labelsOf(shared, lang)]);
      }

      return show(t("kind.edge", lang), rows);
    }

    // selection.kind === "dept"
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
      .slice(0, PANEL_CONFIG.listLimit)
      .map(([id]) => id);
    if (relatedIds.length > 0) rows.push([t("field.relatedDepts", lang), deptLabelsOf(relatedIds, lang)]);

    return show(localize(dept.name, dept.name_en, lang), rows);
  }

  render(store.get());
  const unsubscribe = store.subscribe(render);
  return unsubscribe;
}

/**
 * Собирает DOM-карточку из заголовка и списка пар "подпись — значение".
 * Только `textContent` для обычного текста и явные `<a>` с фиксированными
 * `href`/`text` для ссылок — никакого `innerHTML`, данные из графа не
 * должны интерпретироваться как разметка.
 *
 * @param title - заголовок карточки (например, имя автора или "Обзор").
 * @param rows - строки карточки в порядке отображения.
 * @returns `<div class="panel-card">` с заголовком `<h3>` и списком `<dl>`, ещё не вставленный в DOM.
 */
function buildCard(title: string, rows: PanelRow[]): HTMLElement {
  const card = document.createElement("div");
  card.className = "panel-card";

  const heading = document.createElement("h3");
  heading.textContent = title;
  card.appendChild(heading);

  const list = document.createElement("dl");
  for (const [label, value] of rows) {
    const dt = document.createElement("dt");
    dt.textContent = label;

    const dd = document.createElement("dd");
    if (typeof value === "string") {
      dd.textContent = value;
    } else if (value === LOADING) {
      dd.appendChild(createLoadingIndicator());
    } else {
      // Несколько ссылок (например, несколько репозиториев с кодом) —
      // разделяем запятой с пробелом, как и в старом GUI.
      value.forEach((link, i) => {
        if (i > 0) dd.append(", ");
        const a = document.createElement("a");
        a.href = link.href;
        a.target = "_blank";
        a.rel = "noopener noreferrer";
        a.textContent = link.text;
        dd.appendChild(a);
      });
    }

    list.append(dt, dd);
  }
  card.appendChild(list);

  return card;
}

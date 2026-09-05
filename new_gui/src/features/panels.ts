// Слой "features" — панель с информацией о том, что сейчас выбрано.
// Умеет показывать карточку узла (автор/репозиторий/публикация), ребра
// (кто с кем связан и с каким весом), департамента (сводные числа из
// самого Department) и карточку "Обзор" по умолчанию, когда вообще
// ничего не выбрано (сводные числа по всему графу, а не по одному узлу).

import type { GraphData } from "../contracts/graph";
import type { SearchDetail } from "../contracts/search";
import { indexByKey, nodeLabel } from "../core/data";
import { requireElement } from "../core/dom";
import { kindLabel, localize, t } from "../core/i18n";
import type { AppState, Store } from "../core/state";

/** Значение строки карточки — обычный текст либо один или несколько кликабельных ссылок (DOI, ссылка(и) на код). */
type PanelRowValue = string | PanelLink[];
interface PanelLink {
  href: string;
  text: string;
}
type PanelRow = [label: string, value: PanelRowValue];

/**
 * Ссылка на DOI — как и в старом GUI (search.js): если doi уже пришёл
 * полным URL вида https://doi.org/..., не задваиваем префикс.
 */
function doiLink(doi: string): PanelLink {
  return { href: `https://doi.org/${doi.replace(/^https?:\/\/doi\.org\//, "")}`, text: doi };
}

/**
 * Подпись ссылки на код — путь без "https://github.com/", как в старом
 * GUI, чтобы длинный URL не распирал панель. url в перспективе приходит
 * из харвестинга GitHub (внешние данные, не только наша синтетическая
 * fixture) — перед тем как класть его в href, проверяем схему: без этого
 * "javascript:..." в поле code_url привело бы к выполнению кода по клику.
 * DOI (doiLink выше) той же проверки не требует — там схема "https://doi.org/"
 * всегда захардкожена нами, значение подставляется только в путь.
 */
function codeLink(url: string): PanelLink {
  let href = "about:blank";
  try {
    const parsed = new URL(url);
    if (parsed.protocol === "http:" || parsed.protocol === "https:") {
      href = parsed.toString();
    } else {
      console.warn(`codeLink: недопустимая схема в code_url, ссылка заменена на "about:blank": ${url}`);
    }
  } catch {
    console.warn(`codeLink: code_url не распознан как URL, ссылка заменена на "about:blank": ${url}`);
  }
  return { href, text: url.replace("https://github.com/", "") };
}

/**
 * Подключает панель информации: подписывается на Store и перерисовывает
 * содержимое каждый раз, когда меняется state.selection ИЛИ state.lang —
 * оба поля читаются в одном render(), поэтому одной подписки достаточно,
 * без отдельной логики "что именно изменилось". Возвращает функцию
 * отписки (unmount).
 */
export function mountPanel(
  store: Store<AppState>,
  data: GraphData,
  searchDetails: Map<string, SearchDetail>,
): () => void {
  const container = requireElement("panel");

  // Строится один раз при монтировании, а не на каждый рендер — поиск по
  // ключу должен быть мгновенным, а не пересчитывать индекс на каждый клик.
  const index = indexByKey(data);
  const deptById = new Map(data.departments.map((dept) => [dept.id, dept]));

  function hide(): void {
    container.hidden = true;
    container.replaceChildren();
  }

  function show(title: string, rows: PanelRow[]): void {
    container.hidden = false;
    container.replaceChildren(buildCard(title, rows));
  }

  /** Карточка по умолчанию, пока ничего не выбрано — сводные числа по всему текущему графу данных (не зависит от активной вкладки). */
  function renderOverview(lang: AppState["lang"]): void {
    const rows: PanelRow[] = [
      [t("field.authorsCount", lang), String(data.authors.length)],
      [t("field.reposCount", lang), String(data.repos.length)],
      [t("field.pubsCount", lang), String(data.pubs.length)],
      [t("field.deptsCount", lang), String(data.departments.length)],
    ];
    show(t("overview.title", lang), rows);
  }

  /** Пересобирает содержимое панели под текущее состояние — один из видов карточки (обзор/узел/ребро/департамент) либо ничего, если рассинхрон данных. */
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
      const rows: PanelRow[] = [
        [t("field.key", lang), node.key],
        [t("field.kind", lang), kindLabel(node.kind, lang)],
        [t("field.dept", lang), dept ? localize(dept.name, dept.name_en, lang) : t("field.unknownDept", lang)],
      ];
      if (node.kind === "author") rows.push([t("field.pubsCount", lang), String(node.pubs_count)]);
      if (node.kind === "repo") {
        rows.push([t("field.stars", lang), String(node.stars)], [t("field.owner", lang), node.owner]);
      }
      if (node.kind === "pub") {
        rows.push([t("field.year", lang), node.year === null ? t("field.yearUnknown", lang) : String(node.year)]);

        const detail = searchDetails.get(node.key);
        if (detail?.doi) rows.push([t("field.doi", lang), [doiLink(detail.doi)]]);
        if (detail?.has_code && detail.code_url.length > 0) {
          rows.push([t("field.code", lang), detail.code_url.map(codeLink)]);
        }
      }

      return show(nodeLabel(node, lang, searchDetails), rows);
    }

    if (selection.kind === "edge") {
      const from = index.get(selection.s);
      const to = index.get(selection.t);
      if (!from || !to) return hide();

      const rows: PanelRow[] = [
        [t("field.edgeFrom", lang), nodeLabel(from, lang, searchDetails)],
        [t("field.edgeTo", lang), nodeLabel(to, lang, searchDetails)],
        [t("field.edgeWeight", lang), String(selection.w)],
      ];
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
    return show(localize(dept.name, dept.name_en, lang), rows);
  }

  render(store.get());
  const unsubscribe = store.subscribe(render);
  return unsubscribe;
}

/**
 * Собирает DOM-карточку из заголовка и списка пар "подпись — значение".
 * Только textContent для обычного текста и явные <a> с фиксированными
 * href/text для ссылок — никакого innerHTML, данные из графа не должны
 * интерпретироваться как разметка.
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

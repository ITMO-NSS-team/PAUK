"use strict";

// tab-stats.js (tab 5 chrome) isn't migrated — falls through to Russian.
// Values are a plain string, or a function for count-interpolated strings.
const LOCALES = {
  ru: {
    "tab.people": "Персоналии",
    "tab.repos": "Репозитории",
    "tab.pubs": "Публикации",
    "tab.search": "Поиск",
    "tab.health": "Здоровье БД",

    "welcome.badge": "Открытый проект ИТМО",
    "welcome.title": "Карта соавторства<br />и открытого кода ИТМО",
    "welcome.subtitle": "Публикации, авторы и департаменты ИТМО — и связанные с ними open-source репозитории на GitHub.",
    "welcome.cta": "Смотреть карту",
    "welcome.modes": "Персоналии · Публикации · Репозитории · Поиск",

    "search.placeholder": "Автор, репозиторий или публикация",
    "search.loadingHint": "Индекс публикаций ещё загружается — авторы и репозитории уже доступны",
    "search.spPlaceholder": "Поиск автора, публикации или департамента…",

    "filter.coauth": "Мин. совм. публикаций",
    "filter.pubAuthors": "Мин. общих авторов ИТМО",
    "filter.edgesOnly": "только отображение рёбер",
    "filter.pubsByYear": "Публикации по год",

    "detail.close": "закрыть",

    "overview.label": "Обзор",
    "overview.authors": "авторов ИТМО",
    "overview.pubs": "публикаций",
    "overview.repos": "репозиториев",
    "overview.depts": "департаментов",
    "overview.inDev": "Вкладка в разработке",
    "common.noDept": "Без департамента",

    "author.kind": "автор",
    "author.otherSpellings": "Другие написания",
    "author.degree": "Степень",
    "author.coauthors": "соавторов",
    "author.activity": "активность",
    "author.pubsByYear": "Публикации по годам",
    "author.topCoauthors": "Топ соавторы",
    "author.recentPubs": "Последние публикации",
    "common.code": "код",
    "author.more": "Подробнее об авторе →",

    "dept.kind": "департамент",
    "dept.authorsCount": "Авторов:",
    "dept.pubsCount": "Публикаций:",
    "dept.reposCount": "Репозиториев:",
    "dept.collaboratesWith": (n) => `Сотрудничает с (${n})`,
    "dept.more": "Подробнее о департаменте →",

    "repo.kind": "репозиторий",
    "repo.department": "Департамент:",
    "repo.stars": "Звёзды:",
    "repo.itmoMembers": (n) => `Участники ИТМО (${n})`,
    "repo.pubsSection": "Публикации",

    "pub.kind": "публикация",
    "pub.journal": "Журнал:",
    "pub.department": "Департамент:",
    "pub.doi": "DOI:",
    "pub.code": "Код:",
    "pub.none": "нет",
    "pub.itmoAuthors": (n) => `Авторы ИТМО (${n})`,
    "pub.more": "Подробнее о публикации →",

    "edge.coauthorship": "соавторство",
    "edge.jointPapersLabel": "Совместных статей:",
    "edge.pubsSection": (n) => `Публикации (${n})`,
    "edge.pubPubKind": "публикации — общие авторы",
    "edge.sharedAuthorsCountLabel": "Общих авторов ИТМО:",
    "edge.authorsSection": "Авторы",
    "edge.repoRepoKind": "репозитории — общие контрибьюторы",
    "edge.sharedContributorsCountLabel": "Общих контрибьюторов ИТМО:",
    "edge.genericKind": "связь",

    "search.empty": "ничего не найдено",
    "search.kindRepoShort": "репо",
    "search.kindPubShort": "публ.",
    "search.navBack": "← Назад",
    "search.navHome": "На главную",
    "search.statAuthors": "авторов",
    "search.topAuthorsHint": "Топ авторов по публикациям",
    "search.topPubsByAuthors": "Топ публикаций по числу авторов ИТМО",
    "search.topDeptsByPubs": "Топ департаментов по публикациям",
    "search.openDeptProfile": "Открыть профиль департамента",
    "search.byLatestPub": "· по последней публикации",
    "search.coauthorsTitle": (n) => `Соавторы (${n})`,
    "search.deptBreakdown": "Связь с департаментами",
    "search.reposTitle": (n) => `Репозитории (${n})`,
    "search.allPubsTitle": (n) => `Все публикации (${n})`,
    "search.similarPubs": "Похожие публикации",
    "search.year": "год",
    "search.deptAuthorsTitle": (n) => `Топ авторов (${n})`,
    "search.relatedDepts": (n) => `Связанные департаменты (${n})`,
    "search.noDeptShort": "Без отдела",
    "search.other": "Прочие",
    "search.unknownDept": "Неизв.",
  },
  en: {
    "tab.people": "People",
    "tab.repos": "Repositories",
    "tab.pubs": "Publications",
    "tab.search": "Search",
    "tab.health": "DB Health",

    "welcome.badge": "Open ITMO project",
    "welcome.title": "Map of co-authorship<br />and open-source code at ITMO",
    "welcome.subtitle": "ITMO's publications, authors, and departments — and the open-source repositories linked to them on GitHub.",
    "welcome.cta": "View the map",
    "welcome.modes": "People · Publications · Repositories · Search",

    "search.placeholder": "Author, repository, or publication",
    "search.loadingHint": "The publication index is still loading — authors and repositories are already available",
    "search.spPlaceholder": "Search for an author, publication, or department…",

    "filter.coauth": "Min. shared publications",
    "filter.pubAuthors": "Min. shared ITMO authors",
    "filter.edgesOnly": "affects only which links are shown",
    "filter.pubsByYear": "Publications by year",

    "detail.close": "close",

    "overview.label": "Overview",
    "overview.authors": "ITMO authors",
    "overview.pubs": "publications",
    "overview.repos": "repositories",
    "overview.depts": "departments",
    "overview.inDev": "Tab under construction",
    "common.noDept": "No department",

    "author.kind": "author",
    "author.otherSpellings": "Other spellings",
    "author.degree": "Degree",
    "author.coauthors": "co-authors",
    "author.activity": "active",
    "author.pubsByYear": "Publications by year",
    "author.topCoauthors": "Top co-authors",
    "author.recentPubs": "Recent publications",
    "common.code": "code",
    "author.more": "More about this author →",

    "dept.kind": "department",
    "dept.authorsCount": "Authors:",
    "dept.pubsCount": "Publications:",
    "dept.reposCount": "Repositories:",
    "dept.collaboratesWith": (n) => `Collaborates with (${n})`,
    "dept.more": "More about this department →",

    "repo.kind": "repository",
    "repo.department": "Department:",
    "repo.stars": "Stars:",
    "repo.itmoMembers": (n) => `ITMO members (${n})`,
    "repo.pubsSection": "Publications",

    "pub.kind": "publication",
    "pub.journal": "Journal:",
    "pub.department": "Department:",
    "pub.doi": "DOI:",
    "pub.code": "Code:",
    "pub.none": "none",
    "pub.itmoAuthors": (n) => `ITMO authors (${n})`,
    "pub.more": "More about this publication →",

    "edge.coauthorship": "co-authorship",
    "edge.jointPapersLabel": "Joint papers:",
    "edge.pubsSection": (n) => `Publications (${n})`,
    "edge.pubPubKind": "publications — shared authors",
    "edge.sharedAuthorsCountLabel": "Shared ITMO authors:",
    "edge.authorsSection": "Authors",
    "edge.repoRepoKind": "repositories — shared contributors",
    "edge.sharedContributorsCountLabel": "Shared ITMO contributors:",
    "edge.genericKind": "link",

    "search.empty": "no results",
    "search.kindRepoShort": "repo",
    "search.kindPubShort": "pub.",
    "search.navBack": "← Back",
    "search.navHome": "Home",
    "search.statAuthors": "authors",
    "search.topAuthorsHint": "Top authors by publications",
    "search.topPubsByAuthors": "Top publications by number of ITMO authors",
    "search.topDeptsByPubs": "Top departments by publications",
    "search.openDeptProfile": "Open department profile",
    "search.byLatestPub": "· by latest publication",
    "search.coauthorsTitle": (n) => `Co-authors (${n})`,
    "search.deptBreakdown": "Department breakdown",
    "search.reposTitle": (n) => `Repositories (${n})`,
    "search.allPubsTitle": (n) => `All publications (${n})`,
    "search.similarPubs": "Similar publications",
    "search.year": "year",
    "search.deptAuthorsTitle": (n) => `Top authors (${n})`,
    "search.relatedDepts": (n) => `Related departments (${n})`,
    "search.noDeptShort": "No department",
    "search.other": "Other",
    "search.unknownDept": "Unknown",
  },
};

let LANG = localStorage.getItem("pauk-lang") || "en";

function t(key, ...args) {
  const v = LOCALES[LANG]?.[key] ?? LOCALES.ru[key] ?? key;
  return typeof v === "function" ? v(...args) : v;
}

function setLang(lang) {
  if (lang === LANG) return;
  localStorage.setItem("pauk-lang", lang);
  location.reload();
}

// Swaps text on anything in index.html marked data-i18n[-placeholder|...].
// JS-generated content (cards, search results) uses direct t() calls instead.
function applyStaticI18n(root) {
  (root || document).querySelectorAll("[data-i18n]").forEach(el => { el.textContent = t(el.dataset.i18n); });
  (root || document).querySelectorAll("[data-i18n-html]").forEach(el => { el.innerHTML = t(el.dataset.i18nHtml); });
  (root || document).querySelectorAll("[data-i18n-placeholder]").forEach(el => { el.placeholder = t(el.dataset.i18nPlaceholder); });
  (root || document).querySelectorAll("[data-i18n-title]").forEach(el => { el.title = t(el.dataset.i18nTitle); });
  (root || document).querySelectorAll("[data-i18n-aria-label]").forEach(el => { el.setAttribute("aria-label", t(el.dataset.i18nAriaLabel)); });
}
document.addEventListener("DOMContentLoaded", () => applyStaticI18n());

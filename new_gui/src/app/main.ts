// Слой "app" — точка входа. Специально тонкий: собирает Store и фичи
// вместе, сам не содержит бизнес-логики — вся она в core/map/features.

import Graph from "graphology";
import Sigma from "sigma";
import { drawDiscNodeLabel } from "sigma/rendering";
import type { Settings } from "sigma/settings";
import type { NodeDisplayData, PartialButFor } from "sigma/types";
import type { AuthorDetail, PubDetail, RepoDetail } from "../contracts/graph";
import { DATA_CONFIG, FILTER_CONFIG, MAP_CONFIG } from "../core/config";
import { loadDetails, loadGraphData, mergeDetailsInto } from "../core/data";
import { requireElement, showLoadError } from "../core/dom";
import { loggedStep } from "../core/log";
import { Store, type AppState } from "../core/state";
import { parseUrlState } from "../core/url";
import { mountFilters } from "../features/filters";
import { mountLangToggle } from "../features/langToggle";
import { mountPanel } from "../features/panels";
import { mountSelection } from "../features/selection";
import { mountStart } from "../features/start";
import { mountTabs } from "../features/tabs";
import { mountUrlSync } from "../features/urlSync";
import { mountReactiveGraph, populateGraph } from "../map/build";

// "Obsidian"-свечение выбранного/наведённого узла — Sigma вызывает эту
// функцию и на реальное наведение мышью, и на узел, у которого reducer в
// map/build.ts::applyHighlighting вернул `highlighted: true` (для Sigma
// это один и тот же визуальный случай). Рисует мягкое свечение вокруг
// узла его же цветом через штатный Canvas2D `context.shadowBlur`, затем
// подпись — штатной `drawDiscNodeLabel`, чтобы не пересобирать её
// позиционирование вручную.
//
// Живёт здесь, а не в map/build.ts: импорт "sigma/rendering" тянет за
// собой рантайм всего рендер-модуля Sigma, который на уровне модуля
// трогает WebGL2RenderingContext — в jsdom (тесты) его нет. map/build.ts
// импортируется тестами напрямую, а app/main.ts — нет (как и сама
// конструкция `new Sigma(...)`, которая по той же причине живёт только здесь).
function drawGlowingNodeHover(
  context: CanvasRenderingContext2D,
  data: PartialButFor<NodeDisplayData, "x" | "y" | "size" | "label" | "color">,
  settings: Settings,
): void {
  context.save();
  context.shadowBlur = MAP_CONFIG.node.glowBlur;
  context.shadowColor = data.color;
  context.fillStyle = data.color;
  context.beginPath();
  context.arc(data.x, data.y, data.size, 0, Math.PI * 2);
  context.closePath();
  context.fill();
  context.restore();

  drawDiscNodeLabel(context, data, settings);
}

// Единственное место, где создаётся Store — дальше он просто передаётся
// в конструкторы фич (features/*), которые сами решают, на какую часть
// state подписаться. yearMax по умолчанию — это FILTER_CONFIG.year.max
// (текущий год), а не отдельно вычисленный new Date().getFullYear(): так
// стартовое "без фильтра по году" значение и верхняя граница самого
// слайдера физически не могут разойтись, это одно и то же число.
const store = new Store<AppState>({
  tab: 1,
  lang: "ru",
  selection: null,
  filters: { minCoauth: 1, minSharedAuthors: 1, yearMax: FILTER_CONFIG.year.max },
});

// Данные приходят из четырёх *.json в корне сайта (см. DATA_CONFIG в
// core/config.ts) — статика, которую пишет new_generate/generate_data.py в
// <repo_root>/data/gui/private (пока работаем только с приватным
// вариантом — публичный, урезанный, вариант данных подключим отдельно,
// когда дойдём до скрытия полей/усечения инициалов). Файлов может не
// быть, пока никто не прогнал генератор локально — тогда fetch ниже
// падает, loggedStep() логирует это в консоль, .catch() показывает
// баннер, приложение не рушится.
/**
 * Догружает один `*-detail.json` фоном и домешивает результат в уже
 * переданную фичам карту `target` — общая часть трёх одинаковых по форме
 * загрузок (публикации/авторы/репозитории) ниже, отличающихся только типом
 * `T`, именем шага, URL и картой-приёмником.
 *
 * @param name - имя шага для логов (см. {@link loggedStep}), например "pubs-detail.json".
 * @param url - адрес `*-detail.json` (см. {@link DATA_CONFIG}).
 * @param target - карта, в которую нужно домешать результат (мутируется на месте, см. {@link mergeDetailsInto}).
 */
function loadDetailsInto<T extends { key: string }>(name: string, url: string, target: Map<string, T>): void {
  loggedStep(name, () => loadDetails<T>(url))
    .then((details) => {
      mergeDetailsInto(target, details);
      store.notify();
    })
    .catch(() => {
      // Ошибка уже залогирована внутри loggedStep — здесь только не даём
      // ей всплыть дальше как необработанный rejection. Баннер не
      // показываем: отсутствие ОДНОГО detail-файла (например, у --public
      // сборки нет authors-detail.json) не должно ронять всю страницу —
      // соответствующие поля карточки просто останутся в состоянии
      // "загрузка" навсегда (LOADING в features/panels.ts), а не покажут
      // ошибку поверх всего интерфейса.
    });
}

// Стартовый экран (boot-progress + welcome-hero, см. features/start.ts) —
// монтируется сразу, до первого fetch: boot-screen должен быть виден с
// первого кадра, а не появиться с задержкой.
const start = mountStart(store);
start.setBootStage("loading");

// Приоритетная загрузка: сперва graph-data.json — этого одного достаточно,
// чтобы построить граф и все списки (summary-полей хватает на всё, что
// видно сразу). Три *-detail.json грузятся уже ПОСЛЕ первой отрисовки,
// фоном, не блокируя её — граф и списки не должны ждать самых тяжёлых
// (потенциально) файлов ради полей, которые видны только по клику.
//
// В отличие от MapLibre-версии здесь нет отдельного шага "подождать, пока
// карта сама инициализируется" (там это было map.on("load", ...) — стиль
// MapLibre грузится асинхронно): конструктор Sigma синхронный, поэтому
// рендерер строится сразу же, как только есть данные для его наполнения.
loggedStep("graph-data.json", () => loadGraphData(DATA_CONFIG.graphDataUrl))
  .then((data) => {
    start.setBootStage("rendering");
    // Пустые карты передаются во все фичи один раз — заполняются на месте
    // (mergeDetailsInto), когда придёт соответствующий *-detail.json, см.
    // ниже. Мутация видна всем, кто уже держит эту же ссылку, без
    // повторного монтирования — только store.notify(), чтобы разбудить
    // то, что уже подписано на Store (mountPanel).
    const pubDetailsByKey = new Map<string, PubDetail>();
    const authorDetailsByKey = new Map<string, AuthorDetail>();
    const repoDetailsByKey = new Map<string, RepoDetail>();

    // URL при первой загрузке может задавать другую вкладку/выбор, чем
    // дефолт Store (например, открыли сохранённую ссылку) — применяем это
    // ДО монтирования остальных фич, чтобы они сразу увидели нужное
    // состояние, а не мигнули дефолтом и тут же переключились на него.
    // Тот же разбор переиспользуется ниже для hasDeepLink — открыли не с
    // дефолтным view, значит welcome-экран должен пропуститься (как и в
    // старом GUI: заставка не должна перекрывать уже осмысленную ссылку).
    const urlState = parseUrlState(location.search, data);
    store.set(urlState);

    // Sigma конструируется с уже непустым графом — populateGraph() строит
    // его под начальное состояние ДО new Sigma(...), а не после (в
    // отличие от MapLibre, где источники/слои добавлялись в пустую карту
    // уже после её асинхронной инициализации).
    const container = requireElement("map");
    container.style.background = MAP_CONFIG.backgroundColor;

    const graph = new Graph();
    const initial = store.get();
    populateGraph(graph, data, initial.lang, initial.tab, initial.filters, pubDetailsByKey);

    const renderer = new Sigma(graph, container, {
      stagePadding: MAP_CONFIG.fitPadding,
      enableEdgeEvents: true,
      // Как и в MapLibre-версии (map.dragRotate.disable()) — это не
      // географическая карта, вращение холста не нужно.
      enableCameraRotation: false,
      // "Obsidian"-свечение выбранного/наведённого узла — см. map/build.ts.
      defaultDrawNodeHover: drawGlowingNodeHover,
    });

    // mountReactiveGraph дальше следит за store сама — остальным фичам
    // достаточно менять store.tab/lang/filters, не заботясь о том, что
    // ещё перерисовать.
    mountReactiveGraph(renderer, store, data, pubDetailsByKey);

    // mountSelection слушает клики по графу и пишет выбор в store;
    // mountPanel слушает store и рисует карточку; mountTabs слушает клики
    // по кнопкам вкладок и переключает список в сайдбаре; mountFilters —
    // регуляторы порогов; mountLangToggle слушает клик по кнопке языка.
    // Они не знают друг о друге напрямую — связь только через общий
    // Store. Функции отписки (unmount) не вызываются: все они живут всё
    // время работы страницы — здесь ничего не пересоздаётся поверх них
    // самих (внутри mountTabs свои unmount вызываются при смене вкладки —
    // это устройство самой этой фичи).
    mountSelection(renderer, store);
    mountPanel(store, data, pubDetailsByKey, authorDetailsByKey, repoDetailsByKey);
    mountTabs(
      requireElement("tab-buttons"),
      requireElement("tab-content"),
      store,
      renderer,
      data,
      pubDetailsByKey,
      repoDetailsByKey,
    );
    mountFilters(store);
    mountLangToggle(store);
    mountUrlSync(store, data);

    console.info("Граф отрисован (списки видны сразу, detail-файлы догружаются):", {
      департаменты: data.departments.length,
      авторы: data.authors.length,
      репозитории: data.repos.length,
      публикации: data.pubs.length,
    });

    // Дальше — три detail-файла фоном, каждый независимо от других: тот,
    // что придёт первым, сразу домешивается и будит подписчиков, не ждёт
    // остальных два. Если клик по узлу случится раньше, чем придёт его
    // detail, mountPanel сама покажет индикатор загрузки (LOADING в
    // features/panels.ts) — это единственное, что должно произойти, а не
    // пустая/сломанная карточка. У --public сборки authors-detail.json
    // вовсе нет — тогда loggedStep() залогирует 404, карточка автора так
    // и останется в состоянии "загрузка", без падения и без баннера.
    loadDetailsInto<PubDetail>("pubs-detail.json", DATA_CONFIG.pubDetailsUrl, pubDetailsByKey);
    loadDetailsInto<AuthorDetail>("authors-detail.json", DATA_CONFIG.authorDetailsUrl, authorDetailsByKey);
    loadDetailsInto<RepoDetail>("repos-detail.json", DATA_CONFIG.repoDetailsUrl, repoDetailsByKey);

    // Boot-экран прячется, welcome-hero показывается — но не поверх уже
    // осмысленной ссылки (urlState.selection !== null означает, что в URL
    // была не дефолтная вкладка/выбор, а не просто "страница открылась").
    start.finishBoot(urlState.selection !== null);
  })
  .catch((error: unknown) => {
    start.setBootStage("error");
    // loggedStep логирует только провал самого fetch/JSON.parse — ошибка,
    // брошенная уже ПОСЛЕ успешной загрузки (где-то в
    // mountReactiveGraph/mountSelection/... внутри .then() выше, включая
    // саму конструкцию Sigma — например, если в браузере недоступен
    // WebGL), в консоль сама по себе не попадает никак, потому что этот
    // .catch() ловит её молча. Логируем явно — иначе баннер "не удалось
    // загрузить данные графа" вводит в заблуждение (данные загрузились
    // нормально, упало что-то другое), а разобраться, что именно, без
    // единой строчки в консоли невозможно.
    console.error("[graph-data.json] сбой после успешной загрузки:", error);
    showLoadError(
      `Не удалось загрузить данные графа (${DATA_CONFIG.graphDataUrl}). ` +
        "Проверьте, что new_generate/generate_data.py сгенерировал файлы в data/gui/private.",
    );
  });

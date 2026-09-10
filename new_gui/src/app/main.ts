// Слой "app" — точка входа. Специально тонкий: собирает Store и фичи
// вместе, сам не содержит бизнес-логики — вся она в core/map/features.

import Graph from "graphology";
import Sigma from "sigma";
import type { Settings } from "sigma/settings";
import type { NodeDisplayData, PartialButFor } from "sigma/types";
import type { AuthorDetail, PubDetail, RepoDetail } from "../contracts/graph";
import { DATA_CONFIG, FILTER_CONFIG, MAP_CONFIG } from "../core/config";
import { loadDetails, loadGraphData, mergeDetailsInto } from "../core/data";
import { requireElement, showLoadError } from "../core/dom";
import { loggedStep } from "../core/log";
import { Store, type AppState } from "../core/state";
import { mountFilters } from "../features/filters";
import { mountGlobalSearch } from "../features/globalSearch";
import { mountPanel } from "../features/panels";
import { mountSelection } from "../features/selection";
import { mountStart } from "../features/start";
import { mountTabs } from "../features/tabs";
import { mountUrlSync } from "../features/urlSync";
import { mountReactiveGraph, mountZoomDebug, populateGraph } from "../map/build";

// Подпись узла — в цвет ЕГО ДЕПАРТАМЕНТА (data.color, тот же цвет, что и у
// самого узла), а не одним общим цветом на все узлы, с белой обводкой
// (context.strokeText до fillText), чтобы цветной текст не терялся на фоне
// такого же цвета соседних узлов/рёбер (прямая просьба — "не белым цветом
// подписывать узлы, а в цвет департамента с белой обводкой"). Позиция та же
// формула, что и у штатного Sigma drawDiscNodeLabel — просто вместо него.
//
// Используется и как settings.defaultDrawNodeLabel (обычная отрисовка), и
// как settings.defaultDrawNodeHover (наведённый мышью ИЛИ выбранный кликом
// узел, reducer в map/build.ts помечает его highlighted: true — для Sigma
// это один и тот же визуальный случай) — БЕЗ отдельного свечения
// (context.shadowBlur), которое было раньше: оно делало наведение почти
// неотличимым от выбора на вид ("наведение выглядит как выбор" — прямая
// жалоба), а простое увеличение размера выбранного узла уже даёт reducer
// (MAP_CONFIG.node.radiusSelected), отдельный ободок не нужен.
//
// Живёт здесь, а не в map/build.ts: конструкция `new Sigma(...)` и вообще
// любой рантайм-импорт из "sigma"/"sigma/rendering" на уровне модуля трогает
// WebGL2RenderingContext, которого нет в jsdom (тесты). map/build.ts
// импортируется тестами напрямую, а app/main.ts — нет.
function drawHaloedNodeLabel(
  context: CanvasRenderingContext2D,
  data: PartialButFor<NodeDisplayData, "x" | "y" | "size" | "label" | "color">,
  settings: Settings,
): void {
  if (!data.label) return;

  context.font = `${settings.labelWeight} ${settings.labelSize}px ${settings.labelFont}`;
  const x = data.x + data.size + 3;
  const y = data.y + settings.labelSize / 3;

  context.lineJoin = "round";
  context.lineWidth = MAP_CONFIG.node.labelHaloWidth;
  context.strokeStyle = MAP_CONFIG.node.labelHaloColor;
  context.strokeText(data.label, x, y);

  context.fillStyle = data.color;
  context.fillText(data.label, x, y);
}

// Единственное место, где создаётся Store — дальше он просто передаётся
// в конструкторы фич (features/*), которые сами решают, на какую часть
// state подписаться. yearMax по умолчанию — это FILTER_CONFIG.year.max
// (текущий год), а не отдельно вычисленный new Date().getFullYear(): так
// стартовое "без фильтра по году" значение и верхняя граница самого
// слайдера физически не могут разойтись, это одно и то же число.
const store = new Store<AppState>({
  screen: "menu",
  tab: 1,
  lang: "en",
  selection: null,
  filters: { minCoauth: 1, minSharedAuthors: 1, yearMax: FILTER_CONFIG.year.max, showNoDeptAuthors: true, showNoDeptPubs: true },
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

// Стартовый экран (boot-progress + меню, см. features/start.ts) —
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

    // Перезагрузка страницы ВСЕГДА показывает меню — прямая просьба
    // ("при перезагрузке сайта переходим на tab=start"), безусловно, даже
    // если в адресной строке была сохранённая ссылка на конкретный узел.
    // Store и так по умолчанию создан с screen: "menu" (см. выше) —
    // отдельного store.set() здесь не нужно. URL из адресной строки при
    // этом не читается вообще: разбор query (parseUrlState) остаётся
    // нужен только features/urlSync.ts — для popstate (кнопки
    // "назад"/"вперёд" браузера УЖЕ внутри текущей сессии), а не для
    // самого первого захода на страницу.

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
      // Одна и та же функция для обычной подписи и для наведённого/
      // выбранного узла (без отдельного свечения) — см. выше.
      defaultDrawNodeHover: drawHaloedNodeLabel,
      defaultDrawNodeLabel: drawHaloedNodeLabel,
      // Подпись узла рисуется, только когда сам узел на экране достаточно
      // крупный — иначе на маленьком зуме подписи наваливаются друг на
      // друга сплошным нечитаемым слоем. Число подбирается на глаз через
      // mountZoomDebug ниже.
      labelRenderedSizeThreshold: MAP_CONFIG.node.labelVisibleAtSize,
      // Штатное прореживание подписей по сетке экрана, независимое от
      // labelRenderedSizeThreshold — тоже подбирается на глаз.
      labelDensity: MAP_CONFIG.node.labelDensity,
      // Множитель zoom за один тик колеса — меньше дефолтного (1.7), чтобы
      // зум ощущался медленнее. Кривая одного тика (easing) у Sigma зашита
      // в коде жёстко, настройками не меняется — см. core/config.ts::camera.
      zoomingRatio: MAP_CONFIG.camera.zoomingRatio,
    });

    // mountReactiveGraph дальше следит за store сама — остальным фичам
    // достаточно менять store.tab/lang/filters, не заботясь о том, что
    // ещё перерисовать.
    mountReactiveGraph(renderer, store, data, pubDetailsByKey);
    // Временный инструмент калибровки MAP_CONFIG.region.ratioThreshold и
    // .node.labelVisibleAtSize — удалить вызов, когда числа подобраны.
    mountZoomDebug(renderer);

    // mountSelection слушает клики по графу и пишет выбор в store;
    // mountPanel слушает store и рисует карточку; mountTabs слушает клики
    // по кнопкам вкладок и переключает список в сайдбаре; mountFilters —
    // регуляторы порогов (язык теперь переключается кнопками меню внутри
    // самого mountStart, отдельного mountLangToggle больше нет);
    // mountGlobalSearch — окно поиска по всем видам сразу (клавиша "/"),
    // единственный способ найти департамент вообще. Они не знают друг о
    // друге напрямую — связь только через общий Store. Функции отписки
    // (unmount) не вызываются: все они живут всё время работы страницы —
    // здесь ничего не пересоздаётся поверх них самих (внутри mountTabs свои
    // unmount вызываются при смене вкладки — это устройство самой этой фичи).
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
    mountGlobalSearch(store, data, pubDetailsByKey, repoDetailsByKey);
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

    // Прячет boot-экран — под ним всегда меню (см. выше), features/start.ts
    // сама следит за store.screen и переключает видимость дальше.
    start.finishBoot();
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

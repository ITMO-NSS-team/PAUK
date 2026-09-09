// Слой "app" — точка входа. Специально тонкий: собирает карту, Store и
// фичи вместе, сам не содержит бизнес-логики — вся она в core/map/features.

import "maplibre-gl/dist/maplibre-gl.css";
import { Map as MapLibreMap, NavigationControl, setWorkerUrl } from "maplibre-gl";
// Параметр `?worker&url` говорит Vite собрать этот файл отдельным
// самодостаточным чанком воркера, а не тащить его через обычный
// оптимизатор зависимостей — без этого Vite ищет файл воркера не там,
// где он реально лежит, и карта падает в рантайме с "file does not exist".
import workerUrl from "maplibre-gl/dist/maplibre-gl-worker.mjs?worker&url";
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
import { mountTabs } from "../features/tabs";
import { mountUrlSync } from "../features/urlSync";
import { mountReactiveGraph, nodeBounds } from "../map/build";

setWorkerUrl(workerUrl);

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

// Тот же placeholder-стиль, что и в старом GUI: это не географическая
// карта, а холст для собственной раскладки графа, поэтому тайловый
// провайдер и API-ключ не нужны.
const map = new MapLibreMap({
  container: "map",
  style: {
    version: 8,
    sources: {},
    layers: [{ id: "bg", type: "background", paint: { "background-color": MAP_CONFIG.backgroundColor } }],
  },
  center: [0, 0],
  zoom: MAP_CONFIG.initialZoom,
  minZoom: MAP_CONFIG.minZoom,
  maxZoom: MAP_CONFIG.maxZoom,
  renderWorldCopies: false,
  attributionControl: false,
});

map.dragRotate.disable();
map.touchZoomRotate.disableRotation();
map.addControl(new NavigationControl({ showCompass: false }), "bottom-left");

// Без этого обработчика сбой самой MapLibre (например, воркер не
// загрузился — см. комментарий про workerUrl выше) проходит вообще без
// единого следа: map.on("load", ...) ниже просто никогда не сработает, а
// значит не сработает и вся логика логирования/баннера внутри него —
// с виду "пустая страница без единой ошибки в консоли". showLoadError()
// та же самая, что и на провал загрузки данных — пользователю неважно,
// что именно сломалось, важно что сломалось и что дальше рисовать нечего.
map.on("error", (e) => {
  console.error("[maplibre] ошибка карты:", e.error);
  showLoadError(`Карта не смогла инициализироваться: ${e.error.message}`);
});

// Источники/слои можно добавлять только после того, как стиль карты
// загрузился — поэтому вся отрисовка живёт внутри map.on("load", ...).
// Данные приходят из четырёх *.json в корне сайта (см. DATA_CONFIG в core/config.ts) —
// статика, которую пишет new_generate/generate_data.py в
// <repo_root>/data/gui/private (пока работаем только с приватным
// вариантом — публичный, урезанный, вариант данных подключим отдельно,
// когда дойдём до скрытия полей/усечения инициалов). Файлов может не
// быть, пока никто не прогнал генератор локально — тогда fetch ниже
// падает, loggedStep() логирует это в консоль, .catch() показывает
// баннер, приложение не рушится.
/**
 * Догружает один `*-detail.json` фоном и домешивает результат в уже
 * переданную фичам карту `target` — общая часть трёх одинаковых по форме
 * загрузок (публикации/авторы/репозитории) внутри `map.on("load", ...)`
 * ниже, отличающихся только типом `T`, именем шага, URL и картой-приёмником.
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

map.on("load", () => {
  // Приоритетная загрузка: сперва graph-data.json — этого одного достаточно,
  // чтобы нарисовать карту и все списки (summary-полей хватает на всё, что
  // видно сразу). Три *-detail.json грузятся уже ПОСЛЕ первой отрисовки,
  // фоном, не блокируя её — карта и списки не должны ждать самых тяжёлых
  // (потенциально) файлов ради полей, которые видны только по клику.
  loggedStep("graph-data.json", () => loadGraphData(DATA_CONFIG.graphDataUrl))
    .then((data) => {
      // Пустые карты передаются во все фичи один раз — заполняются на месте
      // (mergeDetailsInto), когда придёт соответствующий *-detail.json, см.
      // ниже. Мутация видна всем, кто уже держит эту же ссылку, без
      // повторного монтирования — только store.notify(), чтобы разбудить
      // то, что уже подписано на Store (mountPanel, mountReactiveGraph).
      const pubDetailsByKey = new Map<string, PubDetail>();
      const authorDetailsByKey = new Map<string, AuthorDetail>();
      const repoDetailsByKey = new Map<string, RepoDetail>();

      // URL при первой загрузке может задавать другую вкладку/выбор, чем
      // дефолт Store (например, открыли сохранённую ссылку) — применяем это
      // ДО монтирования остальных фич, чтобы они сразу увидели нужное
      // состояние, а не мигнули дефолтом и тут же переключились на него.
      store.set(parseUrlState(location.search, data));

      // mountReactiveGraph рисует граф под текущие tab/lang/filters и сама
      // следит за store дальше — остальным фичам достаточно менять
      // store.tab/lang/filters, не заботясь о том, что ещё перерисовать.
      mountReactiveGraph(map, store, data, pubDetailsByKey);
      map.fitBounds(nodeBounds(data), { padding: MAP_CONFIG.fitPadding, animate: false });

      // mountSelection слушает клики по карте и пишет выбор в store;
      // mountPanel слушает store и рисует карточку; mountTabs слушает клики
      // по кнопкам вкладок и переключает список в сайдбаре; mountFilters —
      // регуляторы порогов; mountLangToggle слушает клик по кнопке языка.
      // Они не знают друг о друге напрямую — связь только через общий
      // Store. Функции отписки (unmount) не вызываются: все они живут всё
      // время работы страницы — здесь ничего не пересоздаётся поверх них
      // самих (внутри mountTabs свои unmount вызываются при смене вкладки —
      // это устройство самой этой фичи).
      mountSelection(map, store);
      mountPanel(store, data, pubDetailsByKey, authorDetailsByKey, repoDetailsByKey);
      mountTabs(
        requireElement("tab-buttons"),
        requireElement("tab-content"),
        store,
        map,
        data,
        pubDetailsByKey,
        repoDetailsByKey,
      );
      mountFilters(store);
      mountLangToggle(store);
      mountUrlSync(store, data);

      console.info("Граф отрисован (карта и списки видны сразу, detail-файлы догружаются):", {
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
    })
    .catch((error: unknown) => {
      // loggedStep логирует только провал самого fetch/JSON.parse —
      // ошибка, брошенная уже ПОСЛЕ успешной загрузки (где-то в
      // mountReactiveGraph/fitBounds/mountSelection/... внутри .then()
      // выше), в консоль сама по себе не попадает никак, потому что этот
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
});

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
import { FILTER_CONFIG, MAP_CONFIG } from "../core/config";
import {
  loadSampleAuthorDetails,
  loadSampleGraphData,
  loadSamplePubDetails,
  loadSampleRepoDetails,
  mergeDetailsInto,
} from "../core/data";
import { requireElement } from "../core/dom";
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

// Источники/слои можно добавлять только после того, как стиль карты
// загрузился — поэтому вся отрисовка живёт внутри map.on("load", ...).
// Данные пока синтетические (v2-прототип), реальный pauk/gui/data не
// трогаем.
map.on("load", () => {
  // Приоритетная загрузка: сперва graph-data.json — этого одного достаточно,
  // чтобы нарисовать карту и все списки (summary-полей хватает на всё, что
  // видно сразу). Три *-detail.json грузятся уже ПОСЛЕ первой отрисовки,
  // фоном, не блокируя её — карта и списки не должны ждать самых тяжёлых
  // (потенциально) файлов ради полей, которые видны только по клику.
  loadSampleGraphData()
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
      // пустая/сломанная карточка.
      loadSamplePubDetails()
        .then((details) => {
          mergeDetailsInto(pubDetailsByKey, details);
          store.notify();
        })
        .catch((error: unknown) => console.error("Не удалось догрузить детали публикаций:", error));

      loadSampleAuthorDetails()
        .then((details) => {
          mergeDetailsInto(authorDetailsByKey, details);
          store.notify();
        })
        .catch((error: unknown) => console.error("Не удалось догрузить детали авторов:", error));

      loadSampleRepoDetails()
        .then((details) => {
          mergeDetailsInto(repoDetailsByKey, details);
          store.notify();
        })
        .catch((error: unknown) => console.error("Не удалось догрузить детали репозиториев:", error));
    })
    .catch((error: unknown) => {
      console.error("Не удалось загрузить или отрисовать фикстур-данные:", error);
    });
});

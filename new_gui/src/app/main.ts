// Слой "app" — точка входа. Специально тонкий: собирает карту, Store и
// фичи вместе, сам не содержит бизнес-логики — вся она в core/map/features.

import "maplibre-gl/dist/maplibre-gl.css";
import { Map as MapLibreMap, NavigationControl, setWorkerUrl } from "maplibre-gl";
// Параметр `?worker&url` говорит Vite собрать этот файл отдельным
// самодостаточным чанком воркера, а не тащить его через обычный
// оптимизатор зависимостей — без этого Vite ищет файл воркера не там,
// где он реально лежит, и карта падает в рантайме с "file does not exist".
import workerUrl from "maplibre-gl/dist/maplibre-gl-worker.mjs?worker&url";
import { loadSampleGraphData } from "../core/data";
import { requireElement } from "../core/dom";
import { Store, type AppState } from "../core/state";
import { mountLangToggle } from "../features/langToggle";
import { mountPanel } from "../features/panels";
import { mountSelection } from "../features/selection";
import { mountTabs } from "../features/tabs";
import { mountGraphLayers, nodeBounds } from "../map/build";

setWorkerUrl(workerUrl);

// Единственное место, где создаётся Store — дальше он просто передаётся
// в конструкторы фич (features/*), которые сами решают, на какую часть
// state подписаться. Значения по умолчанию ни на что пока не влияют
// (фильтры/вкладки/язык ещё не читает ни один код) — заданы такими, какими
// реально должны быть, когда соответствующие фичи появятся, а не "как попало".
const store = new Store<AppState>({
  tab: 1,
  lang: "ru",
  selection: null,
  filters: { minCoauth: 1, minPubAuthors: 1, yearMax: new Date().getFullYear() },
});

// Тот же placeholder-стиль, что и в старом GUI: это не географическая
// карта, а холст для собственной раскладки графа, поэтому тайловый
// провайдер и API-ключ не нужны.
const map = new MapLibreMap({
  container: "map",
  style: {
    version: 8,
    sources: {},
    layers: [{ id: "bg", type: "background", paint: { "background-color": "#ffffff" } }],
  },
  center: [0, 0],
  zoom: 5,
  minZoom: 4.3,
  maxZoom: 15,
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
  loadSampleGraphData()
    .then((data) => {
      mountGraphLayers(map, data, store.get().lang, store.get().tab);
      map.fitBounds(nodeBounds(data), { padding: 40, animate: false });

      // mountSelection слушает клики по карте и пишет выбор в store;
      // mountPanel слушает store и рисует карточку; mountTabs слушает клики
      // по кнопкам вкладок и переключает список в сайдбаре; mountLangToggle
      // слушает клик по кнопке языка и пишет lang в store. Они не знают
      // друг о друге напрямую — связь только через общий Store. Функции
      // отписки (unmount) не вызываются: все они живут всё время работы
      // страницы — здесь ничего не пересоздаётся поверх них самих (внутри
      // mountTabs свои unmount вызываются при смене вкладки — это
      // устройство самой этой фичи).
      mountSelection(map, store);
      mountPanel(store, data);
      mountTabs(requireElement("tab-buttons"), requireElement("tab-content"), store, map, data);
      mountLangToggle(store, map, data);

      console.info("Граф отрисован:", {
        департаменты: data.departments.length,
        авторы: data.authors.length,
        репозитории: data.repos.length,
        публикации: data.pubs.length,
      });
    })
    .catch((error: unknown) => {
      console.error("Не удалось загрузить или отрисовать фикстур-данные:", error);
    });
});

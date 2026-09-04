import "maplibre-gl/dist/maplibre-gl.css";
import { Map as MapLibreMap, NavigationControl, setWorkerUrl } from "maplibre-gl";
// Параметр `?worker&url` говорит Vite собрать этот файл отдельным
// самодостаточным чанком воркера, а не тащить его через обычный
// оптимизатор зависимостей — без этого Vite ищет файл воркера не там,
// где он реально лежит, и карта падает в рантайме с "file does not exist".
import workerUrl from "maplibre-gl/dist/maplibre-gl-worker.mjs?worker&url";
import { loadSampleGraphData } from "../core/data";
import { mountGraphLayers, nodeBounds } from "../map/build";

setWorkerUrl(workerUrl);

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
// трогаем. Клик/hover/выбор узла — отдельный следующий шаг
// (features/selection.ts), здесь только статичная отрисовка.
map.on("load", () => {
  loadSampleGraphData()
    .then((data) => {
      mountGraphLayers(map, data);
      map.fitBounds(nodeBounds(data), { padding: 40, animate: false });
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

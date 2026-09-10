// Слой "map" — превращает GraphData в graphology.Graph и рисует его через
// Sigma.js. Логика интерпретации данных (какая подпись у узла, как искать
// узел по ключу) живёт в core/data.ts — этот файл только про сам граф и его
// отрисовку, ничего не решает про данные.
//
// Важно: рендерер показывает не всё сразу, а один из ТРЁХ РАЗНЫХ ГРАФОВ —
// какой набор узлов/рёбер рисовать, зависит от активной вкладки: вкладка
// "Авторы" — только авторы и соавторство, "Репозитории" — только
// репозитории и их связи, "Публикации" — только публикации. Показывать все
// сущности одновременно было бы другим (и неверным) поведением, а не тем же
// графом "покрасивее".

import type Graph from "graphology";
import type Sigma from "sigma";
import type { Camera } from "sigma";
import type { EdgeDisplayData, NodeDisplayData } from "sigma/types";
import type { AuthorNode, Edge, GraphData, PubDetail, PubNode, RepoNode } from "../contracts/graph";
import { MAP_CONFIG, NO_DEPT_COLOR } from "../core/config";
import { nodeLabel } from "../core/data";
import { localize, type Lang } from "../core/i18n";
import type { AppState, Selection, Store, TabId } from "../core/state";

type GraphNode = AuthorNode | RepoNode | PubNode;
type Filters = AppState["filters"];
type PubDetailsByKey = Map<string, PubDetail>;

// "dept:<id>" — совпадает по формату с features/search/index.ts::deptHitKey,
// но это не тот же ключ и не общий хелпер: там это ключ строки списка
// результатов поиска, здесь — настоящий id узла графа (graph.addNode,
// graph.hasNode, приходит в события clickNode). Разные причины меняться,
// поэтому не объединяю в одну функцию ради формального неповторения кода.
const DEPT_NODE_PREFIX = "dept:";

/**
 * Строит ключ узла-"якоря подписи" департамента (см. {@link addDeptLabelAnchors},
 * {@link applyGraphStyling}) — не блоб вместо реальных узлов, а невидимая
 * точка, которую Sigma подписывает именем департамента на маленьком зуме.
 * Коллизии с настоящими ключами узлов (OpenAlex id вида `"A5133538481"`,
 * repo-ключи вида `"owner/repo"`) практически исключены.
 *
 * @param deptId - `Department.id`.
 * @returns Строковый ключ вида `"dept:0"`.
 */
export function deptNodeKey(deptId: number): string {
  return `${DEPT_NODE_PREFIX}${deptId}`;
}

/**
 * Обратное преобразование к {@link deptNodeKey}.
 *
 * @param key - ключ узла графа.
 * @returns `Department.id`, если `key` — ключ узла-якоря; иначе `null`
 *   (обычный узел графа — автор/репозиторий/публикация).
 *
 * @example
 * parseDeptNodeKey("dept:3"); // 3
 * parseDeptNodeKey("A5133538481"); // null
 */
export function parseDeptNodeKey(key: string): number | null {
  if (!key.startsWith(DEPT_NODE_PREFIX)) return null;
  const id = Number(key.slice(DEPT_NODE_PREFIX.length));
  return Number.isNaN(id) ? null : id;
}

/**
 * Обрезает подпись узла НА КАРТЕ до `maxLength` символов с многоточием.
 * `core/data.ts::nodeLabel()` остаётся полным — та же строка нужна ещё
 * сайдбару и поиску, где длина не проблема (список сам переносит/обрезает
 * CSS-эллипсисом свою строку). На карте длинное название публикации иначе
 * растягивается на 1-2 строки и перекрывает соседние узлы.
 *
 * @param label - исходная подпись.
 * @param maxLength - максимальная длина без учёта многоточия.
 * @returns Подпись как есть, если короче `maxLength`, иначе обрезанная с `"…"`.
 */
function truncateLabel(label: string, maxLength: number): string {
  return label.length > maxLength ? `${label.slice(0, maxLength - 1)}…` : label;
}

/**
 * Id синтетического департамента "Без департамента" (см.
 * `new_generate/departments.py`), если он есть в данных — определяется по
 * цвету ({@link NO_DEPT_COLOR}, должен совпадать с
 * `new_generate/config.py::NO_DEPT_COLOR`), а не по имени: имя локализуется
 * и могло бы разъехаться между ru/en, цвет — техническая константа.
 *
 * @param data - данные графа.
 * @returns Id департамента-заглушки, либо `null`, если такого нет в данных
 *   (например, на тестовых фикстурах) — тогда фильтр "без департамента"
 *   просто ничего не отсекает, а не падает.
 */
function noDeptId(data: GraphData): number | null {
  return data.departments.find((dept) => dept.color === NO_DEPT_COLOR)?.id ?? null;
}

/**
 * Возвращает узлы, которые должна показывать активная вкладка.
 *
 * Фильтры узлов: год публикации на вкладке 3 (`filters.yearMax`) —
 * публикации без известного года (`year === null`) никогда не скрываются
 * этим фильтром, мы не знаем их год, а не знаем, что он "слишком поздний",
 * это разные вещи; и "без департамента" на вкладках 1/3
 * (`filters.showNoDeptAuthors`/`showNoDeptPubs`) — скрывает узлы, чей `dept`
 * указывает на синтетический "Без департамента" (см. {@link noDeptId}).
 *
 * @param data - данные графа.
 * @param tab - активная вкладка.
 * @param filters - текущие пороги фильтров.
 * @returns Список узлов, которые нужно нарисовать для этой вкладки.
 */
function tabGraphNodes(data: GraphData, tab: TabId, filters: Filters): GraphNode[] {
  const excludedDept = noDeptId(data);
  switch (tab) {
    case 1:
      return filters.showNoDeptAuthors ? data.authors : data.authors.filter((a) => a.dept !== excludedDept);
    case 2:
      return data.repos;
    case 3: {
      const pubs = data.pubs.filter((pub) => pub.year === null || pub.year <= filters.yearMax);
      return filters.showNoDeptPubs ? pubs : pubs.filter((pub) => pub.dept !== excludedDept);
    }
  }
}

/**
 * Возвращает рёбра, которые должна показывать активная вкладка — тот же
 * принцип, что и {@link tabGraphNodes}, плюс порог веса: на вкладке
 * "Авторы" прячутся слабые связи соавторства (меньше `filters.minCoauth`
 * совместных публикаций), на "Публикациях" — связи между публикациями с
 * малым числом общих авторов (`filters.minSharedAuthors`). У репозиториев
 * (вкладка 2) порога веса нет вообще — как и в старом GUI.
 *
 * @param data - данные графа.
 * @param tab - активная вкладка.
 * @param filters - текущие пороги фильтров.
 * @returns Список рёбер, которые нужно нарисовать для этой вкладки.
 */
function tabGraphEdges(data: GraphData, tab: TabId, filters: Filters): Edge[] {
  switch (tab) {
    case 1:
      return data.coauth_edges.filter((edge) => edge.w >= filters.minCoauth);
    case 2:
      return data.repo_edges;
    case 3:
      return data.pub_edges.filter((edge) => edge.w >= filters.minSharedAuthors);
  }
}

/**
 * Наполняет граф под текущие tab/lang/filters — вызывается и при первом
 * монтировании, и при каждой смене вкладки/языка/фильтров.
 *
 * `graph.clear()` перед пересборкой — тот же принцип "полная пересборка, не
 * keyed-diff", что уже используется в core/render.ts::renderList (осознанный
 * выбор простоты на реалистичном масштабе). Отдельного "setData()", как у
 * MapLibre, здесь не нужно: Sigma сама слушает события graphology
 * (добавление/удаление узла и ребра, clear) и перерисовывается по ним.
 *
 * @param graph - graphology-граф, который нужно наполнить (мутируется на месте).
 * @param data - данные графа.
 * @param lang - язык интерфейса (влияет на подпись узла).
 * @param tab - активная вкладка.
 * @param filters - текущие пороги фильтров.
 * @param pubDetails - карта деталей публикаций (для настоящих названий публикаций в подписях).
 */
export function populateGraph(
  graph: Graph,
  data: GraphData,
  lang: Lang,
  tab: TabId,
  filters: Filters,
  pubDetails: PubDetailsByKey,
): void {
  graph.clear();

  const deptColorById = new Map(data.departments.map((dept) => [dept.id, dept.color]));

  for (const node of tabGraphNodes(data, tab, filters)) {
    graph.addNode(node.key, {
      x: node.gx,
      y: node.gy,
      size: MAP_CONFIG.node.radius,
      color: deptColorById.get(node.dept) ?? MAP_CONFIG.node.fallbackColor,
      label: truncateLabel(nodeLabel(node, lang, pubDetails), MAP_CONFIG.node.labelMaxLength),
    });
  }

  // Ребро рисуется, только если оба конца попали в граф этой же вкладки.
  // graph.hasNode() уже отражает фильтрацию узлов выше (например,
  // публикация, скрытая filters.yearMax, просто не была добавлена) —
  // отдельной повторной проверки года на рёбрах, как в MapLibre-версии
  // (там позиции узлов не зависели от фильтра), здесь не нужно.
  for (const edge of tabGraphEdges(data, tab, filters)) {
    if (!graph.hasNode(edge.s) || !graph.hasNode(edge.t)) continue;
    // mergeEdge, не addEdge: не должно происходить на согласованных данных,
    // но addEdge бросает исключение при повторной паре узлов, а mergeEdge —
    // нет (просто обновит атрибуты уже существующего ребра тем же значением).
    graph.mergeEdge(edge.s, edge.t, {
      size: MAP_CONFIG.edge.width,
      color: MAP_CONFIG.edge.color,
      weight: edge.w,
    });
  }

  addDeptLabelAnchors(graph, data, tab, filters, lang);
}

/**
 * Добавляет по одному невидимому "якорю подписи" на департамент, у которого
 * в текущей вкладке есть хотя бы один реальный узел. Не блоб вместо реальных
 * узлов (так было в прошлой версии этого файла — при ближайшем изучении
 * `pauk/gui/web/overlay.js` оказалось, что старый GUI делал иначе и лучше):
 * реальные точки видны ВСЕГДА, на любом зуме, а якорь — это точка с
 * `size: 0`, которую Sigma подписывает именем департамента через
 * `forceLabel`, пока камера отдалена (см. {@link applyGraphStyling}) — то же
 * самое плавающее название департамента, что рисовал старый GUI на отдельном
 * canvas-оверлее, только через штатный labels-пайплайн самой Sigma.
 *
 * Пересчитывается заново на каждый вызов {@link populateGraph}, а не хранится
 * отдельно: раскладка ForceAtlas2 у авторов/репозиториев/публикаций разная,
 * общий "один центр департамента на все три графа" не совпал бы ни с одним
 * из них. Позиция — центроид (среднее `gx`/`gy`) узлов департамента в ЭТОЙ вкладке.
 *
 * Рёбра между департаментами (`data.dept_edges`, уже посчитаны бэкендом) тоже
 * добавляются, но {@link applyGraphStyling} красит их `hidden: true`
 * безусловно — они никогда не рисуются линией (старый GUI департаменты
 * линиями не соединял вовсе), нужны только затем, чтобы уже существующий
 * механизм притухания не-соседей (`graph.areNeighbors()`) сам заработал и
 * для выбора департамента, без отдельного кода.
 *
 * @param graph - graphology-граф (тот же, что уже наполнен реальными узлами/рёбрами выше).
 * @param data - данные графа.
 * @param tab - активная вкладка.
 * @param filters - текущие пороги фильтров.
 * @param lang - язык интерфейса (влияет на подпись).
 */
function addDeptLabelAnchors(graph: Graph, data: GraphData, tab: TabId, filters: Filters, lang: Lang): void {
  const nodesByDept = new Map<number, GraphNode[]>();
  for (const node of tabGraphNodes(data, tab, filters)) {
    const list = nodesByDept.get(node.dept) ?? [];
    list.push(node);
    nodesByDept.set(node.dept, list);
  }

  const deptById = new Map(data.departments.map((dept) => [dept.id, dept]));

  for (const [deptId, nodes] of nodesByDept) {
    const dept = deptById.get(deptId);
    if (!dept) continue;

    const cx = nodes.reduce((sum, node) => sum + node.gx, 0) / nodes.length;
    const cy = nodes.reduce((sum, node) => sum + node.gy, 0) / nodes.length;

    graph.addNode(deptNodeKey(deptId), {
      x: cx,
      y: cy,
      size: 0,
      color: dept.color,
      label: localize(dept.name, dept.name_en, lang),
    });
  }

  for (const edge of data.dept_edges) {
    if (!graph.hasNode(deptNodeKey(edge.s)) || !graph.hasNode(deptNodeKey(edge.t))) continue;
    graph.mergeEdge(deptNodeKey(edge.s), deptNodeKey(edge.t), {
      size: MAP_CONFIG.edge.width,
      color: MAP_CONFIG.edge.color,
      weight: edge.w,
    });
  }
}

/**
 * Регистрирует ЕДИНЫЙ nodeReducer/edgeReducer, отвечающий за весь внешний
 * вид узлов/рёбер поверх того, что записано в самих их атрибутах. `setSetting`
 * — это ЗАМЕНА предыдущего значения, а не композиция: если завести две разные
 * функции под разные заботы, каждая своим отдельным `setSetting`, вторая
 * просто перезатрёт первую. Поэтому вся логика оформления — в одном месте.
 *
 * Реducer'ы читают `store.get().selection` и локальные `hoveredNode`/
 * `cameraRatio` из замыкания при каждом рендере — сами они ничего не
 * перерисовывают, реальную перерисовку просит `renderer.refresh()` (в
 * {@link mountReactiveGraph} — на смену selection, и здесь же — на смену
 * hoveredNode/cameraRatio через возвращённые setter'ы).
 *
 * Три заботы сразу:
 * 1. **Подписи департаментов vs подписи узлов** — пока `cameraRatio` больше
 *    {@link MAP_CONFIG.region.ratioThreshold}, узлы-якоря департаментов
 *    (см. {@link deptNodeKey}) получают `forceLabel: true` (их размер — 0,
 *    без forceLabel подпись никогда не прошла бы порог размера), а обычные
 *    узлы теряют свою подпись — кроме выбранного, тот виден на любом зуме.
 *    Когда камера приближена — наоборот: обычные подписи по
 *    {@link MAP_CONFIG.node.labelVisibleAtSize}, департаменты не форсируются
 *    и остаются без подписи (у них и так size: 0).
 * 2. **Видимость рёбер** — реальные рёбра прячутся (`hidden: true`), когда
 *    `cameraRatio` больше {@link MAP_CONFIG.edge.visibleBelowRatio} (на
 *    сильном отдалении тысячи рёбер сливаются в сплошную дымку); рёбра между
 *    департаментами скрыты ВСЕГДА (нужны только для соседства в п.3).
 * 3. **Подсветка/притухание** ("Obsidian"-style) — три уровня яркости, а не
 *    два. "Фокус" — наведённый мышью узел, а если наведения нет — выбранный
 *    кликом узел или департамент. Пока фокус есть: сам фокус — крупнее
 *    ({@link MAP_CONFIG.node.radiusSelected}), его соседи
 *    (`graph.areNeighbors`) — чуть крупнее обычного
 *    ({@link MAP_CONFIG.node.neighborSizeScale}), всё остальное — тускнеет в
 *    {@link MAP_CONFIG.node.dimColor} (полупрозрачный — "замылить", а не
 *    сплошной серый) и теряет подпись. Рёбра, не касающиеся фокуса, при этом
 *    `hidden: true` целиком — "остальные рёбра убрать", прямая просьба.
 *
 * @param renderer - Sigma-рендерер.
 * @param store - Store приложения.
 * @returns `setHoveredNode`/`setCameraRatio` — вызывать из обработчиков
 *   `enterNode`/`leaveNode` и `camera.on("updated", ...)` соответственно
 *   (см. {@link mountReactiveGraph}).
 */
function applyGraphStyling(
  renderer: Sigma,
  store: Store<AppState>,
): { setHoveredNode: (key: string | null) => void; setCameraRatio: (ratio: number) => void } {
  const graph = renderer.getGraph();
  let hoveredNode: string | null = null;
  let cameraRatio = 1;

  function showingRegionLabels(): boolean {
    return cameraRatio > MAP_CONFIG.region.ratioThreshold;
  }

  function focusKey(): string | null {
    if (hoveredNode) return hoveredNode;
    const selection = store.get().selection;
    if (selection?.kind === "node") return selection.key;
    if (selection?.kind === "dept") return deptNodeKey(selection.id);
    return null;
  }

  renderer.setSetting("nodeReducer", (nodeKey, data): Partial<NodeDisplayData> => {
    const isRegion = parseDeptNodeKey(nodeKey) !== null;
    const res: Partial<NodeDisplayData> = { ...data };
    const selection = store.get().selection;
    const isSelected =
      (selection?.kind === "node" && selection.key === nodeKey) ||
      (selection?.kind === "dept" && deptNodeKey(selection.id) === nodeKey);

    if (isRegion) {
      res.forceLabel = showingRegionLabels();
    } else if (showingRegionLabels() && !isSelected) {
      // Пока показываются имена департаментов, обычные подписи не рисуются
      // вовсе (кроме выбранного узла — он виден на любом зуме) — иначе оба
      // вида подписей накладывались бы друг на друга на сильном отдалении.
      res.label = "";
    }

    if (isSelected) {
      res.highlighted = true;
      // Только у реальных узлов — у якоря департамента size всегда 0,
      // фиксированная абсолютная radiusSelected сделала бы его видимым
      // кружком там, где его никогда не было.
      if (!isRegion) res.size = MAP_CONFIG.node.radiusSelected;
    }

    const focus = focusKey();
    // isSelected исключён отдельно: выбранный узел/департамент — сам себе
    // фокус, когда ничего не наведено, но должен оставаться ярким, даже
    // пока наводят на что-то другое, а не тускнеть под собственной подсветкой.
    // focus !== nodeKey исключает сам фокус (наведённый, но не выбранный узел) —
    // у него уже нет ни radiusSelected, ни повода тускнеть или расти как сосед.
    if (!isSelected && focus && focus !== nodeKey) {
      if (graph.areNeighbors(focus, nodeKey)) {
        // Якоря департаментов (size: 0) не растим — фиксированный размер
        // сделал бы невидимую точку видимым кружком там, где его не было.
        if (!isRegion) res.size = MAP_CONFIG.node.radius * MAP_CONFIG.node.neighborSizeScale;
      } else {
        res.color = MAP_CONFIG.node.dimColor;
        res.label = "";
      }
    }

    return res;
  });

  renderer.setSetting("edgeReducer", (edgeKey, data): Partial<EdgeDisplayData> => {
    const [s, t] = graph.extremities(edgeKey);
    // Рёбра между департаментами никогда не рисуются линией (в отличие от
    // реальных, они существуют в графе только для areNeighbors() выше).
    if (parseDeptNodeKey(s) !== null) return { ...data, hidden: true };

    if (cameraRatio > MAP_CONFIG.edge.visibleBelowRatio) return { ...data, hidden: true };

    const selection = store.get().selection;
    const isSelectedEdge =
      selection?.kind === "edge" &&
      ((s === selection.s && t === selection.t) || (s === selection.t && t === selection.s));

    if (isSelectedEdge) {
      return { ...data, color: MAP_CONFIG.edge.colorSelected, size: MAP_CONFIG.edge.widthSelected };
    }

    const focus = focusKey();
    if (focus && s !== focus && t !== focus) {
      return { ...data, hidden: true };
    }

    return data;
  });

  return {
    setHoveredNode(key) {
      if (hoveredNode === key) return;
      hoveredNode = key;
      renderer.refresh();
    },
    setCameraRatio(ratio) {
      if (cameraRatio === ratio) return;
      cameraRatio = ratio;
      renderer.refresh();
    },
  };
}

/**
 * Плавно подлетает камерой к только что выбранному узлу/департаменту —
 * единая точка "красивого" фокуса камеры для ЛЮБОГО источника выбора (клик
 * по графу, по строке списка вкладки, по результату поиска в панели):
 * все они одинаково пишут `store.selection`, а саму анимацию запускает
 * только {@link mountReactiveGraph} через подписку на Store. Отдельные
 * места, где раньше был свой `camera.animate` (например,
 * features/tabs/nodeListTab.ts), его больше не вызывают — дублировать
 * анимацию незачем.
 *
 * Ребро (`selection.kind === "edge"`) камеру не двигает — у ребра нет одной
 * точки, к которой имело бы смысл приближаться (это остаётся как раньше,
 * ребро просто подсвечивается через edgeReducer).
 *
 * @param camera - Sigma-камера рендерера.
 * @param graph - graphology-граф (нужен, чтобы прочитать x/y выбранного узла/якоря департамента).
 * @param selection - новое выбранное состояние (см. {@link Selection}).
 */
function flyToSelection(camera: Camera, graph: Graph, selection: Selection): void {
  if (selection === null || selection.kind === "edge") return;

  const key = selection.kind === "node" ? selection.key : deptNodeKey(selection.id);
  if (!graph.hasNode(key)) return;

  const { x, y } = graph.getNodeAttributes(key);
  camera.animate(
    { x, y, ratio: MAP_CONFIG.camera.focusRatio },
    { duration: MAP_CONFIG.camera.focusDuration, easing: "quadraticInOut" },
  );
}

/**
 * Единственная точка входа для реактивности графа: принимает уже
 * построенный Sigma-рендерер (граф внутри него уже наполнен под начальное
 * состояние — см. {@link populateGraph} и app/main.ts, где это делается
 * ДО конструирования Sigma, ей нужен непустой граф с самого начала) и
 * дальше следит за store — пересобирает узлы/рёбра при смене вкладки,
 * языка ИЛИ фильтров, и при смене selection обновляет подсветку (через
 * reducer + `refresh()`, без пересборки графа) и подлетает камерой к
 * выбранному (см. {@link flyToSelection}).
 *
 * Рендерер принимается снаружи, а не строится внутри этой функции, —
 * так же, как в MapLibre-версии `map` строился в app/main.ts, а не в
 * mountReactiveGraph: конструктору Sigma нужен реальный WebGL-контекст,
 * которого нет в тестовом jsdom-окружении, а тестировать логику
 * пересборки/подсветки нужно без браузера — с фейковым объектом вместо
 * настоящего рендерера (см. tests/build.test.ts).
 *
 * @param renderer - уже построенный Sigma-рендерер с наполненным графом.
 * @param store - Store приложения.
 * @param data - данные графа.
 * @param pubDetails - карта деталей публикаций.
 * @returns Функцию отписки (unmount) — снимает подписку на Store и обработчики hover-событий.
 */
export function mountReactiveGraph(
  renderer: Sigma,
  store: Store<AppState>,
  data: GraphData,
  pubDetails: PubDetailsByKey,
): () => void {
  const graph = renderer.getGraph();
  const { setHoveredNode, setCameraRatio } = applyGraphStyling(renderer, store);

  // Отдельная пара обработчиков от той, что в features/selection.ts —
  // там enterNode/leaveNode меняют курсор (что делать по клику), здесь —
  // что подсвечивать (как рисовать). Разные слушатели одного и того же
  // события у Sigma не конфликтуют между собой.
  function onEnterNode({ node }: { node: string }): void {
    setHoveredNode(node);
  }
  function onLeaveNode(): void {
    setHoveredNode(null);
  }
  renderer.on("enterNode", onEnterNode);
  renderer.on("leaveNode", onLeaveNode);

  // Камера сообщает свой ratio отдельным событием, не через store — это не
  // состояние приложения, а состояние самого рендерера (как и hoveredNode).
  // Начальное значение читается сразу же, а не только с первого движения
  // камеры — иначе до первого зума/панорамирования показывался бы неверный
  // (дефолтный) режим подписей департаментов/узлов.
  const camera = renderer.getCamera();
  function onCameraUpdated(state: { ratio: number }): void {
    setCameraRatio(state.ratio);
  }
  camera.on("updated", onCameraUpdated);
  setCameraRatio(camera.getState().ratio);

  let prev = store.get();
  const unsubscribe = store.subscribe((state) => {
    // filters — новый объект только когда его реально меняли (Store.set
    // мержит патч поверх состояния, не трогая поля вне патча), поэтому
    // сравнение по ссылке здесь корректно и дешевле глубокого сравнения.
    if (state.tab !== prev.tab || state.lang !== prev.lang || state.filters !== prev.filters) {
      prev = state;
      populateGraph(graph, data, state.lang, state.tab, state.filters, pubDetails);
      return;
    }
    if (state.selection !== prev.selection) {
      prev = state;
      // Реducer уже видит новый store.get().selection к этому моменту —
      // refresh() только просит Sigma позвать реducer'ы заново и
      // перерисоваться, без пересборки графа.
      renderer.refresh();
      flyToSelection(camera, graph, state.selection);
    }
  });

  return () => {
    renderer.off("enterNode", onEnterNode);
    renderer.off("leaveNode", onLeaveNode);
    camera.off("updated", onCameraUpdated);
    unsubscribe();
  };
}

/**
 * Небольшой отладочный индикатор текущего `camera.ratio` сбоку экрана —
 * инструмент для подбора {@link MAP_CONFIG.region.ratioThreshold},
 * {@link MAP_CONFIG.node.labelVisibleAtSize} и {@link MAP_CONFIG.edge.visibleBelowRatio}
 * на глаз, не постоянный элемент интерфейса. Создаёт DOM-элемент сам, а не
 * через разметку в `index.html` — убрать индикатор после калибровки можно
 * одной строкой в `app/main.ts`, без правки вёрстки.
 *
 * @param renderer - Sigma-рендерер.
 * @returns Функцию отписки (unmount) — снимает обработчик и удаляет элемент из DOM.
 */
export function mountZoomDebug(renderer: Sigma): () => void {
  const el = document.createElement("div");
  Object.assign(el.style, {
    position: "fixed",
    bottom: "12px",
    left: "12px",
    zIndex: "40",
    padding: "4px 8px",
    background: "rgba(0, 0, 0, 0.6)",
    color: "#fff",
    fontSize: "11px",
    fontFamily: "monospace",
    borderRadius: "4px",
    pointerEvents: "none",
  });
  document.body.appendChild(el);

  const camera = renderer.getCamera();
  function render(): void {
    el.textContent = `zoom ratio: ${camera.getState().ratio.toFixed(3)}`;
  }
  camera.on("updated", render);
  render();

  return () => {
    camera.off("updated", render);
    el.remove();
  };
}

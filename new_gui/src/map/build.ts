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
import type { EdgeDisplayData, NodeDisplayData } from "sigma/types";
import type { AuthorNode, Edge, GraphData, PubDetail, PubNode, RepoNode } from "../contracts/graph";
import { MAP_CONFIG, NO_DEPT_COLOR } from "../core/config";
import { nodeLabel } from "../core/data";
import { localize, type Lang } from "../core/i18n";
import { isRegionMode, type AppState, type Selection, type Store, type TabId } from "../core/state";

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
export function noDeptId(data: GraphData): number | null {
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
export function tabGraphNodes(data: GraphData, tab: TabId, filters: Filters): GraphNode[] {
  const excludedDept = noDeptId(data);
  switch (tab) {
    case 1:
      return filters.showNoDeptAuthors
        ? data.authors
        : data.authors.filter((a) => a.dept !== excludedDept);
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
      dept: node.dept,
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
function addDeptLabelAnchors(
  graph: Graph,
  data: GraphData,
  tab: TabId,
  filters: Filters,
  lang: Lang,
): void {
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
 * 1. **Якоря департаментов** (см. {@link deptNodeKey}) никогда не
 *    подписываются и не подсвечиваются — названия департаментов рисуют
 *    регионы (map/regions.ts). Выбор департамента оставляет яркими узлы
 *    этого департамента и притушает остальные.
 * 2. **Видимость рёбер** — реальные рёбра прячутся (`hidden: true`), когда
 *    `cameraRatio` больше пользовательского порога
 *    `store.get().filters.edgeZoomThreshold` (features/filters.ts — на
 *    сильном отдалении тысячи рёбер сливаются в сплошную дымку); рёбра между
 *    департаментами скрыты ВСЕГДА (нужны только для соседства в п.3).
 * 3. **Подсветка/притухание** ("Obsidian"-style) — три уровня яркости, а не
 *    два, и ДВА НЕЗАВИСИМЫХ источника фокуса сразу, а не один вместо
 *    другого: выбор кликом (см. {@link selectionFocusKey}) остаётся
 *    "закреплённым" фокусом независимо от того, что происходит с мышью —
 *    раньше наведение на любой другой узел ПЕРЕБИВАЛО выбор целиком
 *    (подсветка/рёбра выбранного узла пропадали при простом движении
 *    курсора, прямая жалоба), а после первой попытки исправить это выбор
 *    стал наоборот полностью ИГНОРИРОВАТЬ наведение, что тоже неверно —
 *    заблокировало предпросмотр соседей других узлов, пока что-то уже
 *    выбрано (тоже прямая жалоба: "мы должны иметь возможность искать
 *    дальше"). Правильно — оба источника активны одновременно (см.
 *    {@link isNeighborOf}): узел ярче обычного, если он выбранный,
 *    наведённый, сосед любого из них, ИЛИ один из двух концов ВЫБРАННОГО
 *    (кликом) РЕБРА — выбор ребра подсвечивает оба его конца с подписями,
 *    так же, как выбор узла подсвечивает соседей, только наведение на
 *    ребро (в отличие от наведения на узел) на это никак не влияет —
 *    отдельного hover-состояния для рёбер нет. Крупнее становится только
 *    сам выбор УЗЛА ({@link MAP_CONFIG.node.radiusSelected}) — ни
 *    наведённый узел, ни чьи-либо соседи, ни концы выбранного ребра размер
 *    не меняют (прямая просьба); соседи ВЫБОРА и концы выбранного ребра
 *    (не наведения) дополнительно получают принудительную подпись
 *    (`forceLabel: true`), не зависящую от {@link MAP_CONFIG.node.labelVisibleAtSize}.
 *    Всё, что не подходит ни под одно из условий выше, — тускнеет в {@link
 *    MAP_CONFIG.node.dimColor} (полупрозрачный — "замылить", а не сплошной
 *    серый) и теряет подпись. Рёбра, не касающиеся ни выбора, ни
 *    наведения, при этом `hidden: true` целиком — "остальные рёбра убрать",
 *    прямая просьба. Смена выбора (клик по новому узлу) сама снимает
 *    подсветку старого — она читается из `store.get().selection` заново на
 *    каждый рендер, отдельно ничего сбрасывать не нужно.
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

  function selectionFocusKey(): string | null {
    const selection = store.get().selection;
    return selection?.kind === "node" ? selection.key : null;
  }

  /** Департамент выбора (клик по региону или результат поиска), иначе `null`. */
  function selectedDept(): number | null {
    const selection = store.get().selection;
    return selection?.kind === "dept" ? selection.id : null;
  }

  /** Узел принадлежит выбранному департаменту — атрибут `dept` пишет {@link populateGraph}. */
  function inSelectedDept(nodeKey: string): boolean {
    const dept = selectedDept();
    return (
      dept !== null && graph.hasNode(nodeKey) && graph.getNodeAttribute(nodeKey, "dept") === dept
    );
  }

  /**
   * Оба конца выбранного (кликом) РЕБРА — единственный случай, когда
   * "фокус" это ДВА ключа сразу, а не один, поэтому отдельная функция, а не
   * ещё один вариант {@link selectionFocusKey}. Нужна и nodeReducer (сами
   * концы не тускнеют, подпись форсирована), и edgeReducer (СОСЕДНИЕ рёбра
   * концов остаются видимыми — та же логика "выбор показывает свои
   * связи", что и у выбора узла, не два голых конца без единого другого
   * ребра рядом).
   */
  function selectionEdgeEndpoints(): [string, string] | null {
    const selection = store.get().selection;
    return selection?.kind === "edge" ? [selection.s, selection.t] : null;
  }

  /**
   * `true`, если `nodeKey` — сосед узла/департамента `focus` (а не сам
   * `focus`). `focus === null` или отсутствие `focus` в ТЕКУЩЕМ графе (после
   * смены вкладки — см. развёрнутый комментарий у вызова ниже) — оба безопасно
   * дают `false`, не бросая исключение: `graph.areNeighbors()` на
   * несуществующем узле бросает `NotFoundGraphError`, а не возвращает `false`.
   */
  function isNeighborOf(focus: string | null, nodeKey: string): boolean {
    return (
      focus !== null &&
      focus !== nodeKey &&
      graph.hasNode(focus) &&
      graph.areNeighbors(focus, nodeKey)
    );
  }

  renderer.setSetting("nodeReducer", (nodeKey, data): Partial<NodeDisplayData> => {
    // Якорь департамента — служебная точка: не рисуется вовсе (hidden), не
    // подписывается и не подсвечивается даже при выборе департамента (названия
    // рисуют регионы, map/regions.ts). Координаты скрытого узла Sigma всё равно
    // считает — камера по-прежнему может к нему сдвинуться (flyToSelection).
    if (parseDeptNodeKey(nodeKey) !== null) {
      return { ...data, hidden: true, label: "", forceLabel: false, highlighted: false };
    }
    const res: Partial<NodeDisplayData> = { ...data };
    const selection = store.get().selection;
    const isSelected = selection?.kind === "node" && selection.key === nodeKey;
    // Узел — один из двух концов ВЫБРАННОГО (кликом) ребра — подсвечивается
    // так же, как сосед выбора узла: не тускнеет, подпись форсирована, но
    // размер не растёт и highlighted не ставится — это два конца одного
    // ребра, а не "сам выбор" в смысле isSelected выше. Только выбор
    // (клик), не наведение на ребро — отдельного hover-состояния для рёбер
    // в приложении нет и не появляется здесь (прямая просьба: "поправь
    // выделение", наведение не трогать).
    const edgeEndpoints = selectionEdgeEndpoints();
    const isSelectedEdgeEndpoint = edgeEndpoints !== null && edgeEndpoints.includes(nodeKey);

    if (isSelected) {
      res.highlighted = true;
      res.size = MAP_CONFIG.node.radiusSelected;
    }

    // Выбор и наведение — два НЕЗАВИСИМЫХ источника фокуса (см. развёрнутый
    // комментарий у applyGraphStyling выше) — оба проверяются раздельно, а
    // не через общий "focus", чтобы наведение на другой узел не отменяло
    // подсветку уже выбранного, и наоборот: выбор не блокировал предпросмотр
    // соседей наведения. isSelected исключён отдельно — выбор сам себе
    // фокус, ему незачем тускнеть под собственной подсветкой.
    const selKey = selectionFocusKey();
    const isNeighborOfSelection = isNeighborOf(selKey, nodeKey);
    const isHoveredNode = hoveredNode !== null && hoveredNode === nodeKey;
    const isNeighborOfHover = isNeighborOf(hoveredNode, nodeKey);
    // Соседи ЛЮБОГО из двух концов выбранного ребра — та же логика, что и
    // "выбор узла показывает всех его соседей", применённая к обоим концам
    // сразу: иначе рёбра к третьим узлам от концов оставались бы видны
    // (edgeReducer ниже), но САМИ эти третьи узлы всё равно тускнели бы —
    // видимая линия к притушенному узлу выглядит как рассинхрон.
    const isNeighborOfEdgeSelection =
      edgeEndpoints !== null &&
      (isNeighborOf(edgeEndpoints[0], nodeKey) || isNeighborOf(edgeEndpoints[1], nodeKey));
    // "Активен ли вообще какой-то фокус" — ГЛОБАЛЬНЫЙ вопрос (нужен, чтобы
    // притушить ВСЕ остальные узлы, а не только решить про ЭТОТ конкретный),
    // поэтому берёт selection?.kind === "edge" целиком, а не
    // isSelectedEdgeEndpoint (тот — про ЭТОТ КОНКРЕТНЫЙ nodeKey, для любого
    // узла, не являющегося одним из двух концов, он всегда false — если бы
    // anyFocusActive считался через него, третьи узлы при выбранном ребре
    // никогда бы не тускнели вообще).
    const anyFocusActive =
      selKey !== null ||
      hoveredNode !== null ||
      selection?.kind === "edge" ||
      selection?.kind === "dept";

    if (!isSelected && anyFocusActive) {
      if (
        isNeighborOfSelection ||
        isHoveredNode ||
        isNeighborOfHover ||
        isSelectedEdgeEndpoint ||
        isNeighborOfEdgeSelection ||
        inSelectedDept(nodeKey)
      ) {
        // Крупнее — только сам выбор УЗЛА (radiusSelected выше), ни
        // наведённый узел, ни чьи-либо соседи, ни концы выбранного ребра
        // размер не меняют (прямая просьба). Принудительная подпись
        // (forceLabel, не зависит от labelVisibleAtSize) — у соседей ВЫБОРА
        // (клика), у концов выбранного РЕБРА и у ИХ соседей: имя должно
        // быть видно сразу после клика, а не только если размер узла сам по
        // себе перевалил порог видимости подписи. У соседей НАВЕДЕНИЯ подпись
        // не форсируем — не просили, и на карте с тысячами узлов это была бы
        // лишняя "каша" подписей при простом движении мыши.
        if (isNeighborOfSelection || isSelectedEdgeEndpoint) res.forceLabel = true;
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

    // Порог теперь пользовательский регулятор (features/filters.ts), не
    // захардкоженная MAP_CONFIG.edge.visibleBelowRatio — прямая просьба.
    if (cameraRatio > store.get().filters.edgeZoomThreshold) return { ...data, hidden: true };

    const selection = store.get().selection;
    const isSelectedEdge =
      selection?.kind === "edge" &&
      ((s === selection.s && t === selection.t) || (s === selection.t && t === selection.s));

    if (isSelectedEdge) {
      return { ...data, color: MAP_CONFIG.edge.colorSelected, size: MAP_CONFIG.edge.widthSelected };
    }

    // Видно, если ребро касается ВЫБОРА, НАВЕДЕНИЯ, ИЛИ одного из двух
    // концов ВЫБРАННОГО РЕБРА (независимо друг от друга, см.
    // applyGraphStyling выше) — то же самое "два источника фокуса
    // одновременно", что и в nodeReducer, плюс третий случай: выбор ребра
    // показывает не только само это ребро, но и ВСЕ ОСТАЛЬНЫЕ рёбра его
    // концов — ту же "картину соседства", что уже показывает выбор узла
    // (там ведь тоже остаются видны ВСЕ рёбра выбранного узла, не только
    // одно) — иначе выбор ребра выглядел бы как два никуда больше не
    // подключённых узла с одной линией между ними, даже если на самом
    // деле у них есть другие связи (прямая жалоба: "остальные рёбра не видно").
    const selKey = selectionFocusKey();
    const edgeEndpoints = selectionEdgeEndpoints();
    const touchesSelection = selKey !== null && (s === selKey || t === selKey);
    const touchesEdgeSelectionEndpoint =
      edgeEndpoints !== null && (edgeEndpoints.includes(s) || edgeEndpoints.includes(t));
    const touchesHover = hoveredNode !== null && (s === hoveredNode || t === hoveredNode);
    // Выбранный департамент своих рёбер не показывает — у региона нет узла,
    // рёбра которого можно было бы подсветить, как у выбранного узла.
    const anyFocusActive =
      selKey !== null || hoveredNode !== null || edgeEndpoints !== null || selectedDept() !== null;
    if (anyFocusActive && !touchesSelection && !touchesEdgeSelectionEndpoint && !touchesHover) {
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
      // Отдалились в режим регионов с курсором на узле — снять подсветку узла.
      if (isRegionMode(store.get(), ratio)) hoveredNode = null;
      renderer.refresh();
    },
  };
}

/**
 * Проверяет, что сущность из `selection` реально существует в `graph` —
 * используется при пересборке графа (смена вкладки/фильтров, см.
 * {@link mountReactiveGraph}), чтобы обнулить выбор, который эту пересборку
 * не переживает (узел/департамент с прошлой вкладки, публикация, которую
 * только что скрыл `filters.yearMax`), а не оставлять его висеть на
 * несуществующий ключ — `graph.areNeighbors()` бросает исключение на
 * несуществующем узле, а не возвращает false (см. {@link applyGraphStyling}).
 *
 * @param graph - graphology-граф (актуальный, уже пересобранный).
 * @param selection - проверяемый выбор.
 * @returns `true`, если `selection === null` (нечего проверять) либо
 *   сущность реально есть в `graph`; `false`, если выбор ссылается на то,
 *   чего в графе больше нет.
 */
function selectionExistsIn(graph: Graph, selection: Selection): boolean {
  if (selection === null) return true;
  if (selection.kind === "node") return graph.hasNode(selection.key);
  if (selection.kind === "dept") return graph.hasNode(deptNodeKey(selection.id));
  // graph — "mixed" (graphology-дефолт), а populateGraph() добавляет рёбра
  // через generic mergeEdge(), который на графе типа "mixed" создаёт
  // НАПРАВЛЕННОЕ ребро (s → t). hasEdge(source, target) при этом проверяет
  // только исходящие рёбра источника — в отличие от graph.areNeighbors()
  // выше, которая явно смотрит и in, и out. Рёбра в этом приложении везде
  // считаются неориентированными (см. core/url.ts, map/build.ts::applyGraphStyling::isSelectedEdge),
  // поэтому проверяем оба порядка концов, а не только selection.s → selection.t.
  return graph.hasEdge(selection.s, selection.t) || graph.hasEdge(selection.t, selection.s);
}

/**
 * Плавно панорамирует камеру к только что выбранному узлу/департаменту —
 * единая точка "довезти до узла" для ЛЮБОГО источника выбора (клик по
 * графу, по строке списка вкладки, по результату поиска в панели): все они
 * одинаково пишут `store.selection`, а саму анимацию запускает только
 * {@link mountReactiveGraph} через подписку на Store. Отдельные места, где
 * раньше был свой `camera.animate` (например, features/tabs/nodeListTab.ts),
 * его больше не вызывают — дублировать анимацию незачем.
 *
 * Координаты берутся ЧЕРЕЗ `renderer.getNodeDisplayData(key)`, а НЕ через
 * `graph.getNodeAttributes(key)` — это два РАЗНЫХ пространства координат:
 * атрибуты узла графа хранят "сырые" координаты раскладки ForceAtlas2 (в
 * произвольном масштабе), а Sigma внутри себя нормализует их в так
 * называемое "framed graph" пространство (`Sigma.prototype.process()`:
 * `data.x = attrs.x; ...; this.normalizationFunction.applyTo(data)`) — и
 * ИМЕННО в этом нормализованном пространстве, не в сыром, живут
 * `camera.x`/`camera.y` (см. `Sigma.prototype.graphToViewport()`:
 * `framedGraphToViewport(normalizationFunction(graphPoint))`). Раньше здесь
 * читались сырые атрибуты напрямую — камера уезжала в случайную точку
 * далеко за пределами реального экрана ("пустое пространство", прямая
 * жалоба пользователя), потому что сырые координаты ForceAtlas2 (диапазон
 * порядка тысяч) интерпретировались как уже нормализованные (диапазон
 * порядка единиц).
 *
 * Меняет и x/y, и zoom (`camera.ratio` → {@link MAP_CONFIG.camera.focusRatio}) —
 * абсолютное значение ratio безопасно именно потому, что координаты теперь
 * читаются из normalized "framed graph" пространства (см. выше): Sigma сама
 * приводит любой граф к одному и тому же опорному масштабу ещё до того, как
 * камера в нём начинает работать.
 *
 * Ребро (`selection.kind === "edge"`) камеру не двигает — у ребра нет одной
 * точки, к которой имело бы смысл ехать (это остаётся как раньше, ребро
 * просто подсвечивается через edgeReducer).
 *
 * @param renderer - Sigma-рендерер (нужен и для камеры, и для
 *   {@link Sigma.getNodeDisplayData}, дающего координаты в правильном
 *   пространстве — обычного graphology-графа для этого недостаточно).
 * @param selection - новое выбранное состояние (см. {@link Selection}).
 */
function flyToSelection(renderer: Sigma, selection: Selection): void {
  if (selection === null || selection.kind === "edge") return;

  const key = selection.kind === "node" ? selection.key : deptNodeKey(selection.id);
  const nodeData = renderer.getNodeDisplayData(key);
  if (!nodeData) return;

  // Департамент — только сдвиг к его якорю без приближения: вплотную регионы
  // пропадают (map/regions.ts), а смотреть на департамент нужно именно на них.
  const target =
    selection.kind === "node"
      ? { x: nodeData.x, y: nodeData.y, ratio: MAP_CONFIG.camera.focusRatio }
      : { x: nodeData.x, y: nodeData.y };
  renderer.getCamera().animate(target, {
    duration: MAP_CONFIG.camera.focusDuration,
    easing: "quadraticInOut",
  });
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
    // В режиме регионов узлы не реагируют на мышь — подсвечивается регион (map/regions.ts).
    if (isRegionMode(store.get(), renderer.getCamera().getState().ratio)) return;
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
    // Снимок СТАРЫХ значений в самом начале — единственное место, где
    // обновляется prev (раньше было раскидано по обеим веткам ниже
    // отдельными "prev = state", из-за чего ветка "сменилась вкладка"
    // делала return ДО того, как успевала проверить смену selection —
    // если вкладку и selection меняли ОДНИМ патчем (например,
    // features/globalSearch.ts: {screen, tab, selection} разом), камера
    // так никогда и не подлетала к результату — только подсветка на карте
    // "молча" оказывалась верной, а сам вид оставался там, где был).
    const { tab: prevTab, lang: prevLang, filters: prevFilters, selection: prevSelection } = prev;
    prev = state;

    // filters — новый объект только когда его реально меняли (Store.set
    // мержит патч поверх состояния, не трогая поля вне патча), поэтому
    // сравнение по ссылке здесь корректно и дешевле глубокого сравнения.
    if (state.tab !== prevTab || state.lang !== prevLang || state.filters !== prevFilters) {
      populateGraph(graph, data, state.lang, state.tab, state.filters, pubDetails);
      // Смена вкладки/фильтров пересобирает граф под другой набор
      // сущностей — старый выбор (узел/департамент с прошлой вкладки, или
      // публикация, которую только что скрыл filters.yearMax) почти
      // наверняка в нём больше не существует. Не просто "красиво" —
      // жизненно важно: applyGraphStyling дёргает graph.areNeighbors(focus, ...)
      // для каждого узла, а graphology бросает исключение на несуществующем
      // узле вместо false, что роняло рендер-цикл Sigma намертво (реальный
      // репорт: "нажал на ноду, начал переходить в другую вкладку — зависло").
      // Вложенный store.set() безопасен благодаря Store.notify(), читающему
      // this.state заново на каждый колбэк (core/state.ts) — иначе
      // подписчики ПОСЛЕ этого в том же раунде (например, mountPanel)
      // получили бы уже устаревший state.selection и перезаписали бы им
      // корректный результат этого вложенного вызова.
      if (!selectionExistsIn(graph, state.selection)) {
        store.set({ selection: null });
        return; // вложенный notify() уже отработал результат за нас (включая refresh() ниже)
      }
    }

    // НЕ "else if" — смена вкладки/фильтров и смена selection не исключают
    // друг друга: features/globalSearch.ts::mountGlobalSearch кладёт их
    // ОДНИМ патчем, если результат принадлежит другой вкладке.
    if (state.selection !== prevSelection) {
      // Реducer уже видит новый store.get().selection к этому моменту —
      // refresh() только просит Sigma позвать реducer'ы заново и
      // перерисоваться, без пересборки графа (а если граф выше уже
      // пересобран populateGraph — без ЕЩЁ одной пересборки).
      renderer.refresh();
      flyToSelection(renderer, state.selection);
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
 * Небольшой отладочный индикатор текущего `camera.ratio` поверх карты
 * (нижний левый угол `#map`, не всего viewport — иначе попадал бы в область
 * сайдбара) — инструмент для подбора {@link MAP_CONFIG.node.labelVisibleAtSize}
 * и порогов зума рёбер/регионов на глаз, не постоянный
 * элемент интерфейса. Создаёт DOM-элемент сам, а не
 * через разметку в `index.html` — убрать индикатор после калибровки можно
 * одной строкой в `app/main.ts`, без правки вёрстки.
 *
 * @param renderer - Sigma-рендерер.
 * @returns Функцию отписки (unmount) — снимает обработчик и удаляет элемент из DOM.
 */
export function mountZoomDebug(renderer: Sigma): () => void {
  const el = document.createElement("div");
  Object.assign(el.style, {
    // absolute относительно #map (position: relative в index.html), не
    // fixed относительно всего viewport — иначе left:12px попадал бы в
    // область сайдбара (280px слева), а не на саму карту.
    position: "absolute",
    bottom: "12px",
    left: "12px",
    // Меньше, чем z-index у #menu (26, index.html) — иначе индикатор,
    // будучи ребёнком #map (сам без своего z-index), просвечивал бы поверх
    // непрозрачного оверлея меню (прямая жалоба — "не показывать zoom
    // ratio на start"). Выше самого канваса Sigma этого достаточно.
    zIndex: "5",
    padding: "4px 8px",
    background: "rgba(0, 0, 0, 0.6)",
    color: "#fff",
    fontSize: "11px",
    fontFamily: "monospace",
    borderRadius: "4px",
    pointerEvents: "none",
  });
  renderer.getContainer().appendChild(el);

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

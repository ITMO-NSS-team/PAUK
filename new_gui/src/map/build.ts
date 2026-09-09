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
import { MAP_CONFIG } from "../core/config";
import { nodeLabel } from "../core/data";
import type { Lang } from "../core/i18n";
import type { AppState, Store, TabId } from "../core/state";

type GraphNode = AuthorNode | RepoNode | PubNode;
type Filters = AppState["filters"];
type PubDetailsByKey = Map<string, PubDetail>;

/**
 * Возвращает узлы, которые должна показывать активная вкладка. Вкладка 4
 * (поиск) — пустой список, граф на ней не привязан ни к одному из трёх.
 *
 * Единственный узловой фильтр — год публикации на вкладке 3
 * (`filters.yearMax`): публикации без известного года (`year === null`)
 * никогда не скрываются этим фильтром — мы не знаем их год, а не знаем,
 * что он "слишком поздний", это разные вещи.
 *
 * @param data - данные графа.
 * @param tab - активная вкладка.
 * @param filters - текущие пороги фильтров (используется только `yearMax`, только для `tab === 3`).
 * @returns Список узлов, которые нужно нарисовать для этой вкладки.
 */
function tabGraphNodes(data: GraphData, tab: TabId, filters: Filters): GraphNode[] {
  switch (tab) {
    case 1:
      return data.authors;
    case 2:
      return data.repos;
    case 3:
      return data.pubs.filter((pub) => pub.year === null || pub.year <= filters.yearMax);
    default:
      return [];
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
    default:
      return [];
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
      label: nodeLabel(node, lang, pubDetails),
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
}

/**
 * Регистрирует nodeReducer/edgeReducer, которые красят выбранный узел/ребро
 * иначе — единая точка правды про то, как выглядит подсветка, вместо
 * прежних отдельных функций setSelectedNode/setSelectedEdge, красивших
 * поверх уже нарисованного. Реducer читает `store.get().selection` из
 * замыкания при каждом рендере — сам он ничего не перерисовывает, это
 * только "чем покрасить, если сейчас рендерится эта точка/линия", реальную
 * перерисовку просит `renderer.refresh()` в {@link mountReactiveGraph}.
 *
 * @param renderer - Sigma-рендерер.
 * @param store - Store приложения.
 */
function applySelectionHighlighting(renderer: Sigma, store: Store<AppState>): void {
  const graph = renderer.getGraph();

  renderer.setSetting("nodeReducer", (nodeKey, data): Partial<NodeDisplayData> => {
    const selection = store.get().selection;
    if (selection?.kind === "node" && selection.key === nodeKey) {
      return { ...data, highlighted: true, size: MAP_CONFIG.node.radiusSelected };
    }
    return data;
  });

  renderer.setSetting("edgeReducer", (edgeKey, data): Partial<EdgeDisplayData> => {
    const selection = store.get().selection;
    if (selection?.kind === "edge") {
      const [s, t] = graph.extremities(edgeKey);
      if ((s === selection.s && t === selection.t) || (s === selection.t && t === selection.s)) {
        return { ...data, color: MAP_CONFIG.edge.colorSelected, size: MAP_CONFIG.edge.widthSelected };
      }
    }
    return data;
  });
}

/**
 * Единственная точка входа для реактивности графа: принимает уже
 * построенный Sigma-рендерер (граф внутри него уже наполнен под начальное
 * состояние — см. {@link populateGraph} и app/main.ts, где это делается
 * ДО конструирования Sigma, ей нужен непустой граф с самого начала) и
 * дальше следит за store — пересобирает узлы/рёбра при смене вкладки,
 * языка ИЛИ фильтров, и обновляет подсветку выбора при смене selection
 * (через reducer + `refresh()`, без пересборки графа).
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
 * @returns Функцию отписки (unmount) от Store.
 */
export function mountReactiveGraph(
  renderer: Sigma,
  store: Store<AppState>,
  data: GraphData,
  pubDetails: PubDetailsByKey,
): () => void {
  const graph = renderer.getGraph();
  applySelectionHighlighting(renderer, store);

  let prev = store.get();
  return store.subscribe((state) => {
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
    }
  });
}

// Слой "map" — регионы департаментов под графом: цветные "территории", как
// в старом GUI (pauk/gui/web/build.js), но построенные по карте плотности,
// а не выпуклой оболочкой, обрезанной ячейками Вороного.
//
// Как строится (buildRegions):
// 1. Каждый узел добавляет гауссово "пятно" в сетку плотности своего департамента.
// 2. Клетка принадлежит департаменту с наибольшей плотностью, если она выше
//    порога, поэтому регионы разных департаментов не перекрываются, а
//    граница проходит там, где один департамент начинает преобладать.
// 3. Связные области ("острова") с числом узлов меньше порога отбрасываются.
// 4. Границы оставшихся клеток обходятся в замкнутые контуры и сглаживаются.
//
// Рисуется на отдельном canvas под рёбрами (mountRegions), названия — на
// canvas над узлами, только пока камера дальше filters.regionZoomThreshold.

import type Sigma from "sigma";
import type { Coordinates } from "sigma/types";
import type { GraphData } from "../contracts/graph";
import { MAP_CONFIG, REGION_CONFIG } from "../core/config";
import { localize } from "../core/i18n";
import { isRegionMode, type AppState, type Store } from "../core/state";
import { noDeptId, tabGraphNodes } from "./build";

/** Узел, участвующий в построении регионов, в координатах раскладки. */
export interface RegionPoint {
  x: number;
  y: number;
  dept: number;
}

/** Регион одного департамента — все его острова, замкнутые контуры в координатах раскладки. */
export interface Region {
  dept: number;
  /** Контуры рисуются и проверяются правилом even-odd: вложенный контур — дырка. */
  rings: [number, number][][];
  /** Где рисовать название: центр узлов самого крупного острова. */
  label: Coordinates;
  /** Узлов в самом крупном острове — приоритет названия при наложении. */
  weight: number;
}

/**
 * Строит регионы департаментов по узлам текущей вкладки.
 *
 * @param points - узлы с координатами раскладки и id департамента.
 * @param minNodes - минимум узлов в острове; острова меньше не попадают в результат.
 * @returns По одному {@link Region} на департамент, у которого остался хоть один остров, по возрастанию `dept`.
 *
 * @example
 * // два плотных скопления разных департаментов далеко друг от друга
 * buildRegions([...cluster(0, 0, 12, 1), ...cluster(500, 0, 12, 2)], 10);
 * // -> [{ dept: 1, rings: [...] }, { dept: 2, rings: [...] }]
 */
export function buildRegions(points: RegionPoint[], minNodes: number): Region[] {
  const byDept = new Map<number, RegionPoint[]>();
  for (const point of points) {
    const list = byDept.get(point.dept) ?? [];
    list.push(point);
    byDept.set(point.dept, list);
  }
  const depts = [...byDept.entries()].filter(([, list]) => list.length >= minNodes);
  if (depts.length === 0) return [];

  const { kernelScale, cellsPerSigma, gridMaxSize, densityThreshold, smoothingRounds } =
    REGION_CONFIG;
  let minX = Infinity;
  let minY = Infinity;
  let maxX = -Infinity;
  let maxY = -Infinity;
  let count = 0;
  for (const [, list] of depts) {
    for (const { x, y } of list) {
      minX = Math.min(minX, x);
      minY = Math.min(minY, y);
      maxX = Math.max(maxX, x);
      maxY = Math.max(maxY, y);
      count++;
    }
  }
  // Размер пятна — от среднего расстояния между узлами (площадь / число узлов),
  // размер клетки — от пятна, но не мельче предела сетки.
  const extent = Math.max(maxX - minX, maxY - minY, 1e-9);
  const spacing = Math.sqrt(Math.max((maxX - minX) * (maxY - minY), extent * 1e-9) / count);
  const sigmaUnits = kernelScale * spacing;
  const cell = Math.max(sigmaUnits / cellsPerSigma, extent / gridMaxSize);
  const kernelSigma = Math.max(sigmaUnits / cell, 1);
  const radius = Math.ceil(kernelSigma * 2.5);
  const pad = radius + 1;
  const width = Math.ceil((maxX - minX) / cell) + 2 * pad + 1;
  const height = Math.ceil((maxY - minY) / cell) + 2 * pad + 1;
  const originX = minX - pad * cell;
  const originY = minY - pad * cell;
  const cellOf = (p: RegionPoint): number =>
    Math.floor((p.y - originY) / cell) * width + Math.floor((p.x - originX) / cell);

  const kernel: { dx: number; dy: number; w: number }[] = [];
  for (let dy = -radius; dy <= radius; dy++) {
    for (let dx = -radius; dx <= radius; dx++) {
      const w = Math.exp(-(dx * dx + dy * dy) / (2 * kernelSigma * kernelSigma));
      if (w > 0.01) kernel.push({ dx, dy, w });
    }
  }

  // 1-2. Плотность каждого департамента и владелец каждой клетки.
  const size = width * height;
  const best = new Float32Array(size);
  const owner = new Int32Array(size).fill(-1);
  const layer = new Float32Array(size);
  depts.forEach(([, list], deptIndex) => {
    layer.fill(0);
    for (const point of list) {
      const idx = cellOf(point);
      const cx = idx % width;
      const cy = (idx - cx) / width;
      for (const { dx, dy, w } of kernel) {
        const i = (cy + dy) * width + cx + dx;
        layer[i] = (layer[i] ?? 0) + w;
      }
    }
    for (let i = 0; i < size; i++) {
      const value = layer[i] ?? 0;
      if (value >= densityThreshold && value > (best[i] ?? 0)) {
        best[i] = value;
        owner[i] = deptIndex;
      }
    }
  });

  // 3. Острова: связные (по 4 соседям) области клеток одного владельца.
  const component = new Int32Array(size).fill(-1);
  let componentCount = 0;
  for (let start = 0; start < size; start++) {
    if (owner[start] === -1 || component[start] !== -1) continue;
    const deptIndex = owner[start];
    const stack = [start];
    component[start] = componentCount;
    while (stack.length > 0) {
      const i = stack.pop() ?? 0;
      const x = i % width;
      const neighbours = [x > 0 ? i - 1 : -1, x < width - 1 ? i + 1 : -1, i - width, i + width];
      for (const n of neighbours) {
        if (n >= 0 && n < size && component[n] === -1 && owner[n] === deptIndex) {
          component[n] = componentCount;
          stack.push(n);
        }
      }
    }
    componentCount++;
  }

  const nodesInComponent = new Int32Array(componentCount);
  const sumX = new Float64Array(componentCount);
  const sumY = new Float64Array(componentCount);
  depts.forEach(([, list], deptIndex) => {
    for (const point of list) {
      const idx = cellOf(point);
      const comp = component[idx] ?? 0;
      if (owner[idx] !== deptIndex) continue;
      nodesInComponent[comp] = (nodesInComponent[comp] ?? 0) + 1;
      sumX[comp] = (sumX[comp] ?? 0) + point.x;
      sumY[comp] = (sumY[comp] ?? 0) + point.y;
    }
  });
  // Самый крупный (по числу узлов) остров каждого департамента — под название.
  const biggest = depts.map(([, list], deptIndex) => {
    let best = -1;
    for (const point of list) {
      const idx = cellOf(point);
      const comp = component[idx] ?? 0;
      if (owner[idx] !== deptIndex) continue;
      if (best === -1 || (nodesInComponent[comp] ?? 0) > (nodesInComponent[best] ?? 0)) best = comp;
    }
    return best;
  });
  const kept = (i: number): boolean =>
    owner[i] !== -1 && (nodesInComponent[component[i] ?? 0] ?? 0) >= minNodes;

  // 4. Контуры: направленные рёбра границы (внутренность справа), затем обход в петли.
  const vertexKey = (vx: number, vy: number): number => vy * (width + 1) + vx;
  const toGraph = (key: number): [number, number] => {
    const vx = key % (width + 1);
    const vy = (key - vx) / (width + 1);
    return [originX + vx * cell, originY + vy * cell];
  };

  const regions: Region[] = [];
  depts.forEach(([dept], deptIndex) => {
    const solid = (x: number, y: number): boolean =>
      x >= 0 &&
      y >= 0 &&
      x < width &&
      y < height &&
      owner[y * width + x] === deptIndex &&
      kept(y * width + x);

    const outgoing = new Map<number, number[]>();
    const addEdge = (from: number, to: number): void => {
      const list = outgoing.get(from) ?? [];
      list.push(to);
      outgoing.set(from, list);
    };
    for (let i = 0; i < size; i++) {
      if (owner[i] !== deptIndex || !kept(i)) continue;
      const x = i % width;
      const y = (i - x) / width;
      if (!solid(x, y - 1)) addEdge(vertexKey(x, y), vertexKey(x + 1, y));
      if (!solid(x + 1, y)) addEdge(vertexKey(x + 1, y), vertexKey(x + 1, y + 1));
      if (!solid(x, y + 1)) addEdge(vertexKey(x + 1, y + 1), vertexKey(x, y + 1));
      if (!solid(x - 1, y)) addEdge(vertexKey(x, y + 1), vertexKey(x, y));
    }

    const rings: [number, number][][] = [];
    for (const start of outgoing.keys()) {
      while ((outgoing.get(start)?.length ?? 0) > 0) {
        const loop: number[] = [];
        let current = start;
        do {
          loop.push(current);
          current = outgoing.get(current)?.pop() ?? start;
        } while (current !== start);
        rings.push(smooth(dropCollinear(loop.map(toGraph)), smoothingRounds));
      }
    }
    const labelComp = biggest[deptIndex] ?? -1;
    const labelCount = nodesInComponent[labelComp] ?? 0;
    if (rings.length > 0 && labelCount > 0) {
      regions.push({
        dept,
        rings,
        label: { x: (sumX[labelComp] ?? 0) / labelCount, y: (sumY[labelComp] ?? 0) / labelCount },
        weight: labelCount,
      });
    }
  });

  return regions.sort((a, b) => a.dept - b.dept);
}

/** Убирает вершины посередине прямых отрезков контура — на сетке их большинство. */
function dropCollinear(ring: [number, number][]): [number, number][] {
  return ring.filter((point, i) => {
    const prev = ring[(i - 1 + ring.length) % ring.length] ?? point;
    const next = ring[(i + 1) % ring.length] ?? point;
    return !(
      (prev[0] === point[0] && point[0] === next[0]) ||
      (prev[1] === point[1] && point[1] === next[1])
    );
  });
}

/** Сглаживание замкнутого контура методом Chaikin: каждый отрезок заменяется точками на 1/4 и 3/4. */
function smooth(ring: [number, number][], rounds: number): [number, number][] {
  let result = ring;
  for (let round = 0; round < rounds; round++) {
    const next: [number, number][] = [];
    result.forEach((p, i) => {
      const q = result[(i + 1) % result.length] ?? p;
      next.push([0.75 * p[0] + 0.25 * q[0], 0.75 * p[1] + 0.25 * q[1]]);
      next.push([0.25 * p[0] + 0.75 * q[0], 0.25 * p[1] + 0.75 * q[1]]);
    });
    result = next;
  }
  return result;
}

/**
 * Какой департамент занимает точку (правило even-odd по всем контурам региона).
 *
 * @param regions - результат {@link buildRegions}.
 * @param point - точка в координатах раскладки.
 * @returns `dept` региона, внутри которого точка, иначе `null`.
 */
export function regionDeptAt(regions: Region[], point: Coordinates): number | null {
  for (const region of regions) {
    let inside = false;
    for (const ring of region.rings) {
      for (let i = 0, j = ring.length - 1; i < ring.length; j = i++) {
        const [xi, yi] = ring[i] ?? [0, 0];
        const [xj, yj] = ring[j] ?? [0, 0];
        if (
          yi > point.y !== yj > point.y &&
          point.x < ((xj - xi) * (point.y - yi)) / (yj - yi) + xi
        ) {
          inside = !inside;
        }
      }
    }
    if (inside) return region.dept;
  }
  return null;
}

/**
 * Разбивает название на строки не шире `maxWidth` (по словам); строк не
 * больше `maxLines`, последняя при нехватке места обрезается многоточием.
 * Слово длиннее `maxWidth` остаётся целым на своей строке.
 *
 * @param text - название.
 * @param measure - ширина строки в пикселях (обычно `context.measureText(...).width`).
 * @param maxWidth - предельная ширина строки.
 * @param maxLines - предельное число строк.
 *
 * @example
 * wrapLabel("Институт прикладных компьютерных наук", (s) => s.length * 7, 150, 3);
 * // -> ["Институт прикладных", "компьютерных наук"]
 */
export function wrapLabel(
  text: string,
  measure: (line: string) => number,
  maxWidth: number,
  maxLines: number,
): string[] {
  const lines: string[] = [];
  for (const word of text.split(/\s+/).filter(Boolean)) {
    const last = lines[lines.length - 1];
    if (last !== undefined && measure(`${last} ${word}`) <= maxWidth) {
      lines[lines.length - 1] = `${last} ${word}`;
    } else {
      lines.push(word);
    }
  }
  if (lines.length <= maxLines) return lines;

  const kept = lines.slice(0, maxLines);
  let tail = `${kept[maxLines - 1] ?? ""}…`;
  while (tail.length > 1 && measure(tail) > maxWidth) tail = `${tail.slice(0, -2)}…`;
  kept[maxLines - 1] = tail;
  return kept;
}

/**
 * Подключает регионы департаментов: пересчитывает их при смене
 * вкладки/фильтров и рисует на каждый кадр Sigma (`afterRender`).
 *
 * В режиме регионов (core/state.ts::isRegionMode — камера дальше порога):
 * заливка, обводка и названия; регион под курсором ярче, клик по нему
 * выбирает департамент. Названия не накладываются: крупные регионы важнее, а
 * при выбранном департаменте видно только его название. Ближе порога — только
 * обводка, без названий и без реакции на мышь.
 *
 * @param renderer - Sigma-рендерер.
 * @param store - Store приложения.
 * @param data - данные графа.
 * @returns `deptAtViewport` — департамент под точкой экрана в режиме
 *   регионов, иначе `null` (для клика, см. features/selection.ts), и функция отписки.
 */
export function mountRegions(
  renderer: Sigma,
  store: Store<AppState>,
  data: GraphData,
): { deptAtViewport: (point: Coordinates) => number | null; unmount: () => void } {
  // Заливка — под рёбрами и узлами; названия — над узлами, иначе их закрывают точки.
  const fillCanvas = renderer.createCanvas("regions", { beforeLayer: "edges" });
  const labelCanvas = renderer.createCanvas("region-labels", { afterLayer: "labels" });
  const deptById = new Map(data.departments.map((dept) => [dept.id, dept]));
  const deptByNode = new Map(
    [...data.authors, ...data.repos, ...data.pubs].map((node) => [node.key, node.dept]),
  );
  const excludedDept = noDeptId(data);

  let regions: Region[] = [];
  let hoveredDept: number | null = null;

  function rebuild(state: AppState): void {
    const points = tabGraphNodes(data, state.tab, state.filters)
      .filter((node) => node.dept !== excludedDept)
      .map((node) => ({ x: node.gx, y: node.gy, dept: node.dept }));
    regions = buildRegions(points, state.filters.regionMinNodes);
  }

  function regionMode(): boolean {
    return isRegionMode(store.get(), renderer.getCamera().getState().ratio);
  }

  function deptAtViewport(point: Coordinates): number | null {
    return regionMode() ? regionDeptAt(regions, renderer.viewportToGraph(point)) : null;
  }

  /** Подгоняет canvas под размер рендерера и очищает его; `null`, если 2d-контекста нет. */
  function prepare(canvas: HTMLCanvasElement): CanvasRenderingContext2D | null {
    const context = canvas.getContext("2d");
    if (!context) return null;
    const { width, height } = renderer.getDimensions();
    const pixelRatio = window.devicePixelRatio || 1;
    if (canvas.width !== width * pixelRatio || canvas.height !== height * pixelRatio) {
      canvas.width = width * pixelRatio;
      canvas.height = height * pixelRatio;
      canvas.style.width = `${width}px`;
      canvas.style.height = `${height}px`;
    }
    context.setTransform(pixelRatio, 0, 0, pixelRatio, 0, 0);
    context.clearRect(0, 0, width, height);
    return context;
  }

  function draw(): void {
    const fill = prepare(fillCanvas);
    const labels = prepare(labelCanvas);
    const state = store.get();
    if (!fill || !labels || !state.filters.showRegions[state.tab]) return;

    const full = regionMode();
    const { selection, lang } = state;
    const focusDept =
      selection?.kind === "dept"
        ? selection.id
        : selection?.kind === "node"
          ? (deptByNode.get(selection.key) ?? null)
          : null;
    const cfg = REGION_CONFIG;

    for (const region of regions) {
      const dim = focusDept !== null && region.dept !== focusDept ? cfg.dimFactor : 1;
      const color = deptById.get(region.dept)?.color ?? MAP_CONFIG.node.fallbackColor;
      fill.beginPath();
      for (const ring of region.rings) {
        ring.forEach(([x, y], i) => {
          const p = renderer.graphToViewport({ x, y });
          if (i === 0) fill.moveTo(p.x, p.y);
          else fill.lineTo(p.x, p.y);
        });
        fill.closePath();
      }
      fill.fillStyle = color;
      fill.strokeStyle = color;
      if (full) {
        const alpha = region.dept === hoveredDept ? cfg.hoverFillAlpha : cfg.fillAlpha;
        fill.globalAlpha = alpha * dim;
        fill.fill("evenodd");
      }
      fill.globalAlpha = cfg.strokeAlpha * dim;
      fill.lineWidth = cfg.strokeWidth;
      fill.stroke();
    }
    fill.globalAlpha = 1;
    if (!full) return;

    // Названия: при выбранном департаменте — только его; иначе сначала регион
    // под курсором, потом по убыванию размера, пропуская перекрывающиеся.
    const selectedDept = selection?.kind === "dept" ? selection.id : null;
    const ordered = regions
      .filter((region) => selectedDept === null || region.dept === selectedDept)
      .sort(
        (a, b) =>
          Number(b.dept === hoveredDept) - Number(a.dept === hoveredDept) || b.weight - a.weight,
      );

    labels.font = `${cfg.labelWeight} ${cfg.labelSize}px ${renderer.getSetting("labelFont")}`;
    labels.textAlign = "center";
    labels.textBaseline = "middle";
    labels.lineJoin = "round";
    labels.lineWidth = MAP_CONFIG.node.labelHaloWidth;
    labels.strokeStyle = MAP_CONFIG.node.labelHaloColor;
    const lineHeight = cfg.labelSize * cfg.labelLineHeight;
    const placed: { left: number; top: number; right: number; bottom: number }[] = [];

    for (const region of ordered) {
      const dept = deptById.get(region.dept);
      if (!dept) continue;
      const lines = wrapLabel(
        localize(dept.name, dept.name_en, lang),
        (line) => labels.measureText(line).width,
        cfg.labelMaxWidth,
        cfg.labelMaxLines,
      );
      const p = renderer.graphToViewport(region.label);
      const halfWidth = Math.max(...lines.map((line) => labels.measureText(line).width)) / 2;
      const halfHeight = (lines.length * lineHeight) / 2;
      const box = {
        left: p.x - halfWidth - cfg.labelGap,
        right: p.x + halfWidth + cfg.labelGap,
        top: p.y - halfHeight - cfg.labelGap,
        bottom: p.y + halfHeight + cfg.labelGap,
      };
      const overlaps = placed.some(
        (other) =>
          box.left < other.right &&
          box.right > other.left &&
          box.top < other.bottom &&
          box.bottom > other.top,
      );
      if (overlaps) continue;
      placed.push(box);

      labels.fillStyle = dept.color;
      lines.forEach((line, i) => {
        const y = p.y - halfHeight + lineHeight * (i + 0.5);
        labels.strokeText(line, p.x, y);
        labels.fillText(line, p.x, y);
      });
    }
  }

  // Наведение на регион — только в режиме регионов; перерисовываются лишь
  // свои canvas, без перерисовки графа.
  const captor = renderer.getMouseCaptor();
  function onMouseMove(event: Coordinates): void {
    const dept = deptAtViewport(event);
    if (dept === hoveredDept) return;
    hoveredDept = dept;
    renderer.getContainer().style.cursor = dept === null ? "" : "pointer";
    draw();
  }
  function onMouseLeave(): void {
    if (hoveredDept === null) return;
    hoveredDept = null;
    renderer.getContainer().style.cursor = "";
    draw();
  }
  captor.on("mousemove", onMouseMove);
  captor.on("mouseleave", onMouseLeave);

  rebuild(store.get());
  let prev = store.get();
  const unsubscribe = store.subscribe((state) => {
    if (state.tab !== prev.tab || state.filters !== prev.filters) {
      rebuild(state);
      renderer.scheduleRender();
    }
    prev = state;
  });
  renderer.on("afterRender", draw);

  return {
    deptAtViewport,
    unmount: () => {
      renderer.off("afterRender", draw);
      captor.off("mousemove", onMouseMove);
      captor.off("mouseleave", onMouseLeave);
      unsubscribe();
      fillCanvas.remove();
      labelCanvas.remove();
    },
  };
}

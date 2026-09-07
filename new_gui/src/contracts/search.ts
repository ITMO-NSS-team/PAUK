import type { NodeKind } from "./graph";

// В отличие от PubDetail (contracts/graph.ts) — это НЕ то, что пишет Python.
// Индекс поиска сегодня строится в браузере (search.js) из уже загруженного
// GraphData — в new_gui это остаётся клиентской функцией buildSearchIndex().
export interface SearchHit {
  key: string;
  kind: NodeKind | "dept";
  label: string;
  sub: string | null;
}

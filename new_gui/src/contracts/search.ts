import type { NodeKind } from "./graph";

// Форма graph-search.js (build_search_detail() в generate_data.py) —
// детали публикации, подгружаемые по клику.
export interface SearchDetail {
  key: string;
  label: string;
  journal: string;
  doi: string;
  has_code: boolean;
  code_url: string[];
}

// В отличие от SearchDetail, это НЕ то, что пишет Python. Индекс поиска
// сегодня строится в браузере (search.js) из уже загруженного GraphData —
// в new_gui это остаётся клиентской функцией buildSearchIndex().
export interface SearchHit {
  key: string;
  kind: NodeKind | "dept";
  label: string;
  sub: string | null;
}

import type { NodeKind } from "./graph";

// Built in the browser from GraphData, not written by the Python builder.
export interface SearchHit {
  key: string;
  kind: NodeKind | "dept";
  label: string;
  sub: string | null;
  // Other spellings (RU/EN, name variants): searched, not shown.
  terms?: string;
}

import type { NodeKind } from "./graph";

export interface SearchHit {
  key: string;
  kind: NodeKind | "dept";
  label: string;
  sub: string | null;
  terms?: string;
}

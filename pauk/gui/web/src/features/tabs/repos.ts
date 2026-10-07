import type { RepoNode } from "../../contracts/graph";
import { createNodeListTab } from "./nodeListTab";

export const reposTab = createNodeListTab<RepoNode>({
  items: (data) => data.repos,
  compare: (a, b) => b.stars - a.stars,
  label: (repo) => repo.label,
  meta: (repo) => `★ ${repo.stars}`,
});

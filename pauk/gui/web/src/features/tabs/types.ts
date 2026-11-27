import type Sigma from "sigma";
import type { GraphData, PubDetail, RepoDetail } from "../../contracts/graph";
import type { AppState, Store } from "../../core/state";

export interface TabModule {
  mount(
    container: HTMLElement,
    store: Store<AppState>,
    renderer: Sigma,
    data: GraphData,
    pubDetails: Map<string, PubDetail>,
    repoDetails: Map<string, RepoDetail>,
  ): () => void;
}

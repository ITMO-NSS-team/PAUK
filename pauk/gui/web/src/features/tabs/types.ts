import type Sigma from "sigma";
import type { GraphData, PubDetail, RepoDetail } from "../../contracts/graph";
import type { AppState, Store } from "../../core/state";

/** Switching tabs is "unmount the current one, mount the next one". */
export interface TabModule {
  /** @returns Unmount function: unsubscribes from the Store and removes handlers. */
  mount(
    container: HTMLElement,
    store: Store<AppState>,
    renderer: Sigma,
    data: GraphData,
    pubDetails: Map<string, PubDetail>,
    repoDetails: Map<string, RepoDetail>,
  ): () => void;
}

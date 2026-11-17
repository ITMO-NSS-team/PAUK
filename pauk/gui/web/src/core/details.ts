import { loadDetails, mergeDetailsInto } from "./data";
import { loggedStep } from "./log";

export interface DetailResource {
  status: "idle" | "loading" | "loaded" | "failed";
  load(): Promise<void>;
}

/** Keeps a failed fetch distinct from an empty file and deduplicates retries. */
export class DetailLoader<T extends { key: string }> implements DetailResource {
  status: DetailResource["status"] = "idle";
  private pending: Promise<void> | null = null;

  constructor(
    private url: string,
    private target: Map<string, T>,
    private notify: () => void,
  ) {}

  load(): Promise<void> {
    if (this.pending) return this.pending;
    this.status = "loading";
    this.pending = loggedStep(this.url, () => loadDetails<T>(this.url))
      .then((rows) => {
        mergeDetailsInto(this.target, rows);
        this.status = "loaded";
      })
      .catch(() => {
        this.status = "failed";
      })
      .finally(() => {
        this.pending = null;
        this.notify();
      });
    this.notify();
    return this.pending;
  }
}

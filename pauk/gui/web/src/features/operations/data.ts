export type Page = "status" | "backups" | "site" | "operations";
export type OperationState = "queued" | "running" | "done" | "failed" | "cancelled";

export interface Backup {
  managed: boolean;
  components: string[];
  id: string;
  name: string;
  created: string;
  size: string;
  restored: boolean;
  verified: boolean;
}

export interface Release {
  id: string;
  commit: string;
  branch: string;
  created: string;
  snapshot: string;
  port?: number;
}

export interface Operation {
  id: string;
  title: string;
  kind: string;
  state: OperationState;
  started: string;
  duration: string;
  stages: string[];
  step: number;
  logs: string[];
  actor: string;
}

export interface Service {
  ports: string;
  name: string;
  role: string;
  icon: string;
  version: string;
  cpu: string;
  memory: string;
  healthy: boolean;
  status: string;
}

export interface OperationsState {
  page: Page;
  theme: "dark" | "light";
  backups: Backup[];
  live: Release | null;
  preview: Release | null;
  archive: Release[];
  publicRelease: { source: string; commit: string; created: string } | null;
  operations: Operation[];
  schedule: { enabled: boolean; time: string; keep: number };
  services: Service[];
  storage: { name: string; used: string; total: string; percent: number | null }[];
  checks: { label: string; healthy: boolean; detail: string; warning?: string }[];
  pagesEnabled: boolean;
  stand: string;
  host: string;
  error: string;
  filter: string;
  search: string;
}

export const pages: { id: Page; title: string }[] = [
  { id: "status", title: "Состояние" },
  { id: "backups", title: "Дампы" },
  { id: "site", title: "Сайт" },
  { id: "operations", title: "Операции" },
];

export function initialState(): OperationsState {
  return {
    page: "status",
    theme: "dark",
    backups: [],
    live: null,
    preview: null,
    archive: [],
    publicRelease: null,
    operations: [],
    schedule: { enabled: false, time: "03:00", keep: 3 },
    services: [],
    storage: [],
    checks: [],
    pagesEnabled: false,
    stand: "",
    host: "",
    error: "",
    filter: "all",
    search: "",
  };
}

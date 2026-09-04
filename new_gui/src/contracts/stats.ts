// Форма graph-stats.js (generate_stats.py::collect()) — вкладка "Здоровье БД".
// Почти везде генератор уже отдаёт язык парами x/x_en — перевод на клиенте
// не переводит текст, а выбирает готовое поле (см. core/i18n.ts::localize()).

export type CheckStatus = "ok" | "warn" | "fail" | "error";

export interface Check {
  id: string;
  group: string;
  group_en: string;
  title: string;
  title_en: string;
  n: number | null;
  of: number | null;
  pct: number | null;
  status: CheckStatus;
  hint: string | null;
  hint_en: string | null;
  has_examples: boolean;
}

export interface Stats {
  generated_at: string;
  nodes: { label: string; label_en: string; n: number; note?: string; note_en?: string }[];
  rels: { type: string; n: number; note?: string; note_en?: string }[];
  totals: { nodes: number; rels: number };
  checks: Check[];
  years: { year: number; n: number }[];
  top_depts: { name: string; name_en: string; n: number }[];
}

// Ответ живого GET /api/check?id=... (serve.py) — примеры строк для попапа
// на вкладке "Здоровье БД". Не часть статического graph-stats.js.
export interface CheckExample {
  id: string;
  title: string;
  title_en: string;
  group: string;
  group_en: string;
  hint: string | null;
  hint_en: string | null;
  total: number;
  columns: string[];
  rows: unknown[][];
  shown: number;
  limit: number;
  truncated: boolean;
}

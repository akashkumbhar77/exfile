// Shapes returned by /api/v1 (see docs/API-FIELDS.md). Only what the pages use.

export type Segment =
  | { kind: "text"; text: string }
  | { kind: "color"; name: string; hex: string };

export interface ReadbackDetail {
  text: string;
  segments: Segment[];
}

export interface ReadbackRule {
  number: number;
  id: string;
  action: string;
  headline: string;
  when: string;
  details: ReadbackDetail[];
}

export interface Readback {
  summary: string;
  governed_tabs: string[];
  tabs_missing: string[];
  stages: string[];
  rules: ReadbackRule[];
  guards: string[];
  lines: string[];
  live: boolean;
}

export interface SheetRef {
  id: number;
  google_sheet_id: string;
  title: string;
  status: string;
}

export interface ConfigSummary {
  id: number;
  sheet_id: number;
  version: number;
  status: "PENDING_APPROVAL" | "ACTIVE" | "SUPERSEDED" | "REJECTED";
  created_at: string | null;
  approved_by: string | null;
  approved_at: string | null;
  rejected_by: string | null;
  rejected_at: string | null;
  decision_reason: string | null;
  summary_title: string | null;
  source: { kind?: string; model?: string; session_id?: string; prompt_version?: string };
  governed_tabs: string[];
  rule_count: number;
  has_consolidate: boolean;
  sheet?: SheetRef;
}

export interface ConfigDetail extends ConfigSummary {
  body: {
    rules: Array<{ action: string; presentation?: { title?: string | null } }>;
  } & Record<string, unknown>;
  sheet: SheetRef;
}

export interface PreviewCell {
  v: string | number | boolean | null;
  font: string | null;
  bg: string | null;
  strike: boolean;
}

export interface PreviewRow {
  row: number;
  kind: "title" | "header" | "data";
  before: PreviewCell[];
  after: PreviewCell[];
  changed: number[];
}

export interface PreviewTab {
  tab: string;
  exists: boolean;
  rows_to_reorder: number;
  rows_to_recolor: number;
  rows_added: number;
  rows_removed: number;
  rows_cleared: number;
  cells_to_write: number;
  cells_to_recolor: number;
  changed_rows_total: number;
  headers: string[];
  sample: PreviewRow[];
}

export interface Preview {
  config_id: number;
  status: string;
  today: string;
  timezone: string;
  ops: number;
  drift: Array<{ tab: string; expected: string; actual: string | null }>;
  warnings: string[];
  tabs: PreviewTab[];
}

export interface ErrorEnvelope {
  error: { code: string; message: string; request_id: string; details?: unknown[] };
}

/** Thin typed client for the backend API. */

export type Severity = "OK" | "WARNING" | "ERROR" | "NEEDS_USER_INPUT";
export type Confidence = "high" | "medium" | "low";

export interface Health {
  status: string;
  catalog_items: number;
  template_sections: number;
  quantity_rules: number;
  ai_available: boolean;
  ai_model: string;
}

export interface Project {
  id: number;
  name: string;
  client_name: string;
  address: string;
  manager: string;
  brief: string;
  status: string;
  created_at: string | null;
  updated_at: string | null;
  document_count: number;
  estimate_count: number;
}

export interface ProjectDetail extends Project {
  documents: DocumentInfo[];
  latest_analysis_id: number | null;
  latest_estimate_id: number | null;
}

export interface DocumentInfo {
  id: number;
  filename: string;
  kind: string;
  status: string;
  page_count: number;
  size_bytes: number;
  error: string | null;
}

export interface DocumentPage {
  page_number: number;
  page_type: string;
  page_type_label: string;
  text_length: number;
  image_count: number;
  needs_vision: boolean;
  analysed: boolean;
  confidence: Confidence;
  findings: Record<string, unknown>;
}

export interface Fact {
  key: string;
  label: string;
  value: string;
  unit: string;
  status: "confirmed" | "assumption" | "unknown" | "needs_user_input";
  confidence: Confidence;
  source_type: string;
  source_ref: string;
  note: string;
}

export interface PlantRow {
  number?: string;
  name: string;
  latin_name?: string;
  quantity: number | null;
  size?: string;
  note?: string;
  is_existing?: boolean;
  confidence?: Confidence;
}

export interface Analysis {
  id: number;
  project_id: number;
  version: number;
  status: string;
  object_type: string;
  summary: string;
  facts: Fact[];
  systems: { key: string; label: string; evidence: string; confidence: Confidence }[];
  components: { name: string; quantity: number | null; unit: string; note: string }[];
  plants: PlantRow[];
  assumptions: string[];
  unknowns: string[];
  risks: string[];
  conflicts: { topic: string; values: string[]; impact: string; question: string }[];
  updated_at: string | null;
}

export interface Line {
  id: number;
  section: string;
  section_title: string;
  block: "materials" | "works" | "plants";
  name: string;
  catalog_id: number | null;
  unit: string;
  quantity: number;
  unit_price: number;
  unit_cost: number;
  total: number;
  margin: number;
  qty_source: string;
  qty_expr: string | null;
  qty_trace: string | null;
  locked: boolean;
  match_status: string;
  confidence: Confidence;
  status: Severity;
  reasons: string[];
  source_refs: { source_type?: string; source_ref?: string; detail?: string }[];
  candidates: CatalogItem[];
  comment: string;
  formula: string;
}

export interface Block {
  kind: "materials" | "works" | "plants";
  lines: Line[];
  total: number;
}

export interface Section {
  key: string;
  title: string;
  blocks: Block[];
  materials_total: number;
  works_total: number;
  total: number;
}

export interface Totals {
  materials_total: number;
  works_total: number;
  subtotal: number;
  surcharge: number;
  grand_total: number;
  prepayment_materials: number;
  prepayment_works: number;
  balance: number;
  cost_total: number;
  margin: number;
  margin_pct: number;
  formulas: Record<string, string>;
  sections: { key: string; title: string; materials: number; works: number; plants: number; total: number }[];
}

export interface Finding {
  code: string;
  severity: Severity;
  title: string;
  detail: string;
  fix_hint: string;
  impact: string;
  line_name: string | null;
  section: string | null;
}

export interface Validation {
  status: Severity;
  exportable: boolean;
  counts: Record<string, number>;
  findings: Finding[];
}

export interface Estimate {
  id: number;
  project_id: number;
  version: number;
  title: string;
  status: string;
  options: Record<string, boolean>;
  settings: Record<string, unknown>;
  totals: Totals;
  sections: Section[];
  issues: { id: number; code: string; severity: Severity; title: string; detail: string; fix_hint: string; impact: string }[];
}

export interface CatalogItem {
  catalog_id: number;
  name: string;
  category: string;
  unit: string;
  kind: string;
  unit_price: number;
  unit_cost: number;
  margin_pct?: number;
  owner?: string;
  score?: number;
  reasons?: string[];
}

export interface Question {
  id: number;
  group: string;
  code: string;
  /** Question family: price | match | missing | coverage | conflict. */
  kind_key: string;
  /** The thing being asked about (a product name, a topic). */
  subject: string;
  text: string;
  why: string;
  kind: "text" | "number" | "choice" | "boolean";
  choices: string[];
  default_value: string;
  answer: string | null;
  status: string;
  affects: string[];
}

export interface QuestionFamily {
  family: string;
  label: string;
  open: number;
  total: number;
}

export interface QuestionSummary {
  total: number;
  open: number;
  families: QuestionFamily[];
}

export interface SectionMeta {
  key: string;
  title: string;
  drivers: string[];
  options: string[];
  line_count: number;
}

export class ApiError extends Error {
  constructor(message: string, readonly status: number, readonly body?: unknown) {
    super(message);
  }
}

/**
 * Where the API lives.
 *
 * Empty in local dev and when the backend serves the built SPA itself — the
 * Vite proxy handles `/api`. On Vercel it points at the Render service via
 * VITE_API_BASE_URL, set in the project's environment variables.
 */
export const API_BASE = (import.meta.env.VITE_API_BASE_URL ?? "").replace(/\/$/, "");

function url(path: string): string {
  return `${API_BASE}/api${path}`;
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(url(path), {
    headers: init?.body instanceof FormData ? undefined : { "Content-Type": "application/json" },
    ...init,
  });
  if (!response.ok) {
    let body: unknown;
    let message = `${response.status} ${response.statusText}`;
    try {
      body = await response.json();
      const detail = (body as { detail?: unknown; message?: unknown }).detail
        ?? (body as { message?: unknown }).message;
      if (typeof detail === "string") message = detail;
      else if (detail && typeof detail === "object" && "message" in detail) {
        message = String((detail as { message: unknown }).message);
      }
    } catch {
      /* response had no JSON body */
    }
    throw new ApiError(message, response.status, body);
  }
  if (response.status === 204) return undefined as T;
  return response.json() as Promise<T>;
}

export const api = {
  health: () => request<Health>("/health"),
  dashboard: () => request<{ totals: Record<string, number>; recent_projects: Project[] }>("/dashboard"),
  sections: () => request<SectionMeta[]>("/meta/sections"),

  listProjects: () => request<Project[]>("/projects"),
  getProject: (id: number) => request<ProjectDetail>(`/projects/${id}`),
  createProject: (body: Partial<Project>) =>
    request<Project>("/projects", { method: "POST", body: JSON.stringify(body) }),
  updateProject: (id: number, body: Partial<Project>) =>
    request<Project>(`/projects/${id}`, { method: "PATCH", body: JSON.stringify(body) }),
  deleteProject: (id: number) => request<void>(`/projects/${id}`, { method: "DELETE" }),

  uploadDocument: (projectId: number, file: File) => {
    const form = new FormData();
    form.append("file", file);
    return request<DocumentInfo>(`/projects/${projectId}/documents`, { method: "POST", body: form });
  },
  getDocument: (id: number) =>
    request<DocumentInfo & { pages: DocumentPage[] }>(`/documents/${id}`),
  deleteDocument: (id: number) => request<void>(`/documents/${id}`, { method: "DELETE" }),

  analyze: (projectId: number) =>
    request<{ status: string; analysis_id?: number; message?: string; errors?: string[]; skipped?: unknown[] }>(
      `/projects/${projectId}/analyze`,
      { method: "POST" },
    ),
  getAnalysis: (projectId: number) => request<Analysis>(`/projects/${projectId}/analysis`),
  updateAnalysis: (id: number, body: Partial<Analysis>) =>
    request<Analysis>(`/analysis/${id}`, { method: "PATCH", body: JSON.stringify(body) }),

  createEstimate: (
    projectId: number,
    body: {
      sections?: string[];
      quantities?: Record<string, Record<string, number>>;
      plants?: unknown[];
      options?: Record<string, boolean>;
      settings?: Record<string, unknown>;
    },
  ) =>
    request<{ status: string; estimate_id?: number; message?: string; validation?: Validation }>(
      `/projects/${projectId}/estimates`,
      { method: "POST", body: JSON.stringify(body) },
    ),
  getEstimate: (id: number) => request<Estimate>(`/estimates/${id}`),
  updateLine: (estimateId: number, lineId: number, body: Record<string, unknown>) =>
    request<{ sections: Section[]; totals: Totals; validation: Validation }>(
      `/estimates/${estimateId}/lines/${lineId}`,
      { method: "PATCH", body: JSON.stringify(body) },
    ),
  addLine: (estimateId: number, body: Record<string, unknown>) =>
    request<{ sections: Section[]; totals: Totals; validation: Validation }>(
      `/estimates/${estimateId}/lines`,
      { method: "POST", body: JSON.stringify(body) },
    ),
  deleteLine: (estimateId: number, lineId: number) =>
    request<{ sections: Section[]; totals: Totals; validation: Validation }>(
      `/estimates/${estimateId}/lines/${lineId}`,
      { method: "DELETE" },
    ),
  setOptions: (estimateId: number, options: Record<string, boolean>, settings: Record<string, unknown> = {}) =>
    request<{ sections: Section[]; totals: Totals; validation: Validation }>(
      `/estimates/${estimateId}/options`,
      { method: "POST", body: JSON.stringify({ options, settings }) },
    ),
  recalculate: (estimateId: number) =>
    request<{ sections: Section[]; totals: Totals; validation: Validation }>(
      `/estimates/${estimateId}/recalculate`,
      { method: "POST" },
    ),
  validateEstimate: (estimateId: number) => request<Validation>(`/estimates/${estimateId}/validate`),
  approve: (estimateId: number) =>
    request<{ status: string; report: Validation }>(`/estimates/${estimateId}/approve`, { method: "POST" }),
  exportUrl: (estimateId: number) => url(`/estimates/${estimateId}/export`),

  listQuestions: (
    projectId: number,
    filters: { status?: string; family?: string; q?: string } = {},
  ) => {
    const search = new URLSearchParams();
    Object.entries(filters).forEach(([k, v]) => {
      if (v) search.set(k, v);
    });
    const qs = search.toString();
    return request<Question[]>(`/projects/${projectId}/questions${qs ? `?${qs}` : ""}`);
  },
  questionsSummary: (projectId: number) =>
    request<QuestionSummary>(`/projects/${projectId}/questions/summary`),
  answerQuestion: (questionId: number, answer: string) =>
    request<unknown>(`/questions/${questionId}/answer`, {
      method: "POST",
      body: JSON.stringify({ answer }),
    }),
  bulkAnswer: (projectId: number, questionIds: number[], answer: string) =>
    request<{ status: string; answered: number }>(
      `/projects/${projectId}/questions/bulk-answer`,
      { method: "POST", body: JSON.stringify({ question_ids: questionIds, answer }) },
    ),
  dismissQuestions: (projectId: number, questionIds: number[], reason: string) =>
    request<{ status: string; dismissed: number }>(
      `/projects/${projectId}/questions/dismiss`,
      { method: "POST", body: JSON.stringify({ question_ids: questionIds, reason }) },
    ),

  catalog: (params: { q?: string; kind?: string; category?: string; limit?: number; offset?: number }) => {
    const search = new URLSearchParams();
    Object.entries(params).forEach(([k, v]) => {
      if (v !== undefined && v !== "") search.set(k, String(v));
    });
    return request<{ total: number; ranked: boolean; items: CatalogItem[] }>(`/catalog?${search}`);
  },
  catalogCategories: () => request<{ category: string; total: number }[]>("/catalog/categories"),
  catalogMatch: (q: string) =>
    request<{ status: string; confidence: Confidence; reason: string; best: CatalogItem | null; candidates: CatalogItem[] }>(
      `/catalog/match?q=${encodeURIComponent(q)}`,
    ),
};

export const money = (v: number | undefined | null) =>
  typeof v === "number"
    ? v.toLocaleString("uk-UA", { minimumFractionDigits: 2, maximumFractionDigits: 2 }) + " грн"
    : "—";

export const num = (v: number | undefined | null) =>
  typeof v === "number" ? v.toLocaleString("uk-UA", { maximumFractionDigits: 3 }) : "—";

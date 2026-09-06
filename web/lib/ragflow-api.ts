import { apiFetch, apiUrl } from "./api";

export interface RagflowRequestOptions {
  signal?: AbortSignal;
  /** Reuse only for the same user intent; writes are never retried here. */
  idempotencyKey?: string;
}

export type RagflowAccessMode = "read_only" | "manage_documents";
export type RagflowVisibility = "draft" | "active" | "disabled" | "archived";
export type RagflowDocumentState = "unstart" | "parsing" | "ready" | "failed" | "cancelled" | "unknown";
export type RagflowOperationState =
  | "INTENT_RECORDED" | "SEND_STARTED" | "ACKNOWLEDGED" | "RESULT_UNKNOWN"
  | "RECONCILING" | "RECONCILED" | "FAILED" | "NEEDS_ATTENTION";

export interface RagflowProfile {
  profile_id: string;
  base_url: string;
  allow_http: boolean;
  connect_timeout: number;
  read_timeout: number;
  upload_timeout: number;
  credential_revision: number;
  api_contract: string;
  has_api_key: boolean;
}
export interface RagflowProfileInput {
  base_url: string;
  api_key?: string;
  allow_http?: boolean;
  connect_timeout?: number;
  read_timeout?: number;
  upload_timeout?: number;
}
export interface RagflowProbe {
  ok: boolean;
  health: "unknown" | "healthy" | "unhealthy";
  auth: "unknown" | "valid" | "invalid";
  contract: "unknown" | "compatible" | "incompatible";
  version_unknown: boolean;
  contract_version: string;
  checks: Record<string, Record<string, string>>;
}
export interface RagflowDataset { id: string; name: string }
export interface RagflowConnectInput {
  name: string;
  profile_id: string;
  dataset_id: string;
  selected_document_ids: string[];
  access_mode?: RagflowAccessMode;
  description?: string;
}
export interface RagflowCreateInput {
  name: string;
  profile_id: string;
  description?: string;
  chunk_method?: string;
  embedding_model?: string;
}
export interface RagflowConnection {
  id: string;
  name: string;
  type: "ragflow";
  ownership: "attached";
  access_mode: RagflowAccessMode;
  active_documents: number;
}
export interface RagflowBinding {
  id: string;
  name: string;
  ownership: "attached" | "managed";
  access_mode: RagflowAccessMode;
  read_only: boolean;
  can_manage: boolean;
  can_write: boolean;
  can_reconcile: boolean;
  lifecycle_state: string;
}
export interface RagflowDocument {
  document_ref: string;
  name: string;
  state: RagflowDocumentState;
  visibility: RagflowVisibility | "deleted";
  revision: number;
  chunk_count: number;
  progress: number;
  meta_fields?: Record<string, unknown>;
  chunk_method?: string | null;
  parser_config?: Record<string, unknown>;
}
export interface RagflowDocumentPatch {
  name?: string;
  meta_fields?: Record<string, unknown>;
  chunk_method?: string;
  parser_config?: Record<string, unknown>;
}
export interface RagflowParseInput {
  use_ingestion_pipeline?: boolean;
  rebuild?: boolean;
  confirm_rebuild?: boolean;
}
export interface RagflowChunkInput {
  content: string;
  important_keywords?: string[];
  questions?: string[];
  tag_kwd?: string[];
}
export interface RagflowChunkPatch extends Partial<RagflowChunkInput> { available?: boolean }
export interface RagflowChunk extends RagflowChunkPatch {
  chunk_ref: string;
  document_ref: string;
  positions?: number[][];
}
export interface RagflowOperation {
  operation_id: string;
  task_id: string;
  action: string;
  state: RagflowOperationState;
  revision: number;
  /** Acceptance is not completion. Only RECONCILED is verified success. */
  accepted: true;
  error_type?: string | null;
  result: {
    document_ref?: string;
    chunk_ref?: string;
    document_status?: RagflowDocumentState;
    progress?: number;
  };
}
export type RagflowCreateResult = { id: string; name: string; http_status: number } &
  (RagflowOperation | { status: "ready" });
export type RagflowUploadResult = RagflowOperation | { accepted: true; operations: RagflowOperation[] };
export interface RagflowOperationEvent {
  id: number;
  state: RagflowOperationState;
  revision: number;
  event_type: string;
  error_type?: string | null;
  /** Unix seconds, as persisted by the server's operation store. */
  created_at: number;
}
export interface RagflowRetrievalSettings {
  page_size: number;
  similarity_threshold: number;
  vector_similarity_weight: number;
  rerank_id: string | null;
}
export interface RagflowRetrievalResult {
  query: string;
  content: string;
  answer: string;
  sources: Array<{
    document_id: string;
    chunk_id: string;
    title: string;
    content: string;
    score: number;
    page?: number;
    truncated?: boolean;
  }>;
  provider: "ragflow";
  content_kind: "retrieval_context";
  mode: "hybrid";
}
export interface RagflowPageParams { page?: number; page_size?: number }

const messages: Record<string, string> = {
  request_failed: "请求失败，请稍后重试。",
  auth_error: "连接认证失败，请检查服务端凭据配置。",
  unauthorized: "登录状态已失效，请重新登录。",
  forbidden: "没有权限执行此操作。",
  validation_error: "请求参数无效，请检查输入。",
  invalid_ragflow_request: "请求参数无效，请检查输入。",
  invalid_response: "服务返回格式不兼容，无法确认操作结果。",
  transport_error: "网络连接中断，操作结果可能未知；请先查看操作记录。",
  rate_limit: "请求过于频繁，请稍后再试。",
  upstream_error: "RAGFlow 服务暂时不可用，请查看操作记录。",
  provider_incompatible: "RAGFlow API 契约不兼容，请检查服务版本。",
  response_too_large: "响应超过安全大小限制。",
  file_error: "文件处理失败，请检查文件后重试。",
  not_found: "目标不存在或当前账号不可访问。",
  conflict: "状态已变化或存在未完成操作，请刷新后查看。",
  ragflow_operation_conflict: "存在未完成或需人工核验的操作，请先查看操作记录。",
  ragflow_read_only: "此连接为只读，不能修改远程内容。",
  ragflow_profile_admin_required: "只有管理员可以修改 RAGFlow 连接配置。",
  ragflow_profile_changed: "连接凭据已变化，请重新探测后操作。",
  ragflow_binding_changed: "知识库绑定已变化，请刷新后操作。",
  operation_binding_changed: "操作所属绑定已变化，不能在当前绑定继续对账。",
  ragflow_dataset_deleted: "远程知识库已删除，请先断开原绑定；不会自动重建。",
  ragflow_dataset_not_found: "远程知识库不存在或不可访问。",
  selected_document_not_ready: "所选文档尚未解析就绪，无法设为生效。",
  remote_web_ingestion_disabled: "当前适配器不支持网页摄取，请上传文件。",
  unavailable_chunk: "检索命中了不可用或尚不支持的父子分块，已安全拒绝。",
  attached_dataset_delete_forbidden: "接入的外部知识库不能从本平台整库删除。",
  dataset_delete_confirmation_mismatch: "确认名称与知识库名称不一致。",
  chunk_edits_require_rebuild_confirmation: "重新解析会覆盖分块编辑，请明确确认重建。",
  document_not_ready_for_activation: "文档尚未解析就绪，不能设为生效。",
  no_available_chunks: "文档没有可用分块，不能设为生效。",
  publication_sample_limit: "可用分块验证超过安全范围，请检查远程文档。",
  ragflow_upload_size_limit: "上传文件总量超过 100 MiB 限制。",
  ragflow_spool_capacity_reached: "上传暂存容量不足，请稍后重试。",
  invalid_upload_count: "请一次选择 1 至 20 个文件。",
  empty_upload: "不能上传空文件。",
  parse_failed: "远程解析失败，当前操作已结束。",
  empty_document: "解析结果没有分块，当前操作已失败。",
  ambiguous_remote_result: "远程结果无法唯一确认，需要人工核验。",
  partial_or_conflicting_update: "远程存在部分更新或冲突，需要人工核验。",
  chunk_creation_result_unknown: "分块创建结果未知，需要人工核验，不能自动重发。",
  document_finalization_incomplete: "文档收尾处理未完成，请检查操作记录。",
};

function safeCode(value: unknown, status = 0): string {
  if (typeof value === "string" && Object.hasOwn(messages, value)) return value;
  if (status === 401) return "unauthorized";
  if (status === 403) return "forbidden";
  if (status === 404 || status === 410) return "not_found";
  if (status === 409) return "conflict";
  if (status === 400 || status === 422) return "validation_error";
  if (status === 413) return "response_too_large";
  if (status === 429) return "rate_limit";
  if (status >= 500) return "upstream_error";
  return "request_failed";
}

export class RagflowApiError extends Error {
  readonly status: number;
  readonly code: string;
  constructor(status: number, code: string) {
    const safe = safeCode(code, status);
    super(messages[safe]);
    this.name = "RagflowApiError";
    this.status = status;
    this.code = safe;
  }
}

export type RagflowErrorTranslator = (key: string, options: { defaultValue: string }) => string;

/** Never send arbitrary Error.message or backend validation input to translation. */
export function ragflowErrorMessage(error: unknown, translate?: RagflowErrorTranslator): string {
  const code = error instanceof RagflowApiError ? safeCode(error.code, error.status)
    : typeof error === "string" ? safeCode(error) : "request_failed";
  const fallback = messages[code];
  return translate ? translate(`ragflow.error.${code}`, { defaultValue: fallback }) : fallback;
}

export function newOperationKey(): string {
  if (typeof crypto.randomUUID === "function") return crypto.randomUUID();
  // getRandomValues remains available on HTTP LAN origins without randomUUID.
  return Array.from(crypto.getRandomValues(new Uint8Array(16)), (byte) => byte.toString(16).padStart(2, "0")).join("");
}

export function isOperationPending(operation: string | { state: string }): boolean {
  const state = typeof operation === "string" ? operation : operation.state;
  return !["RECONCILED", "FAILED", "NEEDS_ATTENTION"].includes(state);
}

const base = "/api/v1/knowledge";
function segment(value: string): string {
  if (!value || value === "." || value === ".." || /[/\\?#\u0000-\u001f\u007f]/.test(value)) {
    throw new RagflowApiError(0, "validation_error");
  }
  return encodeURIComponent(value);
}
const kbPath = (kb: string) => `/${segment(kb)}`;
const docPath = (kb: string, doc: string) => `${kbPath(kb)}/documents/${segment(doc)}`;
const chunkPath = (kb: string, doc: string, chunk: string) => `${docPath(kb, doc)}/chunks/${segment(chunk)}`;
const opPath = (kb: string, op: string) => `${kbPath(kb)}/operations/${segment(op)}`;
function query(params: object = {}): string {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value !== undefined && value !== null) search.set(key, String(value));
  }
  return search.size ? `?${search}` : "";
}
function record(value: unknown): value is Record<string, unknown> {
  return !!value && typeof value === "object" && !Array.isArray(value);
}
function invalid(): never { throw new RagflowApiError(0, "invalid_response"); }

async function request(path: string, method: string, payload: unknown, options?: RagflowRequestOptions, readOnly = method === "GET"): Promise<Response> {
  const headers = new Headers();
  const multipart = payload instanceof FormData;
  if (payload !== undefined && !multipart) headers.set("Content-Type", "application/json");
  if (!readOnly && options?.idempotencyKey) headers.set("Idempotency-Key", options.idempotencyKey);
  try {
    return await apiFetch(apiUrl(base + path), {
      method, headers, cache: "no-store", signal: options?.signal,
      body: payload === undefined ? undefined : multipart ? payload : JSON.stringify(payload),
    });
  } catch (error) {
    if (record(error) && error.name === "AbortError") throw new DOMException("请求已取消。", "AbortError");
    throw new RagflowApiError(0, "transport_error");
  }
}
async function decode(response: Response): Promise<unknown> {
  let body: unknown;
  try { body = await response.json(); }
  catch { throw new RagflowApiError(response.status, response.ok ? "invalid_response" : safeCode(null, response.status)); }
  if (!response.ok) {
    throw new RagflowApiError(response.status, safeCode(record(body) ? body.detail ?? body.error_type : undefined, response.status));
  }
  return body;
}
async function jsonRequest<T>(path: string, method = "GET", payload?: unknown, options?: RagflowRequestOptions, readOnly = method === "GET"): Promise<T> {
  return await decode(await request(path, method, payload, options, readOnly)) as T;
}
function parseOperation(value: unknown): RagflowOperation {
  const states: RagflowOperationState[] = ["INTENT_RECORDED", "SEND_STARTED", "ACKNOWLEDGED", "RESULT_UNKNOWN", "RECONCILING", "RECONCILED", "FAILED", "NEEDS_ATTENTION"];
  if (!record(value) || typeof value.operation_id !== "string" || !value.operation_id ||
      typeof value.task_id !== "string" || typeof value.action !== "string" ||
      !states.includes(value.state as RagflowOperationState) || !Number.isInteger(value.revision) ||
      Number(value.revision) < 1 || value.accepted !== true || !record(value.result)) invalid();
  return {
    operation_id: value.operation_id, task_id: value.task_id, action: value.action,
    state: value.state as RagflowOperationState, revision: value.revision as number, accepted: true,
    error_type: value.error_type ? safeCode(value.error_type) : null,
    result: value.result as RagflowOperation["result"],
  };
}
async function operationRequest(path: string, method: string, payload?: unknown, options?: RagflowRequestOptions): Promise<RagflowOperation> {
  return parseOperation(await jsonRequest(path, method, payload, options));
}
function parseProfile(value: unknown): RagflowProfile {
  if (!record(value) || typeof value.profile_id !== "string" || typeof value.base_url !== "string" ||
      typeof value.has_api_key !== "boolean") invalid();
  // Explicit projection: credentials must never enter reusable frontend state.
  const { profile_id, base_url, allow_http, connect_timeout, read_timeout, upload_timeout, credential_revision, api_contract, has_api_key } = value;
  return { profile_id, base_url, allow_http, connect_timeout, read_timeout, upload_timeout, credential_revision, api_contract, has_api_key } as RagflowProfile;
}
async function fetchBlob(path: string, options?: RagflowRequestOptions): Promise<Blob> {
  const response = await request(path, "GET", undefined, options);
  if (!response.ok) { await decode(response); invalid(); }
  // JSON documents are legitimate attachments; unlabelled JSON can be an error envelope.
  if (response.headers.get("Content-Type")?.includes("json") &&
      !/^attachment(?:;|$)/i.test(response.headers.get("Content-Disposition") ?? "")) {
    const body = await decode(response);
    throw new RagflowApiError(response.status, safeCode(record(body) ? body.error_type ?? body.detail : undefined));
  }
  try { return await response.blob(); }
  catch (error) {
    if (record(error) && error.name === "AbortError") throw new DOMException("请求已取消。", "AbortError");
    throw new RagflowApiError(0, "transport_error");
  }
}

export const ragflowApi = {
  async listProfiles(options?: RagflowRequestOptions): Promise<{ profiles: RagflowProfile[] }> {
    const body = await jsonRequest<{ profiles: unknown[] }>("/rag-pipelines/ragflow/profiles", "GET", undefined, options);
    if (!record(body) || !Array.isArray(body.profiles)) invalid();
    return { profiles: body.profiles.map(parseProfile) };
  },
  async getProfile(profileId: string, options?: RagflowRequestOptions): Promise<RagflowProfile> {
    return parseProfile(await jsonRequest(`/rag-pipelines/ragflow/profiles/${segment(profileId)}`, "GET", undefined, options));
  },
  async saveProfile(profileId: string, payload: RagflowProfileInput, options?: RagflowRequestOptions): Promise<RagflowProfile> {
    return parseProfile(await jsonRequest(`/rag-pipelines/ragflow/profiles/${segment(profileId)}`, "PUT", payload, options));
  },
  probe: (payload: { profile_id: string }, options?: RagflowRequestOptions) =>
    jsonRequest<RagflowProbe>("/ragflow/probe", "POST", payload, options, true),
  listDatasets: (params: { profile_id: string } & RagflowPageParams, options?: RagflowRequestOptions) =>
    jsonRequest<{ datasets: RagflowDataset[] }>(`/ragflow/datasets${query(params)}`, "GET", undefined, options),
  connect: (payload: RagflowConnectInput, options?: RagflowRequestOptions) =>
    jsonRequest<RagflowConnection>("/connect-ragflow", "POST", payload, options),
  async create(payload: RagflowCreateInput, options?: RagflowRequestOptions): Promise<RagflowCreateResult> {
    const response = await request("/ragflow/create", "POST", payload, options);
    const body = await decode(response);
    if (!record(body) || typeof body.id !== "string" || typeof body.name !== "string") invalid();
    const identity = { id: body.id, name: body.name, http_status: response.status };
    return body.status === "ready" ? { ...identity, status: "ready" } : { ...parseOperation(body), ...identity };
  },
  getBinding: (kbRef: string, options?: RagflowRequestOptions) =>
    jsonRequest<RagflowBinding>(`${kbPath(kbRef)}/ragflow/binding`, "GET", undefined, options),
  listDocuments: (kbRef: string, params: RagflowPageParams & { keywords?: string } = {}, options?: RagflowRequestOptions) =>
    jsonRequest<{ documents: RagflowDocument[]; total: number }>(`${kbPath(kbRef)}/documents${query(params)}`, "GET", undefined, options),
  getDocument: (kbRef: string, documentRef: string, options?: RagflowRequestOptions) =>
    jsonRequest<RagflowDocument>(docPath(kbRef, documentRef), "GET", undefined, options),
  async upload(kbRef: string, files: File[], options?: RagflowRequestOptions): Promise<RagflowUploadResult> {
    if (files.length < 1 || files.length > 20) throw new RagflowApiError(0, "validation_error");
    const form = new FormData();
    files.forEach((file) => form.append("files", file));
    const body = await jsonRequest<unknown>(`${kbPath(kbRef)}/documents/upload`, "POST", form, options);
    if (record(body) && Array.isArray(body.operations) && body.accepted === true) {
      return { accepted: true, operations: body.operations.map(parseOperation) };
    }
    return parseOperation(body);
  },
  createDocument: (kbRef: string, payload: { name: string; type?: "empty" }, options?: RagflowRequestOptions) =>
    operationRequest(`${kbPath(kbRef)}/documents`, "POST", payload, options),
  updateDocument: (kbRef: string, documentRef: string, payload: RagflowDocumentPatch, options?: RagflowRequestOptions) =>
    operationRequest(docPath(kbRef, documentRef), "PATCH", payload, options),
  deleteDocument: (kbRef: string, documentRef: string, options?: RagflowRequestOptions) =>
    operationRequest(docPath(kbRef, documentRef), "DELETE", undefined, options),
  parse: (kbRef: string, documentRef: string, payload: RagflowParseInput = {}, options?: RagflowRequestOptions) =>
    operationRequest(`${docPath(kbRef, documentRef)}/parse`, "POST", payload, options),
  cancel: (kbRef: string, documentRef: string, payload: RagflowParseInput = {}, options?: RagflowRequestOptions) =>
    operationRequest(`${docPath(kbRef, documentRef)}/cancel`, "POST", payload, options),
  listChunks: (kbRef: string, documentRef: string, params: RagflowPageParams = {}, options?: RagflowRequestOptions) =>
    jsonRequest<{ chunks: RagflowChunk[]; total: number }>(`${docPath(kbRef, documentRef)}/chunks${query(params)}`, "GET", undefined, options),
  getChunk: (kbRef: string, documentRef: string, chunkRef: string, options?: RagflowRequestOptions) =>
    jsonRequest<RagflowChunk>(chunkPath(kbRef, documentRef, chunkRef), "GET", undefined, options),
  addChunk: (kbRef: string, documentRef: string, payload: RagflowChunkInput, options?: RagflowRequestOptions) =>
    operationRequest(`${docPath(kbRef, documentRef)}/chunks`, "POST", payload, options),
  updateChunk: (kbRef: string, documentRef: string, chunkRef: string, payload: RagflowChunkPatch, options?: RagflowRequestOptions) =>
    operationRequest(chunkPath(kbRef, documentRef, chunkRef), "PATCH", payload, options),
  deleteChunk: (kbRef: string, documentRef: string, chunkRef: string, options?: RagflowRequestOptions) =>
    operationRequest(chunkPath(kbRef, documentRef, chunkRef), "DELETE", undefined, options),
  setChunkAvailability: (kbRef: string, documentRef: string, chunkRef: string, payload: { available: boolean }, options?: RagflowRequestOptions) =>
    operationRequest(`${chunkPath(kbRef, documentRef, chunkRef)}/availability`, "PUT", payload, options),
  setVisibility: (kbRef: string, documentRef: string, payload: { visibility: RagflowVisibility; expected_revision: number }, options?: RagflowRequestOptions) =>
    jsonRequest<{ document_ref: string; visibility: RagflowVisibility; revision: number }>(`${docPath(kbRef, documentRef)}/visibility`, "PUT", payload, options),
  getRetrievalSettings: (kbRef: string, options?: RagflowRequestOptions) =>
    jsonRequest<RagflowRetrievalSettings>(`${kbPath(kbRef)}/retrieval-settings`, "GET", undefined, options),
  saveRetrievalSettings: (kbRef: string, payload: RagflowRetrievalSettings, options?: RagflowRequestOptions) =>
    jsonRequest<RagflowRetrievalSettings>(`${kbPath(kbRef)}/retrieval-settings`, "PUT", payload, options),
  async retrieve(kbRef: string, payload: { query: string; preview_document_refs?: string[] }, options?: RagflowRequestOptions): Promise<RagflowRetrievalResult> {
    const response = await request(`${kbPath(kbRef)}/retrieval-test`, "POST", payload, options, true);
    const body = await decode(response);
    if (!record(body)) invalid();
    if (body.error_type) throw new RagflowApiError(response.status, safeCode(body.error_type));
    if (!Array.isArray(body.sources) || typeof body.content !== "string") invalid();
    return body as unknown as RagflowRetrievalResult;
  },
  async listOperations(kbRef: string, options?: RagflowRequestOptions): Promise<{ operations: RagflowOperation[] }> {
    const body = await jsonRequest<{ operations: unknown[] }>(`${kbPath(kbRef)}/operations`, "GET", undefined, options);
    if (!record(body) || !Array.isArray(body.operations)) invalid();
    return { operations: body.operations.map(parseOperation) };
  },
  async getOperation(kbRef: string, operationId: string, options?: RagflowRequestOptions): Promise<RagflowOperation> {
    return parseOperation(await jsonRequest(opPath(kbRef, operationId), "GET", undefined, options));
  },
  getEvents: (kbRef: string, operationId: string, params: { after_id?: number } = {}, options?: RagflowRequestOptions) =>
    jsonRequest<{ events: RagflowOperationEvent[] }>(`${opPath(kbRef, operationId)}/events${query(params)}`, "GET", undefined, options),
  reconcile: (kbRef: string, operationId: string, payload: { expected_revision: number }, options?: RagflowRequestOptions) =>
    operationRequest(`${opPath(kbRef, operationId)}/reconcile`, "POST", payload, options),
  deleteDataset: (kbRef: string, payload: { confirm_name: string }, options?: RagflowRequestOptions) =>
    operationRequest(`${kbPath(kbRef)}/ragflow/dataset`, "DELETE", payload, options),
  downloadDocument: (kbRef: string, documentRef: string, options?: RagflowRequestOptions) =>
    fetchBlob(`${docPath(kbRef, documentRef)}/download`, options),
};

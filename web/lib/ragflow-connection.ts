/** Connection form values stay in the mounted component; none are persisted here. */
export interface RagflowProfileForm {
  profileId: string;
  baseUrl: string;
  apiKey: string;
  allowHttp: boolean;
}

export class RagflowConnectionInputError extends Error {
  constructor(readonly code: string, message: string) {
    super(message);
  }
}

export function emptyRagflowProfileForm(profile?: {
  profile_id: string;
  base_url: string;
  allow_http?: boolean;
}): RagflowProfileForm {
  return {
    profileId: profile?.profile_id ?? "default",
    baseUrl: profile?.base_url ?? "",
    apiKey: "",
    allowHttp: profile?.allow_http ?? false,
  };
}

export function buildRagflowProfilePayload(
  form: RagflowProfileForm, hasSavedKey: boolean, existingProfileIds: string[] = [],
  previousTimeouts?: { connect_timeout: number; read_timeout: number; upload_timeout: number },
) {
  if (!/^[A-Za-z0-9][A-Za-z0-9_.-]{0,159}$/.test(form.profileId))
    throw new RagflowConnectionInputError("profile_id_invalid", "连接标识只能包含字母、数字、下划线、点和短横线。");
  if (!hasSavedKey && existingProfileIds.includes(form.profileId))
    throw new RagflowConnectionInputError("profile_exists", "连接标识已存在。请先从列表选择该连接，再轮换密钥。");
  const baseUrl = form.baseUrl.trim().replace(/\/+$/, "");
  let url: URL;
  try {
    url = new URL(baseUrl);
  } catch {
    throw new RagflowConnectionInputError("service_url_required", "请填写完整的 RAGFlow 服务地址。");
  }
  if (
    !url.hostname || url.username || url.password || url.search || url.hash ||
    (url.protocol !== "https:" && !(form.allowHttp && url.protocol === "http:"))
  ) throw new RagflowConnectionInputError("service_url_unsafe", "服务地址需使用 HTTPS，且不能包含用户名、密码或查询参数。");
  const apiKey = form.apiKey.trim();
  if ((!hasSavedKey && !apiKey) || (apiKey && !/^[\x21-\x7e]+$/.test(apiKey)))
    throw new RagflowConnectionInputError("api_key_invalid", "请填写有效的 API Key。");
  return {
    base_url: baseUrl, allow_http: form.allowHttp, ...(apiKey ? { api_key: apiKey } : {}),
    ...(previousTimeouts ? {
      connect_timeout: previousTimeouts.connect_timeout,
      read_timeout: previousTimeouts.read_timeout,
      upload_timeout: previousTimeouts.upload_timeout,
    } : {}),
  };
}

type ManagedInput = { mode: "managed"; name: string; profileId: string };
type AttachInput = {
  mode: "attach";
  name: string;
  profileId: string;
  datasetId: string;
  accessMode?: "read_only" | "manage_documents";
};
type ManagedRequest = { mode: "managed"; payload: { name: string; profile_id: string } };
type AttachRequest = { mode: "attach"; payload: {
  name: string; profile_id: string; dataset_id: string;
  access_mode: "read_only" | "manage_documents"; selected_document_ids: string[];
} };

export function buildKnowledgeConnection(input: AttachInput): AttachRequest;
export function buildKnowledgeConnection(input: ManagedInput): ManagedRequest;
export function buildKnowledgeConnection(input: AttachInput | ManagedInput): AttachRequest | ManagedRequest;
export function buildKnowledgeConnection(input: AttachInput | ManagedInput): AttachRequest | ManagedRequest {
  const name = input.name.trim();
  if (!name || /[\/\\\x00-\x1f]/.test(name) || name === "." || name === "..")
    throw new RagflowConnectionInputError("knowledge_name_invalid", "请填写有效的知识库名称。");
  if (!input.profileId) throw new RagflowConnectionInputError("profile_required", "请先选择并检测连接。");
  if (input.mode === "managed") return { mode: "managed", payload: { name, profile_id: input.profileId } };
  if (!input.datasetId) throw new RagflowConnectionInputError("dataset_required", "请选择要接入的数据集。");
  return { mode: "attach", payload: {
    name, profile_id: input.profileId, dataset_id: input.datasetId,
    access_mode: input.accessMode ?? "read_only", selected_document_ids: [],
  } };
}

export function canUseRagflowProfile(probe: {
  ok: boolean; health: string; auth: string; contract: string;
} | null, dirty: boolean): boolean {
  return !dirty && probe?.ok === true && probe.health === "healthy" &&
    probe.auth === "valid" && probe.contract === "compatible";
}

export type ConnectionChannel = "profiles" | "save" | "probe" | "datasets" | "submit";
interface ConnectionTicket { generation: number; channel: ConnectionChannel; sequence: number }

export function createConnectionRequests() {
  let generation = 0;
  const sequences = new Map<ConnectionChannel, number>();
  return {
    invalidate() { generation += 1; sequences.clear(); },
    begin(channel: ConnectionChannel): ConnectionTicket {
      const sequence = (sequences.get(channel) ?? 0) + 1;
      sequences.set(channel, sequence);
      return { generation, channel, sequence };
    },
    isCurrent(ticket: ConnectionTicket): boolean {
      return ticket.generation === generation && sequences.get(ticket.channel) === ticket.sequence;
    },
  };
}

export interface RagflowConnectionOutcome {
  kbRef: string;
  operationId: string;
  status: "ready" | "pending" | "attention";
}

export function connectionResult(result: {
  id: string; state?: string; status?: string; operation_id?: string; http_status?: number;
}, mode: "managed" | "attach"): RagflowConnectionOutcome {
  if (!/^(user|admin):kb:[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i.test(result.id))
    throw new RagflowConnectionInputError("knowledge_identity_invalid", "服务未返回有效的知识库标识，请刷新知识库列表核对。");
  const status = result.state === "FAILED" || result.state === "NEEDS_ATTENTION" ? "attention"
    : result.http_status === 202 ? "pending"
    : mode === "attach" || result.status === "ready" || result.state === "RECONCILED" ? "ready"
    : "pending";
  return { kbRef: result.id, operationId: result.operation_id ?? "", status };
}

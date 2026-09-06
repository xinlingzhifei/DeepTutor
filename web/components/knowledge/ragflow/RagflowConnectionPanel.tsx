"use client";

import { useCallback, useEffect, useId, useRef, useState } from "react";
import { CheckCircle2, ChevronLeft, ChevronRight, Database, Loader2, RefreshCw, Server } from "lucide-react";
import { useTranslation } from "react-i18next";

import { useAuthStatus } from "@/hooks/useAuthStatus";
import {
  newOperationKey, ragflowApi, ragflowErrorMessage,
  type RagflowDataset, type RagflowProbe, type RagflowProfile,
} from "@/lib/ragflow-api";
import {
  buildKnowledgeConnection, buildRagflowProfilePayload, canUseRagflowProfile,
  connectionResult, createConnectionRequests, emptyRagflowProfileForm,
  RagflowConnectionInputError,
  type ConnectionChannel, type RagflowConnectionOutcome, type RagflowProfileForm,
} from "@/lib/ragflow-connection";

interface RagflowConnectionPanelProps {
  onConnected: (kbRef: string) => void;
  onChanged?: () => void;
  mode?: "create" | "settings";
}

const PAGE_SIZE = 20;
const inputClass = "w-full rounded-lg border border-[var(--border)] bg-[var(--background)] px-3 py-2 text-sm text-[var(--foreground)] outline-none focus:border-[var(--foreground)]/40 disabled:opacity-50";
const buttonClass = "inline-flex items-center justify-center gap-2 rounded-lg border border-[var(--border)] px-3 py-2 text-xs font-medium text-[var(--foreground)] transition-colors hover:bg-[var(--muted)] disabled:cursor-not-allowed disabled:opacity-50";

export default function RagflowConnectionPanel({
  onConnected, onChanged, mode = "create",
}: RagflowConnectionPanelProps) {
  const { t } = useTranslation();
  const auth = useAuthStatus();
  const fieldId = useId();
  const [requests] = useState(createConnectionRequests);
  const controllers = useRef(new Set<AbortController>());
  const submission = useRef<{ identity: string; key: string } | null>(null);
  const [profiles, setProfiles] = useState<RagflowProfile[]>([]);
  const [selectedProfileId, setSelectedProfileId] = useState("");
  const [form, setForm] = useState(emptyRagflowProfileForm);
  const [probeResult, setProbeResult] = useState<RagflowProbe | null>(null);
  const [datasets, setDatasets] = useState<RagflowDataset[]>([]);
  const [datasetId, setDatasetId] = useState("");
  const [page, setPage] = useState(1);
  const [loadedDatasets, setLoadedDatasets] = useState(false);
  const [connectionMode, setConnectionMode] = useState<"managed" | "attach">("managed");
  const [accessMode, setAccessMode] = useState<"read_only" | "manage_documents">("read_only");
  const [name, setName] = useState("");
  const [busy, setBusy] = useState<ConnectionChannel | null>(null);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [outcome, setOutcome] = useState<RagflowConnectionOutcome | null>(null);

  const selectedProfile = profiles.find((profile) => profile.profile_id === selectedProfileId);
  const dirty = !selectedProfile || form.profileId !== selectedProfile.profile_id ||
    form.baseUrl.trim().replace(/\/+$/, "") !== selectedProfile.base_url ||
    form.allowHttp !== Boolean(selectedProfile.allow_http) || Boolean(form.apiKey);
  const verified = Boolean(selectedProfile) && canUseRagflowProfile(probeResult, dirty);
  const writing = busy === "save" || busy === "submit";

  const resetEvidence = useCallback(() => {
    requests.invalidate();
    controllers.current.forEach((controller) => controller.abort());
    controllers.current.clear();
    submission.current = null;
    setBusy(null);
    setProbeResult(null);
    setDatasets([]);
    setDatasetId("");
    setPage(1);
    setLoadedDatasets(false);
    setAccessMode("read_only");
    setOutcome(null);
    setError("");
    setNotice("");
  }, [requests]);

  const applyProfile = useCallback((profile?: RagflowProfile) => {
    resetEvidence();
    setSelectedProfileId(profile?.profile_id ?? "");
    setForm(emptyRagflowProfileForm(profile));
  }, [resetEvidence]);

  const perform = useCallback(async <T,>(
    channel: ConnectionChannel,
    work: (signal: AbortSignal) => Promise<T>,
    accept: (result: T) => void,
  ) => {
    const ticket = requests.begin(channel);
    const controller = new AbortController();
    controllers.current.add(controller);
    setBusy(channel);
    setError("");
    try {
      const result = await work(controller.signal);
      if (requests.isCurrent(ticket) && !controller.signal.aborted) accept(result);
    } catch (failure) {
      if (requests.isCurrent(ticket) && !controller.signal.aborted) {
        setError(failure instanceof RagflowConnectionInputError
          ? t(`ragflow.input.${failure.code}`, { defaultValue: failure.message })
          : ragflowErrorMessage(failure, t));
      }
    } finally {
      controllers.current.delete(controller);
      if (requests.isCurrent(ticket)) setBusy(null);
    }
  }, [requests, t]);

  const loadProfiles = useCallback((preferredId = "") => perform(
    "profiles", (signal) => ragflowApi.listProfiles({ signal }),
    (result) => {
      setProfiles(result.profiles);
      applyProfile(result.profiles.find((profile) => profile.profile_id === preferredId) ?? result.profiles[0]);
    },
  ), [applyProfile, perform]);

  useEffect(() => {
    if (auth.loading) return;
    applyProfile();
    setProfiles([]);
    void loadProfiles();
    const pendingControllers = controllers.current;
    return () => {
      requests.invalidate();
      pendingControllers.forEach((controller) => controller.abort());
      pendingControllers.clear();
    };
  }, [auth.loading, auth.isAdmin, auth.activeTenantId, applyProfile, loadProfiles, requests]);

  const changeForm = (patch: Partial<RagflowProfileForm>) => {
    resetEvidence();
    setForm((current) => ({ ...current, ...patch }));
  };

  const saveProfile = async () => {
    if (!auth.isAdmin || auth.loading) return;
    try {
      const payload = buildRagflowProfilePayload(
        form, Boolean(selectedProfile?.has_api_key), profiles.map((profile) => profile.profile_id), selectedProfile,
      );
      await perform("save", (signal) => ragflowApi.saveProfile(form.profileId, payload, { signal }), (saved) => {
        resetEvidence();
        setProfiles((current) => [...current.filter((profile) => profile.profile_id !== saved.profile_id), saved]
          .sort((left, right) => left.profile_id.localeCompare(right.profile_id)));
        setSelectedProfileId(saved.profile_id);
        setForm(emptyRagflowProfileForm(saved));
        setNotice(t("ragflow.connection.saved", { defaultValue: "连接已保存，密钥输入已清空。请重新检测连接。" }));
        onChanged?.();
      });
    } catch (failure) {
      setError(failure instanceof RagflowConnectionInputError
        ? t(`ragflow.input.${failure.code}`, { defaultValue: failure.message })
        : ragflowErrorMessage(failure, t));
    }
  };

  const testConnection = async () => {
    if (!selectedProfileId || dirty) return;
    resetEvidence();
    await perform("probe", (signal) => ragflowApi.probe({ profile_id: selectedProfileId }, { signal }), setProbeResult);
  };

  const loadDatasets = async (nextPage: number) => {
    if (!verified) return;
    setDatasetId("");
    setDatasets([]);
    setOutcome(null);
    await perform("datasets", (signal) => ragflowApi.listDatasets({
      profile_id: selectedProfileId, page: nextPage, page_size: PAGE_SIZE,
    }, { signal }), (result) => {
      setDatasets(result.datasets);
      setPage(nextPage);
      setLoadedDatasets(true);
    });
  };

  const connectKnowledge = async () => {
    if (!verified) return;
    try {
      const request = connectionMode === "managed"
        ? buildKnowledgeConnection({ mode: "managed", name, profileId: selectedProfileId })
        : buildKnowledgeConnection({ mode: "attach", name, profileId: selectedProfileId, datasetId, accessMode });
      const identity = JSON.stringify(request);
      if (submission.current?.identity !== identity)
        submission.current = { identity, key: newOperationKey() };
      const idempotencyKey = submission.current.key;
      await perform("submit", async (signal) => {
        const response = request.mode === "managed"
          ? await ragflowApi.create(request.payload, { signal, idempotencyKey })
          : await ragflowApi.connect(request.payload, { signal, idempotencyKey });
        return connectionResult(response, request.mode);
      }, (result) => {
        setOutcome(result);
        onChanged?.();
        if (result.status === "ready") onConnected(result.kbRef);
      });
    } catch (failure) {
      setError(failure instanceof RagflowConnectionInputError
        ? t(`ragflow.input.${failure.code}`, { defaultValue: failure.message })
        : ragflowErrorMessage(failure, t));
    }
  };

  return (
    <section className="space-y-5 text-[var(--foreground)]" aria-label={t("ragflow.connection.label", { defaultValue: "RAGFlow 连接与知识库" })}>
      <div className="flex items-start gap-3">
        <Server className="mt-0.5 h-5 w-5 shrink-0 text-[var(--muted-foreground)]" aria-hidden="true" />
        <div className="space-y-1">
          <p className="text-sm font-medium">{mode === "settings" ? t("ragflow.connection.settingsTitle", { defaultValue: "RAGFlow 连接设置" }) : t("ragflow.connection.title", { defaultValue: "接入 RAGFlow 知识库" })}</p>
          <p className="text-xs leading-relaxed text-[var(--muted-foreground)]">{t("ragflow.connection.description", { defaultValue: "选择服务端保存的连接，检测后创建数据集或接入已有数据集。" })}</p>
        </div>
      </div>

      <div className="space-y-3 rounded-xl border border-[var(--border)] p-4">
        <div className="flex items-end gap-2">
          <label htmlFor={`${fieldId}-profile`} className="min-w-0 flex-1 space-y-1.5 text-xs">
            <span>{t("ragflow.connection.profile", { defaultValue: "连接配置" })}</span>
            <select id={`${fieldId}-profile`} className={inputClass} value={selectedProfileId}
              disabled={auth.loading || writing || busy === "profiles"}
              onChange={(event) => applyProfile(profiles.find((profile) => profile.profile_id === event.target.value))}>
              <option value="">{auth.isAdmin ? t("ragflow.connection.newProfile", { defaultValue: "新建连接配置" }) : t("ragflow.connection.selectProfile", { defaultValue: "请选择连接配置" })}</option>
              {profiles.map((profile) => <option key={profile.profile_id} value={profile.profile_id}>{profile.profile_id}</option>)}
            </select>
          </label>
          <button type="button" className={buttonClass} disabled={auth.loading || Boolean(busy)} onClick={() => void loadProfiles(selectedProfileId)}
            aria-label={t("ragflow.connection.reloadProfiles", { defaultValue: "刷新连接列表" })}>
            <RefreshCw className={`h-4 w-4 ${busy === "profiles" ? "animate-spin" : ""}`} aria-hidden="true" />
          </button>
        </div>

        {auth.loading ? <p className="flex items-center gap-2 text-xs text-[var(--muted-foreground)]"><Loader2 className="h-4 w-4 animate-spin" aria-hidden="true" />{t("ragflow.connection.loadingPermissions", { defaultValue: "正在读取权限…" })}</p> : auth.isAdmin ? (
          <>
            <div className="grid gap-3 sm:grid-cols-2">
              <label htmlFor={`${fieldId}-profile-id`} className="space-y-1.5 text-xs">
                <span>{t("ragflow.connection.profileId", { defaultValue: "连接标识" })}</span>
                <input id={`${fieldId}-profile-id`} value={form.profileId} onChange={(event) => changeForm({ profileId: event.target.value })}
                  disabled={writing || Boolean(selectedProfileId)} maxLength={160} autoComplete="off" className={inputClass} />
              </label>
              <label htmlFor={`${fieldId}-url`} className="space-y-1.5 text-xs">
                <span>{t("ragflow.connection.url", { defaultValue: "RAGFlow 服务地址" })}</span>
                <input id={`${fieldId}-url`} type="url" value={form.baseUrl} onChange={(event) => changeForm({ baseUrl: event.target.value })}
                  disabled={writing} placeholder={t("ragflow.connection.exampleUrl", { defaultValue: "https://ragflow.example" })} autoComplete="off" spellCheck={false} className={inputClass} />
              </label>
            </div>
            <label htmlFor={`${fieldId}-key`} className="block space-y-1.5 text-xs">
              <span>{t("ragflow.connection.apiKey", { defaultValue: "API Key" })}</span>
              <input id={`${fieldId}-key`} type="password" value={form.apiKey} onChange={(event) => changeForm({ apiKey: event.target.value })}
                disabled={writing} autoComplete="new-password" spellCheck={false}
                placeholder={selectedProfile?.has_api_key ? t("ragflow.connection.keepKey", { defaultValue: "已保存密钥；留空保持原密钥，填写新值进行轮换" }) : t("ragflow.connection.enterKey", { defaultValue: "输入 RAGFlow API Key" })}
                className={inputClass} />
            </label>
            <label className="flex items-start gap-2 text-xs text-[var(--muted-foreground)]">
              <input type="checkbox" checked={form.allowHttp} onChange={(event) => changeForm({ allowHttp: event.target.checked })} disabled={writing} className="mt-0.5 accent-[var(--primary)]" />
              <span>{t("ragflow.connection.allowHttp", { defaultValue: "允许 HTTP 连接（仅用于已配置的受信任内网服务）" })}</span>
            </label>
            <div className="flex flex-wrap items-center gap-3">
              <button type="button" className={buttonClass} onClick={() => void saveProfile()} disabled={Boolean(busy) || !dirty}>
                {busy === "save" && <Loader2 className="h-3.5 w-3.5 animate-spin" aria-hidden="true" />}
                {t("ragflow.connection.save", { defaultValue: "保存连接" })}
              </button>
              <p className="text-xs text-[var(--muted-foreground)]">{t("ragflow.connection.secretHelp", { defaultValue: "密钥不会回填到页面；保存后输入值会清空。" })}</p>
            </div>
          </>
        ) : <p className="text-xs leading-relaxed text-[var(--muted-foreground)]">{t("ragflow.connection.adminOnly", { defaultValue: "连接地址和密钥由管理员配置；你可以使用当前账户可访问的连接。" })}</p>}

        <div className="border-t border-[var(--border)] pt-3">
          <button type="button" className={buttonClass} onClick={() => void testConnection()} disabled={!selectedProfileId || dirty || Boolean(busy)}>
            {busy === "probe" ? <Loader2 className="h-3.5 w-3.5 animate-spin" aria-hidden="true" /> : <Server className="h-3.5 w-3.5" aria-hidden="true" />}
            {t("ragflow.connection.probe", { defaultValue: "检测连接" })}
          </button>
          {dirty && selectedProfile && <p className="mt-2 text-xs text-[var(--muted-foreground)]">{t("ragflow.connection.saveFirst", { defaultValue: "配置已变更，请先保存，再重新检测。" })}</p>}
          {probeResult && <div role="status" className="mt-3 space-y-1.5 text-xs">
            <p className="flex items-center gap-2 font-medium">
              {verified && <CheckCircle2 className="h-4 w-4 text-emerald-600 dark:text-emerald-400" aria-hidden="true" />}
              {verified ? t("ragflow.connection.probePassed", { defaultValue: "连接与接口检测通过" }) : t("ragflow.connection.probeUnconfirmed", { defaultValue: "连接检测尚未全部通过" })}
            </p>
            <dl className="grid grid-cols-3 gap-2 rounded-lg bg-[var(--muted)]/40 p-3">
              <div><dt className="text-[var(--muted-foreground)]">{t("ragflow.connection.health", { defaultValue: "服务健康" })}</dt><dd className="mt-1">{probeResult.health === "healthy" ? t("ragflow.connection.passed", { defaultValue: "通过" }) : probeResult.health === "unhealthy" ? t("ragflow.connection.failed", { defaultValue: "未通过" }) : t("ragflow.connection.unknown", { defaultValue: "未确认" })}</dd></div>
              <div><dt className="text-[var(--muted-foreground)]">{t("ragflow.connection.auth", { defaultValue: "凭据" })}</dt><dd className="mt-1">{probeResult.auth === "valid" ? t("ragflow.connection.valid", { defaultValue: "有效" }) : probeResult.auth === "invalid" ? t("ragflow.connection.invalid", { defaultValue: "无效" }) : t("ragflow.connection.unknown", { defaultValue: "未确认" })}</dd></div>
              <div><dt className="text-[var(--muted-foreground)]">{t("ragflow.connection.contract", { defaultValue: "API 契约" })}</dt><dd className="mt-1">{probeResult.contract === "compatible" ? t("ragflow.connection.compatible", { defaultValue: "兼容" }) : probeResult.contract === "incompatible" ? t("ragflow.connection.incompatible", { defaultValue: "不兼容" }) : t("ragflow.connection.unknown", { defaultValue: "未确认" })}</dd></div>
            </dl>
            <p className="text-[var(--muted-foreground)]">{t("ragflow.connection.versionUnknown", { defaultValue: "服务版本未确认（version_unknown）；检测不代表模型或文档解析已经可用。" })}</p>
          </div>}
        </div>
      </div>

      <div className="space-y-3 rounded-xl border border-[var(--border)] p-4">
        <div className="flex items-center gap-2 text-sm font-medium"><Database className="h-4 w-4" aria-hidden="true" />{t("ragflow.connection.knowledge", { defaultValue: "知识库接入方式" })}</div>
        <div className="flex flex-wrap gap-2" role="group" aria-label={t("ragflow.connection.ownership", { defaultValue: "数据集接入方式" })}>
          <button type="button" className={`${buttonClass} ${connectionMode === "managed" ? "bg-[var(--muted)]" : ""}`} aria-pressed={connectionMode === "managed"} disabled={writing}
            onClick={() => { setConnectionMode("managed"); setOutcome(null); }}>
            {t("ragflow.connection.managed", { defaultValue: "创建新数据集" })}
          </button>
          <button type="button" className={`${buttonClass} ${connectionMode === "attach" ? "bg-[var(--muted)]" : ""}`} aria-pressed={connectionMode === "attach"} disabled={writing}
            onClick={() => { setConnectionMode("attach"); setOutcome(null); }}>
            {t("ragflow.connection.attach", { defaultValue: "接入已有数据集" })}
          </button>
        </div>
        <label htmlFor={`${fieldId}-name`} className="block space-y-1.5 text-xs">
          <span>{t("ragflow.connection.name", { defaultValue: "yFeiSTAI 知识库名称" })}</span>
          <input id={`${fieldId}-name`} value={name} onChange={(event) => { setName(event.target.value); setOutcome(null); }} disabled={writing} maxLength={200} className={inputClass} />
        </label>
        {connectionMode === "attach" && <>
          <div className="flex flex-wrap items-center justify-between gap-2">
            <button type="button" className={buttonClass} onClick={() => void loadDatasets(1)} disabled={!verified || Boolean(busy)}>
              {busy === "datasets" && <Loader2 className="h-3.5 w-3.5 animate-spin" aria-hidden="true" />}
              {t("ragflow.connection.loadDatasets", { defaultValue: "读取数据集列表" })}
            </button>
            {loadedDatasets && <div className="flex items-center gap-2 text-xs">
              <button type="button" className={buttonClass} disabled={page <= 1 || Boolean(busy)} onClick={() => void loadDatasets(page - 1)} aria-label={t("ragflow.connection.previousPage", { defaultValue: "上一页" })}><ChevronLeft className="h-3.5 w-3.5" aria-hidden="true" /></button>
              <span>{t("ragflow.connection.page", { defaultValue: "第 {{page}} 页", page })}</span>
              <button type="button" className={buttonClass} disabled={datasets.length < PAGE_SIZE || Boolean(busy)} onClick={() => void loadDatasets(page + 1)} aria-label={t("ragflow.connection.nextPage", { defaultValue: "下一页" })}><ChevronRight className="h-3.5 w-3.5" aria-hidden="true" /></button>
            </div>}
          </div>
          <label htmlFor={`${fieldId}-dataset`} className="block space-y-1.5 text-xs">
            <span>{t("ragflow.connection.dataset", { defaultValue: "RAGFlow 数据集" })}</span>
            <select id={`${fieldId}-dataset`} value={datasetId} onChange={(event) => { setDatasetId(event.target.value); setOutcome(null); }} disabled={!verified || Boolean(busy)} className={inputClass}>
              <option value="">{t("ragflow.connection.selectDataset", { defaultValue: "请选择数据集" })}</option>
              {datasets.map((dataset) => <option key={dataset.id} value={dataset.id}>{dataset.name}</option>)}
            </select>
          </label>
          {loadedDatasets && datasets.length === 0 && busy !== "datasets" && <p className="text-xs text-[var(--muted-foreground)]">{t("ragflow.connection.noDatasets", { defaultValue: "当前页没有可访问的数据集。" })}</p>}
          <label htmlFor={`${fieldId}-access`} className="block space-y-1.5 text-xs">
            <span>{t("ragflow.connection.access", { defaultValue: "远端文档管理权限" })}</span>
            <select id={`${fieldId}-access`} value={accessMode} onChange={(event) => setAccessMode(event.target.value === "manage_documents" ? "manage_documents" : "read_only")} disabled={writing} className={inputClass}>
              <option value="read_only">{t("ragflow.connection.readOnly", { defaultValue: "只读接入（默认）" })}</option>
              <option value="manage_documents">{t("ragflow.connection.manageDocuments", { defaultValue: "允许管理远端文档" })}</option>
            </select>
          </label>
          {accessMode === "manage_documents" && <p className="text-xs leading-relaxed text-[var(--muted-foreground)]">{t("ragflow.connection.manageHelp", { defaultValue: "允许上传、解析、编辑和删除远端文档。附加的数据集不能通过此绑定删除。" })}</p>}
        </>}
        <p className="text-xs leading-relaxed text-[var(--muted-foreground)]">{t("ragflow.connection.draftHelp", { defaultValue: "接入后文档默认为草稿，不参与普通检索。请进入知识库逐条核验并发布。" })}</p>
        {!verified && <p className="text-xs text-[var(--muted-foreground)]">{t("ragflow.connection.probeFirst", { defaultValue: "请先完成上方连接检测。" })}</p>}
        <button type="button" className="inline-flex items-center justify-center gap-2 rounded-lg bg-[var(--primary)] px-4 py-2 text-sm font-medium text-[var(--primary-foreground)] disabled:cursor-not-allowed disabled:opacity-50"
          disabled={!verified || !name.trim() || (connectionMode === "attach" && !datasetId) || Boolean(busy)} onClick={() => void connectKnowledge()}>
          {busy === "submit" && <Loader2 className="h-4 w-4 animate-spin" aria-hidden="true" />}
          {connectionMode === "managed" ? t("ragflow.connection.create", { defaultValue: "创建知识库" }) : t("ragflow.connection.connect", { defaultValue: "接入知识库" })}
        </button>
      </div>

      {notice && <p role="status" className="text-xs text-[var(--muted-foreground)]">{notice}</p>}
      {error && <p role="alert" className="rounded-lg border border-red-500/20 bg-red-500/5 px-3 py-2 text-sm text-red-700 dark:text-red-300">{error}</p>}
      {outcome && outcome.status !== "ready" && <div role="status" className="space-y-2 rounded-lg border border-[var(--border)] bg-[var(--muted)]/40 p-3 text-sm">
        <p>{outcome.status === "attention" ? t("ragflow.connection.needsAttention", { defaultValue: "创建操作需要处理，请进入知识库查看详情。" }) : t("ragflow.connection.pending", { defaultValue: "创建请求已受理，仍在处理中，尚未确认完成。" })}</p>
        {outcome.operationId && <p className="break-all text-xs text-[var(--muted-foreground)]">{t("ragflow.connection.operation", { defaultValue: "操作编号：{{id}}", id: outcome.operationId })}</p>}
        <button type="button" className={buttonClass} onClick={() => onConnected(outcome.kbRef)}>{t("ragflow.connection.openPending", { defaultValue: "进入知识库查看操作" })}</button>
      </div>}
    </section>
  );
}

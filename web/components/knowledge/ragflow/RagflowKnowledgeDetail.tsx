"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import {
  AlertTriangle,
  ArrowLeft,
  CheckCircle2,
  ChevronLeft,
  ChevronRight,
  Database,
  Download,
  FileText,
  History,
  Layers,
  Loader2,
  Plus,
  RefreshCw,
  Search,
  Settings,
  ShieldCheck,
  Upload,
} from "lucide-react";
import Modal from "@/components/common/Modal";
import type { KnowledgeBase } from "@/lib/knowledge-helpers";
import {
  ragflowApi,
  RagflowApiError,
  ragflowErrorMessage,
  newOperationKey,
  type RagflowBinding,
  type RagflowDocument,
  type RagflowOperation,
  type RagflowRetrievalSettings,
  type RagflowRetrievalResult,
  type RagflowVisibility,
} from "@/lib/ragflow-api";
import {
  IntentKeys,
  managementAccess,
  operationPhase,
  shouldPollOperations,
} from "./management-state";
import {
  buttonClass,
  fieldClass,
  primaryClass,
  ChunkPanel,
  DocumentEditor,
  type RunMutation,
} from "./RagflowContentPanels";
import { RagflowOperationEvents } from "./RagflowOperationEvents";

interface Props {
  kb: KnowledgeBase;
  onBack: () => void;
  onChanged: () => void;
  onDisconnect: (kbRef: string) => Promise<void>;
}

const labels: Record<string, string> = {
  draft: "草稿",
  active: "已发布",
  disabled: "已停用",
  archived: "已归档",
  deleted: "已删除",
  uploading: "上传中",
  unstart: "待解析",
  parsing: "解析中",
  ready: "解析完成",
  failed: "失败",
  cancelled: "已取消",
  unknown: "状态待核验",
  INTENT_RECORDED: "已受理",
  SEND_STARTED: "已发送",
  ACKNOWLEDGED: "远端已接收",
  RECONCILING: "核对结果中",
  RESULT_UNKNOWN: "结果未知",
  RECONCILED: "已核对完成",
  FAILED: "执行失败",
  NEEDS_ATTENTION: "需要处理",
  upload: "上传文档",
  create_document: "创建文档",
  update_document: "更新文档",
  delete_document: "删除文档",
  parse: "解析文档",
  cancel: "取消解析",
  create_dataset: "创建知识库",
  delete_dataset: "删除远端知识库",
  add_chunk: "新增片段",
  update_chunk: "编辑片段",
  delete_chunk: "删除片段",
  enable_chunk: "片段启停",
};

function asOperations(value: unknown): RagflowOperation[] {
  if (!value || typeof value !== "object") return [];
  if ("operation_id" in value) return [value as RagflowOperation];
  if ("operations" in value && Array.isArray(value.operations))
    return value.operations as RagflowOperation[];
  return [];
}

export default function RagflowKnowledgeDetail(props: Props) {
  // Remounting by immutable resource identity discards all old dialogs and pending UI state.
  return <Detail key={props.kb.id ?? "missing-identity"} {...props} />;
}

function Detail({ kb, onBack, onChanged, onDisconnect }: Props) {
  const { t } = useTranslation();
  const kbRef = kb.id ?? "";
  const [binding, setBinding] = useState<RagflowBinding | null>(null);
  const [bindingLoaded, setBindingLoaded] = useState(false);
  const [section, setSection] = useState<"documents" | "retrieval" | "operations" | "settings">(
    "documents",
  );
  const [documents, setDocuments] = useState<RagflowDocument[]>([]);
  const [operations, setOperations] = useState<RagflowOperation[]>([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [query, setQuery] = useState("");
  const [keywords, setKeywords] = useState("");
  const [refresh, setRefresh] = useState(0);
  const [loaded, setLoaded] = useState(false);
  const [documentsAvailable, setDocumentsAvailable] = useState(false);
  const [operationsSnapshot, setOperationsSnapshot] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [selectedDocument, setSelectedDocument] = useState<RagflowDocument | null>(null);
  const [editing, setEditing] = useState<RagflowDocument | null>(null);
  const [newDocumentOpen, setNewDocumentOpen] = useState(false);
  const [newName, setNewName] = useState("");
  const [files, setFiles] = useState<File[]>([]);
  const [parseTarget, setParseTarget] = useState<RagflowDocument | null>(null);
  const [parseAction, setParseAction] = useState<"parse" | "cancel">("parse");
  const [pipelineMode, setPipelineMode] = useState(false);
  const [confirmRebuild, setConfirmRebuild] = useState(false);
  const [deletion, setDeletion] = useState<
    | { kind: "document"; document: RagflowDocument }
    | { kind: "dataset" }
    | { kind: "disconnect" }
    | null
  >(null);
  const [confirmName, setConfirmName] = useState("");
  const [settings, setSettings] = useState<RagflowRetrievalSettings | null>(null);
  const [question, setQuestion] = useState("");
  const [preview, setPreview] = useState(false);
  const [previewRefs, setPreviewRefs] = useState<string[]>([]);
  const [retrieving, setRetrieving] = useState(false);
  const [result, setResult] = useState<RagflowRetrievalResult | null>(null);
  const [resultPreview, setResultPreview] = useState(false);
  const [intentKeys] = useState(() => new IntentKeys(newOperationKey));
  const controllers = useRef(new Set<AbortController>());
  const epoch = useRef(0);
  const mutationPending = useRef(false);
  const changed = useRef(onChanged);
  const observedRevisions = useRef("");
  const editRequest = useRef<AbortController | null>(null);
  const access = managementAccess(kb, binding);
  const hasUncertain = operations.some(
    (operation) => operationPhase(operation.state) === "attention",
  );
  const requestSnapshot = `${page}:${keywords}:${refresh}`;
  const operationsCurrent = operationsSnapshot === requestSnapshot;
  const canSubmit =
    access.write && documentsAvailable && loaded && operationsCurrent && !busy && !hasUncertain;
  const canReconcile = access.manage && binding?.can_reconcile === true;
  const hasPending = operations.some(
    (operation) =>
      operationPhase(operation.state) === "running" ||
      operationPhase(operation.state) === "attention",
  );
  const operationRevision = operations
    .map((operation) => `${operation.operation_id}:${operation.revision}`)
    .join("|");
  const status = (value: string) =>
    t(`ragflow.state.${value}`, { defaultValue: labels[value] ?? value });
  useEffect(() => {
    changed.current = onChanged;
  }, [onChanged]);
  useEffect(() => {
    const activeControllers = controllers.current;
    return () => {
      epoch.current += 1;
      for (const controller of activeControllers) controller.abort();
      activeControllers.clear();
    };
  }, [access.manage, access.write, canReconcile]);
  const begin = useCallback(() => {
    const controller = new AbortController();
    controllers.current.add(controller);
    return {
      controller,
      stamp: epoch.current,
      finish: () => controllers.current.delete(controller),
    };
  }, []);
  const reload = () => setRefresh((value) => value + 1);

  useEffect(() => {
    if (!kbRef) return;
    const controller = new AbortController();
    ragflowApi
      .getBinding(kbRef, { signal: controller.signal })
      .then((current) => {
        if (!controller.signal.aborted) {
          setBinding(current);
          setBindingLoaded(true);
        }
      })
      .catch((failure) => {
        if (!controller.signal.aborted) {
          setBinding(null);
          setDocuments([]);
          setOperations([]);
          setLoaded(false);
          setDocumentsAvailable(false);
          setError(ragflowErrorMessage(failure, t));
          setBindingLoaded(true);
        }
      });
    return () => controller.abort();
  }, [kbRef, refresh, t]);

  useEffect(() => {
    if (!access.manage) return;
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout> | undefined;
    const poll = async () => {
      let keepPolling = false;
      try {
        const [docs, tasks] = await Promise.allSettled([
          ragflowApi.listDocuments(
            kbRef,
            { page, page_size: 20, keywords },
            { signal: controller.signal },
          ),
          ragflowApi.listOperations(kbRef, { signal: controller.signal }),
        ]);
        if (controller.signal.aborted) return;
        setLoaded(true);
        // A deleted/provisioning Dataset must not make its operation audit disappear.
        if (docs.status === "fulfilled") {
          setDocumentsAvailable(true);
          setDocuments(docs.value.documents);
          setTotal(docs.value.total);
          setSelectedDocument((current) =>
            current
              ? (docs.value.documents.find((item) => item.document_ref === current.document_ref) ??
                null)
              : null,
          );
          setParseTarget((current) =>
            current
              ? (docs.value.documents.find((item) => item.document_ref === current.document_ref) ??
                null)
              : null,
          );
          setPreviewRefs((current) =>
            current.filter((ref) => docs.value.documents.some((item) => item.document_ref === ref)),
          );
        } else {
          setDocumentsAvailable(false);
          setDocuments([]);
          setTotal(0);
          setError(ragflowErrorMessage(docs.reason, t));
        }
        if (tasks.status === "fulfilled") {
          setOperationsSnapshot(requestSnapshot);
          setOperations(tasks.value.operations);
          intentKeys.observe(tasks.value.operations);
          const next = tasks.value.operations
            .map((operation) => `${operation.operation_id}:${operation.revision}`)
            .join("|");
          if (observedRevisions.current && observedRevisions.current !== next) changed.current();
          observedRevisions.current = next;
          keepPolling = shouldPollOperations(tasks.value.operations);
        } else {
          setOperationsSnapshot("");
          setError(ragflowErrorMessage(tasks.reason, t));
        }
      } catch (failure) {
        if (!controller.signal.aborted) setError(ragflowErrorMessage(failure, t));
      } finally {
        if (!controller.signal.aborted && keepPolling) timer = setTimeout(() => void poll(), 2500);
      }
    };
    void poll();
    return () => {
      controller.abort();
      if (timer) clearTimeout(timer);
    };
  }, [kbRef, access.manage, page, keywords, refresh, intentKeys, requestSnapshot, t]);

  useEffect(() => {
    if (!access.manage) return;
    const controller = new AbortController();
    ragflowApi
      .getRetrievalSettings(kbRef, { signal: controller.signal })
      .then((value) => {
        if (!controller.signal.aborted) setSettings(value);
      })
      .catch((failure) => {
        if (!controller.signal.aborted) setError(ragflowErrorMessage(failure, t));
      });
    return () => controller.abort();
  }, [kbRef, access.manage, t]);

  const mutate: RunMutation = async (action, payload, request, remoteWrite = true) => {
    if (action === "retrieval-settings" && (hasPending || !operationsCurrent)) return false;
    if (
      mutationPending.current ||
      !access.manage ||
      (remoteWrite && !access.write) ||
      (action.startsWith("reconcile:") && !canReconcile)
    )
      return false;
    if (
      remoteWrite &&
      (!documentsAvailable || !operationsCurrent || !intentKeys.canSubmit(action, payload))
    ) {
      setError(
        t("ragflow.unreceiptedWrite", {
          defaultValue:
            "尚未核实上次写入的操作记录。请刷新后沿用原内容重试，不要创建新的写入意图。",
        }),
      );
      return false;
    }
    if (
      remoteWrite &&
      hasUncertain &&
      !action.startsWith("reconcile:") &&
      !action.startsWith("cancel:")
    ) {
      setError(
        t("ragflow.resolveUnknownFirst", {
          defaultValue: "存在结果未知的操作。请先查看操作记录并核对，避免重复写入。",
        }),
      );
      return false;
    }
    const pending = begin();
    mutationPending.current = true;
    setBusy(true);
    setError("");
    setNotice("");
    const key = intentKeys.get(action, payload);
    try {
      const value = await request({ signal: pending.controller.signal, idempotencyKey: key });
      if (pending.controller.signal.aborted || pending.stamp !== epoch.current) return false;
      const accepted = asOperations(value);
      intentKeys.accept(
        action,
        payload,
        accepted.map((operation) => operation.operation_id),
      );
      setNotice(
        accepted.length
          ? t("ragflow.operationAccepted", {
              defaultValue: "操作已受理。请在操作记录查看核对后的结果。",
            })
          : t("ragflow.saved", { defaultValue: "更改已保存。" }),
      );
      reload();
      changed.current();
      return true;
    } catch (failure) {
      if (!pending.controller.signal.aborted && pending.stamp === epoch.current) {
        if (
          remoteWrite &&
          !(
            failure instanceof RagflowApiError &&
            [400, 401, 403, 404, 409, 410, 413, 415, 422].includes(failure.status)
          )
        )
          intentKeys.markUncertain(action, payload);
        setError(
          `${ragflowErrorMessage(failure, t)} ${remoteWrite ? t("ragflow.keepRequestKey", { defaultValue: "请先刷新操作记录；再次提交相同内容会沿用本次请求标识。" }) : ""}`,
        );
        reload();
      }
      return false;
    } finally {
      pending.finish();
      if (pending.stamp === epoch.current) {
        mutationPending.current = false;
        setBusy(false);
      }
    }
  };

  const editDocument = async (document: RagflowDocument) => {
    editRequest.current?.abort();
    const pending = begin();
    editRequest.current = pending.controller;
    try {
      const detail = await ragflowApi.getDocument(kbRef, document.document_ref, {
        signal: pending.controller.signal,
      });
      if (!pending.controller.signal.aborted && pending.stamp === epoch.current) setEditing(detail);
    } catch (failure) {
      if (!pending.controller.signal.aborted) setError(ragflowErrorMessage(failure, t));
    } finally {
      pending.finish();
    }
  };
  const download = async (document: RagflowDocument) => {
    const pending = begin();
    try {
      const blob = await ragflowApi.downloadDocument(kbRef, document.document_ref, {
        signal: pending.controller.signal,
      });
      if (pending.controller.signal.aborted || pending.stamp !== epoch.current) return;
      const url = URL.createObjectURL(blob);
      const anchor = window.document.createElement("a");
      anchor.href = url;
      anchor.download = document.name;
      anchor.click();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
    } catch (failure) {
      if (!pending.controller.signal.aborted) setError(ragflowErrorMessage(failure, t));
    } finally {
      pending.finish();
    }
  };
  const testRetrieval = async () => {
    if (!question.trim() || !access.manage || (preview && !previewRefs.length)) return;
    const pending = begin();
    setRetrieving(true);
    setError("");
    setResult(null);
    try {
      const value = await ragflowApi.retrieve(
        kbRef,
        { query: question, ...(preview ? { preview_document_refs: previewRefs } : {}) },
        { signal: pending.controller.signal },
      );
      if (!pending.controller.signal.aborted && pending.stamp === epoch.current) {
        setResult(value);
        setResultPreview(preview);
      }
    } catch (failure) {
      if (!pending.controller.signal.aborted) setError(ragflowErrorMessage(failure, t));
    } finally {
      pending.finish();
      if (pending.stamp === epoch.current) setRetrieving(false);
    }
  };
  const submitDelete = async () => {
    if (!deletion || busy || !access.manage) return;
    if (deletion.kind === "disconnect") {
      const pending = begin();
      setBusy(true);
      try {
        await onDisconnect(kbRef);
        if (pending.stamp === epoch.current) setDeletion(null);
      } catch (failure) {
        if (pending.stamp === epoch.current) setError(ragflowErrorMessage(failure, t));
      } finally {
        pending.finish();
        if (pending.stamp === epoch.current) setBusy(false);
      }
      return;
    }
    const target = deletion;
    const ok =
      target.kind === "dataset"
        ? await mutate("delete-dataset", { confirm_name: confirmName }, (options) =>
            ragflowApi.deleteDataset(kbRef, { confirm_name: confirmName }, options),
          )
        : await mutate(`delete-document:${target.document.document_ref}`, {}, (options) =>
            ragflowApi.deleteDocument(kbRef, target.document.document_ref, options),
          );
    if (ok) setDeletion(null);
  };

  const tabs = [
    {
      id: "documents" as const,
      icon: FileText,
      label: t("ragflow.documents", { defaultValue: "文档与片段" }),
    },
    {
      id: "retrieval" as const,
      icon: Search,
      label: t("ragflow.retrievalTest", { defaultValue: "检索测试" }),
    },
    {
      id: "operations" as const,
      icon: History,
      label: t("ragflow.operations", { defaultValue: "操作记录" }),
    },
    { id: "settings" as const, icon: Settings, label: t("Settings") },
  ];
  return (
    <main className="flex h-full min-w-0 flex-1 flex-col overflow-hidden bg-[var(--background)] text-[var(--foreground)]">
      <header className="border-b border-[var(--border)] bg-[var(--card)] px-6 py-4">
        <button
          onClick={onBack}
          className="mb-2 inline-flex items-center gap-1 text-xs text-[var(--muted-foreground)] hover:text-[var(--foreground)]"
        >
          <ArrowLeft size={14} />
          {t("Knowledge bases")}
        </button>
        <div className="flex items-start justify-between gap-3">
          <div className="min-w-0">
            <h1 className="flex items-center gap-2 truncate font-serif text-lg font-semibold">
              <Database size={18} />
              {kb.name}
              <span className="rounded-md bg-[var(--muted)] px-2 py-0.5 font-sans text-[10px] font-medium">
                {t("ragflow.provider", { defaultValue: "RAGFlow" })}
              </span>
            </h1>
            <p className="mt-1 text-xs text-[var(--muted-foreground)]">
              {binding?.ownership === "managed"
                ? t("ragflow.managed", { defaultValue: "平台管理的知识库" })
                : t("ragflow.attached", { defaultValue: "已连接的知识库" })}{" "}
              ·{" "}
              {access.write
                ? t("ragflow.remoteEditable", { defaultValue: "可管理远端内容" })
                : t("ragflow.remoteReadOnly", { defaultValue: "远端内容只读" })}
            </p>
          </div>
          <button className={buttonClass} onClick={reload} aria-label={t("Refresh")}>
            <RefreshCw size={14} />
            {t("Refresh")}
          </button>
        </div>
        {access.manage && (
          <nav
            className="-mb-4 mt-4 flex gap-1 overflow-x-auto"
            aria-label={t("ragflow.managementSections", { defaultValue: "知识库管理栏目" })}
          >
            {tabs.map(({ id, icon: Icon, label }) => (
              <button
                key={id}
                onClick={() => setSection(id)}
                className={`inline-flex shrink-0 items-center gap-1.5 border-b-2 px-3 py-2.5 text-xs font-medium ${section === id ? "border-[var(--primary)] text-[var(--foreground)]" : "border-transparent text-[var(--muted-foreground)] hover:text-[var(--foreground)]"}`}
                aria-current={section === id ? "page" : undefined}
              >
                <Icon size={14} />
                {label}
                {id === "operations" &&
                  operations.some(
                    (operation) =>
                      operationPhase(operation.state) !== "success" &&
                      operationPhase(operation.state) !== "failed",
                  ) && <span className="h-1.5 w-1.5 rounded-full bg-amber-500" />}
              </button>
            ))}
          </nav>
        )}
      </header>
      <div className="min-h-0 flex-1 overflow-y-auto p-6">
        {!kbRef && (
          <p role="alert" className="text-sm text-red-600">
            {t("ragflow.missingIdentity", {
              defaultValue: "知识库缺少稳定身份。请返回列表并刷新。",
            })}
          </p>
        )}
        {error && (
          <div
            role="alert"
            className="mb-4 flex items-start gap-2 rounded-xl border border-red-500/20 bg-red-500/5 p-3 text-sm text-red-700 dark:text-red-300"
          >
            <AlertTriangle size={16} className="mt-0.5 shrink-0" />
            <span>{error}</span>
          </div>
        )}
        {notice && (
          <div
            role="status"
            className="mb-4 flex items-start gap-2 rounded-xl border border-emerald-500/20 bg-emerald-500/5 p-3 text-sm text-emerald-700 dark:text-emerald-300"
          >
            <CheckCircle2 size={16} className="mt-0.5 shrink-0" />
            {notice}
          </div>
        )}
        {kbRef && !bindingLoaded && (
          <p
            role="status"
            className="flex items-center gap-2 text-sm text-[var(--muted-foreground)]"
          >
            <Loader2 size={16} className="animate-spin" />
            {t("Loading...")}
          </p>
        )}
        {bindingLoaded && !access.manage && (
          <div className="mx-auto max-w-lg rounded-2xl border border-[var(--border)] bg-[var(--card)] p-6">
            <ShieldCheck className="mb-3 text-[var(--muted-foreground)]" size={24} />
            <h2 className="font-medium">
              {t("ragflow.assignedReadOnly", { defaultValue: "这是只读知识库" })}
            </h2>
            <p className="mt-2 text-sm leading-6 text-[var(--muted-foreground)]">
              {t("ragflow.assignedDescription", {
                defaultValue:
                  "你可以在对话中使用获授权的已发布知识。文档、连接密钥和管理操作由知识库管理员维护。",
              })}
            </p>
          </div>
        )}
        {access.manage && (
          <>
            {hasUncertain && (
              <div className="mb-4 rounded-xl border border-amber-500/25 bg-amber-500/5 p-3 text-xs leading-5 text-amber-800 dark:text-amber-200">
                {t("ragflow.uncertainBanner", {
                  defaultValue:
                    "有操作需要核对。远端写入暂时停用；你仍可查看内容、下架知识，或在操作记录中授权只读对账。",
                })}
                <button className="ml-2 underline" onClick={() => setSection("operations")}>
                  {t("ragflow.viewOperations", { defaultValue: "查看操作记录" })}
                </button>
              </div>
            )}
            {section === "documents" &&
              (selectedDocument ? (
                <ChunkPanel
                  key={selectedDocument.document_ref}
                  kbRef={kbRef}
                  document={selectedDocument}
                  canWrite={canSubmit}
                  busy={busy}
                  revision={operationRevision}
                  onBack={() => setSelectedDocument(null)}
                  mutate={mutate}
                />
              ) : (
                <div className="space-y-4">
                  <div className="flex flex-wrap items-center justify-between gap-3">
                    <div>
                      <h2 className="text-base font-semibold">
                        {t("ragflow.documents", { defaultValue: "文档与片段" })}
                      </h2>
                      <p className="mt-1 text-xs text-[var(--muted-foreground)]">
                        {t("ragflow.documentTotal", {
                          defaultValue: "共 {{count}} 份文档。上传和修改后的内容需要检查并发布。",
                          count: total,
                        })}
                      </p>
                    </div>
                    <button
                      className={buttonClass}
                      disabled={!canSubmit}
                      onClick={() => {
                        setNewName("");
                        setNewDocumentOpen(true);
                      }}
                    >
                      <Plus size={14} />
                      {t("ragflow.newEmptyDocument", { defaultValue: "新建空文档" })}
                    </button>
                  </div>
                  {access.write && (
                    <form
                      className="rounded-xl border border-dashed border-[var(--border)] bg-[var(--card)] p-4"
                      onSubmit={(event) => {
                        event.preventDefault();
                        const selected = files;
                        const payload = selected.map((file) => ({
                          name: file.name,
                          size: file.size,
                          lastModified: file.lastModified,
                        }));
                        void mutate("upload", payload, (options) =>
                          ragflowApi.upload(kbRef, selected, options),
                        ).then((ok) => {
                          if (ok) setFiles([]);
                        });
                      }}
                    >
                      <div className="flex flex-wrap items-center gap-3">
                        <Upload size={18} className="text-[var(--muted-foreground)]" />
                        <label className="min-w-0 flex-1">
                          <span className="mb-1 block text-xs font-medium">
                            {t("ragflow.uploadDocuments", { defaultValue: "上传资料" })}
                          </span>
                          <input
                            type="file"
                            multiple
                            disabled={!canSubmit}
                            accept=".pdf,.txt,.md,.doc,.docx,.rtf,.html,.htm,.xml,.json,.csv,.xlsx,.xls,.ppt,.pptx,.epub"
                            className="block w-full text-xs text-[var(--muted-foreground)] file:mr-3 file:rounded-md file:border-0 file:bg-[var(--muted)] file:px-2 file:py-1 file:text-[var(--foreground)]"
                            onChange={(event) => setFiles(Array.from(event.target.files ?? []))}
                          />
                        </label>
                        <button
                          type="submit"
                          className={primaryClass}
                          disabled={
                            !canSubmit ||
                            !files.length ||
                            files.length > 20 ||
                            files.reduce((sum, file) => sum + file.size, 0) > 100 * 1024 * 1024
                          }
                        >
                          {busy && <Loader2 size={14} className="animate-spin" />}
                          {t("Upload")}
                        </button>
                      </div>
                      <p className="mt-2 text-xs text-[var(--muted-foreground)]">
                        {t("ragflow.uploadLimit", {
                          defaultValue: "每批最多 20 个文件、合计 100 MiB。网页导入尚未开放。",
                        })}
                        {files.length > 0 && ` · ${files.length}`}
                      </p>
                    </form>
                  )}
                  <form
                    className="flex gap-2"
                    onSubmit={(event) => {
                      event.preventDefault();
                      setPage(1);
                      setKeywords(query);
                    }}
                  >
                    <label className="relative min-w-0 flex-1">
                      <span className="sr-only">
                        {t("ragflow.searchDocuments", { defaultValue: "搜索文档" })}
                      </span>
                      <Search
                        size={15}
                        className="absolute left-3 top-2.5 text-[var(--muted-foreground)]"
                      />
                      <input
                        value={query}
                        onChange={(event) => setQuery(event.target.value)}
                        className={`${fieldClass} !pl-9`}
                        placeholder={t("ragflow.searchDocuments", { defaultValue: "搜索文档" })}
                      />
                    </label>
                    <button type="submit" className={buttonClass}>
                      {t("Search")}
                    </button>
                  </form>
                  {!loaded ? (
                    <p className="flex items-center gap-2 p-6 text-sm text-[var(--muted-foreground)]">
                      <Loader2 size={16} className="animate-spin" />
                      {t("Loading...")}
                    </p>
                  ) : !documents.length ? (
                    <div className="rounded-xl border border-dashed border-[var(--border)] p-10 text-center">
                      <FileText size={28} className="mx-auto mb-3 text-[var(--muted-foreground)]" />
                      <p className="text-sm text-[var(--muted-foreground)]">
                        {t("ragflow.noDocuments", {
                          defaultValue: "暂无文档。上传资料或新建空文档开始整理知识。",
                        })}
                      </p>
                    </div>
                  ) : (
                    <div className="overflow-x-auto rounded-xl border border-[var(--border)]">
                      <table className="w-full min-w-[760px] text-left text-xs">
                        <thead className="border-b border-[var(--border)] bg-[var(--muted)]/50 text-[var(--muted-foreground)]">
                          <tr>
                            <th className="px-4 py-3 font-medium">{t("Name")}</th>
                            <th className="px-3 py-3 font-medium">{t("Status")}</th>
                            <th className="px-3 py-3 font-medium">
                              {t("ragflow.visibility", { defaultValue: "发布状态" })}
                            </th>
                            <th className="px-3 py-3 font-medium">
                              {t("ragflow.chunks", { defaultValue: "片段" })}
                            </th>
                            <th className="px-4 py-3 font-medium">{t("Actions")}</th>
                          </tr>
                        </thead>
                        <tbody>
                          {documents.map((document) => (
                            <tr
                              key={document.document_ref}
                              className="border-b border-[var(--border)] last:border-0"
                            >
                              <td className="max-w-[220px] px-4 py-3">
                                <button
                                  className="block max-w-full truncate text-left font-medium hover:underline"
                                  onClick={() => setSelectedDocument(document)}
                                >
                                  {document.name}
                                </button>
                              </td>
                              <td className="px-3 py-3">
                                <span>{status(document.state)}</span>
                                {document.state === "parsing" && (
                                  <progress
                                    max={1}
                                    value={document.progress ?? 0}
                                    className="mt-1 block h-1.5 w-20"
                                  />
                                )}
                              </td>
                              <td className="px-3 py-3">
                                <select
                                  aria-label={t("ragflow.visibilityFor", {
                                    defaultValue: "{{name}} 的发布状态",
                                    name: document.name,
                                  })}
                                  value={document.visibility}
                                  disabled={busy || document.visibility === "deleted"}
                                  className="max-w-28 rounded-md border border-[var(--border)] bg-[var(--background)] p-1.5"
                                  onChange={(event) => {
                                    const visibility = event.target.value as RagflowVisibility;
                                    const payload = {
                                      visibility,
                                      expected_revision: document.revision,
                                    };
                                    void mutate(
                                      `visibility:${document.document_ref}`,
                                      payload,
                                      (options) =>
                                        ragflowApi.setVisibility(
                                          kbRef,
                                          document.document_ref,
                                          payload,
                                          options,
                                        ),
                                      false,
                                    );
                                  }}
                                >
                                  {["draft", "active", "disabled", "archived"].map((value) => (
                                    <option
                                      key={value}
                                      value={value}
                                      disabled={
                                        value === "active" &&
                                        (document.state !== "ready" ||
                                          document.chunk_count < 1 ||
                                          hasUncertain)
                                      }
                                    >
                                      {status(value)}
                                    </option>
                                  ))}
                                  {document.visibility === "deleted" && (
                                    <option value="deleted">{status("deleted")}</option>
                                  )}
                                </select>
                              </td>
                              <td className="px-3 py-3">
                                <button
                                  className="inline-flex items-center gap-1 hover:underline"
                                  onClick={() => setSelectedDocument(document)}
                                >
                                  <Layers size={13} />
                                  {document.chunk_count}
                                </button>
                              </td>
                              <td className="px-4 py-3">
                                <div className="flex flex-wrap gap-2">
                                  <button
                                    className="text-[var(--muted-foreground)] hover:text-[var(--foreground)]"
                                    onClick={() => void download(document)}
                                    aria-label={t("ragflow.downloadDocument", {
                                      defaultValue: "下载文档",
                                    })}
                                  >
                                    <Download size={14} />
                                  </button>
                                  <button
                                    disabled={!canSubmit}
                                    className="disabled:opacity-35"
                                    onClick={() => void editDocument(document)}
                                  >
                                    {t("Edit")}
                                  </button>
                                  <button
                                    disabled={!canSubmit}
                                    className="disabled:opacity-35"
                                    onClick={() => {
                                      setParseAction("parse");
                                      setParseTarget(document);
                                      setConfirmRebuild(false);
                                      setPipelineMode(false);
                                    }}
                                  >
                                    {document.state === "unstart"
                                      ? t("ragflow.parse", { defaultValue: "解析" })
                                      : t("ragflow.reparse", { defaultValue: "重新解析" })}
                                  </button>
                                  {document.state === "parsing" && (
                                    <button
                                      disabled={!access.write || busy}
                                      className="disabled:opacity-35"
                                      onClick={() => {
                                        setParseAction("cancel");
                                        setParseTarget(document);
                                        setPipelineMode(false);
                                      }}
                                    >
                                      {t("Cancel")}
                                    </button>
                                  )}
                                  <button
                                    disabled={!canSubmit}
                                    className="text-red-600 disabled:opacity-35"
                                    onClick={() => setDeletion({ kind: "document", document })}
                                  >
                                    {t("Delete")}
                                  </button>
                                </div>
                              </td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    </div>
                  )}
                  <div className="flex items-center justify-end gap-3 text-xs">
                    <button
                      className={buttonClass}
                      disabled={page === 1}
                      onClick={() => setPage((value) => value - 1)}
                      aria-label={t("ragflow.connection.previousPage", { defaultValue: "上一页" })}
                    >
                      <ChevronLeft size={14} />
                    </button>
                    <span>
                      {page} / {Math.max(1, Math.ceil(total / 20))}
                    </span>
                    <button
                      className={buttonClass}
                      disabled={page * 20 >= total}
                      onClick={() => setPage((value) => value + 1)}
                      aria-label={t("ragflow.connection.nextPage", { defaultValue: "下一页" })}
                    >
                      <ChevronRight size={14} />
                    </button>
                  </div>
                </div>
              ))}
            {section === "retrieval" && (
              <div className="mx-auto max-w-3xl space-y-5">
                <div>
                  <h2 className="text-base font-semibold">
                    {t("ragflow.retrievalTest", { defaultValue: "检索测试" })}
                  </h2>
                  <p className="mt-1 text-xs leading-5 text-[var(--muted-foreground)]">
                    {t("ragflow.retrievalDescription", {
                      defaultValue:
                        "查看真实命中的知识片段与来源。默认仅查询已发布文档，不额外生成聊天回答。",
                    })}
                  </p>
                </div>
                <form
                  className="space-y-4"
                  onSubmit={(event) => {
                    event.preventDefault();
                    void testRetrieval();
                  }}
                >
                  <label className="block space-y-1 text-xs">
                    {t("ragflow.testQuestion", { defaultValue: "测试问题" })}
                    <textarea
                      rows={4}
                      value={question}
                      disabled={retrieving}
                      onChange={(event) => {
                        setQuestion(event.target.value);
                        setResult(null);
                      }}
                      className={fieldClass}
                      maxLength={20000}
                    />
                  </label>
                  <label className="flex items-center gap-2 text-sm">
                    <input
                      type="checkbox"
                      checked={preview}
                      disabled={retrieving}
                      onChange={(event) => {
                        setPreview(event.target.checked);
                        setResult(null);
                      }}
                    />
                    {t("ragflow.draftPreview", {
                      defaultValue: "预览选定文档（可包含未发布内容）",
                    })}
                  </label>
                  {preview && (
                    <div className="max-h-48 overflow-auto rounded-lg border border-[var(--border)] p-3">
                      <p className="mb-2 text-xs text-[var(--muted-foreground)]">
                        {t("ragflow.previewSelection", {
                          defaultValue: "勾选当前文档页中的资料。预览结果不会自动发布知识。",
                        })}
                      </p>
                      {documents.map((document) => (
                        <label
                          key={document.document_ref}
                          className="mb-1 flex items-center gap-2 text-xs"
                        >
                          <input
                            type="checkbox"
                            checked={previewRefs.includes(document.document_ref)}
                            disabled={retrieving}
                            onChange={(event) => {
                              setResult(null);
                              setPreviewRefs((current) =>
                                event.target.checked
                                  ? [...current, document.document_ref]
                                  : current.filter((ref) => ref !== document.document_ref),
                              );
                            }}
                          />
                          {document.name}
                          <span className="text-[var(--muted-foreground)]">
                            {status(document.visibility)}
                          </span>
                        </label>
                      ))}
                    </div>
                  )}
                  <button
                    type="submit"
                    className={primaryClass}
                    disabled={retrieving || !question.trim() || (preview && !previewRefs.length)}
                  >
                    {retrieving ? (
                      <Loader2 size={14} className="animate-spin" />
                    ) : (
                      <Search size={14} />
                    )}
                    {t("ragflow.runRetrieval", { defaultValue: "开始检索" })}
                  </button>
                </form>
                {result && (
                  <div className="space-y-3">
                    <p className="text-xs text-[var(--muted-foreground)]">
                      {t("ragflow.resultSnapshot", {
                        defaultValue: "本次查询：{{query}} · {{mode}}",
                        query: result.query,
                        mode: resultPreview
                          ? t("ragflow.previewMode", { defaultValue: "指定文档预览" })
                          : t("ragflow.publishedMode", { defaultValue: "仅已发布知识" }),
                      })}
                    </p>
                    {!result.sources.length && (
                      <p
                        role="status"
                        className="rounded-xl border border-dashed border-[var(--border)] p-6 text-sm text-[var(--muted-foreground)]"
                      >
                        {t("ragflow.noRetrievalResults", {
                          defaultValue: "没有命中资料。请检查文档发布状态、解析结果或调整问题。",
                        })}
                      </p>
                    )}
                    {result.sources.map((source, index) => (
                      <article
                        key={`${source.chunk_id}:${index}`}
                        className="rounded-xl border border-[var(--border)] bg-[var(--card)] p-4"
                      >
                        <div className="mb-2 flex flex-wrap items-center justify-between gap-2 text-xs">
                          <strong>{source.title}</strong>
                          <span className="text-[var(--muted-foreground)]">
                            {source.page
                              ? t("ragflow.sourcePage", {
                                  defaultValue: "第 {{page}} 页",
                                  page: source.page,
                                })
                              : ""}{" "}
                            {typeof source.score === "number" ? source.score.toFixed(3) : ""}
                          </span>
                        </div>
                        <p className="whitespace-pre-wrap text-sm leading-6">{source.content}</p>
                      </article>
                    ))}
                  </div>
                )}
              </div>
            )}
            {section === "operations" && (
              <div className="space-y-4">
                <div>
                  <h2 className="text-base font-semibold">
                    {t("ragflow.operations", { defaultValue: "操作记录" })}
                  </h2>
                  <p className="mt-1 text-xs leading-5 text-[var(--muted-foreground)]">
                    {t("ragflow.operationsDescription", {
                      defaultValue:
                        "已受理或远端已接收不等于完成。进行中的操作会自动刷新；结果未知时不会自动重复写入。",
                    })}
                  </p>
                </div>
                {!operations.length ? (
                  <p className="rounded-xl border border-dashed border-[var(--border)] p-8 text-center text-sm text-[var(--muted-foreground)]">
                    {t("ragflow.noOperations", { defaultValue: "暂无操作记录" })}
                  </p>
                ) : (
                  <div className="space-y-3">
                    {operations.map((operation) => {
                      const phase = operationPhase(operation.state);
                      return (
                        <article
                          key={operation.operation_id}
                          className="rounded-xl border border-[var(--border)] bg-[var(--card)] p-4"
                        >
                          <div className="flex flex-wrap items-start justify-between gap-3">
                            <div>
                              <h3 className="text-sm font-medium">{status(operation.action)}</h3>
                              <p className="mt-1 break-all font-mono text-[10px] text-[var(--muted-foreground)]">
                                {operation.operation_id}
                              </p>
                            </div>
                            <span
                              className={`inline-flex items-center gap-1.5 rounded-full px-2 py-1 text-[11px] ${phase === "success" ? "bg-emerald-500/10 text-emerald-700 dark:text-emerald-300" : phase === "running" ? "bg-blue-500/10 text-blue-700 dark:text-blue-300" : "bg-amber-500/10 text-amber-800 dark:text-amber-200"}`}
                            >
                              {phase === "running" && (
                                <Loader2 size={12} className="animate-spin" />
                              )}
                              {status(operation.state)}
                            </span>
                          </div>
                          {operation.error_type && (
                            <p className="mt-2 text-xs text-[var(--muted-foreground)]">
                              {ragflowErrorMessage(operation.error_type, t)}{" "}
                              <code>{operation.error_type}</code>
                            </p>
                          )}
                          {operation.result?.document_status && (
                            <p className="mt-2 text-xs">
                              {status(operation.result.document_status)}
                            </p>
                          )}
                          {typeof operation.result?.progress === "number" &&
                            phase === "running" && (
                              <progress
                                max={1}
                                value={operation.result.progress}
                                className="mt-3 h-1.5 w-full"
                              />
                            )}
                          {(operation.state === "NEEDS_ATTENTION" ||
                            operation.state === "RESULT_UNKNOWN") && (
                            <div className="mt-3 border-t border-[var(--border)] pt-3">
                              <p className="mb-2 text-xs leading-5 text-[var(--muted-foreground)]">
                                {t("ragflow.readOnlyReconciliation", {
                                  defaultValue:
                                    "确认远端已处理或凭据已恢复后，可授权再次读取远端状态。此动作不会重新发送上传、删除或编辑请求；活动租约未结束时请稍后再试。",
                                })}
                              </p>
                              <button
                                className={buttonClass}
                                disabled={!canReconcile || busy}
                                onClick={() =>
                                  void mutate(
                                    `reconcile:${operation.operation_id}`,
                                    { expected_revision: operation.revision },
                                    (options) =>
                                      ragflowApi.reconcile(
                                        kbRef,
                                        operation.operation_id,
                                        { expected_revision: operation.revision },
                                        options,
                                      ),
                                    false,
                                  )
                                }
                              >
                                <RefreshCw size={13} />
                                {t("ragflow.authorizeReadback", { defaultValue: "授权只读对账" })}
                              </button>
                            </div>
                          )}
                          <RagflowOperationEvents
                            kbRef={kbRef}
                            operationId={operation.operation_id}
                          />
                        </article>
                      );
                    })}
                  </div>
                )}
              </div>
            )}
            {section === "settings" && (
              <div className="mx-auto max-w-3xl space-y-6">
                <section className="rounded-xl border border-[var(--border)] bg-[var(--card)] p-5">
                  <h2 className="text-sm font-semibold">
                    {t("ragflow.retrievalSettings", { defaultValue: "检索参数" })}
                  </h2>
                  {settings ? (
                    <form
                      className="mt-4 space-y-4"
                      onSubmit={(event) => {
                        event.preventDefault();
                        void mutate(
                          "retrieval-settings",
                          settings,
                          (options) => ragflowApi.saveRetrievalSettings(kbRef, settings, options),
                          false,
                        );
                      }}
                    >
                      <div className="grid gap-4 sm:grid-cols-3">
                        <label className="block space-y-1 text-xs">
                          {t("ragflow.resultCount", { defaultValue: "返回片段数量" })}
                          <input
                            type="number"
                            min={1}
                            max={20}
                            required
                            className={fieldClass}
                            value={settings.page_size}
                            onChange={(event) =>
                              setSettings({ ...settings, page_size: Number(event.target.value) })
                            }
                          />
                        </label>
                        <label className="block space-y-1 text-xs">
                          {t("ragflow.similarityThreshold", { defaultValue: "相似度阈值" })}
                          <input
                            type="number"
                            min={0}
                            max={1}
                            step={0.01}
                            required
                            className={fieldClass}
                            value={settings.similarity_threshold}
                            onChange={(event) =>
                              setSettings({
                                ...settings,
                                similarity_threshold: Number(event.target.value),
                              })
                            }
                          />
                        </label>
                        <label className="block space-y-1 text-xs">
                          {t("ragflow.vectorWeight", { defaultValue: "向量相似度权重" })}
                          <input
                            type="number"
                            min={0}
                            max={1}
                            step={0.01}
                            required
                            className={fieldClass}
                            value={settings.vector_similarity_weight}
                            onChange={(event) =>
                              setSettings({
                                ...settings,
                                vector_similarity_weight: Number(event.target.value),
                              })
                            }
                          />
                        </label>
                      </div>
                      <label className="block space-y-1 text-xs">
                        {t("ragflow.reranker", { defaultValue: "重排模型标识（可选）" })}
                        <input
                          className={fieldClass}
                          value={settings.rerank_id ?? ""}
                          maxLength={256}
                          onChange={(event) =>
                            setSettings({ ...settings, rerank_id: event.target.value || null })
                          }
                        />
                      </label>
                      <button
                        className={primaryClass}
                        disabled={busy || !loaded || hasPending}
                        type="submit"
                      >
                        {t("Save")}
                      </button>
                    </form>
                  ) : (
                    <p className="mt-3 text-xs text-[var(--muted-foreground)]">{t("Loading...")}</p>
                  )}
                </section>
                <section className="rounded-xl border border-[var(--border)] bg-[var(--card)] p-5">
                  <h2 className="text-sm font-semibold">
                    {t("ragflow.connectionPermissions", { defaultValue: "连接与权限" })}
                  </h2>
                  <p className="mt-2 text-sm leading-6 text-[var(--muted-foreground)]">
                    {access.write
                      ? t("ragflow.managementDescription", {
                          defaultValue:
                            "可以上传、解析和编辑远端文档。文档发布与停用由本平台管理。",
                        })
                      : t("ragflow.readOnlyRemoteDescription", {
                          defaultValue:
                            "可以查看和测试资料、管理本平台的发布状态；远端文档保持只读。",
                        })}
                  </p>
                </section>
                <section className="rounded-xl border border-red-500/20 p-5">
                  <h2 className="text-sm font-semibold text-red-700 dark:text-red-300">
                    {t("ragflow.disconnectAndDelete", { defaultValue: "解除连接与删除" })}
                  </h2>
                  <p className="mt-2 text-xs leading-5 text-[var(--muted-foreground)]">
                    {t("ragflow.disconnectDescription", {
                      defaultValue:
                        "解除连接仅移除本地绑定，远端资料继续保留。删除远端知识库会移除 RAGFlow 中的 Dataset，完成后仍需解除本地连接。",
                    })}
                  </p>
                  <div className="mt-4 flex flex-wrap gap-3">
                    <button
                      className={buttonClass}
                      disabled={busy}
                      onClick={() => {
                        setConfirmName("");
                        setDeletion({ kind: "disconnect" });
                      }}
                    >
                      {t("ragflow.disconnect", { defaultValue: "解除本地连接" })}
                    </button>
                    {binding?.ownership === "managed" && access.write && (
                      <button
                        className={`${buttonClass} text-red-600`}
                        disabled={
                          !canSubmit ||
                          operations.some(
                            (operation) => operationPhase(operation.state) === "running",
                          )
                        }
                        onClick={() => {
                          setConfirmName("");
                          setDeletion({ kind: "dataset" });
                        }}
                      >
                        {t("ragflow.deleteRemoteDataset", { defaultValue: "删除远端知识库" })}
                      </button>
                    )}
                  </div>
                </section>
              </div>
            )}
          </>
        )}
      </div>
      {access.manage && editing && (
        <DocumentEditor
          key={editing.document_ref}
          document={editing}
          busy={busy}
          onClose={() => setEditing(null)}
          onSave={(patch) =>
            mutate(`edit-document:${editing.document_ref}`, patch, (options) =>
              ragflowApi.updateDocument(kbRef, editing.document_ref, patch, options),
            )
          }
        />
      )}
      <Modal
        isOpen={access.manage && newDocumentOpen}
        onClose={() => setNewDocumentOpen(false)}
        title={t("ragflow.newEmptyDocument", { defaultValue: "新建空文档" })}
        closeOnBackdrop={!busy}
        closeOnEscape={!busy}
        showCloseButton={!busy}
        footer={
          <div className="flex justify-end gap-2">
            <button
              className={buttonClass}
              disabled={busy}
              onClick={() => setNewDocumentOpen(false)}
            >
              {t("Cancel")}
            </button>
            <button
              className={primaryClass}
              disabled={!canSubmit || !newName.trim()}
              onClick={() => {
                const payload = { name: newName.trim(), type: "empty" as const };
                void mutate("create-document", payload, (options) =>
                  ragflowApi.createDocument(kbRef, payload, options),
                ).then((ok) => {
                  if (ok) setNewDocumentOpen(false);
                });
              }}
            >
              {t("Create")}
            </button>
          </div>
        }
      >
        <label className="block space-y-2 p-5 text-sm">
          {t("ragflow.documentName", { defaultValue: "文档名称" })}
          <input
            className={fieldClass}
            value={newName}
            maxLength={180}
            onChange={(event) => setNewName(event.target.value)}
            placeholder={t("ragflow.emptyDocumentExample", { defaultValue: "例如：课程笔记.txt" })}
          />
        </label>
      </Modal>
      <Modal
        isOpen={access.manage && Boolean(parseTarget)}
        onClose={() => setParseTarget(null)}
        title={
          parseAction === "cancel"
            ? t("ragflow.cancelParsing", { defaultValue: "取消解析" })
            : t("ragflow.parseDocument", { defaultValue: "解析文档" })
        }
        closeOnBackdrop={!busy}
        closeOnEscape={!busy}
        showCloseButton={!busy}
        footer={
          <div className="flex justify-end gap-2">
            <button className={buttonClass} disabled={busy} onClick={() => setParseTarget(null)}>
              {t("Cancel")}
            </button>
            <button
              className={primaryClass}
              disabled={
                parseAction === "cancel"
                  ? !access.write || busy
                  : !canSubmit || (Boolean(parseTarget?.chunk_count) && !confirmRebuild)
              }
              onClick={() => {
                const document = parseTarget;
                if (!document) return;
                const payload =
                  parseAction === "cancel"
                    ? { use_ingestion_pipeline: pipelineMode }
                    : {
                        use_ingestion_pipeline: pipelineMode,
                        rebuild: document.chunk_count > 0,
                        confirm_rebuild: confirmRebuild,
                      };
                void mutate(`${parseAction}:${document.document_ref}`, payload, (options) =>
                  ragflowApi[parseAction](kbRef, document.document_ref, payload, options),
                ).then((ok) => {
                  if (ok) setParseTarget(null);
                });
              }}
            >
              {parseAction === "cancel"
                ? t("ragflow.confirmCancelParsing", { defaultValue: "确认取消解析" })
                : t("ragflow.startParsing", { defaultValue: "开始解析" })}
            </button>
          </div>
        }
      >
        <div className="space-y-4 p-5 text-sm">
          <p className="break-words font-medium">{parseTarget?.name}</p>
          <p className="text-xs leading-5 text-[var(--muted-foreground)]">
            {parseAction === "cancel"
              ? t("ragflow.cancelDescription", {
                  defaultValue:
                    "请选择与启动解析相同的方式。取消被受理后仍需核对远端终态，不代表已经停止。",
                })
              : t("ragflow.parseDescription", {
                  defaultValue:
                    "解析开始前会下架文档。完成后请检查片段并重新发布；重新解析可能覆盖人工编辑内容。",
                })}
          </p>
          <label className="flex items-start gap-2 text-xs">
            <input
              type="checkbox"
              checked={pipelineMode}
              onChange={(event) => setPipelineMode(event.target.checked)}
            />
            {t("ragflow.useExistingPipeline", {
              defaultValue: "使用文档已配置的 Ingestion Pipeline",
            })}
          </label>
          {parseAction === "parse" && Boolean(parseTarget?.chunk_count) && (
            <label className="flex items-start gap-2 text-xs">
              <input
                type="checkbox"
                checked={confirmRebuild}
                onChange={(event) => setConfirmRebuild(event.target.checked)}
              />
              {t("ragflow.confirmRebuild", {
                defaultValue: "确认重新生成片段，并接受现有人工编辑可能被覆盖。",
              })}
            </label>
          )}
        </div>
      </Modal>
      <Modal
        isOpen={access.manage && Boolean(deletion)}
        onClose={() => setDeletion(null)}
        title={
          deletion?.kind === "disconnect"
            ? t("ragflow.disconnect", { defaultValue: "解除本地连接" })
            : deletion?.kind === "dataset"
              ? t("ragflow.deleteRemoteDataset", { defaultValue: "删除远端知识库" })
              : t("ragflow.deleteDocument", { defaultValue: "删除文档" })
        }
        closeOnBackdrop={!busy}
        closeOnEscape={!busy}
        showCloseButton={!busy}
        footer={
          <div className="flex justify-end gap-2">
            <button className={buttonClass} disabled={busy} onClick={() => setDeletion(null)}>
              {t("Cancel")}
            </button>
            <button
              className={`${primaryClass} !bg-red-600 !text-white`}
              disabled={busy || (deletion?.kind === "dataset" && confirmName !== kb.name)}
              onClick={() => void submitDelete()}
            >
              {deletion?.kind === "disconnect"
                ? t("ragflow.disconnect", { defaultValue: "解除本地连接" })
                : t("Delete")}
            </button>
          </div>
        }
      >
        <div className="space-y-4 p-5 text-sm">
          <p className="text-[var(--muted-foreground)]">
            {deletion?.kind === "disconnect"
              ? t("ragflow.confirmDisconnect", {
                  defaultValue:
                    "将解除 yFeiSTAI 中的连接，RAGFlow 中的资料会保留。在途操作记录继续保存在服务端。",
                })
              : deletion?.kind === "dataset"
                ? t("ragflow.confirmRemoteDelete", {
                    defaultValue:
                      "将删除整个远端知识库及其内容。此操作无法通过平台直接恢复，请确认已有备份。",
                  })
                : t("ragflow.confirmDocumentDelete", {
                    defaultValue: "将删除远端文档及其片段。该文档会先从本平台的检索范围移除。",
                  })}
          </p>
          {deletion?.kind === "document" && (
            <p className="break-words font-medium">{deletion.document.name}</p>
          )}
          {deletion?.kind === "dataset" && (
            <label className="block space-y-2 text-xs">
              {t("ragflow.typeNameToConfirm", {
                defaultValue: "输入 {{name}} 确认",
                name: kb.name,
              })}
              <input
                className={fieldClass}
                value={confirmName}
                onChange={(event) => setConfirmName(event.target.value)}
                autoComplete="off"
              />
            </label>
          )}
        </div>
      </Modal>
    </main>
  );
}

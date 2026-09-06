"use client";

import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { ArrowLeft, ChevronLeft, ChevronRight, Loader2, Plus, RefreshCw } from "lucide-react";
import Modal from "@/components/common/Modal";
import {
  ragflowApi,
  ragflowErrorMessage,
  type RagflowChunk,
  type RagflowDocument,
  type RagflowRequestOptions,
} from "@/lib/ragflow-api";
import { parseObjectJson } from "./management-state";

export const fieldClass =
  "w-full rounded-lg border border-[var(--border)] bg-[var(--background)] px-3 py-2 text-sm text-[var(--foreground)] outline-none focus:border-[var(--primary)] disabled:opacity-50";
export const buttonClass =
  "inline-flex items-center justify-center gap-1.5 rounded-lg border border-[var(--border)] px-3 py-2 text-xs font-medium text-[var(--foreground)] transition-colors hover:bg-[var(--muted)] disabled:cursor-not-allowed disabled:opacity-40";
export const primaryClass =
  "inline-flex items-center justify-center gap-1.5 rounded-lg bg-[var(--primary)] px-3 py-2 text-xs font-medium text-[var(--primary-foreground)] hover:opacity-90 disabled:cursor-not-allowed disabled:opacity-40";
export type RunMutation = (
  action: string,
  payload: unknown,
  request: (options: RagflowRequestOptions) => Promise<unknown>,
  remoteWrite?: boolean,
) => Promise<boolean>;

interface DocumentEditorProps {
  document: RagflowDocument;
  busy: boolean;
  onClose: () => void;
  onSave: (patch: {
    name: string;
    meta_fields: Record<string, unknown>;
    chunk_method?: string;
    parser_config: Record<string, unknown>;
  }) => Promise<boolean>;
}

export function DocumentEditor({ document, busy, onClose, onSave }: DocumentEditorProps) {
  const { t } = useTranslation();
  const [name, setName] = useState(document.name);
  const [metadata, setMetadata] = useState(() =>
    JSON.stringify(
      Object.fromEntries(
        Object.entries(document.meta_fields ?? {}).filter(([key]) => !key.startsWith("yfeistai_")),
      ),
      null,
      2,
    ),
  );
  const [method, setMethod] = useState(document.chunk_method ?? "");
  const [parser, setParser] = useState(() => JSON.stringify(document.parser_config ?? {}, null, 2));
  const [error, setError] = useState("");
  const save = async () => {
    try {
      const patch = {
        name: name.trim(),
        meta_fields: parseObjectJson(metadata),
        ...(method.trim() ? { chunk_method: method.trim() } : {}),
        parser_config: parseObjectJson(parser),
      };
      if (await onSave(patch)) onClose();
    } catch {
      setError(
        t("ragflow.invalidJson", { defaultValue: "元数据和解析配置必须是有效的 JSON 对象。" }),
      );
    }
  };
  return (
    <Modal
      isOpen
      onClose={onClose}
      title={t("ragflow.editDocument", { defaultValue: "编辑文档" })}
      width="lg"
      closeOnBackdrop={!busy}
      closeOnEscape={!busy}
      showCloseButton={!busy}
      footer={
        <div className="flex justify-end gap-2">
          <button className={buttonClass} disabled={busy} onClick={onClose}>
            {t("Cancel")}
          </button>
          <button
            className={primaryClass}
            disabled={busy || !name.trim()}
            onClick={() => void save()}
          >
            {busy && <Loader2 size={14} className="animate-spin" />}
            {t("Save")}
          </button>
        </div>
      }
    >
      <div className="space-y-4 p-5">
        <p className="text-xs text-[var(--muted-foreground)]">
          {t("ragflow.editUnpublishes", {
            defaultValue: "修改会先将已发布文档下架。处理完成并检查内容后，再重新发布。",
          })}
        </p>
        <label className="block space-y-1 text-xs">
          {t("ragflow.documentName", { defaultValue: "文档名称" })}
          <input
            className={fieldClass}
            value={name}
            maxLength={180}
            onChange={(event) => setName(event.target.value)}
          />
        </label>
        <label className="block space-y-1 text-xs">
          {t("ragflow.metadataJson", { defaultValue: "元数据（JSON）" })}
          <textarea
            className={`${fieldClass} font-mono`}
            rows={5}
            value={metadata}
            onChange={(event) => setMetadata(event.target.value)}
          />
        </label>
        <label className="block space-y-1 text-xs">
          {t("ragflow.chunkMethod", { defaultValue: "分块方法（留空保留）" })}
          <input
            className={fieldClass}
            value={method}
            onChange={(event) => setMethod(event.target.value)}
          />
        </label>
        <label className="block space-y-1 text-xs">
          {t("ragflow.parserConfig", { defaultValue: "解析配置（JSON）" })}
          <textarea
            className={`${fieldClass} font-mono`}
            rows={5}
            value={parser}
            onChange={(event) => setParser(event.target.value)}
          />
        </label>
        {error && (
          <p role="alert" className="text-sm text-red-600">
            {error}
          </p>
        )}
      </div>
    </Modal>
  );
}

interface ChunkPanelProps {
  kbRef: string;
  document: RagflowDocument;
  canWrite: boolean;
  busy: boolean;
  revision: string;
  onBack: () => void;
  mutate: RunMutation;
}

export function ChunkPanel({
  kbRef,
  document,
  canWrite,
  busy,
  revision,
  onBack,
  mutate,
}: ChunkPanelProps) {
  const { t } = useTranslation();
  const [page, setPage] = useState(1);
  const [chunks, setChunks] = useState<RagflowChunk[]>([]);
  const [total, setTotal] = useState(0);
  const [loadedRequest, setLoadedRequest] = useState("");
  const [refresh, setRefresh] = useState(0);
  const [error, setError] = useState("");
  const [editor, setEditor] = useState<RagflowChunk | "new" | null>(null);
  const [deleting, setDeleting] = useState<RagflowChunk | null>(null);
  const requestKey = `${kbRef}:${document.document_ref}:${page}:${refresh}:${revision}`;
  const loading = loadedRequest !== requestKey;
  const writable = canWrite && !loading && !error;
  useEffect(() => {
    const controller = new AbortController();
    ragflowApi
      .listChunks(
        kbRef,
        document.document_ref,
        { page, page_size: 20 },
        { signal: controller.signal },
      )
      .then((data) => {
        if (!controller.signal.aborted) {
          setChunks(data.chunks);
          setTotal(data.total);
          setError("");
          setEditor((current) =>
            current &&
            current !== "new" &&
            !data.chunks.some((chunk) => chunk.chunk_ref === current.chunk_ref)
              ? null
              : current,
          );
          setDeleting((current) =>
            current && !data.chunks.some((chunk) => chunk.chunk_ref === current.chunk_ref)
              ? null
              : current,
          );
        }
      })
      .catch((failure) => {
        if (!controller.signal.aborted) {
          setChunks([]);
          setTotal(0);
          setEditor(null);
          setDeleting(null);
          setError(ragflowErrorMessage(failure, t));
        }
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoadedRequest(requestKey);
      });
    return () => controller.abort();
  }, [kbRef, document.document_ref, page, refresh, revision, requestKey, t]);
  const save = async (patch: {
    content: string;
    important_keywords: string[];
    questions: string[];
    tag_kwd: string[];
  }) => {
    const current = editor;
    if (!current || !writable) return;
    const ok = await mutate(
      `chunk:${document.document_ref}:${current === "new" ? "new" : current.chunk_ref}`,
      patch,
      (options) =>
        current === "new"
          ? ragflowApi.addChunk(kbRef, document.document_ref, patch, options)
          : ragflowApi.updateChunk(kbRef, document.document_ref, current.chunk_ref, patch, options),
    );
    if (ok) {
      setEditor(null);
      setRefresh((value) => value + 1);
    }
  };
  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <button
            className="mb-2 flex items-center gap-1 text-xs text-[var(--muted-foreground)]"
            onClick={onBack}
          >
            <ArrowLeft size={14} />
            {t("ragflow.backToDocuments", { defaultValue: "返回文档" })}
          </button>
          <h2 className="text-base font-semibold">{document.name}</h2>
          <p className="mt-1 text-xs text-[var(--muted-foreground)]">
            {t("ragflow.chunksCount", { defaultValue: "{{count}} 个知识片段", count: total })}
          </p>
        </div>
        <div className="flex gap-2">
          <button
            className={buttonClass}
            onClick={() => setRefresh((value) => value + 1)}
            aria-label={t("Refresh")}
          >
            <RefreshCw size={14} />
          </button>
          <button
            className={primaryClass}
            disabled={!writable || busy}
            onClick={() => setEditor("new")}
          >
            <Plus size={14} />
            {t("ragflow.addChunk", { defaultValue: "新增片段" })}
          </button>
        </div>
      </div>
      {error && (
        <p
          role="alert"
          className="rounded-lg bg-red-500/10 p-3 text-sm text-red-700 dark:text-red-300"
        >
          {error}
        </p>
      )}
      {loading ? (
        <p
          role="status"
          className="flex items-center gap-2 p-6 text-sm text-[var(--muted-foreground)]"
        >
          <Loader2 className="animate-spin" size={16} />
          {t("Loading...")}
        </p>
      ) : !chunks.length ? (
        <p className="rounded-xl border border-dashed border-[var(--border)] p-8 text-center text-sm text-[var(--muted-foreground)]">
          {t("ragflow.noChunks", { defaultValue: "暂无片段。请先解析文档，或手工添加内容。" })}
        </p>
      ) : (
        <div className="space-y-3">
          {chunks.map((chunk, index) => (
            <article
              key={chunk.chunk_ref}
              className="rounded-xl border border-[var(--border)] bg-[var(--card)] p-4"
            >
              <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
                <span className="text-xs text-[var(--muted-foreground)]">
                  {t("ragflow.chunkNumber", {
                    defaultValue: "片段 {{number}}",
                    number: (page - 1) * 20 + index + 1,
                  })}
                </span>
                <div className="flex items-center gap-2">
                  <label className="flex items-center gap-1 text-xs">
                    <input
                      type="checkbox"
                      checked={chunk.available === true}
                      disabled={!writable || busy}
                      onChange={(event) => {
                        const available = event.target.checked;
                        void mutate(
                          `chunk-availability:${chunk.chunk_ref}`,
                          { available },
                          (options) =>
                            ragflowApi.setChunkAvailability(
                              kbRef,
                              document.document_ref,
                              chunk.chunk_ref,
                              { available },
                              options,
                            ),
                        );
                      }}
                    />
                    {t("Enabled")}
                  </label>
                  <button
                    className={buttonClass}
                    disabled={!writable || busy}
                    onClick={() => setEditor(chunk)}
                  >
                    {t("Edit")}
                  </button>
                  <button
                    className={`${buttonClass} text-red-600`}
                    disabled={!writable || busy}
                    onClick={() => setDeleting(chunk)}
                  >
                    {t("Delete")}
                  </button>
                </div>
              </div>
              <pre className="max-h-72 overflow-auto whitespace-pre-wrap break-words font-sans text-sm leading-6">
                {chunk.content}
              </pre>
              {chunk.important_keywords?.length ? (
                <p className="mt-3 text-xs text-[var(--muted-foreground)]">
                  {chunk.important_keywords.join(" · ")}
                </p>
              ) : null}
            </article>
          ))}
        </div>
      )}
      <div className="flex items-center justify-end gap-3 text-xs">
        <button
          className={buttonClass}
          disabled={page <= 1 || loading}
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
          disabled={page * 20 >= total || loading}
          onClick={() => setPage((value) => value + 1)}
          aria-label={t("ragflow.connection.nextPage", { defaultValue: "下一页" })}
        >
          <ChevronRight size={14} />
        </button>
      </div>
      {editor && !loading && !error && (
        <ChunkEditor
          key={editor === "new" ? "new" : editor.chunk_ref}
          chunk={editor === "new" ? null : editor}
          busy={busy}
          onClose={() => setEditor(null)}
          onSave={save}
        />
      )}
      <Modal
        isOpen={Boolean(deleting) && !loading && !error}
        onClose={() => setDeleting(null)}
        title={t("ragflow.deleteChunk", { defaultValue: "删除知识片段" })}
        closeOnBackdrop={!busy}
        closeOnEscape={!busy}
        showCloseButton={!busy}
        footer={
          <div className="flex justify-end gap-2">
            <button className={buttonClass} disabled={busy} onClick={() => setDeleting(null)}>
              {t("Cancel")}
            </button>
            <button
              className={`${primaryClass} !bg-red-600 !text-white`}
              disabled={busy || !writable}
              onClick={() => {
                const target = deleting;
                if (target)
                  void mutate(`delete-chunk:${target.chunk_ref}`, {}, (options) =>
                    ragflowApi.deleteChunk(kbRef, document.document_ref, target.chunk_ref, options),
                  ).then((ok) => {
                    if (ok) setDeleting(null);
                  });
              }}
            >
              {t("Delete")}
            </button>
          </div>
        }
      >
        <p className="p-5 text-sm text-[var(--muted-foreground)]">
          {t("ragflow.deleteChunkWarning", {
            defaultValue:
              "将删除 RAGFlow 中的这个片段，并先将文档下架。该操作不能通过平台直接撤销。",
          })}
        </p>
      </Modal>
    </div>
  );
}

function ChunkEditor({
  chunk,
  busy,
  onClose,
  onSave,
}: {
  chunk: RagflowChunk | null;
  busy: boolean;
  onClose: () => void;
  onSave: (patch: {
    content: string;
    important_keywords: string[];
    questions: string[];
    tag_kwd: string[];
  }) => Promise<void>;
}) {
  const { t } = useTranslation();
  const [content, setContent] = useState(chunk?.content ?? "");
  const [keywords, setKeywords] = useState((chunk?.important_keywords ?? []).join("\n"));
  const [questions, setQuestions] = useState((chunk?.questions ?? []).join("\n"));
  const [tags, setTags] = useState((chunk?.tag_kwd ?? []).join("\n"));
  const lines = (value: string) =>
    value
      .split("\n")
      .map((line) => line.trim())
      .filter(Boolean);
  return (
    <Modal
      isOpen
      onClose={onClose}
      title={
        chunk
          ? t("ragflow.editChunk", { defaultValue: "编辑片段" })
          : t("ragflow.addChunk", { defaultValue: "新增片段" })
      }
      width="xl"
      closeOnBackdrop={!busy}
      closeOnEscape={!busy}
      showCloseButton={!busy}
      footer={
        <div className="flex justify-end gap-2">
          <button className={buttonClass} disabled={busy} onClick={onClose}>
            {t("Cancel")}
          </button>
          <button
            className={primaryClass}
            disabled={busy || !content.trim()}
            onClick={() =>
              void onSave({
                content,
                important_keywords: lines(keywords),
                questions: lines(questions),
                tag_kwd: lines(tags),
              })
            }
          >
            {busy && <Loader2 size={14} className="animate-spin" />}
            {t("Save")}
          </button>
        </div>
      }
    >
      <div className="space-y-4 p-5">
        <label className="block space-y-1 text-xs">
          {t("ragflow.chunkContent", { defaultValue: "片段内容" })}
          <textarea
            className={fieldClass}
            rows={10}
            maxLength={200000}
            value={content}
            onChange={(event) => setContent(event.target.value)}
          />
        </label>
        <div className="grid gap-3 sm:grid-cols-3">
          <label className="block space-y-1 text-xs">
            {t("ragflow.keywordsLines", { defaultValue: "关键词（每行一个）" })}
            <textarea
              rows={4}
              className={fieldClass}
              value={keywords}
              onChange={(event) => setKeywords(event.target.value)}
            />
          </label>
          <label className="block space-y-1 text-xs">
            {t("ragflow.questionsLines", { defaultValue: "问题（每行一个）" })}
            <textarea
              rows={4}
              className={fieldClass}
              value={questions}
              onChange={(event) => setQuestions(event.target.value)}
            />
          </label>
          <label className="block space-y-1 text-xs">
            {t("ragflow.tagsLines", { defaultValue: "标签（每行一个）" })}
            <textarea
              rows={4}
              className={fieldClass}
              value={tags}
              onChange={(event) => setTags(event.target.value)}
            />
          </label>
        </div>
      </div>
    </Modal>
  );
}

"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import { CheckCircle2, LoaderCircle, RefreshCw, RotateCcw } from "lucide-react";
import { useTranslation } from "react-i18next";

import {
  canonicalOutlineSha256,
  confirmSelectedBatchOutlines,
  getTeachingClassroom,
  listTeachingBatches,
  retryBatchItem,
  type TeachingBatch,
  type TeachingBatchItem,
  type TeachingClassroom,
} from "@/lib/teaching-api";

const ITEM_STATUSES = [
  "awaiting_confirmation",
  "queued",
  "running",
  "succeeded",
  "failed",
] as const;

function itemCounts(batch: TeachingBatch): Record<string, number> {
  return Object.fromEntries(
    ITEM_STATUSES.map(status => [
      status,
      batch.items.filter(item => item.status === status).length,
    ]),
  );
}

export function BatchWorkbench() {
  const { t } = useTranslation();
  const [batches, setBatches] = useState<TeachingBatch[]>([]);
  const [selectedBatchId, setSelectedBatchId] = useState("");
  const [classrooms, setClassrooms] = useState<
    Record<string, TeachingClassroom>
  >({});
  const [selectedItems, setSelectedItems] = useState<Set<string>>(
    () => new Set(),
  );
  const [loading, setLoading] = useState(true);
  const [confirming, setConfirming] = useState(false);
  const [retrying, setRetrying] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const next = await listTeachingBatches();
      setBatches(next);
      setSelectedBatchId(current =>
        next.some(batch => batch.id === current) ? current : next[0]?.id || "",
      );
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const selectedBatch =
    batches.find(batch => batch.id === selectedBatchId) ?? batches[0];

  useEffect(() => {
    if (!selectedBatch) {
      setClassrooms({});
      setSelectedItems(new Set());
      return;
    }
    let active = true;
    const candidates = selectedBatch.items.filter(
      item => item.status === "awaiting_confirmation" && item.classroomAssetId,
    );
    Promise.all(
      candidates.map(async item => [
        item.id,
        await getTeachingClassroom(item.classroomAssetId as string),
      ] as const),
    )
      .then(entries => {
        if (active) setClassrooms(Object.fromEntries(entries));
      })
      .catch(reason => {
        if (active) setError(reason instanceof Error ? reason.message : String(reason));
      });
    setSelectedItems(new Set());
    return () => {
      active = false;
    };
  }, [selectedBatch]);

  const counts = useMemo(
    () => (selectedBatch ? itemCounts(selectedBatch) : {}),
    [selectedBatch],
  );

  const replaceBatch = (next: TeachingBatch) => {
    setBatches(current =>
      current.map(batch => (batch.id === next.id ? next : batch)),
    );
  };

  const toggleItem = (itemId: string) => {
    setSelectedItems(current => {
      const next = new Set(current);
      if (next.has(itemId)) next.delete(itemId);
      else next.add(itemId);
      return next;
    });
  };

  const confirmSelected = async () => {
    if (!selectedBatch || selectedItems.size === 0) return;
    setConfirming(true);
    setError(null);
    try {
      const confirmations = await Promise.all(
        [...selectedItems].map(async itemId => {
          const classroom = classrooms[itemId];
          if (!classroom?.outline) {
            throw new Error(t("teaching.batches.outlineUnavailable"));
          }
          return {
            itemId,
            revision: classroom.revision,
            outlineSha256: await canonicalOutlineSha256(classroom.outline),
          };
        }),
      );
      replaceBatch(
        await confirmSelectedBatchOutlines(selectedBatch.id, confirmations),
      );
      setSelectedItems(new Set());
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setConfirming(false);
    }
  };

  const retry = async (item: TeachingBatchItem) => {
    if (!selectedBatch) return;
    setRetrying(item.id);
    setError(null);
    try {
      await retryBatchItem(selectedBatch.id, item.id);
      await refresh();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setRetrying(null);
    }
  };

  if (loading && !batches.length) {
    return (
      <div className="flex items-center gap-2 p-6 text-sm text-[var(--muted-foreground)]">
        <LoaderCircle className="h-4 w-4 animate-spin" aria-hidden />
        {t("teaching.common.loading")}
      </div>
    );
  }

  if (!batches.length) {
    return (
      <div className="rounded-2xl border border-dashed border-[var(--border)] p-8 text-center text-sm text-[var(--muted-foreground)]">
        {t("teaching.batches.empty")}
      </div>
    );
  }

  return (
    <div className="grid gap-4 xl:grid-cols-[18rem_minmax(0,1fr)]">
      <aside className="space-y-2 rounded-2xl border border-[var(--border)] bg-[var(--card)] p-3">
        {batches.map(batch => (
          <button
            key={batch.id}
            type="button"
            onClick={() => setSelectedBatchId(batch.id)}
            className={`w-full rounded-xl border p-3 text-left text-sm ${
              selectedBatch?.id === batch.id
                ? "border-[var(--primary)] bg-[var(--primary)]/5"
                : "border-transparent hover:bg-[var(--muted)]/40"
            }`}
          >
            <span className="block truncate font-semibold">{batch.id}</span>
            <span className="mt-1 block text-xs text-[var(--muted-foreground)]">
              {batch.status} · {batch.itemCount}
            </span>
          </button>
        ))}
      </aside>

      {selectedBatch ? (
        <main className="space-y-4">
          <header className="rounded-2xl border border-[var(--border)] bg-[var(--card)] p-5">
            <div className="flex flex-wrap items-center justify-between gap-3">
              <div>
                <h2 className="text-xl font-semibold">{selectedBatch.id}</h2>
                <p className="mt-1 text-sm text-[var(--muted-foreground)]">
                  {t("teaching.batches.createdBy", { user: selectedBatch.actorId })}
                </p>
              </div>
              <button
                type="button"
                onClick={() => void refresh()}
                className="inline-flex items-center gap-2 rounded-lg border border-[var(--border)] px-3 py-2 text-sm"
              >
                <RefreshCw className="h-4 w-4" aria-hidden />
                {t("teaching.common.refresh")}
              </button>
            </div>
            <dl className="mt-4 grid gap-3 sm:grid-cols-3 lg:grid-cols-6">
              <div>
                <dt className="text-xs text-[var(--muted-foreground)]">{t("teaching.batches.total")}</dt>
                <dd className="text-lg font-semibold">{selectedBatch.itemCount}</dd>
              </div>
              {ITEM_STATUSES.map(status => (
                <div key={status}>
                  <dt className="text-xs text-[var(--muted-foreground)]">
                    {t(`teaching.batches.status.${status}`)}
                  </dt>
                  <dd className="text-lg font-semibold">{counts[status] ?? 0}</dd>
                </div>
              ))}
            </dl>
          </header>

          <div className="space-y-3">
            {selectedBatch.items.map(item => {
              const classroom = classrooms[item.id];
              const reviewable = Boolean(
                item.status === "awaiting_confirmation" && classroom?.outline,
              );
              return (
                <article
                  key={item.id}
                  className="rounded-2xl border border-[var(--border)] bg-[var(--card)] p-5"
                >
                  <div className="flex flex-wrap items-start justify-between gap-3">
                    <div>
                      <h3 className="font-semibold">{classroom?.title ?? item.id}</h3>
                      <p className="mt-1 text-xs text-[var(--muted-foreground)]">
                        {item.id} · {item.status}
                      </p>
                    </div>
                    {reviewable ? (
                      <label className="flex items-center gap-2 text-sm font-medium">
                        <input
                          type="checkbox"
                          checked={selectedItems.has(item.id)}
                          onChange={() => toggleItem(item.id)}
                        />
                        {t("teaching.batches.selectOutline")}
                      </label>
                    ) : null}
                    {item.status === "failed" ? (
                      <button
                        type="button"
                        onClick={() => void retry(item)}
                        disabled={retrying !== null}
                        className="inline-flex items-center gap-2 rounded-lg border border-[var(--border)] px-3 py-2 text-sm font-semibold disabled:opacity-50"
                      >
                        {retrying === item.id ? <LoaderCircle className="h-4 w-4 animate-spin" aria-hidden /> : <RotateCcw className="h-4 w-4" aria-hidden />}
                        {t("teaching.batches.retryItem")}
                      </button>
                    ) : null}
                  </div>
                  {classroom?.outline ? (
                    <details className="mt-3 rounded-lg bg-[var(--muted)]/35 p-3">
                      <summary className="cursor-pointer text-sm font-medium">
                        {t("teaching.batches.reviewOutline")}
                      </summary>
                      <pre className="mt-3 max-h-80 overflow-auto whitespace-pre-wrap text-xs">
                        {JSON.stringify(classroom.outline, null, 2)}
                      </pre>
                    </details>
                  ) : null}
                </article>
              );
            })}
          </div>

          <div className="sticky bottom-4 flex flex-wrap items-center justify-between gap-3 rounded-2xl border border-[var(--border)] bg-[var(--background)]/95 p-4 shadow-lg backdrop-blur">
            <span className="text-sm text-[var(--muted-foreground)]">
              {t("teaching.batches.selected", { count: selectedItems.size })}
            </span>
            <button
              type="button"
              onClick={() => void confirmSelected()}
              disabled={selectedItems.size === 0 || confirming}
              className="inline-flex items-center gap-2 rounded-lg bg-[var(--primary)] px-4 py-2 text-sm font-semibold text-[var(--primary-foreground)] disabled:opacity-50"
            >
              {confirming ? <LoaderCircle className="h-4 w-4 animate-spin" aria-hidden /> : <CheckCircle2 className="h-4 w-4" aria-hidden />}
              {t("teaching.batches.confirmSelected")}
            </button>
          </div>
          {error ? (
            <p role="alert" className="text-sm text-[var(--destructive)]">
              {error}
            </p>
          ) : null}
        </main>
      ) : null}
    </div>
  );
}

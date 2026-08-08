"use client";

import { useCallback, useEffect, useState } from "react";
import Link from "next/link";
import { ArrowRight, LoaderCircle, Plus, RefreshCw } from "lucide-react";
import { useTranslation } from "react-i18next";

import {
  classroomNextRoute,
  listTeachingClassrooms,
  type TeachingClassroom,
} from "@/lib/teaching-api";

function routeStatus(classroom: TeachingClassroom): string {
  if (classroom.status === "awaiting_confirmation") return classroom.status;
  if (classroom.lifecycleState === "awaiting_outline") return "awaiting_confirmation";
  return classroom.lifecycleState;
}

export default function TeachingClassroomsPage() {
  const { t } = useTranslation();
  const [items, setItems] = useState<TeachingClassroom[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      setItems(await listTeachingClassrooms());
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  return (
    <div className="space-y-5">
      <header className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">{t("teaching.classrooms.title")}</h1>
          <p className="mt-1 text-sm text-[var(--muted-foreground)]">
            {t("teaching.classrooms.description")}
          </p>
        </div>
        <div className="flex gap-2">
          <button
            type="button"
            onClick={() => void refresh()}
            className="inline-flex items-center gap-2 rounded-lg border border-[var(--border)] px-3 py-2 text-sm font-medium"
          >
            <RefreshCw className="h-4 w-4" aria-hidden />
            {t("teaching.common.refresh")}
          </button>
          <Link
            href="/teaching/classrooms/new"
            className="inline-flex items-center gap-2 rounded-lg bg-[var(--primary)] px-3 py-2 text-sm font-semibold text-[var(--primary-foreground)]"
          >
            <Plus className="h-4 w-4" aria-hidden />
            {t("teaching.classrooms.new")}
          </Link>
        </div>
      </header>

      {loading && !items.length ? (
        <div className="flex items-center gap-2 p-6 text-sm text-[var(--muted-foreground)]">
          <LoaderCircle className="h-4 w-4 animate-spin" aria-hidden />
          {t("teaching.common.loading")}
        </div>
      ) : items.length ? (
        <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
          {items.map(classroom => {
            const href = classroomNextRoute({
              assetId: classroom.assetId,
              status: routeStatus(classroom),
            });
            return (
              <article key={classroom.assetId} className="rounded-2xl border border-[var(--border)] bg-[var(--card)] p-5">
                <div className="flex items-start justify-between gap-3">
                  <div className="min-w-0">
                    <h2 className="truncate font-semibold">{classroom.title}</h2>
                    <p className="mt-1 truncate text-xs text-[var(--muted-foreground)]">
                      {classroom.courseId} / {classroom.classId}
                    </p>
                  </div>
                  <span className="shrink-0 rounded-full bg-[var(--muted)] px-2.5 py-1 text-[11px] font-medium">
                    {classroom.status}
                  </span>
                </div>
                <dl className="mt-4 grid grid-cols-2 gap-3 text-xs">
                  <div>
                    <dt className="text-[var(--muted-foreground)]">{t("teaching.classrooms.lifecycle")}</dt>
                    <dd className="mt-1 font-medium">{classroom.lifecycleState}</dd>
                  </div>
                  <div>
                    <dt className="text-[var(--muted-foreground)]">{t("teaching.classrooms.revision")}</dt>
                    <dd className="mt-1 font-medium">{classroom.revision}</dd>
                  </div>
                </dl>
                {href !== "/teaching/classrooms" ? (
                  <Link
                    href={href}
                    className="mt-4 inline-flex items-center gap-2 text-sm font-semibold text-[var(--primary)]"
                  >
                    {t("teaching.classrooms.continue")}
                    <ArrowRight className="h-4 w-4" aria-hidden />
                  </Link>
                ) : (
                  <p className="mt-4 text-xs text-[var(--muted-foreground)]">
                    {t("teaching.classrooms.processing")}
                  </p>
                )}
              </article>
            );
          })}
        </div>
      ) : (
        <div className="rounded-2xl border border-dashed border-[var(--border)] p-10 text-center text-sm text-[var(--muted-foreground)]">
          {t("teaching.classrooms.empty")}
        </div>
      )}
      {error ? <p role="alert" className="text-sm text-[var(--destructive)]">{error}</p> : null}
    </div>
  );
}

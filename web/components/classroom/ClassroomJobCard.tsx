"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  CheckCircle2,
  CircleAlert,
  Clock3,
  ExternalLink,
  LoaderCircle,
  RefreshCcw,
  Save,
  ShieldCheck,
} from "lucide-react";
import { useTranslation } from "react-i18next";

import {
  confirmStudentClassroomOutline,
  getStudentClassroom,
  getStudentClassroomJob,
  resolveStudentClassroomCardState,
  shouldPollStudentClassroom,
  studentClassroomApprovalState,
  studentClassroomPlayRoute,
  studentClassroomPollIntervalMs,
  studentClassroomPollRetryDelay,
  studentClassroomRequiresOutline,
  studentClassroomStatusKind,
  updateStudentClassroomOutline,
  type StudentClassroomJob,
  type StudentClassroomState,
  type StudentClassroomTask,
} from "@/lib/student-classroom-config";

function parseOutline(text: string): Record<string, unknown> {
  const parsed: unknown = JSON.parse(text);
  if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
    throw new Error("outline_must_be_object");
  }
  return parsed as Record<string, unknown>;
}

export default function ClassroomJobCard({
  task,
}: {
  task: StudentClassroomTask;
}) {
  const { t } = useTranslation();
  const [classroom, setClassroom] = useState<StudentClassroomState | null>(null);
  const [job, setJob] = useState<StudentClassroomJob | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [refreshNonce, setRefreshNonce] = useState(0);
  const initialOutline = task.outline ?? {};
  const [outlineText, setOutlineText] = useState(() =>
    JSON.stringify(initialOutline, null, 2),
  );
  const [outlineDirty, setOutlineDirty] = useState(false);
  const [editRevision, setEditRevision] = useState<number | null>(null);
  const dirtyRef = useRef(outlineDirty);
  dirtyRef.current = outlineDirty;

  const cardState = resolveStudentClassroomCardState(task, classroom);
  const { jobId, status, outline, approvalId, classroomVersionId } = cardState;
  const playRoute = studentClassroomPlayRoute(classroomVersionId);
  const requiresOutline = studentClassroomRequiresOutline(status);
  const approvalState = studentClassroomApprovalState({
    status,
    approvalId,
    jobId,
  });
  const statusKind = studentClassroomStatusKind(status);

  useEffect(() => {
    if (!outline || dirtyRef.current) return;
    setOutlineText(JSON.stringify(outline, null, 2));
  }, [outline]);

  useEffect(() => {
    let active = true;
    let timer: ReturnType<typeof setTimeout> | null = null;
    let failureCount = 0;
    const controller = new AbortController();

    const refresh = async () => {
      try {
        const nextClassroom = await getStudentClassroom(
          task.assetId,
          controller.signal,
        );
        if (!active) return;
        setClassroom(nextClassroom);
        if (!shouldPollStudentClassroom(nextClassroom)) {
          setError(null);
          return;
        }
        const nextJobId = nextClassroom.generationJobId ?? task.jobId;
        const nextJob = nextJobId
          ? await getStudentClassroomJob(nextJobId, controller.signal)
          : null;
        if (!active) return;
        failureCount = 0;
        setJob(nextJob);
        setError(null);

        timer = setTimeout(
          refresh,
          studentClassroomPollIntervalMs(nextClassroom.status),
        );
      } catch (reason) {
        if (!active || controller.signal.aborted) return;
        setError(reason instanceof Error ? reason.message : String(reason));
        failureCount += 1;
        const retryDelay = studentClassroomPollRetryDelay(reason, failureCount);
        if (retryDelay !== null) timer = setTimeout(refresh, retryDelay);
      }
    };

    void refresh();
    return () => {
      active = false;
      controller.abort();
      if (timer) clearTimeout(timer);
    };
  }, [refreshNonce, task.assetId, task.jobId]);

  const saveOutline = useCallback(async () => {
    const parsed = parseOutline(outlineText);
    const revision = editRevision ?? classroom?.revision ?? task.revision;
    const updated = await updateStudentClassroomOutline(
      task.assetId,
      parsed,
      revision,
    );
    setClassroom(updated);
    setOutlineText(JSON.stringify(updated.outline ?? parsed, null, 2));
    setOutlineDirty(false);
    setEditRevision(null);
    return updated;
  }, [classroom?.revision, editRevision, outlineText, task.assetId, task.revision]);

  const handleSave = useCallback(async () => {
    setBusy(true);
    setError(null);
    try {
      await saveOutline();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setBusy(false);
    }
  }, [saveOutline]);

  const handleConfirm = useCallback(async () => {
    setBusy(true);
    setError(null);
    try {
      if (outlineDirty) await saveOutline();
      const confirmed = await confirmStudentClassroomOutline(task.assetId);
      setClassroom(confirmed);
      setRefreshNonce(value => value + 1);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setBusy(false);
    }
  }, [outlineDirty, saveOutline, task.assetId]);

  const statusKey = useMemo(
    () => `studentClassroom.status.${status}`,
    [status],
  );

  return (
    <section
      className="mt-2 overflow-hidden rounded-xl border border-[var(--border)] bg-[var(--card)]"
      data-testid="student-classroom-job-card"
      data-job-id={jobId ?? undefined}
    >
      <header className="flex items-center gap-2 border-b border-[var(--border)]/55 px-4 py-3">
        {statusKind === "success" ? (
          <CheckCircle2 className="h-4 w-4 text-emerald-600" aria-hidden />
        ) : statusKind === "failure" ? (
          <CircleAlert className="h-4 w-4 text-[var(--destructive)]" aria-hidden />
        ) : statusKind === "waiting" ? (
          <Clock3 className="h-4 w-4 text-amber-600" aria-hidden />
        ) : (
          <LoaderCircle className="h-4 w-4 animate-spin text-[var(--primary)]" aria-hidden />
        )}
        <div className="min-w-0 flex-1">
          <h3 className="text-[13px] font-semibold">
            {t("studentClassroom.job.title")}
          </h3>
          <p className="truncate text-[10.5px] text-[var(--muted-foreground)]">
            {t(statusKey, { defaultValue: status })}
            {jobId ? ` · ${jobId}` : ""}
          </p>
        </div>
        <button
          type="button"
          onClick={() => setRefreshNonce(value => value + 1)}
          aria-label={t("studentClassroom.job.refresh")}
          className="rounded-md p-1.5 text-[var(--muted-foreground)] hover:bg-[var(--muted)]"
        >
          <RefreshCcw className="h-3.5 w-3.5" aria-hidden />
        </button>
      </header>

      <div className="space-y-3 p-4">
        {job ? (
          <div className="space-y-1">
            <div className="flex justify-between text-[10.5px] text-[var(--muted-foreground)]">
              <span>{t("studentClassroom.job.progress")}</span>
              <span>{job.progressPercent}%</span>
            </div>
            <div className="h-1.5 overflow-hidden rounded-full bg-[var(--muted)]">
              <div
                className="h-full rounded-full bg-[var(--primary)] transition-[width]"
                style={{ width: `${job.progressPercent}%` }}
              />
            </div>
          </div>
        ) : null}

        <dl className="grid grid-cols-2 gap-2 text-center sm:grid-cols-4">
          <div className="rounded-lg bg-[var(--muted)]/35 p-2">
            <dt className="text-[9.5px] text-[var(--muted-foreground)]">
              {t("studentClassroom.estimate.scenes")}
            </dt>
            <dd className="mt-0.5 text-[11.5px] font-semibold">
              {task.estimate.sceneRange.join("–")}
            </dd>
          </div>
          <div className="rounded-lg bg-[var(--muted)]/35 p-2">
            <dt className="text-[9.5px] text-[var(--muted-foreground)]">
              {t("studentClassroom.estimate.minutes")}
            </dt>
            <dd className="mt-0.5 text-[11.5px] font-semibold">
              {task.estimate.durationMinutesRange.join("–")}
            </dd>
          </div>
          <div className="rounded-lg bg-[var(--muted)]/35 p-2">
            <dt className="text-[9.5px] text-[var(--muted-foreground)]">
              {t("studentClassroom.estimate.quota")}
            </dt>
            <dd className="mt-0.5 text-[11.5px] font-semibold">
              {task.estimate.quotaUnits}
            </dd>
          </div>
          <div className="rounded-lg bg-[var(--muted)]/35 p-2">
            <dt className="text-[9.5px] text-[var(--muted-foreground)]">
              {t("studentClassroom.estimate.approval")}
            </dt>
            <dd className="mt-0.5 text-[11.5px] font-semibold">
              {t(`studentClassroom.job.approval.${approvalState}`)}
            </dd>
          </div>
        </dl>

        {status === "awaiting_approval" ? (
          <p className="flex items-center gap-1.5 rounded-lg bg-amber-500/10 p-2.5 text-[11px] text-amber-700 dark:text-amber-300">
            <ShieldCheck className="h-3.5 w-3.5" aria-hidden />
            {t("studentClassroom.job.awaitingApproval")}
          </p>
        ) : null}

        {requiresOutline && outline ? (
          <div className="space-y-2">
            <label className="block text-[11px] font-semibold">
              {t("studentClassroom.job.outline")}
              <textarea
                value={outlineText}
                rows={10}
                onChange={event => {
                  if (!outlineDirty) {
                    setEditRevision(classroom?.revision ?? task.revision);
                  }
                  setOutlineDirty(true);
                  setOutlineText(event.target.value);
                }}
                className="mt-1.5 w-full rounded-lg border border-[var(--border)] bg-[var(--background)] p-2 font-mono text-[10.5px] font-normal"
              />
            </label>
            <div className="flex justify-end gap-2">
              <button
                type="button"
                disabled={busy || !outlineDirty}
                onClick={() => void handleSave()}
                className="inline-flex items-center gap-1 rounded-md border border-[var(--border)] px-2.5 py-1.5 text-[10.5px] font-semibold disabled:opacity-45"
              >
                <Save className="h-3 w-3" aria-hidden />
                {t("studentClassroom.job.saveOutline")}
              </button>
              <button
                type="button"
                disabled={busy}
                onClick={() => void handleConfirm()}
                className="rounded-md bg-[var(--primary)] px-2.5 py-1.5 text-[10.5px] font-semibold text-[var(--primary-foreground)] disabled:opacity-45"
              >
                {t("studentClassroom.job.confirmOutline")}
              </button>
            </div>
          </div>
        ) : null}

        {playRoute ? (
          <a
            href={playRoute}
            className="inline-flex items-center gap-1.5 rounded-lg bg-[var(--primary)] px-3 py-2 text-[11px] font-semibold text-[var(--primary-foreground)]"
          >
            {t("studentClassroom.job.open")}
            <ExternalLink className="h-3.5 w-3.5" aria-hidden />
          </a>
        ) : status === "succeeded" ? (
          <p role="alert" className="flex items-center gap-1.5 text-[10.5px] text-[var(--destructive)]">
            <Clock3 className="h-3.5 w-3.5" aria-hidden />
            {t("studentClassroom.job.playbackUnavailable")}
          </p>
        ) : null}

        {error ? (
          <p role="alert" className="text-[10.5px] text-[var(--destructive)]">
            {error}
          </p>
        ) : null}
      </div>
    </section>
  );
}

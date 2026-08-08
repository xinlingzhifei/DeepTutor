"use client";

import { useLayoutEffect, useRef, useState } from "react";
import { Download, FileArchive, LoaderCircle } from "lucide-react";
import { useTranslation } from "react-i18next";

import {
  classroomExportDownloadUrl,
  classroomExportFailureDetails,
  createClassroomExportAttemptRegistry,
  createDraftClassroomExport,
  createVersionClassroomExport,
  listClassroomExportOptions,
  pollClassroomExport,
  shouldRetainClassroomExportAttempt,
  type ClassroomExportFormat,
  type ClassroomExportJob,
  type ClassroomExportPolicy,
} from "@/lib/classroom-api";

type ClassroomExportTarget =
  | { kind: "draft"; assetId: string; revision: string }
  | { kind: "version"; versionId: string };

interface ClassroomExportMenuProps {
  target: ClassroomExportTarget;
  policy: ClassroomExportPolicy;
  disabled?: boolean;
  onJobChange?: (job: ClassroomExportJob) => void;
}

export function ClassroomExportMenu({
  target,
  policy,
  disabled = false,
  onJobChange,
}: ClassroomExportMenuProps) {
  const { t } = useTranslation();
  const [job, setJob] = useState<ClassroomExportJob | null>(null);
  const [pendingFormat, setPendingFormat] =
    useState<ClassroomExportFormat | null>(null);
  const [error, setError] = useState(false);
  const controllerRef = useRef<AbortController | null>(null);
  const createAttemptsRef = useRef(createClassroomExportAttemptRegistry());
  const targetKey =
    target.kind === "draft"
      ? JSON.stringify(["draft", target.assetId, target.revision])
      : JSON.stringify(["version", target.versionId]);
  const targetBoundary = targetKey;
  const activeTargetBoundaryRef = useRef(targetBoundary);

  useLayoutEffect(() => {
    activeTargetBoundaryRef.current = targetBoundary;
    controllerRef.current?.abort();
    controllerRef.current = null;
    setPendingFormat(null);
    setJob(null);
    setError(false);
    return () => {
      controllerRef.current?.abort();
      controllerRef.current = null;
    };
  }, [targetBoundary]);

  const publishJob = (next: ClassroomExportJob) => {
    if (activeTargetBoundaryRef.current !== targetBoundary) return;
    setJob(next);
    onJobChange?.(next);
  };

  const startExport = async (format: ClassroomExportFormat) => {
    if (disabled || pendingFormat || controllerRef.current) return;
    const controller = new AbortController();
    controllerRef.current = controller;
    setPendingFormat(format);
    setError(false);
    let createAttempted = false;
    try {
      const existingJob =
        job?.format === format &&
        job.status !== "succeeded" &&
        job.status !== "failed" &&
        job.status !== "canceled"
          ? job
          : null;
      let activeJob = existingJob;
      if (!activeJob) {
        setJob(null);
        const attemptKey = createAttemptsRef.current.keyFor(targetKey, format);
        createAttempted = true;
        activeJob =
          target.kind === "draft"
            ? await createDraftClassroomExport(target.assetId, format, {
                revision: target.revision,
                idempotencyKey: attemptKey,
                signal: controller.signal,
              })
            : await createVersionClassroomExport(target.versionId, format, {
                idempotencyKey: attemptKey,
                signal: controller.signal,
              });
        createAttemptsRef.current.settle(targetKey, format);
        controller.signal.throwIfAborted();
        publishJob(activeJob);
      }
      if (
        activeJob.status !== "succeeded" &&
        activeJob.status !== "failed" &&
        activeJob.status !== "canceled"
      ) {
        await pollClassroomExport(activeJob.jobId, {
          expectedFormat: activeJob.format,
          initialProgressPercent: activeJob.progressPercent,
          initialStatus: activeJob.status,
          signal: controller.signal,
          onUpdate: publishJob,
        });
      }
    } catch (reason) {
      if (createAttempted && !shouldRetainClassroomExportAttempt(reason)) {
        createAttemptsRef.current.settle(targetKey, format);
      }
      if (
        !controller.signal.aborted &&
        activeTargetBoundaryRef.current === targetBoundary
      ) {
        setError(true);
      }
    } finally {
      if (controllerRef.current === controller) {
        controllerRef.current = null;
        setPendingFormat(null);
      }
    }
  };

  const formatLabel = (format: ClassroomExportFormat): string => {
    switch (format) {
      case "classroom_zip":
        return t("classroom.export.format.classroomZip");
      case "pptx":
        return t("classroom.export.format.pptx");
      case "offline_html":
        return t("classroom.export.format.offlineHtml");
      case "mp4":
        return t("classroom.export.format.mp4");
    }
  };

  const statusLabel = (current: ClassroomExportJob): string => {
    switch (current.status) {
      case "created":
        return t("classroom.export.status.created");
      case "quota_reserved":
        return t("classroom.export.status.quotaReserved");
      case "queued":
        return t("classroom.export.status.queued");
      case "exporting":
        return t("classroom.export.status.exporting");
      case "validating":
        return t("classroom.export.status.validating");
      case "materializing":
        return t("classroom.export.status.materializing");
      case "succeeded":
        return t("classroom.export.status.succeeded");
      case "failed":
        return t("classroom.export.status.failed");
      case "canceled":
        return t("classroom.export.status.canceled");
    }
  };
  const failureDetails = job ? classroomExportFailureDetails(job) : null;

  return (
    <section
      aria-label={t("classroom.export.title")}
      className="space-y-3 rounded-xl border border-[var(--border)] bg-[var(--card)] p-4"
    >
      <div className="flex items-center gap-2">
        <FileArchive className="h-5 w-5" aria-hidden="true" />
        <h3 className="font-semibold text-[var(--foreground)]">
          {t("classroom.export.title")}
        </h3>
      </div>
      <div className="grid gap-2 sm:grid-cols-2">
        {listClassroomExportOptions(policy).map(option => {
          const pending = pendingFormat === option.format;
          return (
            <div key={option.format}>
              <button
                type="button"
                disabled={disabled || !option.enabled || pendingFormat !== null}
                onClick={() => startExport(option.format)}
                className="flex w-full items-center justify-center gap-2 rounded-lg border border-[var(--border)] px-3 py-2 text-sm font-medium text-[var(--foreground)] hover:bg-[var(--muted)] disabled:cursor-not-allowed disabled:opacity-50"
              >
                {pending ? (
                  <LoaderCircle
                    className="h-4 w-4 animate-spin motion-reduce:animate-none"
                    aria-hidden="true"
                  />
                ) : null}
                {formatLabel(option.format)}
              </button>
              {option.reason ? (
                <p className="mt-1 text-xs text-[var(--muted-foreground)]">
                  {t("classroom.export.mp4Disabled")}
                </p>
              ) : null}
            </div>
          );
        })}
      </div>
      {job ? (
        <div aria-live="polite" className="space-y-2 text-sm">
          <div className="flex items-center justify-between gap-3 text-[var(--muted-foreground)]">
            <span>{statusLabel(job)}</span>
            <span>{job.progressPercent}%</span>
          </div>
          <div
            role="progressbar"
            aria-valuemin={0}
            aria-valuemax={100}
            aria-valuenow={job.progressPercent}
            className="h-2 overflow-hidden rounded-full bg-[var(--muted)]"
          >
            <div
              className="h-full bg-[var(--primary)] transition-[width] motion-reduce:transition-none"
              style={{ width: `${job.progressPercent}%` }}
            />
          </div>
          {failureDetails ? (
            <dl
              role="alert"
              className="grid gap-1 rounded-lg border border-[var(--destructive)]/30 bg-[var(--destructive)]/5 p-3 text-xs text-[var(--destructive)]"
            >
              <div className="flex flex-wrap gap-1">
                <dt className="font-semibold">
                  {t("classroom.export.errorCategory")}:
                </dt>
                <dd className="font-mono">{failureDetails.errorCategory}</dd>
              </div>
              <div className="flex flex-wrap gap-1">
                <dt className="font-semibold">{t("classroom.export.errorCode")}:</dt>
                <dd className="font-mono">{failureDetails.errorCode}</dd>
              </div>
            </dl>
          ) : null}
          {job.downloadReady ? (
            <a
              href={classroomExportDownloadUrl(job.jobId)}
              download
              className="inline-flex items-center gap-2 rounded-lg bg-[var(--primary)] px-3 py-2 font-semibold text-[var(--primary-foreground)]"
            >
              <Download className="h-4 w-4" aria-hidden="true" />
              {t("classroom.export.download")}
            </a>
          ) : null}
        </div>
      ) : null}
      {error ? (
        <p role="alert" className="text-sm text-[var(--destructive)]">
          {t("classroom.export.failed")}
        </p>
      ) : null}
    </section>
  );
}

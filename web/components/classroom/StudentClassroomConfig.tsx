"use client";

import { useEffect, useState } from "react";
import {
  BookOpen,
  Clock3,
  Layers3,
  LoaderCircle,
  ShieldCheck,
} from "lucide-react";
import { useTranslation } from "react-i18next";

import {
  estimateStudentClassroom,
  listStudentClassroomOptions,
  studentClassroomEstimateRequestKey,
  type StudentClassroomEstimate,
  type StudentClassroomEstimateReadiness,
  type StudentClassroomContentMode,
  type StudentClassroomFormConfig,
  type StudentClassroomMode,
  type StudentClassroomOption,
} from "@/lib/student-classroom-config";

interface StudentClassroomConfigProps {
  value: StudentClassroomFormConfig;
  authorizedSourceCount: number;
  authorizedSourceRef: string | null;
  onChange: (next: StudentClassroomFormConfig) => void;
  onOptionsChange: (options: StudentClassroomOption[]) => void;
  onEstimateReadinessChange: (
    readiness: StudentClassroomEstimateReadiness | null,
  ) => void;
}

export default function StudentClassroomConfig({
  value,
  authorizedSourceCount,
  authorizedSourceRef,
  onChange,
  onOptionsChange,
  onEstimateReadinessChange,
}: StudentClassroomConfigProps) {
  const { t } = useTranslation();
  const [options, setOptions] = useState<StudentClassroomOption[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [estimateResult, setEstimateResult] = useState<{
    requestKey: string;
    estimate: StudentClassroomEstimate;
  } | null>(null);
  const [estimateFailureKey, setEstimateFailureKey] = useState<string | null>(
    null,
  );
  const [estimateRetryNonce, setEstimateRetryNonce] = useState(0);

  useEffect(() => {
    let active = true;
    const controller = new AbortController();
    listStudentClassroomOptions(controller.signal)
      .then(items => {
        if (!active) return;
        setOptions(items);
        onOptionsChange(items);
        setError(null);
      })
      .catch(() => {
        if (active && !controller.signal.aborted) {
          setOptions([]);
          onOptionsChange([]);
          setError(t("studentClassroom.config.courseLoadFailed"));
        }
      })
      .finally(() => {
        if (active) setLoading(false);
      });
    return () => {
      active = false;
      controller.abort();
    };
  }, [onOptionsChange, t]);

  const selectedOption = options.find(option => option.courseId === value.courseId) ?? null;
  const canEstimate = Boolean(
    value.mode &&
      selectedOption?.allowedModes.includes(value.mode) &&
      selectedOption.allowedContentModes.includes(value.contentMode) &&
      (value.contentMode !== "source_grounded" || authorizedSourceRef),
  );
  const estimateSourceRef =
    value.contentMode === "source_grounded" ? authorizedSourceRef : null;
  const estimateRequestKey = canEstimate && value.mode
    ? studentClassroomEstimateRequestKey({
        courseId: value.courseId,
        mode: value.mode,
        contentMode: value.contentMode,
        ...(estimateSourceRef ? { sourceRef: estimateSourceRef } : {}),
      })
    : null;
  const estimate =
    estimateResult?.requestKey === estimateRequestKey
      ? estimateResult.estimate
      : null;
  const estimateFailed = estimateFailureKey === estimateRequestKey;
  const estimateLoading =
    estimateRequestKey !== null && estimate === null && !estimateFailed;

  useEffect(() => {
    if (estimateRequestKey === null || value.mode === null) {
      onEstimateReadinessChange(null);
      return;
    }
    let active = true;
    const controller = new AbortController();
    onEstimateReadinessChange({
      requestKey: estimateRequestKey,
      status: "loading",
    });
    estimateStudentClassroom(
      {
        courseId: value.courseId,
        mode: value.mode,
        contentMode: value.contentMode,
        ...(estimateSourceRef ? { sourceRef: estimateSourceRef } : {}),
      },
      controller.signal,
    )
      .then(next => {
        if (!active) return;
        setEstimateResult({ requestKey: estimateRequestKey, estimate: next });
        setEstimateFailureKey(current =>
          current === estimateRequestKey ? null : current,
        );
        onEstimateReadinessChange({
          requestKey: estimateRequestKey,
          status: "ready",
        });
      })
      .catch(() => {
        if (active && !controller.signal.aborted) {
          setEstimateFailureKey(estimateRequestKey);
          onEstimateReadinessChange({
            requestKey: estimateRequestKey,
            status: "failed",
          });
        }
      });
    return () => {
      active = false;
      controller.abort();
    };
  }, [
    estimateRequestKey,
    estimateRetryNonce,
    estimateSourceRef,
    onEstimateReadinessChange,
    value.contentMode,
    value.courseId,
    value.mode,
  ]);

  const selectMode = (mode: StudentClassroomMode) => {
    onChange({ ...value, mode });
  };
  const selectContentMode = (contentMode: StudentClassroomContentMode) => {
    onChange({ ...value, contentMode });
  };
  const retryEstimate = () => {
    if (estimateRequestKey === null) return;
    setEstimateResult(current =>
      current?.requestKey === estimateRequestKey ? null : current,
    );
    setEstimateFailureKey(current =>
      current === estimateRequestKey ? null : current,
    );
    onEstimateReadinessChange({
      requestKey: estimateRequestKey,
      status: "loading",
    });
    setEstimateRetryNonce(current => current + 1);
  };
  return (
    <div className="space-y-4 p-3.5" data-testid="student-classroom-config">
      <label className="block space-y-1.5 text-[12px] font-semibold text-[var(--foreground)]">
        {t("studentClassroom.config.course")}
        <select
          value={value.courseId}
          disabled={loading}
          onChange={event => {
            const courseId = event.target.value;
            const option = options.find(item => item.courseId === courseId);
            const contentMode = option?.allowedContentModes.includes(
              "source_grounded",
            )
              ? "source_grounded"
              : option?.allowedContentModes.includes("open_creation")
                ? "open_creation"
                : "source_grounded";
            onChange({ courseId, mode: null, contentMode });
          }}
          className="w-full rounded-lg border border-[var(--border)] bg-[var(--background)] px-3 py-2 text-[12px] font-normal disabled:opacity-50"
        >
          <option value="">
            {loading
              ? t("studentClassroom.config.loadingCourses")
              : t("studentClassroom.config.chooseCourse")}
          </option>
          {options.map(option => (
            <option key={option.courseId} value={option.courseId}>
              {option.title}
            </option>
          ))}
        </select>
      </label>

      <fieldset className="space-y-2">
        <legend className="text-[12px] font-semibold text-[var(--foreground)]">
          {t("studentClassroom.config.length")}
        </legend>
        <div className="grid gap-2 sm:grid-cols-2">
          {(["micro", "full"] as const).map(mode => (
            <label
              key={mode}
              className={`rounded-lg border p-3 transition-colors ${
                value.mode === mode
                  ? "border-[var(--primary)] bg-[var(--primary)]/[0.06]"
                  : "border-[var(--border)]"
              } ${
                selectedOption?.allowedModes.includes(mode)
                  ? "cursor-pointer hover:bg-[var(--muted)]/35"
                  : "cursor-not-allowed opacity-45"
              }`}
            >
              <span className="flex items-start gap-2">
                <input
                  type="radio"
                  name="student-classroom-mode"
                  checked={value.mode === mode}
                  disabled={!selectedOption?.allowedModes.includes(mode)}
                  onChange={() => selectMode(mode)}
                  className="mt-0.5"
                />
                <span>
                  <span className="block text-[12px] font-semibold">
                    {t(`studentClassroom.mode.${mode}`)}
                  </span>
                  <span className="mt-0.5 block text-[10.5px] leading-relaxed text-[var(--muted-foreground)]">
                    {t(`studentClassroom.mode.${mode}.hint`)}
                  </span>
                  {selectedOption && !selectedOption.allowedModes.includes(mode) ? (
                    <span className="mt-1 block text-[10px] text-[var(--muted-foreground)]">
                      {t("studentClassroom.config.unavailableForCourse")}
                    </span>
                  ) : null}
                </span>
              </span>
            </label>
          ))}
        </div>
      </fieldset>

      <section className="space-y-2 rounded-lg border border-[var(--border)]/65 bg-[var(--muted)]/20 p-3">
        <div className="flex items-center gap-2 text-[11.5px] font-semibold">
          <BookOpen className="h-3.5 w-3.5" aria-hidden />
          {t("studentClassroom.config.contentMode")}
        </div>
        <label
          className={`flex items-start gap-2 rounded-md border border-[var(--border)]/60 p-2 ${
            selectedOption?.allowedContentModes.includes("source_grounded")
              ? "cursor-pointer"
              : "cursor-not-allowed opacity-45"
          }`}
        >
          <input
            type="radio"
            name="student-classroom-content-mode"
            checked={value.contentMode === "source_grounded"}
            disabled={
              !selectedOption?.allowedContentModes.includes("source_grounded")
            }
            onChange={() => selectContentMode("source_grounded")}
          />
          <span className="text-[10.5px] leading-relaxed">
            <span className="block font-semibold">
              {t("studentClassroom.config.sourceGrounded")}
            </span>
            <span className="text-[var(--muted-foreground)]">
              {authorizedSourceCount > 0
                ? t("studentClassroom.config.sourceReady", {
                    count: authorizedSourceCount,
                  })
                : t("studentClassroom.config.sourceRequired")}
            </span>
          </span>
        </label>
        {selectedOption?.allowedContentModes.includes("open_creation") ? (
          <label className="flex cursor-pointer items-start gap-2 rounded-md border border-[var(--border)]/60 p-2">
            <input
              type="radio"
              name="student-classroom-content-mode"
              checked={value.contentMode === "open_creation"}
              onChange={() => selectContentMode("open_creation")}
            />
            <span className="text-[10.5px] leading-relaxed">
              <span className="block font-semibold">
                {t("studentClassroom.config.openCreation")}
              </span>
              <span className="text-[var(--muted-foreground)]">
                {t("studentClassroom.config.openCreationReady")}
              </span>
            </span>
          </label>
        ) : (
          <p className="flex items-start gap-1.5 text-[10.5px] leading-relaxed text-[var(--muted-foreground)]">
            <ShieldCheck className="mt-0.5 h-3 w-3 shrink-0" aria-hidden />
            {t("studentClassroom.config.openCreationPolicy")}
          </p>
        )}
      </section>

      {estimateLoading ? (
        <p className="flex items-center gap-1.5 text-[10.5px] text-[var(--muted-foreground)]">
          <LoaderCircle className="h-3.5 w-3.5 animate-spin" aria-hidden />
          {t("studentClassroom.config.estimating")}
        </p>
      ) : estimate ? (
        <dl className="grid grid-cols-2 gap-2 rounded-lg bg-[var(--primary)]/[0.045] p-2.5 text-center sm:grid-cols-4">
          <div>
            <dt className="flex items-center justify-center gap-1 text-[9.5px] text-[var(--muted-foreground)]">
              <Layers3 className="h-3 w-3" aria-hidden />
              {t("studentClassroom.estimate.scenes")}
            </dt>
            <dd className="mt-1 text-[12px] font-semibold">
              {estimate.sceneRange.join("–")}
            </dd>
          </div>
          <div>
            <dt className="flex items-center justify-center gap-1 text-[9.5px] text-[var(--muted-foreground)]">
              <Clock3 className="h-3 w-3" aria-hidden />
              {t("studentClassroom.estimate.minutes")}
            </dt>
            <dd className="mt-1 text-[12px] font-semibold">
              {estimate.durationMinutesRange.join("–")}
            </dd>
          </div>
          <div>
            <dt className="text-[9.5px] text-[var(--muted-foreground)]">
              {t("studentClassroom.estimate.quota")}
            </dt>
            <dd className="mt-1 text-[12px] font-semibold">
              {estimate.quotaUnits}
            </dd>
          </div>
          <div>
            <dt className="text-[9.5px] text-[var(--muted-foreground)]">
              {t("studentClassroom.estimate.approval")}
            </dt>
            <dd className="mt-1 text-[12px] font-semibold">
              {t(
                estimate.requiresApproval
                  ? "studentClassroom.job.approval.required"
                  : "studentClassroom.job.approval.notRequired",
              )}
            </dd>
          </div>
        </dl>
      ) : (
        <p className="text-[10.5px] text-[var(--muted-foreground)]">
          {t("studentClassroom.config.chooseMode")}
        </p>
      )}

      {estimateFailed ? (
        <div className="space-y-1.5">
          <p role="alert" className="text-[10.5px] text-[var(--destructive)]">
            {t("studentClassroom.config.estimateFailed")}
          </p>
          <button
            type="button"
            onClick={retryEstimate}
            className="rounded-md border border-[var(--border)] px-2 py-1 text-[10.5px] font-semibold text-[var(--foreground)] hover:bg-[var(--muted)]/50"
          >
            {t("studentClassroom.config.retryEstimate")}
          </button>
        </div>
      ) : null}

      {error ? (
        <p role="alert" className="text-[10.5px] text-[var(--destructive)]">
          {error}
        </p>
      ) : null}
    </div>
  );
}

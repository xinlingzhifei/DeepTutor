"use client";

import { useEffect, useMemo, useState } from "react";
import { useRouter } from "next/navigation";
import { FileUp, LoaderCircle, Sparkles } from "lucide-react";
import { useTranslation } from "react-i18next";

import {
  classroomNextRoute,
  createTeachingClassroom,
  listTeachingClasses,
  listTeachingCourses,
  listTeachingSources,
  uploadTeachingPdfSource,
  type TeachingClass,
  type TeachingClassroomCreateInput,
  type TeachingContentMode,
  type TeachingCourse,
  type TeachingExportFormat,
  type TeachingMediaPolicy,
  type TeachingSource,
} from "@/lib/teaching-api";
import { createTeachingAttemptRegistry } from "@/lib/teaching-workflow";

const EXPORTS: TeachingExportFormat[] = [
  "classroom_zip",
  "pptx",
  "offline_html",
];

function sourceReference(source: TeachingSource): string {
  return source.sourceType === "pdf" ? source.bindingId : source.sourceId;
}

export function TeachingBriefForm() {
  const { t } = useTranslation();
  const router = useRouter();
  const [courses, setCourses] = useState<TeachingCourse[]>([]);
  const [classes, setClasses] = useState<TeachingClass[]>([]);
  const [sources, setSources] = useState<TeachingSource[]>([]);
  const [courseId, setCourseId] = useState("");
  const [classId, setClassId] = useState("");
  const [title, setTitle] = useState("");
  const [objective, setObjective] = useState("");
  const [gradeBand, setGradeBand] = useState("");
  const [audience, setAudience] = useState("intermediate");
  const [durationMinutes, setDurationMinutes] = useState(45);
  const [contentMode, setContentMode] =
    useState<TeachingContentMode>("source_grounded");
  const [sourceKey, setSourceKey] = useState("");
  const [templateId, setTemplateId] = useState("guided-classroom");
  const [templateVersion, setTemplateVersion] = useState("1");
  const [knowledgePointTitle, setKnowledgePointTitle] = useState("");
  const [knowledgePointDescription, setKnowledgePointDescription] = useState("");
  const [webEnabled, setWebEnabled] = useState(false);
  const [mediaPolicy, setMediaPolicy] = useState<TeachingMediaPolicy>(
    "image_audio",
  );
  const [domains, setDomains] = useState("");
  const [requestedExports, setRequestedExports] = useState<
    TeachingExportFormat[]
  >(["classroom_zip"]);
  const [uploading, setUploading] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [creationAttempts] = useState(() => createTeachingAttemptRegistry());

  useEffect(() => {
    let active = true;
    Promise.all([listTeachingCourses(), listTeachingSources()])
      .then(([nextCourses, nextSources]) => {
        if (!active) return;
        setCourses(nextCourses);
        setSources(nextSources);
        setCourseId(current => current || nextCourses[0]?.id || "");
      })
      .catch(reason => {
        if (active) setError(reason instanceof Error ? reason.message : String(reason));
      });
    return () => {
      active = false;
    };
  }, []);

  useEffect(() => {
    if (!courseId) {
      setClasses([]);
      setClassId("");
      return;
    }
    let active = true;
    listTeachingClasses(courseId)
      .then(next => {
        if (!active) return;
        setClasses(next);
        setClassId(current =>
          next.some(item => item.id === current) ? current : next[0]?.id || "",
        );
      })
      .catch(reason => {
        if (active) setError(reason instanceof Error ? reason.message : String(reason));
      });
    return () => {
      active = false;
    };
  }, [courseId]);

  const scopedSources = useMemo(
    () =>
      sources.filter(
        source =>
          (!source.courseId || source.courseId === courseId) &&
          (!source.classId || source.classId === classId),
      ),
    [classId, courseId, sources],
  );
  const selectedSource = scopedSources.find(
    source => `${source.sourceType}:${source.bindingId}` === sourceKey,
  );
  const estimatedScenes = Math.max(3, Math.ceil(durationMinutes / 8));
  const estimatedMinutes = Math.max(2, Math.ceil(estimatedScenes * 0.8));
  const estimatedQuota = estimatedScenes * 2 + requestedExports.length;

  const toggleExport = (format: TeachingExportFormat) => {
    setRequestedExports(current => {
      if (format === "classroom_zip") return current;
      return current.includes(format)
        ? current.filter(item => item !== format)
        : [...current, format];
    });
  };

  const uploadPdf = async (file: File | null) => {
    if (!file || !courseId) return;
    setUploading(true);
    setError(null);
    try {
      const source = await uploadTeachingPdfSource(file, courseId, classId || null);
      setSources(current => [source, ...current]);
      setContentMode("source_grounded");
      setSourceKey(`${source.sourceType}:${source.bindingId}`);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setUploading(false);
    }
  };

  const submit = async (event: React.FormEvent) => {
    event.preventDefault();
    if (!courseId || !classId || !title.trim() || !objective.trim()) return;
    if (contentMode === "source_grounded" && !selectedSource) {
      setError(t("teaching.brief.sourceRequired"));
      return;
    }
    setSubmitting(true);
    setError(null);
    const pointTitle = knowledgePointTitle.trim() || objective.trim();
    const sourceType = selectedSource?.sourceType ?? null;
    const input: TeachingClassroomCreateInput = {
      title: title.trim(),
      courseId,
      classId,
      objective: objective.trim(),
      gradeBand: gradeBand.trim() || "unspecified",
      audience,
      durationMinutes,
      classroomMode: "full",
      webPolicy: webEnabled ? "enabled" : "disabled",
      mediaPolicy,
      allowedWebDomains: webEnabled
        ? domains
            .split(",")
            .map(item => item.trim())
            .filter(Boolean)
        : [],
      templateId: templateId.trim(),
      templateVersion: templateVersion.trim(),
      knowledgePoints: [
        {
          knowledgePointId: `kp-${pointTitle
            .toLowerCase()
            .replace(/[^a-z0-9\u4e00-\u9fff]+/g, "-")
            .replace(/^-|-$/g, "") || "primary"}`,
          title: pointTitle,
          description: knowledgePointDescription.trim() || objective.trim(),
        },
      ],
      contentMode,
      openCreationAcknowledged: contentMode === "open_creation",
      sourceType: contentMode === "source_grounded" ? sourceType : null,
      sourceRef:
        contentMode === "source_grounded" && selectedSource
          ? sourceReference(selectedSource)
          : null,
      requestedExports,
    };
    const requestFingerprint = JSON.stringify(input);
    try {
      const classroom = await createTeachingClassroom(
        input,
        creationAttempts.keyFor(requestFingerprint),
      );
      creationAttempts.settle(requestFingerprint);
      router.push(
        classroomNextRoute({
          assetId: classroom.assetId,
          status: classroom.status,
        }),
      );
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <form onSubmit={submit} className="space-y-6" data-testid="teaching-brief-form">
      <section className="grid gap-4 rounded-2xl border border-[var(--border)] bg-[var(--card)] p-5 md:grid-cols-2">
        <label className="space-y-1.5 text-sm font-medium">
          {t("teaching.brief.course")}
          <select
            value={courseId}
            onChange={event => setCourseId(event.target.value)}
            required
            className="w-full rounded-lg border border-[var(--border)] bg-[var(--background)] px-3 py-2"
          >
            <option value="">{t("teaching.common.select")}</option>
            {courses.map(course => (
              <option key={course.id} value={course.id}>
                {course.title}
              </option>
            ))}
          </select>
        </label>
        <label className="space-y-1.5 text-sm font-medium">
          {t("teaching.brief.class")}
          <select
            value={classId}
            onChange={event => setClassId(event.target.value)}
            required
            className="w-full rounded-lg border border-[var(--border)] bg-[var(--background)] px-3 py-2"
          >
            <option value="">{t("teaching.common.select")}</option>
            {classes.map(item => (
              <option key={item.id} value={item.id}>
                {item.name}
              </option>
            ))}
          </select>
        </label>
        <label className="space-y-1.5 text-sm font-medium md:col-span-2">
          {t("teaching.brief.title")}
          <input
            value={title}
            onChange={event => setTitle(event.target.value)}
            required
            maxLength={255}
            className="w-full rounded-lg border border-[var(--border)] bg-[var(--background)] px-3 py-2"
          />
        </label>
        <label className="space-y-1.5 text-sm font-medium md:col-span-2">
          {t("teaching.brief.objective")}
          <textarea
            value={objective}
            onChange={event => setObjective(event.target.value)}
            required
            rows={3}
            className="w-full rounded-lg border border-[var(--border)] bg-[var(--background)] px-3 py-2"
          />
        </label>
        <label className="space-y-1.5 text-sm font-medium">
          {t("teaching.brief.gradeBand")}
          <input
            value={gradeBand}
            onChange={event => setGradeBand(event.target.value)}
            className="w-full rounded-lg border border-[var(--border)] bg-[var(--background)] px-3 py-2"
          />
        </label>
        <label className="space-y-1.5 text-sm font-medium">
          {t("teaching.brief.audience")}
          <select
            value={audience}
            onChange={event => setAudience(event.target.value)}
            className="w-full rounded-lg border border-[var(--border)] bg-[var(--background)] px-3 py-2"
          >
            <option value="beginner">{t("teaching.brief.beginner")}</option>
            <option value="intermediate">{t("teaching.brief.intermediate")}</option>
            <option value="advanced">{t("teaching.brief.advanced")}</option>
          </select>
        </label>
        <label className="space-y-1.5 text-sm font-medium">
          {t("teaching.brief.duration")}
          <input
            type="number"
            min={1}
            max={600}
            value={durationMinutes}
            onChange={event => setDurationMinutes(Number(event.target.value))}
            className="w-full rounded-lg border border-[var(--border)] bg-[var(--background)] px-3 py-2"
          />
        </label>
        <label className="space-y-1.5 text-sm font-medium">
          {t("teaching.brief.mediaPolicy")}
          <select
            value={mediaPolicy}
            onChange={event =>
              setMediaPolicy(event.target.value as TeachingMediaPolicy)
            }
            className="w-full rounded-lg border border-[var(--border)] bg-[var(--background)] px-3 py-2"
          >
            <option value="text_only">
              {t("teaching.brief.mediaPolicyTextOnly")}
            </option>
            <option value="image_audio">
              {t("teaching.brief.mediaPolicyImageAudio")}
            </option>
          </select>
          <span className="block font-normal text-[var(--muted-foreground)]">
            {t(`teaching.brief.mediaPolicyHint.${mediaPolicy}`)}
          </span>
        </label>
      </section>

      <section className="space-y-4 rounded-2xl border border-[var(--border)] bg-[var(--card)] p-5">
        <h2 className="font-semibold">{t("teaching.brief.sourceTitle")}</h2>
        <div className="flex flex-wrap gap-3">
          {(["source_grounded", "open_creation"] as const).map(mode => (
            <label key={mode} className="flex items-center gap-2 text-sm">
              <input
                type="radio"
                checked={contentMode === mode}
                onChange={() => setContentMode(mode)}
              />
              {t(`teaching.brief.mode.${mode}`)}
            </label>
          ))}
        </div>
        {contentMode === "source_grounded" ? (
          <div className="grid gap-3 md:grid-cols-[1fr_auto]">
            <select
              aria-label={t("teaching.brief.source")}
              value={sourceKey}
              onChange={event => setSourceKey(event.target.value)}
              required
              className="rounded-lg border border-[var(--border)] bg-[var(--background)] px-3 py-2 text-sm"
            >
              <option value="">{t("teaching.brief.chooseSource")}</option>
              {scopedSources.map(source => (
                <option
                  key={`${source.sourceType}:${source.bindingId}`}
                  value={`${source.sourceType}:${source.bindingId}`}
                >
                  {source.filename || source.sourceId} · {source.sourceType}
                </option>
              ))}
            </select>
            <label className="inline-flex cursor-pointer items-center justify-center gap-2 rounded-lg border border-[var(--border)] px-4 py-2 text-sm font-medium hover:bg-[var(--muted)]">
              {uploading ? (
                <LoaderCircle className="h-4 w-4 animate-spin" aria-hidden />
              ) : (
                <FileUp className="h-4 w-4" aria-hidden />
              )}
              {t("teaching.brief.uploadPdf")}
              <input
                type="file"
                accept="application/pdf,.pdf"
                className="sr-only"
                disabled={uploading || !courseId}
                onChange={event => void uploadPdf(event.target.files?.[0] ?? null)}
              />
            </label>
          </div>
        ) : (
          <p className="rounded-lg bg-[var(--muted)]/40 p-3 text-sm text-[var(--muted-foreground)]">
            {t("teaching.brief.openCreationAck")}
          </p>
        )}
      </section>

      <section className="grid gap-4 rounded-2xl border border-[var(--border)] bg-[var(--card)] p-5 md:grid-cols-2">
        <label className="space-y-1.5 text-sm font-medium">
          {t("teaching.brief.knowledgePoint")}
          <input
            value={knowledgePointTitle}
            onChange={event => setKnowledgePointTitle(event.target.value)}
            className="w-full rounded-lg border border-[var(--border)] bg-[var(--background)] px-3 py-2"
          />
        </label>
        <label className="space-y-1.5 text-sm font-medium">
          {t("teaching.brief.knowledgeDescription")}
          <input
            value={knowledgePointDescription}
            onChange={event => setKnowledgePointDescription(event.target.value)}
            className="w-full rounded-lg border border-[var(--border)] bg-[var(--background)] px-3 py-2"
          />
        </label>
        <label className="space-y-1.5 text-sm font-medium">
          {t("teaching.brief.template")}
          <input
            value={templateId}
            onChange={event => setTemplateId(event.target.value)}
            required
            className="w-full rounded-lg border border-[var(--border)] bg-[var(--background)] px-3 py-2"
          />
        </label>
        <label className="space-y-1.5 text-sm font-medium">
          {t("teaching.brief.templateVersion")}
          <input
            value={templateVersion}
            onChange={event => setTemplateVersion(event.target.value)}
            required
            className="w-full rounded-lg border border-[var(--border)] bg-[var(--background)] px-3 py-2"
          />
        </label>
        <label className="flex items-center gap-2 text-sm font-medium">
          <input
            type="checkbox"
            checked={webEnabled}
            onChange={event => setWebEnabled(event.target.checked)}
          />
          {t("teaching.brief.allowWeb")}
        </label>
        <label className="space-y-1.5 text-sm font-medium">
          {t("teaching.brief.domains")}
          <input
            value={domains}
            disabled={!webEnabled}
            onChange={event => setDomains(event.target.value)}
            className="w-full rounded-lg border border-[var(--border)] bg-[var(--background)] px-3 py-2 disabled:opacity-50"
          />
        </label>
        <fieldset className="space-y-2 md:col-span-2">
          <legend className="text-sm font-medium">{t("teaching.brief.exports")}</legend>
          <div className="flex flex-wrap gap-4">
            {EXPORTS.map(format => (
              <label key={format} className="flex items-center gap-2 text-sm">
                <input
                  type="checkbox"
                  checked={requestedExports.includes(format)}
                  disabled={format === "classroom_zip"}
                  onChange={() => toggleExport(format)}
                />
                {format}
              </label>
            ))}
          </div>
        </fieldset>
      </section>

      <section className="rounded-2xl border border-[var(--primary)]/25 bg-[var(--primary)]/5 p-5">
        <div className="flex items-center gap-2 font-semibold">
          <Sparkles className="h-4 w-4" aria-hidden />
          {t("teaching.brief.estimateTitle")}
        </div>
        <dl className="mt-3 grid gap-3 text-sm sm:grid-cols-3">
          <div>
            <dt className="text-[var(--muted-foreground)]">{t("teaching.brief.estimateScenes")}</dt>
            <dd className="text-lg font-semibold">{estimatedScenes}</dd>
          </div>
          <div>
            <dt className="text-[var(--muted-foreground)]">{t("teaching.brief.estimateTime")}</dt>
            <dd className="text-lg font-semibold">
              {t("teaching.brief.estimateMinutesValue", { count: estimatedMinutes })}
            </dd>
          </div>
          <div>
            <dt className="text-[var(--muted-foreground)]">{t("teaching.brief.estimateQuota")}</dt>
            <dd className="text-lg font-semibold">
              {t("teaching.brief.estimateQuotaValue", { count: estimatedQuota })}
            </dd>
          </div>
        </dl>
      </section>

      {error ? (
        <p role="alert" className="text-sm text-[var(--destructive)]">
          {error}
        </p>
      ) : null}
      <button
        type="submit"
        disabled={submitting || uploading}
        className="inline-flex items-center gap-2 rounded-lg bg-[var(--primary)] px-5 py-2.5 text-sm font-semibold text-[var(--primary-foreground)] disabled:opacity-50"
      >
        {submitting ? <LoaderCircle className="h-4 w-4 animate-spin" aria-hidden /> : null}
        {t("teaching.brief.generateOutline")}
      </button>
    </form>
  );
}

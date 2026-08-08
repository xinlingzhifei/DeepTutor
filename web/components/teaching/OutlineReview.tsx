"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import { useRouter } from "next/navigation";
import { CheckCircle2, LoaderCircle, Save } from "lucide-react";
import { useTranslation } from "react-i18next";

import {
  canonicalOutlineSha256,
  classroomNextRoute,
  confirmTeachingOutline,
  updateTeachingOutline,
  type TeachingClassroom,
} from "@/lib/teaching-api";
import type { JsonObject } from "@/lib/openmaic-adapter/contracts";
import { shouldApplyOutlineResponse } from "@/lib/teaching-workflow";

function parseOutlineText(value: string): JsonObject | null {
  try {
    const parsed = JSON.parse(value) as unknown;
    return parsed !== null && typeof parsed === "object" && !Array.isArray(parsed)
      ? (parsed as JsonObject)
      : null;
  } catch {
    return null;
  }
}

export function OutlineReview({
  classroom: initialClassroom,
}: {
  classroom: TeachingClassroom;
}) {
  const { t } = useTranslation();
  const router = useRouter();
  const [classroom, setClassroom] = useState(initialClassroom);
  const [outlineText, setOutlineText] = useState(() =>
    JSON.stringify(initialClassroom.outline ?? {}, null, 2),
  );
  const [outlineHash, setOutlineHash] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [confirming, setConfirming] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const outlineTextRef = useRef(outlineText);
  const editEpochRef = useRef(0);
  const operationRef = useRef<"save" | "confirm" | null>(null);

  const parsed = useMemo(() => parseOutlineText(outlineText), [outlineText]);

  useEffect(() => {
    let active = true;
    if (!parsed) {
      setOutlineHash(null);
      return;
    }
    canonicalOutlineSha256(parsed)
      .then(hash => {
        if (active) setOutlineHash(hash);
      })
      .catch(() => {
        if (active) setOutlineHash(null);
      });
    return () => {
      active = false;
    };
  }, [parsed]);

  const persistOutline = async (): Promise<TeachingClassroom | null> => {
    const requestText = outlineTextRef.current;
    const requestEpoch = editEpochRef.current;
    const requestOutline = parseOutlineText(requestText);
    if (!requestOutline) {
      setError(t("teaching.outline.invalidJson"));
      return null;
    }
    setError(null);
    try {
      const next = await updateTeachingOutline(
        classroom.assetId,
        requestOutline,
        classroom.revision,
      );
      const responseIsCurrent = shouldApplyOutlineResponse({
        requestText,
        currentText: outlineTextRef.current,
        requestEpoch,
        currentEpoch: editEpochRef.current,
      });
      setClassroom(next);
      if (!responseIsCurrent) {
        setError(t("teaching.outline.staleResponse"));
        return null;
      }
      const nextText = JSON.stringify(next.outline ?? requestOutline, null, 2);
      outlineTextRef.current = nextText;
      setOutlineText(nextText);
      return next;
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
      return null;
    }
  };

  const save = async (): Promise<TeachingClassroom | null> => {
    if (operationRef.current) return null;
    operationRef.current = "save";
    setSaving(true);
    try {
      return await persistOutline();
    } finally {
      operationRef.current = null;
      setSaving(false);
    }
  };

  const confirm = async () => {
    if (operationRef.current) return;
    operationRef.current = "confirm";
    setConfirming(true);
    setError(null);
    try {
      const saved = await persistOutline();
      if (!saved) return;
      const next = await confirmTeachingOutline(saved.assetId);
      router.push(
        classroomNextRoute({ assetId: next.assetId, status: next.status }),
      );
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      operationRef.current = null;
      setConfirming(false);
    }
  };

  return (
    <section className="grid gap-5 lg:grid-cols-[minmax(0,1fr)_20rem]">
      <div className="rounded-2xl border border-[var(--border)] bg-[var(--card)] p-5">
        <div className="mb-4 flex flex-wrap items-start justify-between gap-3">
          <div>
            <h1 className="text-xl font-semibold">{classroom.title}</h1>
            <p className="mt-1 text-sm text-[var(--muted-foreground)]">
              {t("teaching.outline.description")}
            </p>
          </div>
          <span className="rounded-full bg-[var(--muted)] px-3 py-1 text-xs font-medium">
            {t("teaching.outline.revision", { revision: classroom.revision })}
          </span>
        </div>
        <label className="space-y-2 text-sm font-medium">
          {t("teaching.outline.json")}
          <textarea
            value={outlineText}
            onChange={event => {
              const next = event.target.value;
              outlineTextRef.current = next;
              editEpochRef.current += 1;
              setOutlineText(next);
            }}
            disabled={saving || confirming}
            rows={24}
            spellCheck={false}
            className="w-full rounded-xl border border-[var(--border)] bg-[var(--background)] p-4 font-mono text-sm leading-6 disabled:opacity-60"
          />
        </label>
        {error ? (
          <p role="alert" className="mt-3 text-sm text-[var(--destructive)]">
            {error}
          </p>
        ) : null}
      </div>
      <aside className="space-y-4">
        <div className="rounded-2xl border border-[var(--border)] bg-[var(--card)] p-5">
          <h2 className="font-semibold">{t("teaching.outline.binding")}</h2>
          <dl className="mt-3 space-y-3 text-sm">
            <div>
              <dt className="text-[var(--muted-foreground)]">{t("teaching.outline.courseClass")}</dt>
              <dd className="font-medium">{classroom.courseId} / {classroom.classId}</dd>
            </div>
            <div>
              <dt className="text-[var(--muted-foreground)]">
                {t("teaching.outline.sha256")}
              </dt>
              <dd className="break-all font-mono text-xs">
                {outlineHash ?? t("teaching.outline.invalidJson")}
              </dd>
            </div>
          </dl>
        </div>
        <button
          type="button"
          onClick={() => void save()}
          disabled={!parsed || saving || confirming}
          className="inline-flex w-full items-center justify-center gap-2 rounded-lg border border-[var(--border)] px-4 py-2.5 text-sm font-semibold disabled:opacity-50"
        >
          {saving ? <LoaderCircle className="h-4 w-4 animate-spin" aria-hidden /> : <Save className="h-4 w-4" aria-hidden />}
          {t("teaching.outline.save")}
        </button>
        <button
          type="button"
          onClick={() => void confirm()}
          disabled={!parsed || saving || confirming}
          className="inline-flex w-full items-center justify-center gap-2 rounded-lg bg-[var(--primary)] px-4 py-2.5 text-sm font-semibold text-[var(--primary-foreground)] disabled:opacity-50"
        >
          {confirming ? <LoaderCircle className="h-4 w-4 animate-spin" aria-hidden /> : <CheckCircle2 className="h-4 w-4" aria-hidden />}
          {t("teaching.outline.confirm")}
        </button>
        <p className="text-xs leading-5 text-[var(--muted-foreground)]">
          {t("teaching.outline.confirmHint")}
        </p>
      </aside>
    </section>
  );
}

"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { useParams } from "next/navigation";
import { FileUp, LoaderCircle, Send, ShieldCheck } from "lucide-react";
import { useTranslation } from "react-i18next";

import {
  ClassroomEditor,
  type ClassroomEditorHandle,
} from "@/components/classroom/ClassroomEditor";
import { ClassroomExportMenu } from "@/components/classroom/ClassroomExportMenu";
import { ImportClassroomDialog } from "@/components/classroom/ImportClassroomDialog";
import { ValidationReport } from "@/components/teaching/ValidationReport";
import {
  classroomRevisionEtag,
  getTeachingClassroom,
  submitTeachingClassroom,
  validateTeachingClassroom,
  type TeachingClassroom,
} from "@/lib/teaching-api";
import {
  createTeachingAttemptRegistry,
  isCurrentTeachingOperation,
  isTeachingClassroomEditable,
  type TeachingOperationSnapshot,
} from "@/lib/teaching-workflow";

type SubmissionScope = "class" | "tenant";
type ActiveOperation = "validate" | "submit";
interface ActiveOperationState {
  kind: ActiveOperation;
  token: number;
}

export default function TeachingClassroomEditPage() {
  const { t } = useTranslation();
  const params = useParams<{ assetId: string }>();
  const assetId = params?.assetId;
  const editorRef = useRef<ClassroomEditorHandle>(null);
  const classroomRef = useRef<TeachingClassroom | null>(null);
  const revisionRef = useRef("");
  const operationEpochRef = useRef(0);
  const operationTokenRef = useRef(0);
  const operationRef = useRef<ActiveOperationState | null>(null);
  const [classroom, setClassroom] = useState<TeachingClassroom | null>(null);
  const [revision, setRevision] = useState("");
  const [editorDirty, setEditorDirty] = useState(false);
  const [importOpen, setImportOpen] = useState(false);
  const [importSummary, setImportSummary] = useState<string | null>(null);
  const [submissionScope, setSubmissionScope] =
    useState<SubmissionScope>("class");
  const [validating, setValidating] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [submissionComplete, setSubmissionComplete] = useState(false);
  const [workflowMessage, setWorkflowMessage] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [submitAttempts] = useState(() => createTeachingAttemptRegistry());
  const operationLocked = validating || submitting;
  const workflowFrozen =
    operationLocked ||
    submissionComplete ||
    Boolean(classroom && !isTeachingClassroomEditable(classroom.lifecycleState));

  const applyClassroom = useCallback((next: TeachingClassroom) => {
    const nextRevision = classroomRevisionEtag(next.revision);
    classroomRef.current = next;
    revisionRef.current = nextRevision;
    setClassroom(next);
    setRevision(nextRevision);
  }, []);

  const currentOperationSnapshot = (): TeachingOperationSnapshot | null => {
    const current = classroomRef.current;
    if (!current) return null;
    return {
      assetId: current.assetId,
      draftId: current.draftId,
      revision: revisionRef.current,
      epoch: operationEpochRef.current,
    };
  };

  const handleEditorDirtyChange = useCallback((dirty: boolean) => {
    setEditorDirty(current => {
      if (dirty && !current) operationEpochRef.current += 1;
      return dirty;
    });
    if (!dirty) return;
    const current = classroomRef.current;
    if (!current?.validationReport) return;
    const next = { ...current, validationReport: null };
    classroomRef.current = next;
    setClassroom(next);
    setWorkflowMessage(null);
  }, []);

  useEffect(() => {
    operationEpochRef.current += 1;
    operationTokenRef.current += 1;
    operationRef.current = null;
    classroomRef.current = null;
    revisionRef.current = "";
    setClassroom(null);
    setRevision("");
    setEditorDirty(false);
    setImportOpen(false);
    setImportSummary(null);
    setSubmissionScope("class");
    setValidating(false);
    setSubmitting(false);
    setSubmissionComplete(false);
    setWorkflowMessage(null);
    setError(null);
    if (!assetId) return;
    let active = true;
    getTeachingClassroom(assetId, { draft: true })
      .then(next => {
        if (!active) return;
        applyClassroom(next);
      })
      .catch(reason => {
        if (active) setError(reason instanceof Error ? reason.message : String(reason));
      });
    return () => {
      active = false;
    };
  }, [applyClassroom, assetId]);

  const validate = async () => {
    if (operationRef.current || editorDirty || submissionComplete) return;
    const expected = currentOperationSnapshot();
    if (!expected) return;
    const token = operationTokenRef.current + 1;
    operationTokenRef.current = token;
    operationRef.current = { kind: "validate", token };
    setValidating(true);
    setImportOpen(false);
    setError(null);
    try {
      const next = await validateTeachingClassroom(expected.assetId);
      const current = currentOperationSnapshot();
      if (
        operationRef.current?.token !== token ||
        !current ||
        !isCurrentTeachingOperation(expected, current)
      ) {
        if (current?.assetId === expected.assetId) {
          setError(t("teaching.editor.staleResponse"));
        }
        return;
      }
      applyClassroom(next);
    } catch (reason) {
      if (operationRef.current?.token === token) {
        setError(reason instanceof Error ? reason.message : String(reason));
      }
    } finally {
      if (operationRef.current?.token === token) {
        operationRef.current = null;
        setValidating(false);
      }
    }
  };

  const submit = async () => {
    if (operationRef.current || editorDirty || submissionComplete) return;
    const expected = currentOperationSnapshot();
    const currentClassroom = classroomRef.current;
    if (!expected || !currentClassroom?.validationReport?.valid) return;
    const classId = submissionScope === "class" ? currentClassroom.classId : null;
    const submissionFingerprint = JSON.stringify([
      expected.draftId,
      expected.revision,
      submissionScope,
      classId,
    ]);
    const token = operationTokenRef.current + 1;
    operationTokenRef.current = token;
    operationRef.current = { kind: "submit", token };
    setSubmitting(true);
    setImportOpen(false);
    setError(null);
    setWorkflowMessage(null);
    try {
      const review = await submitTeachingClassroom(
        expected.assetId,
        { scope: submissionScope, classId },
        submitAttempts.keyFor(submissionFingerprint),
      );
      if (operationRef.current?.token !== token) return;
      setSubmissionComplete(true);
      setImportOpen(false);
      submitAttempts.settle(submissionFingerprint);
      const current = currentOperationSnapshot();
      if (!current || !isCurrentTeachingOperation(expected, current)) {
        setError(t("teaching.editor.staleResponse"));
        return;
      }
      setWorkflowMessage(
        t("teaching.editor.submitted", { id: review.id, status: review.status }),
      );
    } catch (reason) {
      if (operationRef.current?.token === token) {
        setError(reason instanceof Error ? reason.message : String(reason));
      }
    } finally {
      if (operationRef.current?.token === token) {
        operationRef.current = null;
        setSubmitting(false);
      }
    }
  };

  if (error && !classroom) {
    return <p role="alert" className="text-sm text-[var(--destructive)]">{error}</p>;
  }
  if (!classroom) {
    return (
      <div className="flex items-center gap-2 p-6 text-sm text-[var(--muted-foreground)]">
        <LoaderCircle className="h-4 w-4 animate-spin" aria-hidden />
        {t("teaching.common.loading")}
      </div>
    );
  }
  if (!classroom.document) {
    return (
      <div className="rounded-2xl border border-dashed border-[var(--border)] p-8 text-center">
        <h1 className="font-semibold">{classroom.title}</h1>
        <p className="mt-2 text-sm text-[var(--muted-foreground)]">
          {t("teaching.editor.documentPending")}
        </p>
      </div>
    );
  }

  return (
    <div className="space-y-4">
      <header className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">{classroom.title}</h1>
          <p className="mt-1 text-sm text-[var(--muted-foreground)]">
            {classroom.courseId} / {classroom.classId} · {classroom.lifecycleState}
          </p>
        </div>
        <div className="flex flex-wrap items-end gap-2">
          <label className="space-y-1 text-xs font-medium">
            <span className="block text-[var(--muted-foreground)]">
              {t("teaching.editor.submissionScope")}
            </span>
            <select
              value={submissionScope}
              onChange={event => {
                setSubmissionScope(event.target.value as SubmissionScope);
                setWorkflowMessage(null);
              }}
              disabled={workflowFrozen}
              className="rounded-lg border border-[var(--border)] bg-[var(--background)] px-3 py-2 text-sm disabled:opacity-50"
            >
              <option value="class">{t("teaching.editor.scopeClass")}</option>
              <option value="tenant">{t("teaching.editor.scopeTenant")}</option>
            </select>
          </label>
          <button
            type="button"
            onClick={() => {
              if (!workflowFrozen) setImportOpen(true);
            }}
            disabled={workflowFrozen}
            className="inline-flex items-center gap-2 rounded-lg border border-[var(--border)] px-3 py-2 text-sm font-medium disabled:opacity-50"
          >
            <FileUp className="h-4 w-4" aria-hidden />
            {t("classroom.import.action")}
          </button>
          <button
            type="button"
            onClick={() => void validate()}
            disabled={workflowFrozen || editorDirty}
            className="inline-flex items-center gap-2 rounded-lg border border-[var(--border)] px-3 py-2 text-sm font-semibold disabled:opacity-50"
          >
            {validating ? (
              <LoaderCircle className="h-4 w-4 animate-spin" aria-hidden />
            ) : (
              <ShieldCheck className="h-4 w-4" aria-hidden />
            )}
            {t("teaching.editor.validate")}
          </button>
          <button
            type="button"
            onClick={() => void submit()}
            disabled={
              workflowFrozen ||
              editorDirty ||
              !classroom.validationReport?.valid
            }
            className="inline-flex items-center gap-2 rounded-lg bg-[var(--primary)] px-3 py-2 text-sm font-semibold text-[var(--primary-foreground)] disabled:opacity-50"
          >
            {submitting ? (
              <LoaderCircle className="h-4 w-4 animate-spin" aria-hidden />
            ) : (
              <Send className="h-4 w-4" aria-hidden />
            )}
            {t("teaching.editor.submit")}
          </button>
        </div>
      </header>

      <ClassroomEditor
        key={`${classroom.draftId}:${revision}`}
        ref={editorRef}
        initialDocument={classroom.document}
        initialRevision={revision}
        draftMediaAssetId={classroom.assetId}
        disabled={workflowFrozen}
        onDirtyChange={handleEditorDirtyChange}
        onSaved={(document, nextRevision) => {
          const current = classroomRef.current;
          if (
            !current ||
            current.assetId !== classroom.assetId ||
            operationRef.current ||
            submissionComplete
          ) return;
          const next = { ...current, document, validationReport: null };
          operationEpochRef.current += 1;
          classroomRef.current = next;
          revisionRef.current = nextRevision;
          setClassroom(next);
          setEditorDirty(false);
          setRevision(nextRevision);
          setWorkflowMessage(null);
        }}
      />

      <div className="grid gap-4 lg:grid-cols-[minmax(0,1fr)_24rem]">
        <section className="rounded-2xl border border-[var(--border)] bg-[var(--background)] p-5">
          <ValidationReport report={classroom.validationReport} />
        </section>
        <ClassroomExportMenu
          target={{ kind: "draft", assetId: classroom.assetId, revision }}
          policy={{ mp4Enabled: false }}
          disabled={editorDirty || operationLocked}
        />
      </div>

      {importSummary ? (
        <p role="status" className="text-sm text-emerald-700 dark:text-emerald-300">
          {importSummary}
        </p>
      ) : null}
      {workflowMessage ? (
        <p role="status" className="text-sm text-emerald-700 dark:text-emerald-300">
          {workflowMessage}
        </p>
      ) : null}
      {error ? (
        <p role="alert" className="text-sm text-[var(--destructive)]">{error}</p>
      ) : null}

      <ImportClassroomDialog
        isOpen={importOpen && !workflowFrozen}
        assetId={classroom.assetId}
        onClose={() => setImportOpen(false)}
        onImported={result => {
          if (operationRef.current || submissionComplete) {
            setError(t("teaching.editor.operationLocked"));
            return;
          }
          const editor = editorRef.current;
          if (!editor) {
            throw new Error("Classroom editor is unavailable");
          }
          editor.importSlides(result);
          setImportSummary(
            t("teaching.editor.imported", {
              slides: result.slides.length,
              media: result.media.length,
            }),
          );
        }}
      />
    </div>
  );
}

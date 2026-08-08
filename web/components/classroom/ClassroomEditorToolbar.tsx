"use client";

import { useTranslation } from "react-i18next";

export type ClassroomEditorSaveState =
  | { status: "idle" }
  | { status: "saving" }
  | { status: "saved" }
  | { status: "error"; message: string }
  | {
      status: "conflict";
      clientRevision: string;
      serverRevision: string;
    };

export interface ClassroomEditorToolbarProps {
  canUndo: boolean;
  canRedo: boolean;
  dirty: boolean;
  disabled?: boolean;
  saveState: ClassroomEditorSaveState;
  onUndo(): void;
  onRedo(): void;
  onSave(): void;
}

export function ClassroomEditorToolbar({
  canUndo,
  canRedo,
  dirty,
  disabled = false,
  saveState,
  onUndo,
  onRedo,
  onSave,
}: ClassroomEditorToolbarProps) {
  const { t } = useTranslation();
  const saving = saveState.status === "saving";
  return (
    <div className="flex flex-wrap items-center gap-2 border-b border-[var(--border)] bg-[var(--card)] px-4 py-2">
      <button
        type="button"
        disabled={disabled || !canUndo || saving}
        onClick={onUndo}
        className="rounded-md border border-[var(--border)] px-3 py-1.5 text-sm disabled:opacity-40"
      >
        {t("classroom.editor.undo")}
      </button>
      <button
        type="button"
        disabled={disabled || !canRedo || saving}
        onClick={onRedo}
        className="rounded-md border border-[var(--border)] px-3 py-1.5 text-sm disabled:opacity-40"
      >
        {t("classroom.editor.redo")}
      </button>
      <button
        type="button"
        disabled={disabled || !dirty || saving}
        onClick={onSave}
        className="rounded-md bg-[var(--primary)] px-3 py-1.5 text-sm text-[var(--primary-foreground)] disabled:opacity-40"
      >
        {saving
          ? t("classroom.editor.saving")
          : t("classroom.editor.saveDraft")}
      </button>
      <span className="text-xs text-[var(--muted-foreground)]" aria-live="polite">
        {saveState.status === "saved" && t("classroom.editor.draftSaved")}
        {saveState.status === "error" && saveState.message}
        {saveState.status === "conflict" &&
          t("classroom.editor.draftConflict", {
            local: saveState.clientRevision,
            server: saveState.serverRevision,
          })}
        {saveState.status === "idle" &&
          (dirty
            ? t("classroom.editor.unsavedChanges")
            : t("classroom.editor.upToDate"))}
      </span>
    </div>
  );
}

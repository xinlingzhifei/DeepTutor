"use client";

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
  saveState: ClassroomEditorSaveState;
  onUndo(): void;
  onRedo(): void;
  onSave(): void;
}

export function ClassroomEditorToolbar({
  canUndo,
  canRedo,
  dirty,
  saveState,
  onUndo,
  onRedo,
  onSave,
}: ClassroomEditorToolbarProps) {
  const saving = saveState.status === "saving";
  return (
    <div className="flex flex-wrap items-center gap-2 border-b border-[var(--border)] bg-[var(--card)] px-4 py-2">
      <button
        type="button"
        disabled={!canUndo || saving}
        onClick={onUndo}
        className="rounded-md border border-[var(--border)] px-3 py-1.5 text-sm disabled:opacity-40"
      >
        Undo
      </button>
      <button
        type="button"
        disabled={!canRedo || saving}
        onClick={onRedo}
        className="rounded-md border border-[var(--border)] px-3 py-1.5 text-sm disabled:opacity-40"
      >
        Redo
      </button>
      <button
        type="button"
        disabled={!dirty || saving}
        onClick={onSave}
        className="rounded-md bg-[var(--primary)] px-3 py-1.5 text-sm text-[var(--primary-foreground)] disabled:opacity-40"
      >
        {saving ? "Saving…" : "Save draft"}
      </button>
      <span className="text-xs text-[var(--muted-foreground)]" aria-live="polite">
        {saveState.status === "saved" && "Draft saved"}
        {saveState.status === "error" && saveState.message}
        {saveState.status === "conflict" &&
          `Draft conflict: local ${saveState.clientRevision}, server ${saveState.serverRevision}`}
        {saveState.status === "idle" && (dirty ? "Unsaved changes" : "Up to date")}
      </span>
    </div>
  );
}

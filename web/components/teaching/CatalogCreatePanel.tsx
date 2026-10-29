"use client";

import { useRef, useState } from "react";
import { useTranslation } from "react-i18next";

export function CatalogCreatePanel({
  kind, onCreate, onCancel,
}: {
  kind: "course" | "class";
  onCreate: (id: string, name: string) => Promise<void>;
  onCancel: () => void;
}) {
  const { t } = useTranslation();
  const [name, setName] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const attempt = useRef<{ id: string; name: string } | null>(null);
  const submitting = useRef(false);
  const create = async () => {
    const trimmed = name.trim();
    if (!trimmed || submitting.current) return;
    submitting.current = true;
    setBusy(true);
    setError("");
    if (!attempt.current || attempt.current.name !== trimmed) {
      attempt.current = { id: `${kind}-${crypto.randomUUID()}`, name: trimmed };
    }
    try {
      await onCreate(attempt.current.id, trimmed);
    } catch {
      setError(t("Could not create this item. Check your permissions and connection, then retry."));
    } finally {
      submitting.current = false;
      setBusy(false);
    }
  };
  return (
    <fieldset disabled={busy} className="space-y-3 rounded-lg border border-[var(--border)] p-3 md:col-span-2">
      <legend className="px-1 font-medium">{t(kind === "course" ? "Create course" : "Create class")}</legend>
      <label className="block space-y-1 text-sm">
        <span>{t(kind === "course" ? "Course name" : "Class name")}</span>
        <input autoFocus value={name} maxLength={255}
          onChange={event => setName(event.target.value)}
          onKeyDown={event => {
            if (event.key === "Enter" && !event.nativeEvent.isComposing) {
              event.preventDefault(); void create();
            }
          }}
          className="w-full rounded-lg border border-[var(--border)] bg-[var(--background)] px-3 py-2" />
      </label>
      {error ? <p role="alert" className="text-sm text-[var(--destructive)]">{error}</p> : null}
      <div className="flex gap-3 text-sm">
        <button type="button" disabled={!name.trim() || busy} onClick={() => void create()}
          className="rounded-lg bg-[var(--primary)] px-3 py-2 text-[var(--primary-foreground)] disabled:opacity-50">{t(busy ? "Creating…" : "Create")}</button>
        <button type="button" onClick={onCancel}>{t("Cancel")}</button>
      </div>
    </fieldset>
  );
}

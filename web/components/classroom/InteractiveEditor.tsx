"use client";

import { useTranslation } from "react-i18next";

import type { InteractiveScene } from "@/lib/openmaic-adapter/contracts";
import type { InteractiveUpdateOperation } from "@/lib/openmaic-adapter/scene-operations";

export interface InteractiveEditorProps {
  scene: InteractiveScene;
  disabled?: boolean;
  onOperation(operation: InteractiveUpdateOperation): void;
}

export function InteractiveEditor({
  scene,
  disabled = false,
  onOperation,
}: InteractiveEditorProps) {
  const { t } = useTranslation();
  return (
    <div className="space-y-3">
      <p className="text-xs text-[var(--muted-foreground)]">
        {t("classroom.editor.interactiveSafety")}
      </p>
      <textarea
        key={`${scene.id}:${scene.content.html}`}
        aria-label={t("classroom.editor.interactiveHtml")}
        defaultValue={scene.content.html}
        disabled={disabled}
        spellCheck={false}
        onBlur={event => {
          if (event.currentTarget.value !== scene.content.html) {
            onOperation({
              type: "interactive.update",
              sceneId: scene.id,
              html: event.currentTarget.value,
              config: {
                bridgeVersion: "1.0",
                sandbox: { allowScripts: true, allowSameOrigin: false },
              },
            });
          }
        }}
        className="min-h-80 w-full rounded-md border border-[var(--border)] bg-[var(--background)] p-3 font-mono text-xs disabled:opacity-60"
      />
      <dl className="grid grid-cols-2 gap-2 text-xs">
        <dt className="text-[var(--muted-foreground)]">
          {t("classroom.editor.bridge")}
        </dt>
        <dd>{scene.content.bridgeVersion}</dd>
        <dt className="text-[var(--muted-foreground)]">
          {t("classroom.editor.sandbox")}
        </dt>
        <dd>{t("classroom.editor.sandboxPolicy")}</dd>
      </dl>
    </div>
  );
}

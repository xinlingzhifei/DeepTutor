"use client";

import { useTranslation } from "react-i18next";

import type { ClassroomScene } from "@/lib/openmaic-adapter/contracts";
import type { SceneOperation } from "@/lib/openmaic-adapter/scene-operations";

import { InteractiveEditor } from "./InteractiveEditor";
import { PblEditor } from "./PblEditor";
import { QuizEditor } from "./QuizEditor";

export interface ScenePropertiesPanelProps {
  scene: ClassroomScene;
  knowledgePointIds: readonly string[];
  disabled?: boolean;
  selectedElementCount?: number;
  onOperation(operation: SceneOperation): void;
}

export function ScenePropertiesPanel({
  scene,
  knowledgePointIds,
  disabled = false,
  selectedElementCount = 0,
  onOperation,
}: ScenePropertiesPanelProps) {
  const { t } = useTranslation();
  return (
    <aside className="max-h-[28rem] min-h-0 w-full shrink-0 overflow-y-auto border-t border-[var(--border)] bg-[var(--card)] p-4 lg:max-h-none lg:w-80 lg:border-t-0 lg:border-l">
      <label className="block text-xs font-medium text-[var(--muted-foreground)]">
        {t("classroom.editor.sceneTitle")}
        <input
          key={`${scene.id}:title:${scene.title}`}
          defaultValue={scene.title}
          disabled={disabled}
          onBlur={event => {
            if (event.currentTarget.value !== scene.title) {
              onOperation({
                type: "scene.update",
                sceneId: scene.id,
                title: event.currentTarget.value,
              });
            }
          }}
          className="mt-1 w-full rounded-md border border-[var(--border)] bg-[var(--background)] p-2 text-sm disabled:opacity-60"
        />
      </label>
      <div className="my-4 border-t border-[var(--border)]" />
      {scene.type === "slide" && (
        <div className="space-y-2 text-sm">
          <h2 className="font-semibold">
            {t("classroom.editor.slideElements")}
          </h2>
          <p className="text-xs text-[var(--muted-foreground)]">
            {t("classroom.editor.selectedElements", {
              count: selectedElementCount,
            })}
          </p>
        </div>
      )}
      {scene.type === "quiz" && (
        <QuizEditor
          scene={scene}
          knowledgePointIds={knowledgePointIds}
          disabled={disabled}
          onOperation={onOperation}
        />
      )}
      {scene.type === "interactive" && (
        <InteractiveEditor scene={scene} disabled={disabled} onOperation={onOperation} />
      )}
      {scene.type === "pbl" && (
        <PblEditor scene={scene} disabled={disabled} onOperation={onOperation} />
      )}
    </aside>
  );
}

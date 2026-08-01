"use client";

import type { ClassroomScene } from "@/lib/openmaic-adapter/contracts";

export interface SceneNavigatorProps {
  scenes: readonly ClassroomScene[];
  selectedSceneId: string;
  disabled?: boolean;
  onSelect(sceneId: string): void;
  onMove(sceneId: string, toIndex: number): void;
  onDuplicate(sceneId: string): void;
  onDelete(sceneId: string): void;
  onAdd(type: ClassroomScene["type"]): void;
}

export function SceneNavigator({
  scenes,
  selectedSceneId,
  disabled = false,
  onSelect,
  onMove,
  onDuplicate,
  onDelete,
  onAdd,
}: SceneNavigatorProps) {
  return (
    <aside className="flex min-h-0 w-64 shrink-0 flex-col border-r border-[var(--border)] bg-[var(--card)]">
      <div className="border-b border-[var(--border)] p-3">
        <p className="mb-2 text-xs font-semibold uppercase tracking-wide text-[var(--muted-foreground)]">
          Add scene
        </p>
        <div className="grid grid-cols-2 gap-1">
          {(["slide", "quiz", "interactive", "pbl"] as const).map(type => (
            <button
              type="button"
              key={type}
              disabled={disabled}
              onClick={() => onAdd(type)}
              className="rounded border border-[var(--border)] px-2 py-1 text-xs capitalize disabled:opacity-40"
            >
              {type}
            </button>
          ))}
        </div>
      </div>
      <ol className="min-h-0 flex-1 space-y-2 overflow-y-auto p-3">
        {scenes.map((scene, index) => (
          <li
            key={scene.id}
            className={`rounded-lg border p-2 ${
              selectedSceneId === scene.id
                ? "border-[var(--primary)] bg-[var(--accent)]"
                : "border-[var(--border)]"
            }`}
          >
            <button
              type="button"
              onClick={() => onSelect(scene.id)}
              className="w-full text-left"
            >
              <span className="block truncate text-sm font-medium">{scene.title}</span>
              <span className="text-xs capitalize text-[var(--muted-foreground)]">
                {index + 1}. {scene.type}
              </span>
            </button>
            <div className="mt-2 flex gap-1">
              <button
                type="button"
                aria-label={`Move ${scene.title} up`}
                disabled={disabled || index === 0}
                onClick={() => onMove(scene.id, index - 1)}
                className="rounded border border-[var(--border)] px-1.5 text-xs disabled:opacity-30"
              >
                ↑
              </button>
              <button
                type="button"
                aria-label={`Move ${scene.title} down`}
                disabled={disabled || index === scenes.length - 1}
                onClick={() => onMove(scene.id, index + 1)}
                className="rounded border border-[var(--border)] px-1.5 text-xs disabled:opacity-30"
              >
                ↓
              </button>
              <button
                type="button"
                disabled={disabled}
                onClick={() => onDuplicate(scene.id)}
                className="rounded border border-[var(--border)] px-1.5 text-xs disabled:opacity-30"
              >
                Copy
              </button>
              <button
                type="button"
                disabled={disabled || scenes.length === 1}
                onClick={() => onDelete(scene.id)}
                className="rounded border border-[var(--border)] px-1.5 text-xs disabled:opacity-30"
              >
                Delete
              </button>
            </div>
          </li>
        ))}
      </ol>
    </aside>
  );
}

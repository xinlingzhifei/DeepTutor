"use client";

import { useTranslation } from "react-i18next";

import type { PblMilestone, PblRole, PblScene } from "@/lib/openmaic-adapter/contracts";
import type { PblUpdateOperation } from "@/lib/openmaic-adapter/scene-operations";

export interface PblEditorProps {
  scene: PblScene;
  disabled?: boolean;
  onOperation(operation: PblUpdateOperation): void;
}

function newId(prefix: string): string {
  const suffix = globalThis.crypto?.randomUUID?.() ?? `${Date.now()}-${Math.random()}`;
  return `${prefix}-${suffix}`;
}

export function PblEditor({ scene, disabled = false, onOperation }: PblEditorProps) {
  const { t } = useTranslation();
  const updateRole = (index: number, patch: Partial<PblRole>) => {
    const roles = scene.content.roles.map((role, roleIndex) =>
      roleIndex === index ? { ...role, ...patch } : role,
    );
    onOperation({ type: "pbl.update", sceneId: scene.id, roles });
  };
  const updateMilestone = (index: number, patch: Partial<PblMilestone>) => {
    const milestones = scene.content.milestones.map((milestone, milestoneIndex) =>
      milestoneIndex === index ? { ...milestone, ...patch } : milestone,
    );
    onOperation({ type: "pbl.update", sceneId: scene.id, milestones });
  };

  return (
    <div className="space-y-5">
      <label className="block text-xs font-medium text-[var(--muted-foreground)]">
        {t("classroom.editor.scenario")}
        <textarea
          key={`${scene.id}:scenario:${scene.content.scenario}`}
          defaultValue={scene.content.scenario}
          disabled={disabled}
          onBlur={event => {
            if (event.currentTarget.value !== scene.content.scenario) {
              onOperation({
                type: "pbl.update",
                sceneId: scene.id,
                scenario: event.currentTarget.value,
              });
            }
          }}
          className="mt-1 min-h-28 w-full rounded-md border border-[var(--border)] bg-[var(--background)] p-2 text-sm disabled:opacity-60"
        />
      </label>

      <section className="space-y-2">
        <div className="flex items-center justify-between">
          <h3 className="text-sm font-semibold">
            {t("classroom.editor.roles")}
          </h3>
          <button
            type="button"
            disabled={disabled}
            onClick={() =>
              onOperation({
                type: "pbl.update",
                sceneId: scene.id,
                roles: [
                  ...scene.content.roles,
                  {
                    id: newId("role"),
                    name: t("classroom.editor.newRole"),
                    brief: t("classroom.editor.describeRole"),
                  },
                ],
              })
            }
            className="rounded border border-[var(--border)] px-2 py-1 text-xs disabled:opacity-40"
          >
            {t("classroom.editor.addRole")}
          </button>
        </div>
        {scene.content.roles.map((role, index) => (
          <div key={role.id} className="space-y-2 rounded-lg border border-[var(--border)] p-3">
            <input
              key={`${role.id}:name:${role.name}`}
              aria-label={t("classroom.editor.roleName", {
                number: index + 1,
              })}
              defaultValue={role.name}
              disabled={disabled}
              onBlur={event => {
                if (event.currentTarget.value !== role.name) {
                  updateRole(index, { name: event.currentTarget.value });
                }
              }}
              className="w-full rounded border border-[var(--border)] bg-[var(--background)] p-2 text-sm font-medium"
            />
            <textarea
              key={`${role.id}:brief:${role.brief}`}
              aria-label={t("classroom.editor.roleBrief", {
                number: index + 1,
              })}
              defaultValue={role.brief}
              disabled={disabled}
              onBlur={event => {
                if (event.currentTarget.value !== role.brief) {
                  updateRole(index, { brief: event.currentTarget.value });
                }
              }}
              className="min-h-16 w-full rounded border border-[var(--border)] bg-[var(--background)] p-2 text-sm"
            />
            <button
              type="button"
              disabled={disabled || scene.content.roles.length === 1}
              onClick={() =>
                onOperation({
                  type: "pbl.update",
                  sceneId: scene.id,
                  roles: scene.content.roles.filter((_, roleIndex) => roleIndex !== index),
                })
              }
              className="text-xs text-[var(--destructive)] disabled:opacity-40"
            >
              {t("classroom.editor.removeRole")}
            </button>
          </div>
        ))}
      </section>

      <section className="space-y-2">
        <div className="flex items-center justify-between">
          <h3 className="text-sm font-semibold">
            {t("classroom.editor.milestonesAndRubrics")}
          </h3>
          <button
            type="button"
            disabled={disabled}
            onClick={() =>
              onOperation({
                type: "pbl.update",
                sceneId: scene.id,
                milestones: [
                  ...scene.content.milestones,
                  {
                    id: newId("milestone"),
                    title: t("classroom.editor.newMilestone"),
                    rubric: t("classroom.editor.defineSuccess"),
                  },
                ],
              })
            }
            className="rounded border border-[var(--border)] px-2 py-1 text-xs disabled:opacity-40"
          >
            {t("classroom.editor.addMilestone")}
          </button>
        </div>
        {scene.content.milestones.map((milestone, index) => (
          <div key={milestone.id} className="space-y-2 rounded-lg border border-[var(--border)] p-3">
            <input
              key={`${milestone.id}:title:${milestone.title}`}
              aria-label={t("classroom.editor.milestoneTitle", {
                number: index + 1,
              })}
              defaultValue={milestone.title}
              disabled={disabled}
              onBlur={event => {
                if (event.currentTarget.value !== milestone.title) {
                  updateMilestone(index, { title: event.currentTarget.value });
                }
              }}
              className="w-full rounded border border-[var(--border)] bg-[var(--background)] p-2 text-sm font-medium"
            />
            <textarea
              key={`${milestone.id}:rubric:${milestone.rubric}`}
              aria-label={t("classroom.editor.milestoneRubric", {
                number: index + 1,
              })}
              defaultValue={milestone.rubric}
              disabled={disabled}
              onBlur={event => {
                if (event.currentTarget.value !== milestone.rubric) {
                  updateMilestone(index, { rubric: event.currentTarget.value });
                }
              }}
              className="min-h-16 w-full rounded border border-[var(--border)] bg-[var(--background)] p-2 text-sm"
            />
            <button
              type="button"
              disabled={disabled || scene.content.milestones.length === 1}
              onClick={() =>
                onOperation({
                  type: "pbl.update",
                  sceneId: scene.id,
                  milestones: scene.content.milestones.filter(
                    (_, milestoneIndex) => milestoneIndex !== index,
                  ),
                })
              }
              className="text-xs text-[var(--destructive)] disabled:opacity-40"
            >
              {t("classroom.editor.removeMilestone")}
            </button>
          </div>
        ))}
      </section>
    </div>
  );
}

"use client";

import { useMemo, useState, useSyncExternalStore } from "react";
import { useTranslation } from "react-i18next";

import { ClassroomEditor } from "@/components/classroom/ClassroomEditor";
import { ClassroomExportMenu } from "@/components/classroom/ClassroomExportMenu";
import {
  ClassroomPlayer,
  type ClassroomPlaybackHostPorts,
} from "@/components/classroom/ClassroomPlayer";
import { ImportClassroomDialog } from "@/components/classroom/ImportClassroomDialog";
import type {
  ClassroomDocument,
  ClassroomScene,
  ClassroomThemeId,
} from "@/lib/openmaic-adapter/contracts";

export type ClassroomVisualHost = "editor" | "player";
export type ClassroomVisualScene = ClassroomScene["type"];
export type ClassroomVisualTheme = ClassroomThemeId;

const NOW = "2026-07-30T12:00:00+08:00";
const FILE_SHA = "a".repeat(64);
const BRIEF_SHA = "b".repeat(64);
const subscribeToHydration = () => () => undefined;
const getHydratedSnapshot = () => true;
const getServerSnapshot = () => false;

const PLAYER_PORTS: ClassroomPlaybackHostPorts = {
  speak: async () => undefined,
  playVideo: async () => undefined,
  openDiscussion: async () => undefined,
  postWidgetAction: async () => undefined,
  commitCheckpoint: async () => undefined,
};

function classroomDocument(activeType: ClassroomVisualScene): ClassroomDocument {
  const scenes: ClassroomScene[] = [
    {
      id: "scene-slide",
      stageId: "stage-visual",
      title: "Energy transfer",
      order: 0,
      type: "slide",
      content: {
        type: "slide",
        canvas: {
          id: "canvas-visual",
          viewportSize: 1000,
          viewportRatio: 9 / 16,
          elements: [
            {
              id: "title",
              type: "text",
              left: 72,
              top: 64,
              width: 620,
              height: 96,
              rotate: 0,
              content: "How does energy move?",
              defaultFontName: "Arial",
              defaultColor: "#111827",
            },
            {
              id: "summary",
              type: "text",
              left: 84,
              top: 210,
              width: 520,
              height: 180,
              rotate: 0,
              content: "Observe → Model → Explain",
              defaultFontName: "Arial",
              defaultColor: "#334155",
            },
            {
              id: "focus-shape",
              type: "shape",
              left: 650,
              top: 190,
              width: 230,
              height: 230,
              rotate: 0,
              viewBox: [100, 100],
              path: "M50 0 L100 50 L50 100 L0 50 Z",
              fixedRatio: true,
              fill: "#c7d2fe",
              text: {
                content: "Evidence",
                defaultFontName: "Arial",
                defaultColor: "#312e81",
                align: "middle",
              },
            },
          ],
        },
      },
      actions: [],
    },
    {
      id: "scene-quiz",
      stageId: "stage-visual",
      title: "Check your model",
      order: 1,
      type: "quiz",
      content: {
        type: "quiz",
        questions: [
          {
            id: "question-energy",
            prompt: "Which observation best supports energy transfer?",
            questionType: "single_choice",
            options: [
              { id: "option-temperature", label: "A temperature change" },
              { id: "option-color", label: "A label changes color" },
              { id: "option-name", label: "The object is renamed" },
            ],
            correctOptionIds: ["option-temperature"],
            explanation: "Temperature change is measurable evidence of transfer.",
          },
        ],
      },
      actions: [],
    },
    {
      id: "scene-interactive",
      stageId: "stage-visual",
      title: "Build the pathway",
      order: 2,
      type: "interactive",
      content: {
        type: "interactive",
        html: `
          <main>
            <h1>Build an energy pathway</h1>
            <p>Arrange the evidence from source to receiver.</p>
            <button data-yfeistai-event="interactive.completed">Complete pathway</button>
          </main>
        `,
        bridgeVersion: "1.0",
        sandbox: { allowScripts: true, allowSameOrigin: false },
      },
      actions: [],
    },
    {
      id: "scene-pbl",
      stageId: "stage-visual",
      title: "Design a thermal shelter",
      order: 3,
      type: "pbl",
      content: {
        type: "pbl",
        scenario:
          "Design a compact shelter that keeps an ice sample stable for thirty minutes. Use measured evidence to justify each material choice.",
        roles: [
          {
            id: "role-investigator",
            name: "Investigator",
            brief: "Collect temperature evidence and identify transfer pathways.",
          },
          {
            id: "role-designer",
            name: "Designer",
            brief: "Turn the evidence into a testable shelter design.",
          },
        ],
        milestones: [
          {
            id: "milestone-evidence",
            title: "Evidence map",
            rubric: "The map connects each claim to a measurement.",
          },
          {
            id: "milestone-prototype",
            title: "Prototype review",
            rubric: "The design explains how each material changes energy transfer.",
          },
        ],
      },
      actions: [],
    },
  ];
  const ordered = [
    ...scenes.filter(item => item.type === activeType),
    ...scenes.filter(item => item.type !== activeType),
  ].map<ClassroomScene>((item, order) => ({ ...item, order }));

  return {
    schemaVersion: "1.0",
    classroomId: "visual-classroom",
    classroomVersionId: "visual-version",
    contentMode: "open_creation",
    openCreation: true,
    openmaic: {
      dslVersion: "0.1.0",
      stage: {
        id: "stage-visual",
        name: "Energy Lab",
        createdAt: NOW,
        updatedAt: NOW,
      },
      scenes: ordered,
    },
    interactionIds: ["scene-quiz", "scene-interactive", "scene-pbl"],
    sourceRefs: [],
    knowledgePointMappings: [
      {
        knowledgePointId: "kp-energy-transfer",
        sceneIds: ordered.map(item => item.id),
        sourceRefs: [],
      },
    ],
    mediaManifest: [],
    fileSha256: FILE_SHA,
    exportManifest: [],
    generationMetadata: {
      generator: "visual-fixture",
      generatorVersion: "1",
      modelId: "fixture-model",
      generatedAt: NOW,
      teachingBriefId: "brief-visual",
      teachingBriefSha256: BRIEF_SHA,
      templateId: "template-visual",
      templateVersion: "1",
    },
    auditMetadata: {
      templateId: "template-visual",
      templateVersion: "1",
      teachingBriefId: "brief-visual",
      teachingBriefSha256: BRIEF_SHA,
      parentClassroomVersionId: null,
    },
    validationResult: { valid: true, issues: [], validatedAt: NOW },
    migrationRecords: [],
  };
}

export function ClassroomVisualFixture({
  host,
  scene,
  theme,
}: {
  host: ClassroomVisualHost;
  scene: ClassroomVisualScene;
  theme: ClassroomVisualTheme;
}) {
  const { t } = useTranslation();
  const document = useMemo(() => classroomDocument(scene), [scene]);
  const [importOpen, setImportOpen] = useState(false);
  const hydrated = useSyncExternalStore(
    subscribeToHydration,
    getHydratedSnapshot,
    getServerSnapshot,
  );

  return (
    <main
      className="min-h-screen bg-[var(--background)] px-3 py-4 text-[var(--foreground)] sm:px-6 lg:px-8"
      data-testid="classroom-visual-root"
      data-host={host}
      data-hydrated={hydrated ? "true" : "false"}
      data-import-open={importOpen ? "true" : "false"}
      data-scene={scene}
      data-theme={theme}
    >
      <div className="mx-auto max-w-[1600px] space-y-4">
        <header className="flex flex-wrap items-center justify-between gap-3 rounded-2xl border border-[var(--border)] bg-[var(--card)] px-4 py-3 shadow-sm">
          <div className="flex flex-wrap gap-2 text-xs font-medium uppercase tracking-[0.12em] text-[var(--muted-foreground)]">
            <span>{host}</span>
            <span aria-hidden="true">·</span>
            <span>{scene}</span>
            <span aria-hidden="true">·</span>
            <span>{theme}</span>
          </div>
          <button
            type="button"
            data-testid="classroom-import-trigger"
            onClick={() => setImportOpen(true)}
            className="rounded-lg border border-[var(--border)] bg-[var(--background)] px-3 py-2 text-sm font-medium hover:bg-[var(--muted)]"
          >
            {t("classroom.import.action")}
          </button>
        </header>

        <section data-testid="classroom-visual-surface">
          {host === "editor" ? (
            <ClassroomEditor
              initialDocument={document}
              initialRevision={'"visual-revision-1"'}
              theme={theme}
            />
          ) : (
            <ClassroomPlayer
              document={document}
              ports={PLAYER_PORTS}
              sessionNonce="visual-fixture-session"
              theme={theme}
              gradeQuiz={async () => ({
                attemptId: "visual-attempt",
                status: "graded",
                score: 100,
                feedback: "Evidence accepted.",
              })}
              handleInteractiveEvent={async () => undefined}
              completePblMilestone={async () => undefined}
              className="min-h-[640px]"
            />
          )}
        </section>

        <ClassroomExportMenu
          target={{
            kind: "draft",
            assetId: "visual-classroom",
            revision: '"visual-revision-1"',
          }}
          policy={{ mp4Enabled: false }}
        />
      </div>

      <ImportClassroomDialog
        isOpen={importOpen}
        assetId="visual-classroom"
        onClose={() => setImportOpen(false)}
        onImported={async () => setImportOpen(false)}
      />
    </main>
  );
}

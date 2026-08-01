import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import path from "node:path";
import test from "node:test";

import type { ClassroomDocument } from "../lib/openmaic-adapter/contracts";
import {
  createHistory,
  pushHistory,
  undo,
} from "../lib/openmaic-adapter/editor-history";
import {
  applySceneOperations,
  saveClassroomDraft,
} from "../lib/openmaic-adapter/scene-operations";

const NOW = "2026-07-30T12:00:00+08:00";
const SHA = "b".repeat(64);

function classroomWithAllScenes(): ClassroomDocument {
  return {
    schemaVersion: "1.0",
    classroomId: "classroom-1",
    classroomVersionId: "version-1",
    contentMode: "open_creation",
    openCreation: true,
    openmaic: {
      dslVersion: "0.1.0",
      stage: { id: "stage-1", name: "Editor", createdAt: NOW, updatedAt: NOW },
      scenes: [
        {
          id: "slide-1", stageId: "stage-1", title: "Slide", order: 0,
          type: "slide", content: { type: "slide", canvas: { elements: [] } }, actions: [],
        },
        {
          id: "quiz-1", stageId: "stage-1", title: "Quiz", order: 1,
          type: "quiz", content: { type: "quiz", questions: [{
            id: "question-1", prompt: "Original?", questionType: "single_choice",
            options: [{ id: "yes", label: "Yes" }, { id: "no", label: "No" }],
            correctOptionIds: ["yes"], explanation: "Original explanation",
          }] }, actions: [],
        },
        {
          id: "interactive-1", stageId: "stage-1", title: "Interactive", order: 2,
          type: "interactive", content: {
            type: "interactive", html: "<main>Safe</main>", bridgeVersion: "1.0",
            sandbox: { allowScripts: true, allowSameOrigin: false },
          }, actions: [],
        },
        {
          id: "pbl-1", stageId: "stage-1", title: "PBL", order: 3,
          type: "pbl", content: {
            type: "pbl", scenario: "Original scenario",
            roles: [{ id: "role-1", name: "Lead", brief: "Lead the work" }],
            milestones: [{ id: "milestone-1", title: "Plan", rubric: "Clear plan" }],
          }, actions: [],
        },
      ],
    },
    interactionIds: ["interactive-1"],
    sourceRefs: [],
    knowledgePointMappings: [
      { knowledgePointId: "kp-old", sceneIds: ["slide-1", "quiz-1"], sourceRefs: [] },
    ],
    mediaManifest: [], fileSha256: SHA, exportManifest: [],
    generationMetadata: {
      generator: "test", generatorVersion: "1", modelId: "model", generatedAt: NOW,
      teachingBriefId: "brief-1", teachingBriefSha256: SHA,
      templateId: "template-1", templateVersion: "1",
    },
    auditMetadata: {
      templateId: "template-1", templateVersion: "1", teachingBriefId: "brief-1",
      teachingBriefSha256: SHA, parentClassroomVersionId: null,
    },
    validationResult: { valid: true, issues: [], validatedAt: NOW },
    migrationRecords: [],
  };
}

test("scene reorder and quiz edits stay inside the draft aggregate", () => {
  const initial = classroomWithAllScenes();
  const next = applySceneOperations(initial, [
    { type: "scene.reorder", sceneId: "quiz-1", toIndex: 0 },
    {
      type: "quiz.update",
      sceneId: "quiz-1",
      question: "Updated?",
      options: ["Yes", "No"],
      correctOption: 1,
      explanation: "Updated explanation",
      knowledgePointIds: ["kp-new"],
    },
  ]);

  assert.equal(next.openmaic.scenes[0].id, "quiz-1");
  const quiz = next.openmaic.scenes[0];
  assert.equal(quiz.type, "quiz");
  assert.equal(quiz.content.questions[0].prompt, "Updated?");
  assert.deepEqual(quiz.content.questions[0].correctOptionIds, ["no"]);
  assert.deepEqual(
    next.knowledgePointMappings.find(mapping => mapping.knowledgePointId === "kp-new")?.sceneIds,
    ["quiz-1"],
  );
  assert.equal(initial.openmaic.scenes[1].title, "Quiz");
  assert.equal(
    initial.openmaic.scenes[1].type === "quiz"
      ? initial.openmaic.scenes[1].content.questions[0].prompt
      : "",
    "Original?",
  );
});

test("scene add, duplicate, delete and reorder preserve type bindings and order", () => {
  const initial = classroomWithAllScenes();
  const next = applySceneOperations(initial, [
    {
      type: "scene.add",
      toIndex: 1,
      scene: {
        id: "quiz-2", stageId: "stage-1", title: "Second quiz", order: 99,
        type: "quiz", content: { type: "quiz", questions: [{
          id: "question-2", prompt: "Ready?", questionType: "single_choice",
          options: [{ id: "ready", label: "Ready" }, { id: "later", label: "Later" }],
          correctOptionIds: ["ready"], explanation: "Check readiness",
        }] }, actions: [],
      },
    },
    { type: "scene.duplicate", sceneId: "pbl-1", newSceneId: "pbl-2", toIndex: 2 },
    { type: "scene.delete", sceneId: "interactive-1" },
    { type: "scene.reorder", sceneId: "slide-1", toIndex: 3 },
  ]);

  assert.deepEqual(next.openmaic.scenes.map(scene => scene.order), [0, 1, 2, 3, 4]);
  assert.equal(next.openmaic.scenes.find(scene => scene.id === "pbl-2")?.type, "pbl");
  assert.equal(next.openmaic.scenes.some(scene => scene.id === "interactive-1"), false);
  assert.equal(next.interactionIds.includes("interactive-1"), false);
  assert.equal(initial.openmaic.scenes.length, 4);
});

test("interactive and PBL edits use the portable publication contract", () => {
  const initial = classroomWithAllScenes();
  const next = applySceneOperations(initial, [
    {
      type: "interactive.update", sceneId: "interactive-1",
      html: '<main><button data-yfeistai-event="interactive.completed">Go</button></main>',
      config: { bridgeVersion: "1.0", sandbox: { allowScripts: true, allowSameOrigin: false } },
    },
    {
      type: "pbl.update", sceneId: "pbl-1", scenario: "Updated scenario",
      roles: [{ id: "role-2", name: "Analyst", brief: "Analyze evidence" }],
      milestones: [{ id: "milestone-2", title: "Review", rubric: "Evidence based" }],
    },
  ]);

  const interactive = next.openmaic.scenes.find(scene => scene.id === "interactive-1");
  const pbl = next.openmaic.scenes.find(scene => scene.id === "pbl-1");
  assert.equal(interactive?.type === "interactive" && interactive.content.html.includes("button"), true);
  assert.equal(pbl?.type === "pbl" && pbl.content.scenario, "Updated scenario");

  for (const html of [
    "<script>void 0</script>",
    '<script src="https://evil.example/x.js"></script>',
    '<a href="https://evil.example">leave</a>',
    '<iframe srcdoc="<p>nested</p>"></iframe>',
  ]) {
    assert.throws(() => applySceneOperations(initial, [
      { type: "interactive.update", sceneId: "interactive-1", html },
    ]));
  }
  assert.equal(
    initial.openmaic.scenes.find(scene => scene.id === "interactive-1")?.type === "interactive"
      ? (initial.openmaic.scenes.find(scene => scene.id === "interactive-1") as Extract<ClassroomDocument["openmaic"]["scenes"][number], { type: "interactive" }>).content.html
      : "",
    "<main>Safe</main>",
  );
});

test("quiz, interactive and PBL commits share one replayable document history", () => {
  const initial = classroomWithAllScenes();
  const next = applySceneOperations(initial, [
    { type: "quiz.update", sceneId: "quiz-1", question: "One aggregate?" },
    { type: "interactive.update", sceneId: "interactive-1", html: "<main>Updated</main>" },
    { type: "pbl.update", sceneId: "pbl-1", scenario: "Shared history" },
  ]);
  const history = pushHistory(createHistory(initial), next);

  assert.equal(history.past.length, 1);
  assert.deepEqual(undo(history).present, initial);
  assert.equal(history.present.openmaic.scenes.length, 4);
});

test("wrong scene types, duplicate ids and invalid boundaries fail atomically", () => {
  const initial = classroomWithAllScenes();
  const before = structuredClone(initial);

  assert.throws(() => applySceneOperations(initial, [
    { type: "scene.reorder", sceneId: "slide-1", toIndex: 1 },
    { type: "quiz.update", sceneId: "slide-1", question: "Wrong type" },
  ]));
  assert.throws(() => applySceneOperations(initial, [
    { type: "scene.duplicate", sceneId: "slide-1", newSceneId: "quiz-1" },
  ]));
  assert.throws(() => applySceneOperations(initial, [
    { type: "scene.reorder", sceneId: "slide-1", toIndex: 99 },
  ]));
  assert.deepEqual(initial, before);
});

test("draft conflicts expose comparable server state and never retry or overwrite", async () => {
  for (const status of [409, 412]) {
    const clientDocument = classroomWithAllScenes();
    const serverDocument = classroomWithAllScenes();
    serverDocument.openmaic.stage.updatedAt = "2026-07-30T13:00:00+08:00";
    let calls = 0;
    let sentIfMatch = "";
    let sentUrl = "";
    let sentMethod = "";
    let sentBody: unknown;

    const result = await saveClassroomDraft({
      classroomId: "classroom-1",
      revision: '"revision-7"',
      document: clientDocument,
      fetch: async (input, init) => {
        calls += 1;
        sentUrl = String(input);
        sentMethod = init?.method ?? "";
        sentIfMatch = new Headers(init?.headers).get("If-Match") ?? "";
        sentBody = JSON.parse(String(init?.body));
        const conflict = status === 409
          ? { server_revision: '"revision-8"', server_document: serverDocument }
          : { revision: '"revision-8"', document: serverDocument };
        return new Response(JSON.stringify(conflict), {
          status,
          headers: { "Content-Type": "application/json" },
        });
      },
    });

    assert.equal(calls, 1);
    assert.equal(sentUrl, "/api/v1/classrooms/classroom-1/draft");
    assert.equal(sentMethod, "PUT");
    assert.equal(sentIfMatch, '"revision-7"');
    assert.deepEqual(sentBody, { document: clientDocument });
    assert.equal(result.status, "conflict");
    if (result.status === "conflict") {
      assert.equal(result.clientRevision, '"revision-7"');
      assert.equal(result.serverRevision, '"revision-8"');
      assert.equal(result.serverDocument.openmaic.stage.updatedAt, "2026-07-30T13:00:00+08:00");
    }
    assert.equal(clientDocument.openmaic.stage.updatedAt, NOW);
  }
});

test("draft save runs interactive publication safety before transport", async () => {
  const document = classroomWithAllScenes();
  const interactive = document.openmaic.scenes.find(scene => scene.type === "interactive");
  assert.ok(interactive && interactive.type === "interactive");
  interactive.content.html = '<iframe src="/nested"></iframe>';
  let calls = 0;

  await assert.rejects(() => saveClassroomDraft({
    classroomId: "classroom-1",
    revision: '"revision-1"',
    document,
    fetch: async () => {
      calls += 1;
      return new Response();
    },
  }));
  assert.equal(calls, 0);
});

test("the host dynamically loads editing and business components never import OpenMAIC", () => {
  const webRoot = process.cwd();
  const componentNames = [
    "ClassroomEditor.tsx",
    "ClassroomEditorToolbar.tsx",
    "SceneNavigator.tsx",
    "ScenePropertiesPanel.tsx",
    "QuizEditor.tsx",
    "InteractiveEditor.tsx",
    "PblEditor.tsx",
  ];
  const sources = componentNames.map(name =>
    readFileSync(path.join(webRoot, "components", "classroom", name), "utf8"),
  );
  sources.forEach(source => assert.doesNotMatch(source, /["']@openmaic\//));

  const host = sources[0];
  assert.match(host, /dynamic\s*\(/);
  assert.match(host, /import\("@\/lib\/openmaic-adapter"\)/);
  assert.match(host, /\{\s*ssr:\s*false\s*\}/);
  for (const state of [
    "history",
    "selectedSceneId",
    "selection",
    "revision",
    "saveState",
  ]) {
    assert.match(host, new RegExp(`\\[${state},\\s*set`, "m"));
  }
});

test("the adapter reducer covers the installed renderer EditIntent union exactly", () => {
  const webRoot = process.cwd();
  const rendererTypes = readFileSync(
    path.join(
      webRoot,
      "node_modules",
      "@openmaic",
      "renderer",
      "dist",
      "editing",
      "types.d.ts",
    ),
    "utf8",
  );
  const union = rendererTypes.slice(
    rendererTypes.indexOf("export type EditIntent"),
    rendererTypes.indexOf("export interface Selection"),
  );
  const installed = [...union.matchAll(/type:\s*'([^']+)'/g)].map(match => match[1]);
  const expected = [
    "element.update",
    "element.updateMany",
    "element.add",
    "element.delete",
    "element.reorder",
    "element.align",
    "element.removeProps",
    "text.updateContent",
  ];
  assert.deepEqual(installed, expected);

  const reducer = readFileSync(
    path.join(webRoot, "lib", "openmaic-adapter", "edit-intents.ts"),
    "utf8",
  );
  expected.forEach(type => assert.match(reducer, new RegExp(`intent\\.type === "${type}"`)));
});

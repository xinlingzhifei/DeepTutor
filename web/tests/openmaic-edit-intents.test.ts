import assert from "node:assert/strict";
import test from "node:test";

import type { ClassroomDocument } from "../lib/openmaic-adapter/contracts";
import { applyEditIntents } from "../lib/openmaic-adapter/edit-intents";
import {
  createHistory,
  pushHistory,
  redo,
  undo,
} from "../lib/openmaic-adapter/editor-history";

const NOW = "2026-07-30T12:00:00+08:00";
const SHA = "a".repeat(64);

function classroomWithOneSlide(): ClassroomDocument {
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
          id: "slide-1",
          stageId: "stage-1",
          title: "Slide",
          order: 0,
          type: "slide",
          content: {
            type: "slide",
            canvas: {
              id: "canvas-1",
              viewportSize: 1000,
              viewportRatio: 2,
              elements: [
                {
                  id: "title",
                  type: "text",
                  left: 100,
                  top: 40,
                  width: 300,
                  height: 80,
                  rotate: 0,
                  content: "Original",
                  defaultFontName: "Arial",
                  defaultColor: "#111111",
                  fill: "#ffffff",
                },
                {
                  id: "shape",
                  type: "shape",
                  left: 500,
                  top: 200,
                  width: 200,
                  height: 120,
                  rotate: 0,
                  viewBox: [100, 100],
                  path: "M0 0 H100 V100 H0 Z",
                  fixedRatio: false,
                  fill: "#eeeeee",
                  text: {
                    content: "Shape",
                    defaultFontName: "Arial",
                    defaultColor: "#111111",
                    align: "middle",
                  },
                },
              ],
            },
          },
          actions: [],
        },
      ],
    },
    interactionIds: [],
    sourceRefs: [],
    knowledgePointMappings: [
      { knowledgePointId: "kp-1", sceneIds: ["slide-1"], sourceRefs: [] },
    ],
    mediaManifest: [],
    fileSha256: SHA,
    exportManifest: [],
    generationMetadata: {
      generator: "test",
      generatorVersion: "1",
      modelId: "model",
      generatedAt: NOW,
      teachingBriefId: "brief-1",
      teachingBriefSha256: SHA,
      templateId: "template-1",
      templateVersion: "1",
    },
    auditMetadata: {
      templateId: "template-1",
      templateVersion: "1",
      teachingBriefId: "brief-1",
      teachingBriefSha256: SHA,
      parentClassroomVersionId: null,
    },
    validationResult: { valid: true, issues: [], validatedAt: NOW },
    migrationRecords: [],
  };
}

function elements(document: ClassroomDocument): Array<Record<string, unknown>> {
  const scene = document.openmaic.scenes[0];
  assert.equal(scene.type, "slide");
  return scene.content.canvas.elements as Array<Record<string, unknown>>;
}

test("one renderer gesture creates one history entry", () => {
  const initial = classroomWithOneSlide();
  const next = applyEditIntents(initial, [
    { type: "element.update", id: "title", props: { left: 120, top: 60 } },
  ]);
  const history = pushHistory(createHistory(initial), next);

  assert.equal(history.past.length, 1);
  assert.deepEqual(undo(history).present, initial);
  assert.deepEqual(redo(undo(history)).present, next);
});

test("the complete public EditIntent union is applied atomically and immutably", () => {
  const initial = classroomWithOneSlide();
  const next = applyEditIntents(initial, [
    { type: "element.update", id: "title", props: { left: 140 } },
    {
      type: "element.updateMany",
      updates: [
        { id: "title", props: { top: 70 } },
        { id: "shape", props: { top: 220 } },
      ],
    },
    {
      type: "element.add",
      index: 1,
      element: {
        id: "temporary",
        type: "text",
        left: 20,
        top: 360,
        width: 200,
        height: 60,
        rotate: 0,
        content: "Temporary",
        defaultFontName: "Arial",
        defaultColor: "#111111",
      },
    },
    { type: "element.reorder", id: "shape", command: "front" },
    { type: "element.align", ids: ["title", "shape"], command: "left" },
    { type: "element.removeProps", id: "title", props: ["fill"] },
    { type: "text.updateContent", id: "title", target: "text", content: "Updated" },
    { type: "text.updateContent", id: "shape", target: "shape", content: "Updated shape" },
    { type: "element.delete", ids: ["temporary"] },
  ]);

  const updated = elements(next);
  const title = updated.find(element => element.id === "title");
  const shape = updated.find(element => element.id === "shape");
  assert.equal(title?.content, "Updated");
  assert.equal("fill" in (title ?? {}), false);
  assert.equal(title?.left, shape?.left);
  assert.deepEqual((shape?.text as { content: string }).content, "Updated shape");
  assert.equal(updated.at(-1)?.id, "shape");
  assert.equal(updated.some(element => element.id === "temporary"), false);
  assert.equal(elements(initial)[0].content, "Original");
  assert.equal(elements(initial)[0].left, 100);
});

test("unknown ids and invalid batches fail without mutating the document", () => {
  const initial = classroomWithOneSlide();
  const before = structuredClone(initial);

  assert.throws(() =>
    applyEditIntents(initial, [
      { type: "element.update", id: "title", props: { left: 200 } },
      { type: "element.delete", ids: ["missing"] },
    ]),
  );
  assert.throws(() =>
    applyEditIntents(initial, [
      { type: "element.update", id: "title", props: { left: -1 } },
    ]),
  );
  assert.throws(() =>
    applyEditIntents(initial, [
      { type: "element.update", id: "title", props: { type: "shape" } as never },
    ]),
  );
  assert.throws(() =>
    applyEditIntents(initial, [
      { type: "text.updateContent", id: "shape", target: "text", content: "bad" },
    ]),
  );
  assert.throws(() =>
    applyEditIntents(initial, [
      { type: "element.add", element: null as never },
    ]),
  );
  assert.throws(() =>
    applyEditIntents(initial, [
      { type: "element.updateMany", updates: null as never },
    ]),
  );
  assert.deepEqual(initial, before);
});

test("add, index and id validation reject ambiguous element edits", () => {
  const initial = classroomWithOneSlide();
  const duplicate = structuredClone(elements(initial)[0]);

  assert.throws(() =>
    applyEditIntents(initial, [
      { type: "element.add", element: duplicate as never, index: 99 },
    ]),
  );
  assert.throws(() =>
    applyEditIntents(initial, [
      { type: "element.updateMany", updates: [
        { id: "title", props: { left: 110 } },
        { id: "title", props: { left: 120 } },
      ] },
    ]),
  );
});

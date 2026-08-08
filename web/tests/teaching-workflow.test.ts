import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import path from "node:path";
import test from "node:test";

import type {
  TeachingReview,
  TeachingReviewDetail,
} from "../lib/teaching-api";
import {
  canDecideTeachingReview,
  createTeachingAttemptRegistry,
  createTeachingPublicationAttemptRegistry,
  isCurrentTeachingOperation,
  isTeachingClassroomEditable,
  reviewEvidenceMatches,
  shouldApplyOutlineResponse,
} from "../lib/teaching-workflow";

const SHA_A = "a".repeat(64);
const SHA_B = "b".repeat(64);

test("only an editing classroom lifecycle is author-mutable", () => {
  assert.equal(isTeachingClassroomEditable("editing"), true);
  assert.equal(isTeachingClassroomEditable("submitted"), false);
  assert.equal(isTeachingClassroomEditable("approved"), false);
});

function review(overrides: Partial<TeachingReview> = {}): TeachingReview {
  return {
    id: "review-1",
    assetId: "asset-1",
    draftId: "draft-1",
    draftRevision: 3,
    documentSha256: SHA_A,
    validationReportSha256: SHA_B,
    submittedBy: "teacher-a",
    scope: "tenant",
    classId: null,
    status: "pending",
    warnings: [],
    reviewerId: null,
    comment: null,
    ...overrides,
  };
}

function detail(reviewValue = review()): TeachingReviewDetail {
  return {
    review: reviewValue,
    title: "Motion",
    courseId: "course-a",
    targetClassId: "class-a",
    document: {} as TeachingReviewDetail["document"],
    validationReport: { valid: true },
    sourceFragments: [],
    baseline: null,
    changedPaths: ["/"],
  };
}

test("review decisions require evidence pinned to the selected review", () => {
  const selected = review();
  assert.equal(reviewEvidenceMatches(selected, detail()), true);
  assert.equal(
    reviewEvidenceMatches(
      selected,
      detail(review({ id: "review-2" })),
    ),
    false,
  );
  assert.equal(
    reviewEvidenceMatches(
      selected,
      detail(review({ draftRevision: 4 })),
    ),
    false,
  );
  assert.equal(
    reviewEvidenceMatches(
      selected,
      detail(review({ documentSha256: "c".repeat(64) })),
    ),
    false,
  );
  assert.equal(
    reviewEvidenceMatches(
      selected,
      detail(review({ validationReportSha256: "d".repeat(64) })),
    ),
    false,
  );
});

test("review actions stay hidden without loaded evidence or during stale work", () => {
  const selected = review();
  const base = {
    review: selected,
    detail: detail(),
    currentUserId: "reviewer-b",
    loading: false,
    error: null,
  };
  assert.equal(canDecideTeachingReview(base), true);
  assert.equal(canDecideTeachingReview({ ...base, detail: null }), false);
  assert.equal(canDecideTeachingReview({ ...base, loading: true }), false);
  assert.equal(canDecideTeachingReview({ ...base, error: "failed" }), false);
  assert.equal(
    canDecideTeachingReview({ ...base, currentUserId: "teacher-a" }),
    false,
  );
  assert.equal(
    canDecideTeachingReview({
      ...base,
      detail: detail(review({ id: "review-2" })),
    }),
    false,
  );
});

test("tenant publication retries reuse one key until a success settles it", () => {
  let generated = 0;
  const registry = createTeachingPublicationAttemptRegistry(
    assetId => `${assetId}-attempt-${++generated}`,
  );
  const first = registry.keyFor("asset-1");
  assert.equal(registry.keyFor("asset-1"), first);
  assert.notEqual(registry.keyFor("asset-2"), first);
  registry.settle("asset-1");
  assert.notEqual(registry.keyFor("asset-1"), first);
});

test("authoring mutations reuse idempotency keys per exact request fingerprint", () => {
  let generated = 0;
  const attempts = createTeachingAttemptRegistry(
    fingerprint => `${fingerprint}-attempt-${++generated}`,
  );
  const first = attempts.keyFor("draft-1:revision-3:tenant");
  assert.equal(attempts.keyFor("draft-1:revision-3:tenant"), first);
  assert.notEqual(attempts.keyFor("draft-1:revision-4:tenant"), first);
  attempts.settle("draft-1:revision-3:tenant");
  assert.notEqual(attempts.keyFor("draft-1:revision-3:tenant"), first);
});

test("outline responses never overwrite text changed after the request snapshot", () => {
  assert.equal(
    shouldApplyOutlineResponse({
      requestText: "before",
      currentText: "before",
      requestEpoch: 3,
      currentEpoch: 3,
    }),
    true,
  );
  assert.equal(
    shouldApplyOutlineResponse({
      requestText: "before",
      currentText: "after",
      requestEpoch: 3,
      currentEpoch: 3,
    }),
    false,
  );
  assert.equal(
    shouldApplyOutlineResponse({
      requestText: "before",
      currentText: "before",
      requestEpoch: 3,
      currentEpoch: 4,
    }),
    false,
  );
});

test("validation and submission responses apply only to the active draft epoch", () => {
  const expected = {
    assetId: "asset-1",
    draftId: "draft-1",
    revision: '"revision-3"',
    epoch: 2,
  };
  assert.equal(isCurrentTeachingOperation(expected, { ...expected }), true);
  assert.equal(
    isCurrentTeachingOperation(expected, { ...expected, revision: '"revision-4"' }),
    false,
  );
  assert.equal(
    isCurrentTeachingOperation(expected, { ...expected, epoch: 3 }),
    false,
  );
});

test("review queue loads selected immutable evidence and clears stale detail", () => {
  const source = readFileSync(
    path.join(process.cwd(), "components", "teaching", "ReviewQueue.tsx"),
    "utf8",
  );
  assert.match(source, /getTeachingReviewDetail/);
  assert.doesNotMatch(source, /getTeachingClassroom/);
  assert.match(source, /setDetail\(null\)/);
  assert.match(source, /canDecideTeachingReview/);
  assert.match(source, /fragment\.text/);
  assert.match(source, /detail\?\.baseline/);
  assert.match(source, /detail\?\.changedPaths/);
});

test("organization library renders real tenant publications and publish candidates", () => {
  const source = readFileSync(
    path.join(
      process.cwd(),
      "app",
      "(utility)",
      "teaching",
      "library",
      "page.tsx",
    ),
    "utf8",
  );
  assert.match(source, /listTeachingPublications/);
  assert.match(source, /publishTeachingClassroom/);
  assert.match(source, /createTeachingPublicationAttemptRegistry/);
  assert.doesNotMatch(source, /listTeachingClassrooms/);
  assert.doesNotMatch(source, /classroomVersionId/);
  assert.match(source, /disabled=\{publishingAssetId !== null\}/);
  assert.match(
    source,
    /setLibrary\(current => \(\{[\s\S]*candidates: current\.candidates\.filter/,
  );
});

test("teaching brief exposes and sends the selected media policy", () => {
  const source = readFileSync(
    path.join(process.cwd(), "components", "teaching", "TeachingBriefForm.tsx"),
    "utf8",
  );
  assert.match(
    source,
    /useState<TeachingMediaPolicy>\(\s*"image_audio",?\s*\)/,
  );
  assert.match(source, /value="text_only"/);
  assert.match(source, /value="image_audio"/);
  assert.match(source, /mediaPolicy,/);
  assert.match(source, /createTeachingAttemptRegistry/);
  assert.match(source, /creationAttempts\.keyFor\(requestFingerprint\)/);
  assert.match(source, /creationAttempts\.settle\(requestFingerprint\)/);
});

test("outline review locks editing and rejects stale save responses", () => {
  const source = readFileSync(
    path.join(process.cwd(), "components", "teaching", "OutlineReview.tsx"),
    "utf8",
  );
  assert.match(source, /disabled=\{saving \|\| confirming\}/);
  assert.match(source, /shouldApplyOutlineResponse/);
  assert.match(source, /operationRef\.current/);
  assert.match(source, /outlineTextRef\.current/);
  assert.match(
    source,
    /setClassroom\(next\);[\s\S]*if \(!responseIsCurrent\)/,
  );
});

test("teaching edit workflow locks every mutation while validating or submitting", () => {
  const page = readFileSync(
    path.join(
      process.cwd(),
      "app",
      "(utility)",
      "teaching",
      "classrooms",
      "[assetId]",
      "edit",
      "page.tsx",
    ),
    "utf8",
  );
  assert.match(page, /const operationLocked = validating \|\| submitting/);
  assert.match(
    page,
    /const workflowFrozen =[\s\S]*operationLocked \|\|[\s\S]*submissionComplete \|\|[\s\S]*isTeachingClassroomEditable\(classroom\.lifecycleState\)/,
  );
  assert.match(page, /<ClassroomEditor[\s\S]*disabled=\{workflowFrozen\}/);
  assert.match(page, /isCurrentTeachingOperation/);
  assert.match(page, /operationRef\.current/);
  assert.match(page, /isOpen=\{importOpen && !workflowFrozen\}/);
  assert.match(page, /disabled=\{workflowFrozen\}/);
  assert.match(page, /submissionScope === "class"/);
  assert.match(page, /submitAttempts\.keyFor\(submissionFingerprint\)/);
  assert.match(page, /submitAttempts\.settle\(submissionFingerprint\)/);
  assert.match(
    page,
    /setSubmissionComplete\(true\);[\s\S]*isCurrentTeachingOperation/,
  );
  assert.match(
    page,
    /operationEpochRef\.current \+= 1;[\s\S]*classroomRef\.current = null;[\s\S]*getTeachingClassroom/,
  );

  const editor = readFileSync(
    path.join(process.cwd(), "components", "classroom", "ClassroomEditor.tsx"),
    "utf8",
  );
  assert.match(editor, /disabled\?: boolean/);
  assert.match(
    editor,
    /const mutationDisabled = externallyDisabled \|\| saveState\.status === "saving"/,
  );
  assert.match(editor, /if \(mutationDisabled \|\| saveInFlight\.current\)/);
  assert.match(editor, /<ClassroomEditorToolbar[\s\S]*disabled=\{mutationDisabled\}/);
});

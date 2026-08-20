/**
 * One serial browser entrypoint for the first-release classroom candidate.
 *
 * Each flow owns an isolated browser context and mocked API state, while all
 * flows run serially against the same managed web build. Live provider,
 * cross-tenant, exported-artifact inspection, and restore evidence remain
 * separate release layers in scripts/verify_classroom_release.py.
 */

import { runClassroomLearningLoop } from "./classroom-learning-loop";
import { runContentOperationsFlow } from "./content-operations-flow";
import {
  runStudentClassroomFlow,
  runStudentMicroClassroomFlow,
} from "./student-classroom-flow";
import {
  runTeacherClassroomFlow,
  runTeacherExportMatrix,
} from "./teacher-classroom-flow";
import { test } from "./support/teaching-flow-test";

test.use({ locale: "en-US", timezoneId: "UTC" });

test.describe("classroom first-release browser acceptance", () => {
  test.describe.configure({ mode: "serial" });

  test("teacher preparation, submission, and export retry", async ({
    page,
    teachingDownload,
  }) => runTeacherClassroomFlow({ page, teachingDownload }));

  test("student full-classroom outline handoff", async ({ page }) =>
    runStudentClassroomFlow({ page }));

  test("student micro-classroom version handoff", async ({ page }) =>
    runStudentMicroClassroomFlow({ page }));

  test("classroom export matrix preserves all four formats", async ({ page }) =>
    runTeacherExportMatrix({ page }));

  test("content operations review and publication", async ({
    page,
    teachingDownload,
  }) => runContentOperationsFlow({ page, teachingDownload }));

  test("learning events retry and complete exactly once", async ({ page }) =>
    runClassroomLearningLoop({ page }));
});

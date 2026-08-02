import { expect, test } from "@playwright/test";

import {
  CLASSROOM_BASE_URL,
  classroomVisualCasesFor,
  classroomSnapshotName,
  expectCleanClassroomAudit,
  installClassroomNetworkAudit,
  openClassroomVisualFixture,
} from "./support/classroom-visual-support";

const PLAYER_CASES = classroomVisualCasesFor("player");

test.use({
  baseURL: CLASSROOM_BASE_URL,
  channel: "chromium",
  locale: "en-US",
  timezoneId: "UTC",
});
test.describe.configure({ mode: "serial" });

test.describe("Classroom player visual matrix", () => {
  for (const visualCase of PLAYER_CASES) {
    test(`${visualCase.theme.name} / ${visualCase.scene} / ${visualCase.viewport.name} / ${visualCase.language} / ${visualCase.motion}`, async ({
      page,
    }) => {
      test.setTimeout(90_000);
      const audit = await installClassroomNetworkAudit(
        page,
        visualCase.theme.stored,
      );
      const root = await openClassroomVisualFixture(page, visualCase);

      expectCleanClassroomAudit(audit);
      await expect(root).toHaveScreenshot(
        classroomSnapshotName(visualCase),
        {
          animations: "disabled",
          caret: "hide",
          scale: "css",
        },
      );
      expectCleanClassroomAudit(audit);
    });
  }
});

import { runTeacherClassroomFlow, runTeacherExportPolicy } from "./teacher-classroom-flow";
import { test } from "./support/teaching-flow-test";

test.use({ locale: "en-US", timezoneId: "UTC" });
test.describe.configure({ mode: "serial" });

test("teacher creates, confirms, submits a frozen draft, and retries export", async ({
  page,
  teachingDownload,
}) => runTeacherClassroomFlow({ page, teachingDownload }));

test("MP4 export follows the current tenant policy", async ({ page }) =>
  runTeacherExportPolicy({ page }),
);

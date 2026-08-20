import { runStudentClassroomFlow } from "./student-classroom-flow";
import { test } from "./support/teaching-flow-test";

test.use({ locale: "en-US", timezoneId: "UTC" });
test.describe.configure({ mode: "serial" });

test("student edits and confirms a full classroom outline before version handoff", async ({
  page,
}) => runStudentClassroomFlow({ page }));

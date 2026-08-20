import { runClassroomLearningLoop } from "./classroom-learning-loop";
import { test } from "./support/teaching-flow-test";

test.use({ locale: "en-US", timezoneId: "UTC" });
test.describe.configure({ mode: "serial" });

test("learner events survive a network interruption and complete exactly once", async ({
  page,
}) => runClassroomLearningLoop({ page }));

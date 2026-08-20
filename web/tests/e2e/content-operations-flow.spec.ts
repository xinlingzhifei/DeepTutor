import { runContentOperationsFlow } from "./content-operations-flow";
import { test } from "./support/teaching-flow-test";

test.use({ locale: "en-US", timezoneId: "UTC" });
test.describe.configure({ mode: "serial" });

test("content operations retries partial batches, approves evidence, and publishes tenant content", async ({
  page,
  teachingDownload,
}) => runContentOperationsFlow({ page, teachingDownload }));

import { createOutlineGetHandler } from "../../../../../../lib/yfeistai/outline-generation";
import { outlineJobStore } from "../../../../../../lib/yfeistai/job-store";
import { readServiceSecret } from "../../../../../../lib/yfeistai/service-auth";

const getOutline = createOutlineGetHandler({
  readSecret: readServiceSecret,
  store: outlineJobStore,
});

export async function GET(
  request: Request,
  context: { params: Promise<{ jobId: string }> },
): Promise<Response> {
  return getOutline(request, context);
}

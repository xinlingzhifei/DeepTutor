import {
  createExportInputCommitHandler,
  exportInputStagingStore,
} from "../../../../../../../lib/yfeistai/export-input-staging";
import { readServiceSecret } from "../../../../../../../lib/yfeistai/service-auth";

const commitExportInput = createExportInputCommitHandler({
  readSecret: readServiceSecret,
  store: exportInputStagingStore,
});

export async function POST(
  request: Request,
  context: { params: Promise<{ jobId: string }> },
): Promise<Response> {
  return commitExportInput(request, context);
}

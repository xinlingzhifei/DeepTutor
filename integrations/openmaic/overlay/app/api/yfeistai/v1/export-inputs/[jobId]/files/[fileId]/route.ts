import {
  createExportInputUploadHandler,
  exportInputStagingStore,
} from "../../../../../../../../lib/yfeistai/export-input-staging";
import { readServiceSecret } from "../../../../../../../../lib/yfeistai/service-auth";

const uploadExportInput = createExportInputUploadHandler({
  readSecret: readServiceSecret,
  store: exportInputStagingStore,
});

export async function PUT(
  request: Request,
  context: { params: Promise<{ jobId: string; fileId: string }> },
): Promise<Response> {
  return uploadExportInput(request, context);
}

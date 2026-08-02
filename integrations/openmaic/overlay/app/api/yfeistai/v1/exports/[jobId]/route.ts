import {
  createExportGetHandler,
  exportJobStore,
} from "../../../../../../lib/yfeistai/export-generation";
import { readServiceSecret } from "../../../../../../lib/yfeistai/service-auth";

const getExport = createExportGetHandler({
  readSecret: readServiceSecret,
  store: exportJobStore,
});

export async function GET(
  request: Request,
  context: { params: Promise<{ jobId: string }> },
): Promise<Response> {
  return getExport(request, context);
}

import {
  createExportInputReserveHandler,
  exportInputStagingStore,
} from "../../../../../../lib/yfeistai/export-input-staging";
import { readServiceSecret } from "../../../../../../lib/yfeistai/service-auth";

const reserveExportInput = createExportInputReserveHandler({
  readSecret: readServiceSecret,
  store: exportInputStagingStore,
});

export async function POST(
  request: Request,
  context: { params: Promise<{ jobId: string }> },
): Promise<Response> {
  return reserveExportInput(request, context);
}

import {
  contentJobStore,
  createJobCancelHandler,
} from "../../../../../../../lib/yfeistai/content-generation";
import { exportJobStore } from "../../../../../../../lib/yfeistai/export-generation";
import { isPrivateRenderEndpoint } from "../../../../../../../lib/yfeistai/export-generation";
import {
  readServiceSecret,
  signServiceRequest,
} from "../../../../../../../lib/yfeistai/service-auth";
import {
  configuredOpenMaicStateRoot,
  durableFile,
  exactDurableRecord,
  readDurableJson,
} from "../../../../../../../lib/yfeistai/durable-state";

async function cancelPersistedRenderJob(
  tenantId: string,
  jobId: string,
): Promise<void> {
  const configured = process.env.YFEISTAI_OPENMAIC_RENDER_ENDPOINT?.replace(/\/$/, "");
  if (!configured || !isPrivateRenderEndpoint(configured)) {
    return;
  }
  const target = durableFile(
    configuredOpenMaicStateRoot(),
    "render-jobs",
    "checkpoints",
    [tenantId, jobId],
    "render.json",
  );
  const raw = readDurableJson(target);
  if (!raw) {
    return;
  }
  const record = exactDurableRecord(raw, "render checkpoint", [
    "version", "tenantId", "jobId", "idempotencyKey", "rendererBase", "renderJobId",
  ]);
  if (
    record.version !== 1 ||
    record.tenantId !== tenantId ||
    record.jobId !== jobId ||
    record.rendererBase !== configured ||
    typeof record.idempotencyKey !== "string" ||
    typeof record.renderJobId !== "string"
  ) {
    throw new Error("render checkpoint binding is invalid");
  }
  const renderId = encodeURIComponent(record.renderJobId);
  const requestPath = `/yfeistai/v1/render/${renderId}/cancel`;
  const signed = signServiceRequest({
    secret: readServiceSecret(),
    method: "POST",
    path: requestPath,
    tenantId,
    jobId,
    timestamp: Math.floor(Date.now() / 1_000),
    idempotencyKey: `${record.idempotencyKey}:render-cancel`,
    body: "",
  });
  await fetch(`${configured}${requestPath}`, {
    method: "POST",
    redirect: "error",
    signal: AbortSignal.timeout(5_000),
    headers: {
      "x-yfeistai-tenant-id": signed.tenantId,
      "x-yfeistai-job-id": signed.jobId,
      "x-yfeistai-timestamp": String(signed.timestamp),
      "x-yfeistai-idempotency-key": signed.idempotencyKey,
      "x-yfeistai-signature": signed.signature,
    },
  });
}

const cancelJob = createJobCancelHandler({
  readSecret: readServiceSecret,
  stores: [contentJobStore, exportJobStore],
  onCanceled: cancelPersistedRenderJob,
});

export async function POST(
  request: Request,
  context: { params: Promise<{ jobId: string }> },
): Promise<Response> {
  return cancelJob(request, context);
}

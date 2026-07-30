import { createHash } from "node:crypto";

import type { OutlineJob } from "./contracts";

export class IdempotencyConflictError extends Error {
  constructor() {
    super("idempotency key was reused with a different request");
    this.name = "IdempotencyConflictError";
  }
}

export interface OutlineSubmission {
  tenantId: string;
  jobId: string;
  idempotencyKey: string;
  action: "outline";
  canonicalBody: string;
}

interface StoredOutlineJob {
  idempotencyKey: string;
  action: "outline";
  bodySha256: string;
  job: Promise<OutlineJob>;
}

interface StoredIdempotencyBinding {
  jobId: string;
  bodySha256: string;
}

function storageKey(tenantId: string, jobId: string): string {
  return JSON.stringify([tenantId, jobId]);
}

function bodySha256(body: string): string {
  return createHash("sha256").update(body, "utf8").digest("hex");
}

export class OutlineJobStore {
  private readonly jobs = new Map<string, StoredOutlineJob>();
  private readonly idempotencyBindings = new Map<
    string,
    StoredIdempotencyBinding
  >();

  submit(
    submission: OutlineSubmission,
    createJob: () => Promise<OutlineJob>,
  ): Promise<OutlineJob> {
    const key = storageKey(submission.tenantId, submission.jobId);
    const digest = bodySha256(submission.canonicalBody);
    const idempotencyKey = JSON.stringify([
      submission.tenantId,
      submission.action,
      submission.idempotencyKey,
    ]);
    const existing = this.jobs.get(key);
    if (existing) {
      if (
        existing.idempotencyKey !== submission.idempotencyKey ||
        existing.action !== submission.action ||
        existing.bodySha256 !== digest
      ) {
        throw new IdempotencyConflictError();
      }
      return existing.job;
    }
    const existingBinding = this.idempotencyBindings.get(idempotencyKey);
    if (
      existingBinding &&
      (existingBinding.jobId !== submission.jobId ||
        existingBinding.bodySha256 !== digest)
    ) {
      throw new IdempotencyConflictError();
    }

    const job = Promise.resolve().then(createJob);
    this.jobs.set(key, {
      idempotencyKey: submission.idempotencyKey,
      action: submission.action,
      bodySha256: digest,
      job,
    });
    this.idempotencyBindings.set(idempotencyKey, {
      jobId: submission.jobId,
      bodySha256: digest,
    });
    return job;
  }

  read(tenantId: string, jobId: string): Promise<OutlineJob> | undefined {
    return this.jobs.get(storageKey(tenantId, jobId))?.job;
  }
}

const OUTLINE_JOB_STORE_SYMBOL = Symbol.for(
  "yfeistai.openmaic.outline-job-store",
);
const sharedScope = globalThis as typeof globalThis & {
  [key: symbol]: unknown;
};
const existingStore = sharedScope[OUTLINE_JOB_STORE_SYMBOL] as
  | OutlineJobStore
  | undefined;

export const outlineJobStore = existingStore ?? new OutlineJobStore();
if (!existingStore) {
  sharedScope[OUTLINE_JOB_STORE_SYMBOL] = outlineJobStore;
}

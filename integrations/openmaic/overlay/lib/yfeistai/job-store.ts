import { createHash } from "node:crypto";
import path from "node:path";

import type { OutlineJob, OutlineJobStatus } from "./contracts";
import {
  type DurableLeaseClaim,
  claimDurableLease,
  configuredOpenMaicStateRoot,
  durableFile,
  durableLeaseMatches,
  exactDurableRecord,
  isolatedOpenMaicStateRoot,
  readDurableJson,
  readDurableJsonWithMetadata,
  renewDurableLease,
  withDurableLock,
  writeDurableJsonExclusive,
} from "./durable-state";

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

interface StoredOutlineSubmission {
  version: 1;
  tenantId: string;
  jobId: string;
  idempotencyKey: string;
  action: "outline";
  bodySha256: string;
  createdAt: string;
}

function storageKey(tenantId: string, jobId: string): string {
  return JSON.stringify([tenantId, jobId]);
}

function bodySha256(body: string): string {
  return createHash("sha256").update(body, "utf8").digest("hex");
}

export class OutlineJobStore {
  private readonly completions = new Map<string, Promise<OutlineJob>>();

  constructor(
    private readonly stateRoot = isolatedOpenMaicStateRoot("outline-jobs"),
    private readonly leaseMilliseconds = 60_000,
    private readonly nowMilliseconds: () => number = Date.now,
    private readonly heartbeatEnabled = true,
  ) {}

  private submissionPath(tenantId: string, jobId: string): string {
    return durableFile(
      this.stateRoot,
      "outline-jobs",
      "jobs",
      [tenantId, jobId],
      "submission.json",
    );
  }

  private terminalPath(tenantId: string, jobId: string): string {
    return durableFile(
      this.stateRoot,
      "outline-jobs",
      "jobs",
      [tenantId, jobId],
      "terminal.json",
    );
  }

  private leasePath(tenantId: string, jobId: string): string {
    return durableFile(
      this.stateRoot,
      "outline-jobs",
      "jobs",
      [tenantId, jobId],
      "lease.json",
    );
  }

  private jobDirectory(tenantId: string, jobId: string): string {
    return path.dirname(this.submissionPath(tenantId, jobId));
  }

  private bindingPath(tenantId: string, idempotencyKey: string): string {
    return durableFile(
      this.stateRoot,
      "outline-jobs",
      "bindings",
      [tenantId, idempotencyKey],
      "binding.json",
    );
  }

  private readSubmission(
    tenantId: string,
    jobId: string,
  ): StoredOutlineSubmission | null {
    const durable = readDurableJsonWithMetadata(
      this.submissionPath(tenantId, jobId),
    );
    if (!durable) {
      return null;
    }
    const raw = durable.value;
    const fields = [
      "version",
      "tenantId",
      "jobId",
      "idempotencyKey",
      "action",
      "bodySha256",
    ];
    const hasCreatedAt = Object.prototype.hasOwnProperty.call(raw, "createdAt");
    const record = exactDurableRecord(
      raw,
      "outline submission record",
      hasCreatedAt ? [...fields, "createdAt"] : fields,
    );
    if (
      record.version !== 1 ||
      record.tenantId !== tenantId ||
      record.jobId !== jobId ||
      typeof record.idempotencyKey !== "string" ||
      record.action !== "outline" ||
      typeof record.bodySha256 !== "string" ||
      !/^[0-9a-f]{64}$/.test(record.bodySha256) ||
      (hasCreatedAt &&
        (typeof record.createdAt !== "string" ||
          !Number.isFinite(Date.parse(record.createdAt))))
    ) {
      throw new Error("outline submission record binding is invalid");
    }
    return {
      ...(record as unknown as Omit<StoredOutlineSubmission, "createdAt">),
      createdAt: hasCreatedAt
        ? (record.createdAt as string)
        : new Date(durable.mtimeMs).toISOString(),
    };
  }

  private runningJob(submission: StoredOutlineSubmission): OutlineJob {
    return {
      tenantId: submission.tenantId,
      jobId: submission.jobId,
      idempotencyKey: submission.idempotencyKey,
      phase: "outline",
      status: "running",
      createdAt: submission.createdAt,
      updatedAt: submission.createdAt,
    };
  }

  private readTerminal(tenantId: string, jobId: string): OutlineJob | null {
    const raw = readDurableJson(this.terminalPath(tenantId, jobId));
    if (!raw) {
      return null;
    }
    const record = exactDurableRecord(raw, "outline terminal record", [
      "version",
      "job",
    ]);
    const job = record.job as OutlineJob;
    if (
      record.version !== 1 ||
      !job ||
      job.tenantId !== tenantId ||
      job.jobId !== jobId ||
      !["succeeded", "failed", "canceled"].includes(job.status)
    ) {
      throw new Error("outline terminal record binding is invalid");
    }
    return JSON.parse(JSON.stringify(job)) as OutlineJob;
  }

  private async waitForTerminal(
    tenantId: string,
    jobId: string,
  ): Promise<OutlineJob> {
    for (let attempt = 0; attempt < 300; attempt += 1) {
      const terminal = this.readTerminal(tenantId, jobId);
      if (terminal) {
        return terminal;
      }
      await new Promise<void>((resolve) => setTimeout(resolve, 100));
    }
    throw new Error("outline job is durably running and cannot be duplicated");
  }

  private claimExecution(
    tenantId: string,
    jobId: string,
  ): DurableLeaseClaim | null {
    return claimDurableLease({
      directory: this.jobDirectory(tenantId, jobId),
      target: this.leasePath(tenantId, jobId),
      binding: storageKey(tenantId, jobId),
      leaseMilliseconds: this.leaseMilliseconds,
      now: this.nowMilliseconds(),
      mayClaim: () => !this.readTerminal(tenantId, jobId),
    });
  }

  private renewLease(
    tenantId: string,
    jobId: string,
    claim: DurableLeaseClaim,
  ): boolean {
    return renewDurableLease({
      directory: this.jobDirectory(tenantId, jobId),
      target: this.leasePath(tenantId, jobId),
      binding: storageKey(tenantId, jobId),
      claim,
      leaseMilliseconds: this.leaseMilliseconds,
      now: this.nowMilliseconds(),
      mayRenew: () => !this.readTerminal(tenantId, jobId),
    });
  }

  private persistTerminal(
    tenantId: string,
    jobId: string,
    claim: DurableLeaseClaim | undefined,
    job: OutlineJob,
  ): OutlineJob | null {
    return withDurableLock(this.jobDirectory(tenantId, jobId), () => {
      const existing = this.readTerminal(tenantId, jobId);
      if (existing) {
        return existing;
      }
      if (
        claim &&
        !durableLeaseMatches(
          this.leasePath(tenantId, jobId),
          storageKey(tenantId, jobId),
          claim,
          this.nowMilliseconds(),
        )
      ) {
        return null;
      }
      if (
        !writeDurableJsonExclusive(this.terminalPath(tenantId, jobId), {
          version: 1,
          job,
        })
      ) {
        return this.readTerminal(tenantId, jobId);
      }
      return JSON.parse(JSON.stringify(job)) as OutlineJob;
    });
  }

  private executeClaimed(
    submission: OutlineSubmission,
    claim: DurableLeaseClaim,
    createJob: () => Promise<OutlineJob>,
  ): Promise<OutlineJob> {
    const key = storageKey(submission.tenantId, submission.jobId);
    const heartbeat = this.heartbeatEnabled
      ? setInterval(
          () => {
            try {
              this.renewLease(submission.tenantId, submission.jobId, claim);
            } catch {
              // The terminal write is fenced and remains authoritative.
            }
          },
          Math.max(100, Math.floor(this.leaseMilliseconds / 3)),
        )
      : null;
    const completion = Promise.resolve()
      .then(createJob)
      .then(async (job) => {
        if (heartbeat) {
          clearInterval(heartbeat);
        }
        const stored = this.readSubmission(
          submission.tenantId,
          submission.jobId,
        );
        if (
          !stored ||
          job.tenantId !== submission.tenantId ||
          job.jobId !== submission.jobId ||
          job.idempotencyKey !== submission.idempotencyKey ||
          job.phase !== "outline" ||
          !["succeeded", "failed"].includes(job.status)
        ) {
          throw new Error("outline completion binding is invalid");
        }
        const boundJob: OutlineJob = {
          ...job,
          createdAt: stored.createdAt,
          updatedAt: new Date(this.nowMilliseconds()).toISOString(),
        };
        const persisted = this.persistTerminal(
          submission.tenantId,
          submission.jobId,
          claim,
          boundJob,
        );
        return (
          persisted ??
          (await this.waitForTerminal(submission.tenantId, submission.jobId))
        );
      })
      .catch(async () => {
        if (heartbeat) {
          clearInterval(heartbeat);
        }
        try {
          const stored = this.readSubmission(
            submission.tenantId,
            submission.jobId,
          );
          if (!stored) {
            throw new Error("outline submission state disappeared");
          }
          const failed: OutlineJob = {
            tenantId: submission.tenantId,
            jobId: submission.jobId,
            idempotencyKey: submission.idempotencyKey,
            phase: "outline",
            status: "failed",
            createdAt: stored.createdAt,
            updatedAt: new Date(this.nowMilliseconds()).toISOString(),
            error: {
              code: "OUTLINE_GENERATION_FAILED",
              message: "Outline generation failed.",
            },
          };
          const persisted = this.persistTerminal(
            submission.tenantId,
            submission.jobId,
            claim,
            failed,
          );
          return (
            persisted ??
            (await this.waitForTerminal(submission.tenantId, submission.jobId))
          );
        } catch {
          console.error("OpenMAIC outline terminal persistence failed.");
          throw new Error("outline terminal persistence failed");
        }
      });
    this.completions.set(key, completion);
    void completion.then(
      () => this.completions.delete(key),
      () => this.completions.delete(key),
    );
    return completion;
  }

  submit(
    submission: OutlineSubmission,
    createJob: () => Promise<OutlineJob>,
  ): Promise<OutlineJob> {
    const key = storageKey(submission.tenantId, submission.jobId);
    const digest = bodySha256(submission.canonicalBody);
    const binding = {
      version: 1,
      tenantId: submission.tenantId,
      idempotencyKey: submission.idempotencyKey,
      action: submission.action,
      jobId: submission.jobId,
      bodySha256: digest,
    };
    const bindingPath = this.bindingPath(
      submission.tenantId,
      submission.idempotencyKey,
    );
    if (!writeDurableJsonExclusive(bindingPath, binding)) {
      const existing = readDurableJson(bindingPath);
      if (JSON.stringify(existing) !== JSON.stringify(binding)) {
        throw new IdempotencyConflictError();
      }
    }
    const persistedSubmission: StoredOutlineSubmission = {
      version: 1,
      tenantId: submission.tenantId,
      jobId: submission.jobId,
      idempotencyKey: submission.idempotencyKey,
      action: submission.action,
      bodySha256: digest,
      createdAt: new Date(this.nowMilliseconds()).toISOString(),
    };
    const submissionPath = this.submissionPath(
      submission.tenantId,
      submission.jobId,
    );
    if (!writeDurableJsonExclusive(submissionPath, persistedSubmission)) {
      const existing = this.readSubmission(
        submission.tenantId,
        submission.jobId,
      );
      if (
        !existing ||
        existing.idempotencyKey !== submission.idempotencyKey ||
        existing.action !== submission.action ||
        existing.bodySha256 !== digest
      ) {
        throw new IdempotencyConflictError();
      }
      const active = this.completions.get(key);
      if (active) {
        return active;
      }
      const terminal = this.readTerminal(submission.tenantId, submission.jobId);
      if (terminal) {
        return Promise.resolve(terminal);
      }
    }
    const claim = this.claimExecution(submission.tenantId, submission.jobId);
    if (!claim) {
      const terminal = this.readTerminal(submission.tenantId, submission.jobId);
      return terminal
        ? Promise.resolve(terminal)
        : this.waitForTerminal(submission.tenantId, submission.jobId);
    }
    return this.executeClaimed(submission, claim, createJob);
  }

  start(
    submission: OutlineSubmission,
    createJob: () => Promise<OutlineJob>,
  ): OutlineJob {
    const completion = this.submit(submission, createJob);
    void completion.catch(() => undefined);
    const terminal = this.readTerminal(submission.tenantId, submission.jobId);
    if (terminal) {
      return terminal;
    }
    const stored = this.readSubmission(submission.tenantId, submission.jobId);
    if (!stored) {
      throw new Error("outline submission state disappeared");
    }
    return this.runningJob(stored);
  }

  read(tenantId: string, jobId: string): Promise<OutlineJob> | undefined {
    const submission = this.readSubmission(tenantId, jobId);
    if (!submission) {
      return undefined;
    }
    const terminal = this.readTerminal(tenantId, jobId);
    return Promise.resolve(terminal ?? this.runningJob(submission));
  }

  async cancel(
    tenantId: string,
    jobId: string,
  ): Promise<OutlineJobStatus | null> {
    const submission = this.readSubmission(tenantId, jobId);
    if (!submission) {
      return null;
    }
    const terminal = this.readTerminal(tenantId, jobId);
    if (terminal) {
      return terminal.status;
    }
    const canceled = this.persistTerminal(tenantId, jobId, undefined, {
      ...this.runningJob(submission),
      status: "canceled",
      updatedAt: new Date(this.nowMilliseconds()).toISOString(),
      error: {
        code: "JOB_CANCELED",
        message: "The job was canceled.",
      },
    });
    if (!canceled) {
      throw new Error("outline cancellation terminal disappeared");
    }
    return canceled.status;
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

export const outlineJobStore =
  existingStore ?? new OutlineJobStore(configuredOpenMaicStateRoot());
if (!existingStore) {
  sharedScope[OUTLINE_JOB_STORE_SYMBOL] = outlineJobStore;
}

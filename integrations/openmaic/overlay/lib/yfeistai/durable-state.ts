import { createHash, randomUUID } from "node:crypto";
import {
  constants as fsConstants,
  closeSync,
  existsSync,
  fstatSync,
  fsyncSync,
  linkSync,
  lstatSync,
  mkdirSync,
  openSync,
  readFileSync,
  renameSync,
  rmdirSync,
  statSync,
  unlinkSync,
  writeFileSync,
} from "node:fs";
import { hostname, tmpdir } from "node:os";
import path from "node:path";

const SAFE_NAMESPACE = /^[a-z][a-z0-9-]{0,63}$/;
const PROCESS_INSTANCE_ID = randomUUID();

function errorCode(error: unknown): string | null {
  return error !== null && typeof error === "object" && "code" in error
    ? String(error.code)
    : null;
}

function syncDirectory(directory: string): void {
  const descriptor = openSync(directory, "r");
  try {
    if (!fstatSync(descriptor).isDirectory()) {
      throw new Error("durable state parent is not a directory");
    }
    try {
      fsyncSync(descriptor);
    } catch (error) {
      if (
        process.platform === "win32" &&
        ["EINVAL", "ENOTSUP", "EPERM"].includes(errorCode(error) ?? "")
      ) {
        return;
      }
      throw error;
    }
  } finally {
    closeSync(descriptor);
  }
}

function syncParentDirectory(target: string): void {
  syncDirectory(path.dirname(target));
}

export function configuredOpenMaicStateRoot(): string {
  const configured = process.env.YFEISTAI_OPENMAIC_STATE_ROOT?.trim();
  return path.resolve(
    configured || path.join(tmpdir(), "yfeistai-openmaic-state"),
  );
}

export function isolatedOpenMaicStateRoot(scope: string): string {
  return path.join(
    tmpdir(),
    "yfeistai-openmaic-isolated",
    `${scope}-${process.pid}-${randomUUID()}`,
  );
}

function digestPart(value: string): string {
  if (typeof value !== "string" || value.length === 0 || value.includes("\0")) {
    throw new Error("durable state identifier is invalid");
  }
  return createHash("sha256").update(value, "utf8").digest("hex");
}

function ensureSafeDirectory(target: string): void {
  const resolved = path.resolve(target);
  const parsed = path.parse(resolved);
  const relation = path.relative(parsed.root, resolved);
  let current = parsed.root;
  for (const segment of relation.split(path.sep).filter(Boolean)) {
    const parent = current;
    const parentBefore = lstatSync(parent);
    current = path.join(parent, segment);
    let created = false;
    let stat: ReturnType<typeof lstatSync>;
    try {
      stat = lstatSync(current);
    } catch (error) {
      if (errorCode(error) !== "ENOENT") {
        throw error;
      }
      try {
        mkdirSync(current, { mode: 0o700 });
        created = true;
      } catch (mkdirError) {
        if (errorCode(mkdirError) !== "EEXIST") {
          throw mkdirError;
        }
      }
      stat = lstatSync(current);
    }
    const parentAfter = lstatSync(parent);
    if (
      parentBefore.isSymbolicLink() ||
      !parentBefore.isDirectory() ||
      parentAfter.isSymbolicLink() ||
      !parentAfter.isDirectory() ||
      parentBefore.dev !== parentAfter.dev ||
      parentBefore.ino !== parentAfter.ino
    ) {
      throw new Error("durable state parent changed during directory creation");
    }
    if (stat.isSymbolicLink() || !stat.isDirectory()) {
      throw new Error("durable state path contains an unsafe parent");
    }
    if (created) {
      syncDirectory(parent);
    }
  }
}

export function durableDirectory(
  root: string,
  namespace: string,
  ...identifiers: string[]
): string {
  if (!SAFE_NAMESPACE.test(namespace)) {
    throw new Error("durable state namespace is invalid");
  }
  const target = path.join(
    path.resolve(root),
    namespace,
    ...identifiers.map(digestPart),
  );
  ensureSafeDirectory(target);
  return target;
}

export function durableFile(
  root: string,
  namespace: string,
  category: string,
  identifiers: string[],
  name: string,
): string {
  if (!SAFE_NAMESPACE.test(category) || !/^[a-z][a-z0-9-]*\.json$/.test(name)) {
    throw new Error("durable state file name is invalid");
  }
  return path.join(
    durableDirectory(root, namespace, category, ...identifiers),
    name,
  );
}

export function readDurableJson(target: string): unknown | null {
  let descriptor: number;
  try {
    descriptor = openSync(
      target,
      fsConstants.O_RDONLY | (fsConstants.O_NOFOLLOW ?? 0),
    );
  } catch (error) {
    if (errorCode(error) === "ENOENT") {
      return null;
    }
    if (errorCode(error) === "ELOOP") {
      throw new Error("durable state record is unsafe");
    }
    throw error;
  }
  try {
    const descriptorStat = fstatSync(descriptor);
    const targetStat = lstatSync(target);
    if (
      targetStat.isSymbolicLink() ||
      !targetStat.isFile() ||
      !descriptorStat.isFile() ||
      descriptorStat.dev !== targetStat.dev ||
      descriptorStat.ino !== targetStat.ino
    ) {
      throw new Error("durable state record is unsafe");
    }
    const body = readFileSync(descriptor, "utf8");
    try {
      return JSON.parse(body) as unknown;
    } catch {
      throw new Error("durable state record is corrupt");
    }
  } finally {
    closeSync(descriptor);
  }
}

export function writeDurableJsonExclusive(
  target: string,
  value: unknown,
): boolean {
  ensureSafeDirectory(path.dirname(target));
  const temporary = `${target}.tmp-${process.pid}-${randomUUID()}`;
  const body = JSON.stringify(value);
  const descriptor = openSync(temporary, "wx", 0o600);
  try {
    writeFileSync(descriptor, body, { encoding: "utf8" });
    fsyncSync(descriptor);
  } finally {
    closeSync(descriptor);
  }
  try {
    try {
      linkSync(temporary, target);
      syncParentDirectory(target);
      return true;
    } catch (error) {
      if (
        error === null ||
        typeof error !== "object" ||
        !("code" in error) ||
        error.code !== "EEXIST"
      ) {
        throw error;
      }
    }
    try {
      const existing = readDurableJson(target);
      if (existing !== null) {
        return false;
      }
      try {
        linkSync(temporary, target);
        syncParentDirectory(target);
        return true;
      } catch (retryError) {
        if (errorCode(retryError) !== "EEXIST") {
          throw retryError;
        }
        const winner = readDurableJson(target);
        if (winner !== null) {
          return false;
        }
        throw new Error("durable state record changed during publication");
      }
    } catch (error) {
      if (
        !(error instanceof Error) ||
        error.message !== "durable state record is corrupt"
      ) {
        throw error;
      }
      const repairDirectory = path.join(
        path.dirname(target),
        "..",
        ".yfeistai-repair-locks",
        digestPart(path.resolve(target)),
      );
      return withDurableLock(repairDirectory, () => {
        try {
          const current = readDurableJson(target);
          if (current !== null) {
            return false;
          }
        } catch (repairError) {
          if (
            !(repairError instanceof Error) ||
            repairError.message !== "durable state record is corrupt"
          ) {
            throw repairError;
          }
          if (existsSync(target)) {
            renameSync(
              target,
              path.join(
                repairDirectory,
                `corrupt-${process.pid}-${randomUUID()}.json`,
              ),
            );
            syncParentDirectory(target);
            syncDirectory(repairDirectory);
          }
        }
        try {
          linkSync(temporary, target);
          syncParentDirectory(target);
          return true;
        } catch (error) {
          if (
            error !== null &&
            typeof error === "object" &&
            "code" in error &&
            error.code === "EEXIST"
          ) {
            readDurableJson(target);
            return false;
          }
          throw error;
        }
      });
    }
  } finally {
    try {
      unlinkSync(temporary);
    } catch {
      // A process crash may leave a harmless uniquely named orphan temp.
    }
  }
}

export function writeDurableJsonAtomic(target: string, value: unknown): void {
  ensureSafeDirectory(path.dirname(target));
  const temporary = `${target}.tmp-${process.pid}-${randomUUID()}`;
  const descriptor = openSync(temporary, "wx", 0o600);
  try {
    writeFileSync(descriptor, JSON.stringify(value), { encoding: "utf8" });
    fsyncSync(descriptor);
  } finally {
    closeSync(descriptor);
  }
  renameSync(temporary, target);
  syncParentDirectory(target);
}

const lockWaitBuffer = new Int32Array(new SharedArrayBuffer(4));

export function withDurableLock<T>(directory: string, action: () => T): T {
  ensureSafeDirectory(directory);
  const lockPath = path.join(directory, ".state-lock");
  const owner = randomUUID();
  const ownerRecord = JSON.stringify({
    version: 2,
    owner,
    pid: process.pid,
    hostname: hostname(),
    processInstanceId: PROCESS_INSTANCE_ID,
  });
  const deadline = Date.now() + 5_000;
  for (;;) {
    try {
      mkdirSync(lockPath, { mode: 0o700 });
      syncDirectory(directory);
      const ownerPath = path.join(lockPath, "owner");
      const ownerDescriptor = openSync(ownerPath, "wx", 0o600);
      try {
        writeFileSync(ownerDescriptor, ownerRecord, { encoding: "utf8" });
        fsyncSync(ownerDescriptor);
      } finally {
        closeSync(ownerDescriptor);
      }
      syncDirectory(lockPath);
      break;
    } catch (error) {
      if (
        error === null ||
        typeof error !== "object" ||
        !("code" in error) ||
        error.code !== "EEXIST"
      ) {
        throw error;
      }
      let age = 0;
      try {
        age = Date.now() - statSync(lockPath).mtimeMs;
      } catch {
        continue;
      }
      let ownerIsAlive = true;
      if (age > 30_000) {
        try {
          const record = JSON.parse(
            readFileSync(path.join(lockPath, "owner"), "utf8"),
          ) as {
            version?: unknown;
            pid?: unknown;
            hostname?: unknown;
            processInstanceId?: unknown;
          };
          if (
            record.version === 2 &&
            record.hostname === hostname() &&
            Number.isSafeInteger(record.pid) &&
            (record.pid as number) > 0 &&
            typeof record.processInstanceId === "string" &&
            record.processInstanceId.length > 0
          ) {
            if (
              record.pid === process.pid &&
              record.processInstanceId !== PROCESS_INSTANCE_ID
            ) {
              ownerIsAlive = false;
            } else {
              try {
                process.kill(record.pid as number, 0);
              } catch {
                ownerIsAlive = false;
              }
            }
          }
        } catch {
          ownerIsAlive = false;
        }
      }
      if (age > 30_000 && !ownerIsAlive) {
        try {
          renameSync(
            lockPath,
            path.join(directory, `.state-lock-stale-${randomUUID()}`),
          );
          syncDirectory(directory);
          continue;
        } catch {
          // Another contender recovered it first. Re-enter the bounded wait.
        }
      }
      if (Date.now() >= deadline) {
        throw new Error("durable state lock is busy");
      }
      Atomics.wait(lockWaitBuffer, 0, 0, 10);
    }
  }
  try {
    return action();
  } finally {
    try {
      const ownerPath = path.join(lockPath, "owner");
      const ownerDescriptor = openSync(ownerPath, "r");
      const descriptorStat = fstatSync(ownerDescriptor);
      const ownerStat = lstatSync(ownerPath);
      try {
        if (
          !ownerStat.isSymbolicLink() &&
          ownerStat.isFile() &&
          descriptorStat.dev === ownerStat.dev &&
          descriptorStat.ino === ownerStat.ino &&
          readFileSync(ownerDescriptor, "utf8") === ownerRecord
        ) {
          unlinkSync(ownerPath);
          rmdirSync(lockPath);
          syncDirectory(directory);
        }
      } finally {
        closeSync(ownerDescriptor);
      }
    } catch {
      // A stale-lock recovery may already have fenced this owner out.
    }
  }
}

export function exactDurableRecord(
  value: unknown,
  label: string,
  keys: readonly string[],
): Record<string, unknown> {
  if (value === null || typeof value !== "object" || Array.isArray(value)) {
    throw new Error(`${label} is corrupt`);
  }
  const record = value as Record<string, unknown>;
  const expected = new Set(keys);
  if (
    Object.keys(record).length !== keys.length ||
    Object.keys(record).some((key) => !expected.has(key)) ||
    keys.some((key) => !(key in record))
  ) {
    throw new Error(`${label} is corrupt`);
  }
  return record;
}

export interface DurableLeaseClaim {
  owner: string;
  fence: number;
}

export interface DurableLeaseState extends DurableLeaseClaim {
  expiresAt: number;
  updatedAt: string;
}

function leaseBindingSha256(binding: string): string {
  return createHash("sha256").update(binding, "utf8").digest("hex");
}

export function readDurableLease(
  target: string,
  binding: string,
): DurableLeaseState | null {
  const raw = readDurableJson(target);
  if (!raw) {
    return null;
  }
  const record = exactDurableRecord(raw, "durable lease record", [
    "version",
    "bindingSha256",
    "owner",
    "fence",
    "expiresAt",
    "updatedAt",
  ]);
  if (
    record.version !== 1 ||
    record.bindingSha256 !== leaseBindingSha256(binding) ||
    typeof record.owner !== "string" ||
    record.owner.length === 0 ||
    !Number.isSafeInteger(record.fence) ||
    (record.fence as number) <= 0 ||
    !Number.isSafeInteger(record.expiresAt) ||
    typeof record.updatedAt !== "string" ||
    !Number.isFinite(Date.parse(record.updatedAt))
  ) {
    throw new Error("durable lease record binding is invalid");
  }
  return {
    owner: record.owner,
    fence: record.fence,
    expiresAt: record.expiresAt,
    updatedAt: record.updatedAt,
  } as DurableLeaseState;
}

function writeLease(
  target: string,
  binding: string,
  claim: DurableLeaseClaim,
  leaseMilliseconds: number,
  now: number,
): void {
  writeDurableJsonAtomic(target, {
    version: 1,
    bindingSha256: leaseBindingSha256(binding),
    ...claim,
    expiresAt: now + leaseMilliseconds,
    updatedAt: new Date(now).toISOString(),
  });
}

export function claimDurableLease(input: {
  directory: string;
  target: string;
  binding: string;
  leaseMilliseconds: number;
  now: number;
  mayClaim?: () => boolean;
}): DurableLeaseClaim | null {
  return withDurableLock(input.directory, () => {
    if (input.mayClaim && !input.mayClaim()) {
      return null;
    }
    const current = readDurableLease(input.target, input.binding);
    if (current && current.expiresAt > input.now) {
      return null;
    }
    const claim = {
      owner: randomUUID(),
      fence: (current?.fence ?? 0) + 1,
    };
    writeLease(
      input.target,
      input.binding,
      claim,
      input.leaseMilliseconds,
      input.now,
    );
    return claim;
  });
}

export function renewDurableLease(input: {
  directory: string;
  target: string;
  binding: string;
  claim: DurableLeaseClaim;
  leaseMilliseconds: number;
  now: number;
  mayRenew?: () => boolean;
}): boolean {
  return withDurableLock(input.directory, () => {
    if (input.mayRenew && !input.mayRenew()) {
      return false;
    }
    const current = readDurableLease(input.target, input.binding);
    if (
      !current ||
      current.owner !== input.claim.owner ||
      current.fence !== input.claim.fence
    ) {
      return false;
    }
    writeLease(
      input.target,
      input.binding,
      input.claim,
      input.leaseMilliseconds,
      input.now,
    );
    return true;
  });
}

export function durableLeaseMatches(
  target: string,
  binding: string,
  claim: DurableLeaseClaim,
  now = Date.now(),
): boolean {
  const current = readDurableLease(target, binding);
  return (
    current?.owner === claim.owner &&
    current.fence === claim.fence &&
    current.expiresAt > now
  );
}

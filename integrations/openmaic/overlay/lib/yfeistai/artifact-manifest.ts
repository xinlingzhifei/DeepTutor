import { createHash, randomUUID } from "node:crypto";
import {
  constants as fsConstants,
  existsSync,
  lstatSync,
  mkdirSync,
  promises as fs,
  readFileSync,
  readdirSync,
} from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";

import {
  type ArtifactRouteContext,
  type ServiceBoundaryDependencies,
  authenticateServiceRequest,
  serviceError,
} from "./service-boundary";
import { writeDurableJsonExclusive } from "./durable-state";

const SHA256_HEX = /^[0-9a-f]{64}$/;
const SAFE_JOB_PART = /^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/;
const WINDOWS_DEVICE_NAME = /^(?:con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\..*)?$/i;
const MIME_TYPE =
  /^[A-Za-z0-9!#$&^_.+-]+\/[A-Za-z0-9!#$&^_.+-]+(?:\s*;\s*[A-Za-z0-9!#$&^_.+-]+=[A-Za-z0-9!#$&^_.+:"'-]+)*$/;
export const MAX_ARTIFACT_BYTES = 256 * 1024 * 1024;

function startsWithBytes(
  bytes: Uint8Array,
  signature: readonly number[],
): boolean {
  return signature.every((value, index) => bytes[index] === value);
}

export function assertArtifactMimeBytes(bytes: Uint8Array, mime: string): void {
  if (!(bytes instanceof Uint8Array) || bytes.byteLength === 0) {
    throw new Error("artifact bytes are missing");
  }
  if (bytes.byteLength > MAX_ARTIFACT_BYTES) {
    throw new Error("artifact exceeds the maximum byte size");
  }
  const normalized = mime.toLowerCase().split(";", 1)[0].trim();
  const ascii = (start: number, end: number) =>
    String.fromCharCode(...bytes.slice(start, end));
  const valid =
    normalized === "image/png"
      ? startsWithBytes(bytes, [137, 80, 78, 71, 13, 10, 26, 10])
      : normalized === "image/jpeg"
        ? startsWithBytes(bytes, [255, 216, 255])
        : normalized === "image/gif"
          ? ascii(0, 6) === "GIF87a" || ascii(0, 6) === "GIF89a"
          : normalized === "image/webp"
            ? ascii(0, 4) === "RIFF" && ascii(8, 12) === "WEBP"
            : normalized === "audio/mpeg"
              ? ascii(0, 3) === "ID3" ||
                (bytes[0] === 255 && (bytes[1] & 0xe0) === 0xe0)
              : normalized === "audio/wav" || normalized === "audio/x-wav"
                ? ascii(0, 4) === "RIFF" && ascii(8, 12) === "WAVE"
                : normalized === "video/mp4"
                  ? ascii(4, 8) === "ftyp"
                  : normalized === "application/zip" ||
                      normalized ===
                        "application/vnd.openxmlformats-officedocument.presentationml.presentation"
                    ? startsWithBytes(bytes, [80, 75, 3, 4]) ||
                      startsWithBytes(bytes, [80, 75, 5, 6])
                    : true;
  if (!valid) {
    throw new Error("artifact MIME signature does not match its bytes");
  }
}

export interface ArtifactEntry {
  relativePath: string;
  sha256: string;
  bytes: number;
  mime: string;
  downloadPath: string;
  expiresAt: string;
}

export interface CreateArtifactEntryInput {
  relativePath: string;
  bytes: Uint8Array;
  mime: string;
  expiresAt: string;
  expectedSha256?: string;
}

interface ArtifactStat {
  isSymbolicLink(): boolean;
  isDirectory?(): boolean;
}

export interface AssertArtifactReadTargetInput {
  root: string;
  relativePath: string;
  manifest?: readonly ArtifactEntry[];
  now?: Date;
  lstat?: (target: string) => Promise<ArtifactStat>;
}

export interface StoredArtifact {
  entry: ArtifactEntry;
  root: string;
}

export interface StoredArtifactBytes extends StoredArtifact {
  bytes: Uint8Array;
}

export interface AssertArtifactWriteTargetInput {
  baseRoot: string;
  tenantId: string;
  jobId: string;
  relativePath: string;
  lstat?: (target: string) => Promise<ArtifactStat>;
  mkdir?: (target: string) => Promise<void>;
}

export function normalizeArtifactPath(value: string): string {
  if (
    typeof value !== "string" ||
    value.length === 0 ||
    value.includes("\0") ||
    value.normalize("NFC") !== value ||
    /%[0-9A-Fa-f]{2}/.test(value) ||
    value.includes("\\") ||
    /[\u0000-\u001f\u007f<>:"|?*]/.test(value) ||
    value.startsWith("/") ||
    /^[A-Za-z]:/.test(value) ||
    /^[A-Za-z][A-Za-z0-9+.-]*:/.test(value)
  ) {
    throw new Error("artifact path must be a portable relative path");
  }
  const segments = value.split("/");
  if (
    segments.some(
      (segment) =>
        segment.length === 0 ||
        segment === "." ||
        segment === ".." ||
        segment.trim() !== segment ||
        segment.endsWith(".") ||
        WINDOWS_DEVICE_NAME.test(segment),
    )
  ) {
    throw new Error("artifact path must be a portable relative path");
  }
  const normalized = path.posix.normalize(value);
  if (normalized !== value || normalized.startsWith("../")) {
    throw new Error("artifact path must be a portable relative path");
  }
  return normalized;
}

function parseExpiry(value: string): number {
  if (typeof value !== "string" || !/(?:Z|[+-]\d{2}:\d{2})$/.test(value)) {
    throw new Error("artifact expiry must be an aware ISO timestamp");
  }
  const parsed = Date.parse(value);
  if (!Number.isFinite(parsed)) {
    throw new Error("artifact expiry must be an aware ISO timestamp");
  }
  return parsed;
}

function normalizeMime(value: string): string {
  if (typeof value !== "string" || !MIME_TYPE.test(value)) {
    throw new Error("artifact MIME type is invalid");
  }
  return value;
}

export function createArtifactEntry(
  input: CreateArtifactEntryInput,
): ArtifactEntry {
  const relativePath = normalizeArtifactPath(input.relativePath);
  const sha256 = createHash("sha256").update(input.bytes).digest("hex");
  if (
    input.expectedSha256 !== undefined &&
    (!SHA256_HEX.test(input.expectedSha256) || input.expectedSha256 !== sha256)
  ) {
    throw new Error("artifact hash mismatch");
  }
  parseExpiry(input.expiresAt);
  return {
    relativePath,
    sha256,
    bytes: input.bytes.byteLength,
    mime: normalizeMime(input.mime),
    downloadPath: relativePath,
    expiresAt: input.expiresAt,
  };
}

export async function assertArtifactReadTarget(
  input: AssertArtifactReadTargetInput,
): Promise<string> {
  const relativePath = normalizeArtifactPath(input.relativePath);
  const root = path.resolve(input.root);
  const target = path.resolve(root, ...relativePath.split("/"));
  const relation = path.relative(root, target);
  if (
    relation.length === 0 ||
    relation === ".." ||
    relation.startsWith(`..${path.sep}`) ||
    path.isAbsolute(relation)
  ) {
    throw new Error("artifact path escapes its task root");
  }

  const manifestEntry = input.manifest?.find(
    (entry) => entry.relativePath === relativePath,
  );
  if (input.manifest && !manifestEntry) {
    throw new Error("artifact is not present in the task manifest");
  }
  if (
    manifestEntry &&
    parseExpiry(manifestEntry.expiresAt) <= (input.now ?? new Date()).getTime()
  ) {
    throw new Error("artifact download has expired");
  }

  const lstat = input.lstat ?? fs.lstat;
  let current = root;
  const rootStat = await lstat(current);
  if (rootStat.isSymbolicLink() || rootStat.isDirectory?.() === false) {
    throw new Error("artifact task root is unsafe");
  }
  for (const segment of relativePath.split("/")) {
    current = path.join(current, segment);
    const stat = await lstat(current);
    if (stat.isSymbolicLink()) {
      throw new Error("artifact path contains a symbolic link");
    }
  }
  return target;
}

function sameResolvedPath(left: string, right: string): boolean {
  return process.platform === "win32"
    ? path.resolve(left).toLowerCase() === path.resolve(right).toLowerCase()
    : path.resolve(left) === path.resolve(right);
}

async function readArtifactFromSameHandle(input: {
  root: string;
  relativePath: string;
  manifest: readonly ArtifactEntry[];
  now?: Date;
}): Promise<Buffer> {
  const target = await assertArtifactReadTarget(input);
  const root = path.resolve(input.root);
  const realRoot = await fs.realpath(root);
  if (!sameResolvedPath(root, realRoot)) {
    throw new Error("artifact task root must not be a symbolic link");
  }
  const flags =
    fsConstants.O_RDONLY |
    (typeof fsConstants.O_NOFOLLOW === "number" ? fsConstants.O_NOFOLLOW : 0);
  const handle = await fs.open(target, flags);
  try {
    const [handleStat, targetLstat, targetStat, realTarget] = await Promise.all(
      [handle.stat(), fs.lstat(target), fs.stat(target), fs.realpath(target)],
    );
    const relation = path.relative(realRoot, realTarget);
    if (
      targetLstat.isSymbolicLink() ||
      !handleStat.isFile() ||
      !targetStat.isFile() ||
      !sameResolvedPath(target, realTarget) ||
      relation === ".." ||
      relation.startsWith(`..${path.sep}`) ||
      path.isAbsolute(relation) ||
      handleStat.size !== targetStat.size ||
      (handleStat.ino !== 0 &&
        targetStat.ino !== 0 &&
        (handleStat.ino !== targetStat.ino ||
          handleStat.dev !== targetStat.dev))
    ) {
      throw new Error("artifact changed during secure open");
    }
    return await handle.readFile();
  } finally {
    await handle.close();
  }
}

async function createDirectory(target: string): Promise<void> {
  try {
    await fs.mkdir(target, { mode: 0o700 });
  } catch (error) {
    if (
      !(
        error !== null &&
        typeof error === "object" &&
        "code" in error &&
        error.code === "EEXIST"
      )
    ) {
      throw error;
    }
  }
}

export async function assertArtifactWriteTarget(
  input: AssertArtifactWriteTargetInput,
): Promise<string> {
  const tenantId = safeJobPart(input.tenantId, "tenant id");
  const jobId = safeJobPart(input.jobId, "job id");
  const relativePath = normalizeArtifactPath(input.relativePath);
  const baseRoot = path.resolve(input.baseRoot);
  const root = path.resolve(baseRoot, tenantId, jobId);
  const target = path.resolve(root, ...relativePath.split("/"));
  const relation = path.relative(root, target);
  if (
    relation.length === 0 ||
    relation === ".." ||
    relation.startsWith(`..${path.sep}`) ||
    path.isAbsolute(relation)
  ) {
    throw new Error("artifact path escapes its task root");
  }

  const mkdir = input.mkdir ?? createDirectory;
  const lstat = input.lstat ?? fs.lstat;
  const directoryParts = [
    tenantId,
    jobId,
    ...relativePath.split("/").slice(0, -1),
  ];
  await mkdir(baseRoot);
  let current = baseRoot;
  const baseStat = await lstat(current);
  if (baseStat.isSymbolicLink() || baseStat.isDirectory?.() === false) {
    throw new Error("artifact write path contains an unsafe parent");
  }
  for (const segment of directoryParts) {
    current = path.join(current, segment);
    await mkdir(current);
    const stat = await lstat(current);
    if (stat.isSymbolicLink() || stat.isDirectory?.() === false) {
      throw new Error("artifact write path contains an unsafe parent");
    }
  }
  return target;
}

function safeJobPart(value: string, label: string): string {
  if (
    !SAFE_JOB_PART.test(value) ||
    value.endsWith(".") ||
    WINDOWS_DEVICE_NAME.test(value)
  ) {
    throw new Error(`${label} is not a safe task identifier`);
  }
  return value;
}

export class ArtifactStore {
  constructor(
    private readonly baseRoot = path.join(
      tmpdir(),
      "yfeistai-openmaic-artifacts",
    ),
  ) {}

  rootFor(tenantId: string, jobId: string): string {
    return path.join(
      this.baseRoot,
      safeJobPart(tenantId, "tenant id"),
      safeJobPart(jobId, "job id"),
    );
  }

  private manifestRoot(tenantId: string, jobId: string): string {
    return path.join(this.rootFor(tenantId, jobId), ".yfeistai-manifest");
  }

  private manifestPath(
    tenantId: string,
    jobId: string,
    relativePath: string,
  ): string {
    const normalized = normalizeArtifactPath(relativePath);
    const name = createHash("sha256").update(normalized).digest("hex");
    return path.join(this.manifestRoot(tenantId, jobId), `${name}.json`);
  }

  private readManifestEntry(
    tenantId: string,
    jobId: string,
    relativePath: string,
  ): ArtifactEntry | null {
    const normalized = normalizeArtifactPath(relativePath);
    const manifestPath = this.manifestPath(tenantId, jobId, normalized);
    if (!existsSync(manifestPath)) {
      return null;
    }
    const manifestStat = lstatSync(manifestPath);
    if (manifestStat.isSymbolicLink() || !manifestStat.isFile()) {
      throw new Error("artifact manifest is unsafe");
    }
    let value: unknown;
    try {
      value = JSON.parse(readFileSync(manifestPath, "utf8"));
    } catch {
      throw new Error("artifact manifest is corrupt");
    }
    if (value === null || typeof value !== "object" || Array.isArray(value)) {
      throw new Error("artifact manifest is corrupt");
    }
    const record = value as Record<string, unknown>;
    const keys = [
      "relativePath",
      "sha256",
      "bytes",
      "mime",
      "downloadPath",
      "expiresAt",
    ];
    if (
      Object.keys(record).length !== keys.length ||
      keys.some((key) => !(key in record)) ||
      record.relativePath !== normalized ||
      typeof record.downloadPath !== "string" ||
      record.downloadPath.length === 0 ||
      typeof record.sha256 !== "string" ||
      !SHA256_HEX.test(record.sha256) ||
      !Number.isSafeInteger(record.bytes) ||
      (record.bytes as number) <= 0 ||
      typeof record.mime !== "string" ||
      !MIME_TYPE.test(record.mime) ||
      typeof record.expiresAt !== "string"
    ) {
      throw new Error("artifact manifest is corrupt");
    }
    parseExpiry(record.expiresAt);
    return record as unknown as ArtifactEntry;
  }

  async put(input: {
    tenantId: string;
    jobId: string;
    relativePath: string;
    bytes: Uint8Array;
    mime: string;
    expiresAt: string;
    expectedSha256?: string;
    assertPublicationActive?: () => void;
  }): Promise<ArtifactEntry> {
    assertArtifactMimeBytes(input.bytes, input.mime);
    const relativePath = normalizeArtifactPath(input.relativePath);
    const entry = {
      ...createArtifactEntry({ ...input, relativePath }),
      downloadPath:
        `/api/yfeistai/v1/artifacts/${encodeURIComponent(input.jobId)}/` +
        relativePath.split("/").map(encodeURIComponent).join("/"),
    };
    const target = await assertArtifactWriteTarget({
      baseRoot: this.baseRoot,
      tenantId: input.tenantId,
      jobId: input.jobId,
      relativePath: entry.relativePath,
    });

    const recoverPersisted = async (): Promise<ArtifactEntry | null> => {
      let persisted: ArtifactEntry | null;
      try {
        persisted = this.readManifestEntry(
          input.tenantId,
          input.jobId,
          entry.relativePath,
        );
      } catch (error) {
        if (
          error instanceof Error &&
          error.message.includes("artifact manifest is corrupt")
        ) {
          return null;
        }
        throw error;
      }
      if (!persisted) {
        return null;
      }
      if (
        persisted.relativePath !== entry.relativePath ||
        persisted.sha256 !== entry.sha256 ||
        persisted.bytes !== entry.bytes ||
        persisted.mime !== entry.mime
      ) {
        throw new Error("artifact replay conflicts with persisted manifest");
      }
      const bytes = await readArtifactFromSameHandle({
        root: this.rootFor(input.tenantId, input.jobId),
        relativePath: persisted.relativePath,
        manifest: [persisted],
        now: new Date(0),
      });
      if (
        bytes.byteLength !== persisted.bytes ||
        createHash("sha256").update(bytes).digest("hex") !== persisted.sha256
      ) {
        throw new Error("artifact replay conflicts with persisted bytes");
      }
      return persisted;
    };

    const recovered = await recoverPersisted();
    if (recovered) {
      return recovered;
    }

    if (existsSync(target)) {
      const orphanBytes = await readArtifactFromSameHandle({
        root: this.rootFor(input.tenantId, input.jobId),
        relativePath: entry.relativePath,
        manifest: [entry],
        now: new Date(0),
      });
      if (
        orphanBytes.byteLength !== entry.bytes ||
        createHash("sha256").update(orphanBytes).digest("hex") !== entry.sha256
      ) {
        throw new Error("artifact replay conflicts with orphaned bytes");
      }
    } else {
      const parent = path.dirname(target);
      const parentReal = await fs.realpath(parent);
      if (!sameResolvedPath(parent, parentReal)) {
        throw new Error("artifact write path contains an unsafe parent");
      }
      const temporary = path.join(parent, `.artifact-${randomUUID()}.tmp`);
      try {
        await fs.writeFile(temporary, input.bytes, {
          flag: "wx",
          mode: 0o600,
        });
        const temporaryStat = await fs.lstat(temporary);
        const currentParentReal = await fs.realpath(parent);
        if (
          temporaryStat.isSymbolicLink() ||
          !temporaryStat.isFile() ||
          !sameResolvedPath(parentReal, currentParentReal)
        ) {
          throw new Error("artifact write path changed during staged write");
        }
        try {
          input.assertPublicationActive?.();
          await fs.link(temporary, target);
        } catch (error) {
          if (
            !(
              error !== null &&
              typeof error === "object" &&
              "code" in error &&
              error.code === "EEXIST"
            )
          ) {
            throw error;
          }
        }
      } finally {
        await fs.unlink(temporary).catch(() => undefined);
      }
      const targetBytes = await readArtifactFromSameHandle({
        root: this.rootFor(input.tenantId, input.jobId),
        relativePath: entry.relativePath,
        manifest: [entry],
        now: new Date(0),
      });
      if (
        targetBytes.byteLength !== entry.bytes ||
        createHash("sha256").update(targetBytes).digest("hex") !== entry.sha256
      ) {
        throw new Error("artifact replay conflicts with persisted bytes");
      }
    }
    const manifestRoot = this.manifestRoot(input.tenantId, input.jobId);
    mkdirSync(manifestRoot, { recursive: true, mode: 0o700 });
    const manifestRootStat = await fs.lstat(manifestRoot);
    if (manifestRootStat.isSymbolicLink() || !manifestRootStat.isDirectory()) {
      throw new Error("artifact manifest root is unsafe");
    }
    const manifestPath = this.manifestPath(
      input.tenantId,
      input.jobId,
      entry.relativePath,
    );
    input.assertPublicationActive?.();
    writeDurableJsonExclusive(manifestPath, entry);
    return (await recoverPersisted()) ?? entry;
  }

  get(
    tenantId: string,
    jobId: string,
    relativePath: string,
  ): StoredArtifact | null {
    const normalized = normalizeArtifactPath(relativePath);
    const entry = this.readManifestEntry(tenantId, jobId, normalized);
    return entry ? { entry, root: this.rootFor(tenantId, jobId) } : null;
  }

  list(tenantId: string, jobId: string): ArtifactEntry[] {
    const root = this.manifestRoot(tenantId, jobId);
    if (!existsSync(root)) {
      return [];
    }
    const stat = lstatSync(root);
    if (stat.isSymbolicLink() || !stat.isDirectory()) {
      throw new Error("artifact manifest root is unsafe");
    }
    const directoryEntries = readdirSync(root, { withFileTypes: true });
    if (
      directoryEntries.some(
        (entry) =>
          entry.isSymbolicLink() ||
          !entry.isFile() ||
          !/^[0-9a-f]{64}\.json$/.test(entry.name),
      )
    ) {
      throw new Error("artifact manifest is unsafe");
    }
    return directoryEntries.map((entry) => {
      let raw: unknown;
      try {
        raw = JSON.parse(readFileSync(path.join(root, entry.name), "utf8"));
      } catch {
        throw new Error("artifact manifest is corrupt");
      }
      if (
        raw === null ||
        typeof raw !== "object" ||
        Array.isArray(raw) ||
        typeof (raw as Record<string, unknown>).relativePath !== "string"
      ) {
        throw new Error("artifact manifest is corrupt");
      }
      const persisted = this.readManifestEntry(
        tenantId,
        jobId,
        (raw as Record<string, unknown>).relativePath as string,
      );
      if (!persisted) {
        throw new Error("artifact manifest is corrupt");
      }
      const expectedName = `${createHash("sha256")
        .update(persisted.relativePath)
        .digest("hex")}.json`;
      if (entry.name !== expectedName) {
        throw new Error("artifact manifest is corrupt");
      }
      return { ...persisted };
    });
  }

  async read(
    tenantId: string,
    jobId: string,
    relativePath: string,
    now = new Date(),
  ): Promise<StoredArtifactBytes | null> {
    const stored = this.get(tenantId, jobId, relativePath);
    if (!stored) {
      return null;
    }
    const bytes = await readArtifactFromSameHandle({
      root: stored.root,
      relativePath: stored.entry.relativePath,
      manifest: [stored.entry],
      now,
    });
    if (
      bytes.byteLength !== stored.entry.bytes ||
      createHash("sha256").update(bytes).digest("hex") !== stored.entry.sha256
    ) {
      throw new Error("artifact integrity validation failed");
    }
    return { ...stored, entry: { ...stored.entry }, bytes };
  }

  async readBySha256(
    tenantId: string,
    sha256: string,
    now = new Date(),
  ): Promise<StoredArtifactBytes | null> {
    if (!SHA256_HEX.test(sha256)) {
      throw new Error("artifact hash must be a lowercase SHA-256 digest");
    }
    const tenantRoot = path.join(
      path.resolve(this.baseRoot),
      safeJobPart(tenantId, "tenant id"),
    );
    if (!existsSync(tenantRoot)) {
      return null;
    }
    const tenantStat = lstatSync(tenantRoot);
    if (tenantStat.isSymbolicLink() || !tenantStat.isDirectory()) {
      throw new Error("artifact tenant root is unsafe");
    }
    for (const job of readdirSync(tenantRoot, { withFileTypes: true })) {
      if (!job.isDirectory() || job.isSymbolicLink()) {
        continue;
      }
      const jobId = safeJobPart(job.name, "job id");
      for (const entry of this.list(tenantId, jobId)) {
        if (entry.sha256 !== sha256) {
          continue;
        }
        return await this.read(tenantId, jobId, entry.relativePath, now);
      }
    }
    return null;
  }
}

const ARTIFACT_STORE_KEY = Symbol.for("yfeistai.openmaic.artifact-store");
const artifactGlobal = globalThis as typeof globalThis & {
  [ARTIFACT_STORE_KEY]?: ArtifactStore;
};

export const artifactStore =
  artifactGlobal[ARTIFACT_STORE_KEY] ??
  (artifactGlobal[ARTIFACT_STORE_KEY] = new ArtifactStore(
    path.resolve(
      process.env.YFEISTAI_OPENMAIC_ARTIFACT_ROOT ??
        path.join(tmpdir(), "yfeistai-openmaic-artifacts"),
    ),
  ));

export function createArtifactGetHandler(
  dependencies: ServiceBoundaryDependencies & { store: ArtifactStore },
): (request: Request, context: ArtifactRouteContext) => Promise<Response> {
  return async (
    request: Request,
    context: ArtifactRouteContext,
  ): Promise<Response> => {
    const signed = authenticateServiceRequest(request, "", dependencies);
    if (!signed) {
      return serviceError(
        401,
        "AUTHENTICATION_FAILED",
        "Service authentication failed.",
      );
    }
    const { jobId, path: pathParts } = await context.params;
    const relativePath = pathParts.join("/");
    const expectedPath =
      `/api/yfeistai/v1/artifacts/${encodeURIComponent(jobId)}/` +
      pathParts.map(encodeURIComponent).join("/");
    if (
      request.method !== "GET" ||
      new URL(request.url).pathname !== expectedPath
    ) {
      return serviceError(404, "ROUTE_NOT_FOUND", "Route not found.");
    }
    if (signed.jobId !== jobId) {
      return serviceError(
        403,
        "REQUEST_BINDING_MISMATCH",
        "Signed request metadata does not match the requested artifact.",
      );
    }

    let stored: StoredArtifactBytes | null;
    try {
      stored = await dependencies.store.read(
        signed.tenantId,
        jobId,
        relativePath,
      );
    } catch (error) {
      if (
        error instanceof Error &&
        error.message.toLowerCase().includes("expired")
      ) {
        return serviceError(410, "ARTIFACT_EXPIRED", "Artifact has expired.");
      }
      if (
        error instanceof Error &&
        (error.message.includes("portable relative path") ||
          error.message.includes("safe task identifier"))
      ) {
        return serviceError(
          400,
          "INVALID_ARTIFACT_PATH",
          "Artifact path is invalid.",
        );
      }
      if (
        error instanceof Error &&
        error.message.includes("integrity validation failed")
      ) {
        return serviceError(
          409,
          "ARTIFACT_INTEGRITY_FAILED",
          "Artifact integrity validation failed.",
        );
      }
      return serviceError(
        403,
        "ARTIFACT_ACCESS_DENIED",
        "Artifact access was denied.",
      );
    }
    if (!stored) {
      return serviceError(404, "ARTIFACT_NOT_FOUND", "Artifact was not found.");
    }
    const bytes = stored.bytes;
    const responseBody = bytes.buffer.slice(
      bytes.byteOffset,
      bytes.byteOffset + bytes.byteLength,
    ) as ArrayBuffer;
    const fileName = path.posix.basename(stored.entry.relativePath);
    const encodedFileName = encodeURIComponent(fileName).replaceAll("'", "%27");
    return new Response(responseBody, {
      status: 200,
      headers: {
        "cache-control": "private, no-store",
        "content-length": String(bytes.byteLength),
        "content-type": stored.entry.mime,
        "content-disposition": `attachment; filename*=UTF-8''${encodedFileName}`,
        "content-security-policy": "sandbox",
        "x-content-type-options": "nosniff",
      },
    });
  };
}

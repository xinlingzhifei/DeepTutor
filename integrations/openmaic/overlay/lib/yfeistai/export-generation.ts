import { createHash } from "node:crypto";
import { createInflateRaw } from "node:zlib";

import {
  type ArtifactEntry,
  type ArtifactStore,
  type StoredArtifactBytes,
  MAX_ARTIFACT_BYTES,
  createArtifactEntry,
  normalizeArtifactPath,
} from "./artifact-manifest";
import {
  ContentIdempotencyConflictError,
  ContentJobStore,
  type ContentOutputRegistry,
  type EngineJob,
  contentOutputRegistry,
} from "./content-generation";
import { OPENMAIC_EXPORT_FORMATS } from "./contracts";
import { canonicalJson } from "./outline-generation";
import { configuredOpenMaicStateRoot } from "./durable-state";
import {
  type PortableClassroomDocument,
  asPortableDocument as strictAsPortableDocument,
  assertOfflineHtmlSelfContained,
} from "./portable-classroom";
import {
  type JobRouteContext,
  type ServiceBoundaryDependencies,
  authenticateServiceRequest,
  hasSignedBodyBinding,
  serviceError,
} from "./service-boundary";

const SHA256_HEX = /^[0-9a-f]{64}$/;
const MAX_ARCHIVE_ENTRIES = 2_048;
const MAX_ARCHIVE_ENTRY_UNCOMPRESSED_BYTES = 128 * 1024 * 1024;
const MAX_ARCHIVE_UNCOMPRESSED_BYTES = 512 * 1024 * 1024;
const MAX_ARCHIVE_COMPRESSION_RATIO = 100;
const EXTERNAL_LOCATION = /(?:https?:\/\/|file:\/\/|[A-Za-z]:\\|\\\\)/i;
const FORBIDDEN_RUNTIME_INPUT_KEYS = new Set([
  "browserStorageKey",
  "browserStorage",
  "indexedDbName",
  "indexedDB",
  "localPath",
  "absolutePath",
]);

export type ExportFormat = (typeof OPENMAIC_EXPORT_FORMATS)[number];

export interface ArchiveEntryDescriptor {
  relativePath: string;
  uncompressedBytes: number;
  compressedBytes: number;
  kind?: "file" | "directory" | "symlink";
  externalLocation?: string;
}

export interface ExportArtifactOutput {
  bytes: Uint8Array;
  archiveEntries?: ArchiveEntryDescriptor[];
}

export interface ExportGenerationRequest {
  tenantId: string;
  jobId: string;
  idempotencyKey: string;
  format: ExportFormat;
  schemaVersion: "1.0";
  language: string;
  classroomDocument: unknown;
  classroomDocumentSha256: string;
  mediaManifest: unknown;
  mediaManifestSha256: string;
  sourceJobId?: string | null;
  exportPolicy: {
    includeSourceAttribution: boolean;
    allowExternalLinks: boolean;
  };
}

export type ExportSubmissionRequest = Omit<
  ExportGenerationRequest,
  "classroomDocument" | "mediaManifest"
>;

export interface ExporterContext {
  isCanceled?: () => boolean | Promise<boolean>;
}

export interface ExportGenerationDependencies {
  exportClassroomZip?: (
    request: ExportGenerationRequest,
    context: ExporterContext,
  ) => Promise<ExportArtifactOutput>;
  exportPptx?: (
    request: ExportGenerationRequest,
    context: ExporterContext,
  ) => Promise<ExportArtifactOutput>;
  exportOfflineHtml?: (
    request: ExportGenerationRequest,
    context: ExporterContext,
  ) => Promise<ExportArtifactOutput>;
  renderMp4?: (
    request: ExportGenerationRequest,
    context: ExporterContext,
  ) => Promise<ExportArtifactOutput>;
  renderEndpoint?: string;
  fetchExternal?: (...args: unknown[]) => Promise<unknown>;
  isCanceled?: () => boolean | Promise<boolean>;
  now?: () => Date;
  artifactTtlMilliseconds?: number;
  readArtifact?: (input: {
    relativePath: string;
    now: Date;
  }) => Promise<StoredArtifactBytes | null>;
  writeArtifact?: (input: {
    relativePath: string;
    bytes: Uint8Array;
    mime: string;
    expiresAt: string;
  }) => Promise<ArtifactEntry>;
  assertPublicationActive?: () => void;
}

export type ExportGenerationResult =
  | {
      status: "succeeded";
      format: ExportFormat;
      artifact: ArtifactEntry;
    }
  | {
      status: "failed";
      format: ExportFormat;
      error: { code: string; message: string };
    };

export class ExportPipelineError extends Error {
  constructor(
    readonly code:
      | "JOB_CANCELED"
      | "MP4_RENDER_TIMEOUT"
      | "MP4_RENDER_UNAVAILABLE"
      | "MP4_RENDER_INVALID_ARTIFACT"
      | "MP4_RENDER_FAILED",
    readonly publicMessage: string,
  ) {
    super(publicMessage);
    this.name = "ExportPipelineError";
  }
}

function invalidMp4Artifact(): never {
  throw new ExportPipelineError(
    "MP4_RENDER_INVALID_ARTIFACT",
    "MP4 renderer returned an invalid artifact.",
  );
}

function readUint32BigEndian(bytes: Uint8Array, offset: number): number {
  return (
    bytes[offset] * 0x1000000 +
    (bytes[offset + 1] << 16) +
    (bytes[offset + 2] << 8) +
    bytes[offset + 3]
  );
}

export function validateMp4Artifact(bytes: Uint8Array): void {
  if (!(bytes instanceof Uint8Array) || bytes.byteLength < 8) {
    invalidMp4Artifact();
  }
  let offset = 0;
  let boxIndex = 0;
  let hasFtyp = false;
  let hasMoov = false;
  let hasMdat = false;
  while (offset < bytes.byteLength) {
    if (bytes.byteLength - offset < 8) {
      invalidMp4Artifact();
    }
    const size32 = readUint32BigEndian(bytes, offset);
    const type = String.fromCharCode(...bytes.subarray(offset + 4, offset + 8));
    if (!/^[\x20-\x7e]{4}$/.test(type)) {
      invalidMp4Artifact();
    }
    let headerBytes = 8;
    let boxBytes = size32;
    if (size32 === 1) {
      if (bytes.byteLength - offset < 16) {
        invalidMp4Artifact();
      }
      headerBytes = 16;
      const high = readUint32BigEndian(bytes, offset + 8);
      const low = readUint32BigEndian(bytes, offset + 12);
      if (high > 0x1fffff) {
        invalidMp4Artifact();
      }
      boxBytes = high * 0x1_0000_0000 + low;
    } else if (size32 === 0) {
      boxBytes = bytes.byteLength - offset;
    }
    if (
      boxBytes < headerBytes ||
      boxBytes > bytes.byteLength - offset ||
      (size32 === 0 && offset + boxBytes !== bytes.byteLength)
    ) {
      invalidMp4Artifact();
    }
    const payloadBytes = boxBytes - headerBytes;
    if (type === "ftyp") {
      if (
        boxIndex !== 0 ||
        hasFtyp ||
        payloadBytes < 8 ||
        (payloadBytes - 8) % 4 !== 0
      ) {
        invalidMp4Artifact();
      }
      const majorBrand = String.fromCharCode(
        ...bytes.subarray(offset + headerBytes, offset + headerBytes + 4),
      );
      if (!/^[\x20-\x7e]{4}$/.test(majorBrand)) {
        invalidMp4Artifact();
      }
      hasFtyp = true;
    } else if (type === "moov") {
      if (payloadBytes === 0) {
        invalidMp4Artifact();
      }
      hasMoov = true;
    } else if (type === "mdat") {
      if (payloadBytes === 0) {
        invalidMp4Artifact();
      }
      hasMdat = true;
    }
    offset += boxBytes;
    boxIndex += 1;
  }
  if (offset !== bytes.byteLength || !hasFtyp || !hasMoov || !hasMdat) {
    invalidMp4Artifact();
  }
}

export async function readResponseBytesLimited(
  response: Response,
  maximumBytes = MAX_ARTIFACT_BYTES,
): Promise<Uint8Array> {
  if (!Number.isSafeInteger(maximumBytes) || maximumBytes <= 0) {
    throw new Error("response byte limit must be a positive integer");
  }
  const contentLength = response.headers.get("content-length");
  let declaredBytes: number | null = null;
  if (contentLength !== null) {
    if (!/^(?:0|[1-9]\d*)$/.test(contentLength)) {
      invalidMp4Artifact();
    }
    declaredBytes = Number(contentLength);
    if (!Number.isSafeInteger(declaredBytes) || declaredBytes > maximumBytes) {
      invalidMp4Artifact();
    }
  }
  if (!response.body) {
    invalidMp4Artifact();
  }
  const reader = response.body.getReader();
  const chunks: Uint8Array[] = [];
  let totalBytes = 0;
  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (done) {
        break;
      }
      totalBytes += value.byteLength;
      if (totalBytes > maximumBytes) {
        await reader.cancel().catch(() => undefined);
        invalidMp4Artifact();
      }
      chunks.push(value);
    }
  } finally {
    reader.releaseLock();
  }
  if (declaredBytes !== null && totalBytes !== declaredBytes) {
    invalidMp4Artifact();
  }
  const bytes = new Uint8Array(totalBytes);
  let cursor = 0;
  for (const chunk of chunks) {
    bytes.set(chunk, cursor);
    cursor += chunk.byteLength;
  }
  return bytes;
}

export function isMp4MediaType(value: string | null): boolean {
  return value?.split(";", 1)[0].trim().toLowerCase() === "video/mp4";
}

export async function cancelRemoteRenderIfRequested(
  context: ExporterContext,
  cancelRemote: () => Promise<void>,
): Promise<void> {
  let requested = false;
  try {
    requested = Boolean(context.isCanceled && (await context.isCanceled()));
  } catch {
    return;
  }
  if (requested) {
    await cancelRemote();
  }
}

export function canonicalExportJson(value: unknown): string {
  return canonicalJson(value);
}

function digest(value: unknown): string {
  return createHash("sha256")
    .update(canonicalExportJson(value), "utf8")
    .digest("hex");
}

function asRecord(value: unknown, label: string): Record<string, unknown> {
  if (value === null || typeof value !== "object" || Array.isArray(value)) {
    throw new Error(`${label} must be an object`);
  }
  return value as Record<string, unknown>;
}

function exactKeys(
  value: Record<string, unknown>,
  label: string,
  allowed: readonly string[],
  required: readonly string[] = allowed,
): void {
  const allowedKeys = new Set(allowed);
  if (Object.keys(value).some((key) => !allowedKeys.has(key))) {
    throw new Error(`${label} contains an unsupported field`);
  }
  if (required.some((key) => !(key in value))) {
    throw new Error(`${label} is missing a required field`);
  }
}

function nonEmptyString(value: unknown, label: string): string {
  if (typeof value !== "string" || value.length === 0) {
    throw new Error(`${label} must be a non-empty string`);
  }
  return value;
}

function requireSha256(value: unknown, label: string): string {
  const normalized = nonEmptyString(value, label);
  if (!SHA256_HEX.test(normalized)) {
    throw new Error(`${label} must be a lowercase SHA-256 digest`);
  }
  return normalized;
}

export function validateExportInputs(input: {
  classroomDocument: unknown;
  classroomDocumentSha256: string;
  mediaManifest: unknown;
  mediaManifestSha256: string;
}): void {
  const expectedDocumentHash = requireSha256(
    input.classroomDocumentSha256,
    "classroom document hash",
  );
  if (digest(input.classroomDocument) !== expectedDocumentHash) {
    throw new Error("classroom document hash mismatch");
  }
  const expectedMediaHash = requireSha256(
    input.mediaManifestSha256,
    "media manifest hash",
  );
  if (digest(input.mediaManifest) !== expectedMediaHash) {
    throw new Error("media manifest hash mismatch");
  }
}

function finiteNonNegativeInteger(value: number, label: string): number {
  if (!Number.isSafeInteger(value) || value < 0) {
    throw new Error(`${label} must be a non-negative integer`);
  }
  return value;
}

export function validateArchiveEntries(
  entries: readonly ArchiveEntryDescriptor[],
): void {
  if (entries.length > MAX_ARCHIVE_ENTRIES) {
    throw new Error("archive contains too many entries");
  }
  let total = 0;
  const paths = new Set<string>();
  for (const entry of entries) {
    const directory = entry.kind === "directory";
    if (directory !== entry.relativePath.endsWith("/")) {
      throw new Error("archive directory metadata is inconsistent");
    }
    const normalized = normalizeArtifactPath(
      directory ? entry.relativePath.slice(0, -1) : entry.relativePath,
    );
    if (paths.has(normalized)) {
      throw new Error("archive contains duplicate entries");
    }
    paths.add(normalized);
    if (entry.kind === "symlink") {
      throw new Error("archive symbolic links are forbidden");
    }
    if (entry.externalLocation) {
      throw new Error("archive external links are forbidden");
    }
    const uncompressed = finiteNonNegativeInteger(
      entry.uncompressedBytes,
      "archive uncompressed size",
    );
    const compressed = finiteNonNegativeInteger(
      entry.compressedBytes,
      "archive compressed size",
    );
    if (directory && (uncompressed !== 0 || compressed !== 0)) {
      throw new Error("archive directory entries must be empty");
    }
    total += uncompressed;
    if (uncompressed > MAX_ARCHIVE_ENTRY_UNCOMPRESSED_BYTES) {
      throw new Error("archive entry exceeds the uncompressed size limit");
    }
    if (total > MAX_ARCHIVE_UNCOMPRESSED_BYTES) {
      throw new Error("archive exceeds the uncompressed size limit");
    }
    if (
      uncompressed > 0 &&
      (compressed === 0 ||
        uncompressed / compressed > MAX_ARCHIVE_COMPRESSION_RATIO)
    ) {
      throw new Error("archive compression ratio exceeds the safe limit");
    }
  }
}

function readUint16(bytes: Uint8Array, offset: number): number {
  return bytes[offset] | (bytes[offset + 1] << 8);
}

function readUint32(bytes: Uint8Array, offset: number): number {
  return (
    (bytes[offset] |
      (bytes[offset + 1] << 8) |
      (bytes[offset + 2] << 16) |
      (bytes[offset + 3] << 24)) >>>
    0
  );
}

interface ParsedZipEntry extends ArchiveEntryDescriptor {
  compressionMethod: 0 | 8;
  crc32: number;
  dataOffset: number;
  localOffset: number;
}

function parseZipArchive(bytes: Uint8Array): ParsedZipEntry[] {
  if (bytes.byteLength > MAX_ARTIFACT_BYTES) {
    throw new Error("archive exceeds the maximum byte size");
  }
  if (bytes.byteLength < 22) {
    throw new Error("archive central directory is missing");
  }
  const searchStart = Math.max(0, bytes.byteLength - 65_557);
  let eocd = -1;
  for (let offset = bytes.byteLength - 22; offset >= searchStart; offset -= 1) {
    if (readUint32(bytes, offset) === 0x06054b50) {
      eocd = offset;
      break;
    }
  }
  if (eocd < 0) {
    throw new Error("archive central directory is missing");
  }
  const commentLength = readUint16(bytes, eocd + 20);
  const diskNumber = readUint16(bytes, eocd + 4);
  const centralDisk = readUint16(bytes, eocd + 6);
  const diskEntryCount = readUint16(bytes, eocd + 8);
  const entryCount = readUint16(bytes, eocd + 10);
  const centralSize = readUint32(bytes, eocd + 12);
  const centralOffset = readUint32(bytes, eocd + 16);
  if (
    entryCount === 0xffff ||
    centralSize === 0xffffffff ||
    centralOffset === 0xffffffff ||
    eocd + 22 + commentLength !== bytes.byteLength ||
    diskNumber !== 0 ||
    centralDisk !== 0 ||
    diskEntryCount !== entryCount ||
    centralOffset + centralSize !== eocd
  ) {
    throw new Error("ZIP64 or malformed archives are not supported");
  }
  if (entryCount > MAX_ARCHIVE_ENTRIES) {
    throw new Error("archive contains too many entries");
  }
  const entries: ParsedZipEntry[] = [];
  let cursor = centralOffset;
  for (let index = 0; index < entryCount; index += 1) {
    if (cursor + 46 > eocd || readUint32(bytes, cursor) !== 0x02014b50) {
      throw new Error("archive central directory is corrupt");
    }
    const flags = readUint16(bytes, cursor + 8);
    const compressionMethod = readUint16(bytes, cursor + 10);
    const crc32 = readUint32(bytes, cursor + 16);
    const compressedBytes = readUint32(bytes, cursor + 20);
    const uncompressedBytes = readUint32(bytes, cursor + 24);
    const nameLength = readUint16(bytes, cursor + 28);
    const extraLength = readUint16(bytes, cursor + 30);
    const commentLength = readUint16(bytes, cursor + 32);
    const externalAttributes = readUint32(bytes, cursor + 38);
    const localOffset = readUint32(bytes, cursor + 42);
    const next = cursor + 46 + nameLength + extraLength + commentLength;
    if (
      next > eocd ||
      nameLength === 0 ||
      (flags & 0x1) !== 0 ||
      (flags & ~0x0806) !== 0 ||
      ![0, 8].includes(compressionMethod) ||
      localOffset + 30 > centralOffset ||
      readUint32(bytes, localOffset) !== 0x04034b50
    ) {
      throw new Error("archive central directory is corrupt");
    }
    const localNameLength = readUint16(bytes, localOffset + 26);
    const localExtraLength = readUint16(bytes, localOffset + 28);
    const dataOffset = localOffset + 30 + localNameLength + localExtraLength;
    if (
      readUint16(bytes, localOffset + 6) !== flags ||
      readUint16(bytes, localOffset + 8) !== compressionMethod ||
      readUint32(bytes, localOffset + 14) !== crc32 ||
      readUint32(bytes, localOffset + 18) !== compressedBytes ||
      readUint32(bytes, localOffset + 22) !== uncompressedBytes ||
      localNameLength !== nameLength ||
      bytes
        .slice(localOffset + 30, localOffset + 30 + localNameLength)
        .some((value, nameIndex) => value !== bytes[cursor + 46 + nameIndex]) ||
      dataOffset + compressedBytes > centralOffset
    ) {
      throw new Error("archive entry data is truncated");
    }
    const relativePath = new TextDecoder("utf-8", { fatal: true }).decode(
      bytes.slice(cursor + 46, cursor + 46 + nameLength),
    );
    const directory = relativePath.endsWith("/");
    const unixType = (externalAttributes >>> 16) & 0xf000;
    if (
      directory &&
      (compressedBytes !== 0 || uncompressedBytes !== 0 || crc32 !== 0)
    ) {
      throw new Error("archive directory entries must be empty");
    }
    if (unixType === 0x4000 && !directory) {
      throw new Error("archive directory metadata is inconsistent");
    }
    entries.push({
      relativePath,
      compressedBytes,
      uncompressedBytes,
      kind:
        unixType === 0xa000
          ? "symlink"
          : directory || unixType === 0x4000
            ? "directory"
            : "file",
      compressionMethod: compressionMethod as 0 | 8,
      crc32,
      dataOffset,
      localOffset,
    });
    cursor = next;
  }
  if (cursor !== centralOffset + centralSize) {
    throw new Error("archive central directory size is inconsistent");
  }
  return entries;
}

function archiveDescriptors(
  entries: readonly ParsedZipEntry[],
): ArchiveEntryDescriptor[] {
  return entries.map(
    ({ relativePath, compressedBytes, uncompressedBytes, kind }) => ({
      relativePath,
      compressedBytes,
      uncompressedBytes,
      kind,
    }),
  );
}

function assertNonOverlappingZipEntries(
  entries: readonly ParsedZipEntry[],
): void {
  let previousEnd = 0;
  for (const entry of [...entries].sort(
    (left, right) => left.localOffset - right.localOffset,
  )) {
    if (entry.localOffset < previousEnd) {
      throw new Error("archive entries overlap");
    }
    previousEnd = entry.dataOffset + entry.compressedBytes;
  }
}

export function inspectZipArchive(bytes: Uint8Array): ArchiveEntryDescriptor[] {
  const entries = parseZipArchive(bytes);
  assertNonOverlappingZipEntries(entries);
  const descriptors = archiveDescriptors(entries);
  validateArchiveEntries(descriptors);
  return descriptors;
}

const CRC32_TABLE = Uint32Array.from({ length: 256 }, (_value, index) => {
  let crc = index;
  for (let bit = 0; bit < 8; bit += 1) {
    crc = (crc >>> 1) ^ (crc & 1 ? 0xedb88320 : 0);
  }
  return crc >>> 0;
});

function updateCrc32(crc: number, bytes: Uint8Array): number {
  let next = crc;
  for (const byte of bytes) {
    next = CRC32_TABLE[(next ^ byte) & 0xff] ^ (next >>> 8);
  }
  return next >>> 0;
}

async function validateDeflatedEntry(
  bytes: Uint8Array,
  entry: ParsedZipEntry,
  totalBefore: number,
): Promise<number> {
  return new Promise<number>((resolve, reject) => {
    const inflater = createInflateRaw();
    let actualBytes = 0;
    let crc = 0xffffffff;
    inflater.on("data", (chunk: Buffer) => {
      actualBytes += chunk.byteLength;
      if (
        actualBytes > entry.uncompressedBytes ||
        actualBytes > MAX_ARCHIVE_ENTRY_UNCOMPRESSED_BYTES ||
        totalBefore + actualBytes > MAX_ARCHIVE_UNCOMPRESSED_BYTES
      ) {
        inflater.destroy(
          new Error(
            "archive entry decompressed size exceeds its declared size",
          ),
        );
        return;
      }
      crc = updateCrc32(crc, chunk);
    });
    inflater.once("error", reject);
    inflater.once("end", () => {
      if (actualBytes !== entry.uncompressedBytes) {
        reject(new Error("archive entry decompressed size is inconsistent"));
        return;
      }
      if ((crc ^ 0xffffffff) >>> 0 !== entry.crc32) {
        reject(new Error("archive entry CRC validation failed"));
        return;
      }
      resolve(actualBytes);
    });
    inflater.end(
      Buffer.from(
        bytes.buffer,
        bytes.byteOffset + entry.dataOffset,
        entry.compressedBytes,
      ),
    );
  });
}

export async function inspectAndValidateZipArchive(
  bytes: Uint8Array,
): Promise<ArchiveEntryDescriptor[]> {
  const entries = parseZipArchive(bytes);
  assertNonOverlappingZipEntries(entries);
  const descriptors = archiveDescriptors(entries);
  validateArchiveEntries(descriptors);
  let totalActualBytes = 0;
  for (const entry of entries) {
    if (entry.compressionMethod === 8) {
      totalActualBytes += await validateDeflatedEntry(
        bytes,
        entry,
        totalActualBytes,
      );
      continue;
    }
    if (entry.compressedBytes !== entry.uncompressedBytes) {
      throw new Error("stored archive entry size is inconsistent");
    }
    const payload = bytes.subarray(
      entry.dataOffset,
      entry.dataOffset + entry.compressedBytes,
    );
    totalActualBytes += payload.byteLength;
    if (
      payload.byteLength > MAX_ARCHIVE_ENTRY_UNCOMPRESSED_BYTES ||
      totalActualBytes > MAX_ARCHIVE_UNCOMPRESSED_BYTES
    ) {
      throw new Error("archive entry decompressed size exceeds the safe limit");
    }
    if ((updateCrc32(0xffffffff, payload) ^ 0xffffffff) >>> 0 !== entry.crc32) {
      throw new Error("archive entry CRC validation failed");
    }
  }
  return descriptors;
}

export async function validatePptxArchive(
  bytes: Uint8Array,
): Promise<ArchiveEntryDescriptor[]> {
  const entries = await inspectAndValidateZipArchive(bytes);
  const paths = new Set(entries.map((entry) => entry.relativePath));
  const missing = [
    "[Content_Types].xml",
    "_rels/.rels",
    "ppt/presentation.xml",
  ].filter((relativePath) => !paths.has(relativePath));
  if (missing.length > 0) {
    throw new Error(
      `PPTX OOXML package is missing required entries: ${missing.join(", ")}`,
    );
  }
  return entries;
}

export interface OfflineMediaInput {
  relativePath: string;
  mime: string;
  sha256: string;
  bytes: Uint8Array;
}

function escapeOfflineJson(value: unknown): string {
  return canonicalExportJson(value)
    .replaceAll("<", "\\u003c")
    .replaceAll(">", "\\u003e")
    .replaceAll("&", "\\u0026");
}

function embeddedResource(
  value: string,
  embedded: ReadonlyMap<string, string>,
): string {
  const candidate = value.trim();
  if (candidate.startsWith("data:") || candidate.startsWith("#")) {
    return candidate;
  }
  const normalized = candidate.startsWith("./")
    ? candidate.slice(2)
    : candidate;
  const replacement = embedded.get(normalized);
  if (!replacement) {
    throw new Error(
      `offline interactive HTML has an unresolved resource: ${candidate}`,
    );
  }
  return replacement;
}

function inlineInteractiveResources(
  html: string,
  embedded: ReadonlyMap<string, string>,
): string {
  if (
    /<(?:base|iframe|object|embed)\b/i.test(html) ||
    /<meta\b[^>]*http-equiv\s*=\s*["']?refresh/i.test(html)
  ) {
    throw new Error("offline interactive HTML contains an unsafe element");
  }
  let rewritten = html.replace(
    /\b(src|href|poster|action)\s*=\s*(?:(["'])([\s\S]*?)\2|([^\s"'=<>`]+))/gi,
    (
      _match,
      name: string,
      quote: string | undefined,
      quoted: string | undefined,
      unquoted: string | undefined,
    ) => {
      const replacement = embeddedResource(quoted ?? unquoted ?? "", embedded);
      const delimiter = quote ?? '"';
      return `${name}=${delimiter}${replacement}${delimiter}`;
    },
  );
  rewritten = rewritten.replace(
    /\bsrcset\s*=\s*(?:(["'])([\s\S]*?)\1|([^\s"'=<>`]+))/gi,
    (
      _match,
      quote: string | undefined,
      quoted: string | undefined,
      unquoted: string | undefined,
    ) => {
      const value = quoted ?? unquoted ?? "";
      const replacement = value.trim().startsWith("data:")
        ? value.trim()
        : value
            .split(",")
            .map((candidate) => {
              const [resource, ...descriptor] = candidate.trim().split(/\s+/);
              return [embeddedResource(resource, embedded), ...descriptor].join(
                " ",
              );
            })
            .join(", ");
      const delimiter = quote ?? '"';
      return `srcset=${delimiter}${replacement}${delimiter}`;
    },
  );
  rewritten = rewritten.replace(
    /url\(\s*(?:(["'])(.*?)\1|([^)'"\s][^)]*?))\s*\)/gi,
    (
      _match,
      quote: string | undefined,
      quoted: string | undefined,
      unquoted: string | undefined,
    ) => {
      const replacement = embeddedResource(quoted ?? unquoted ?? "", embedded);
      const delimiter = quote ?? '"';
      return `url(${delimiter}${replacement}${delimiter})`;
    },
  );
  const innerCsp =
    "default-src 'none'; img-src data:; media-src data:; style-src 'unsafe-inline'; " +
    "script-src 'unsafe-inline'; connect-src 'none'; object-src 'none'; " +
    "base-uri 'none'; form-action 'none'";
  const withoutDoctype = rewritten.replace(/^\s*<!doctype\s+html\s*>/i, "");
  return (
    '<!doctype html><meta http-equiv="Content-Security-Policy" content="' +
    innerCsp +
    '">' +
    withoutDoctype
  );
}

function embedOfflineValue(
  value: unknown,
  embedded: ReadonlyMap<string, string>,
): unknown {
  if (typeof value === "string") {
    return embedded.get(value) ?? value;
  }
  if (Array.isArray(value)) {
    return value.map((item) => embedOfflineValue(item, embedded));
  }
  if (value && typeof value === "object") {
    return Object.fromEntries(
      Object.entries(value).map(([key, item]) => [
        key,
        embedOfflineValue(item, embedded),
      ]),
    );
  }
  return value;
}

export function createOfflineHtmlArtifact(
  classroomDocument: unknown,
  media: readonly OfflineMediaInput[],
): ExportArtifactOutput {
  const document = strictAsPortableDocument(classroomDocument);
  const manifestByPath = new Map(
    document.mediaManifest.map((entry) => [entry.relativePath, entry]),
  );
  const embedded = new Map<string, string>();
  let totalMediaBytes = 0;
  for (const item of media) {
    const relativePath = normalizeArtifactPath(item.relativePath);
    const manifest = manifestByPath.get(relativePath);
    if (
      embedded.has(relativePath) ||
      !manifest ||
      manifest.mimeType !== item.mime ||
      manifest.sha256 !== item.sha256 ||
      manifest.sizeBytes !== item.bytes.byteLength ||
      createHash("sha256").update(item.bytes).digest("hex") !== item.sha256 ||
      !/^[^\s/]+\/[^\s/]+(?:\s*;\s*[^\r\n]+)?$/.test(item.mime)
    ) {
      throw new Error("offline media does not match the controlled manifest");
    }
    totalMediaBytes += item.bytes.byteLength;
    if (totalMediaBytes > MAX_ARTIFACT_BYTES) {
      throw new Error("offline media exceeds the aggregate size limit");
    }
    embedded.set(
      relativePath,
      `data:${item.mime};base64,${Buffer.from(item.bytes).toString("base64")}`,
    );
  }
  if (embedded.size !== document.mediaManifest.length) {
    throw new Error("offline media manifest is incomplete");
  }
  for (const scene of document.openmaic.scenes) {
    if (scene.content.type === "interactive") {
      const safeHtml = inlineInteractiveResources(scene.content.html, embedded);
      scene.content.html = `data:text/html;base64,${Buffer.from(
        safeHtml,
        "utf8",
      ).toString("base64")}`;
    } else {
      scene.content = embedOfflineValue(
        scene.content,
        embedded,
      ) as typeof scene.content;
    }
    scene.actions = embedOfflineValue(
      scene.actions,
      embedded,
    ) as typeof scene.actions;
  }
  const documentJson = escapeOfflineJson(document);
  const title = document.openmaic.stage.name.replace(/[<>&"]/g, "");
  const html = `<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; img-src data:; media-src data:; style-src 'unsafe-inline'; script-src 'unsafe-inline'; frame-src data:; connect-src 'none'; object-src 'none'; base-uri 'none'; form-action 'none'">
<title>${title}</title>
<style>body{font-family:system-ui,sans-serif;margin:0;background:#f7f8fa;color:#172033}main{max-width:960px;margin:auto;padding:40px}.scene{background:#fff;border:1px solid #e4e7ec;border-radius:12px;padding:24px;margin:18px 0}h1,h2,h3{margin-top:0}.media{display:block;max-width:100%;max-height:420px;margin:12px 0}iframe{width:100%;min-height:420px;border:1px solid #ddd}.nav{display:flex;justify-content:space-between;gap:12px}button{padding:10px 18px}fieldset,label,li{margin:.4rem 0}.canvas-element,.actions{padding:.5rem 0}</style>
</head>
<body><main><h1 id="title"></h1><div id="scene"></div><div class="nav"><button id="previous">Previous</button><span id="position"></span><button id="next">Next</button></div></main>
<script id="classroom" type="application/json">${documentJson}</script>
<script>(()=>{
const d=JSON.parse(document.getElementById("classroom").textContent),scenes=d.openmaic.scenes,root=document.getElementById("scene"),position=document.getElementById("position");let index=0;document.getElementById("title").textContent=d.openmaic.stage.name;
const appendMedia=(value,parent)=>{if(typeof value!=="string")return false;let tag="";if(value.startsWith("data:image/"))tag="img";else if(value.startsWith("data:audio/"))tag="audio";else if(value.startsWith("data:video/"))tag="video";if(!tag)return false;const node=document.createElement(tag);node.className="media";if(tag!=="img")node.controls=true;node.setAttribute("src",value);parent.append(node);return true};
const renderSlideScene=(scene,parent)=>{const elements=scene.content.canvas&&Array.isArray(scene.content.canvas.elements)?scene.content.canvas.elements:[];for(const element of elements){const block=document.createElement("div");block.className="canvas-element";if(!appendMedia(element.src??element.url,block)){const text=element.content??element.text??element.title??element.label;if(text!==undefined)block.textContent=String(text);else Object.values(element).forEach(value=>appendMedia(value,block))}parent.append(block)}};
const renderQuizScene=(scene,parent)=>{for(const q of scene.content.questions||[]){const field=document.createElement("fieldset"),legend=document.createElement("legend");legend.textContent=q.prompt;field.append(legend);for(const option of q.options||[]){const label=document.createElement("label"),input=document.createElement("input");input.type=q.questionType==="multiple_choice"?"checkbox":"radio";input.name=q.id;input.value=option.id;label.append(input,document.createTextNode(String(option.label)));field.append(label,document.createElement("br"))}const explanation=document.createElement("details"),summary=document.createElement("summary");summary.textContent="Explanation";explanation.append(summary,document.createTextNode(q.explanation));field.append(explanation);parent.append(field)}};
const renderPblScene=(scene,parent)=>{const scenario=document.createElement("p");scenario.textContent=scene.content.scenario;parent.append(scenario);const roles=document.createElement("section"),rolesTitle=document.createElement("h3"),roleList=document.createElement("ul");rolesTitle.textContent="Roles";for(const role of scene.content.roles||[]){const item=document.createElement("li");item.textContent=role.name+": "+role.brief;roleList.append(item)}roles.append(rolesTitle,roleList);const milestones=document.createElement("section"),milestonesTitle=document.createElement("h3"),milestoneList=document.createElement("ol");milestonesTitle.textContent="Milestones";for(const milestone of scene.content.milestones||[]){const item=document.createElement("li");item.textContent=milestone.title+": "+milestone.rubric;milestoneList.append(item)}milestones.append(milestonesTitle,milestoneList);parent.append(roles,milestones)};
const renderActions=(scene,parent)=>{if(!Array.isArray(scene.actions)||scene.actions.length===0)return;const section=document.createElement("section"),title=document.createElement("h3"),list=document.createElement("ol");section.className="actions";title.textContent="Actions";for(const action of scene.actions){const item=document.createElement("li"),text=action.text??action.content??action.label??action.type;item.textContent=String(text??"");Object.values(action).forEach(value=>appendMedia(value,item));list.append(item)}section.append(title,list);parent.append(section)};
const render=()=>{root.replaceChildren();const scene=scenes[index],article=document.createElement("article"),heading=document.createElement("h2");article.className="scene";heading.textContent=scene.title;article.append(heading);if(scene.type==="interactive"){const frame=document.createElement("iframe");frame.setAttribute("sandbox","allow-scripts");frame.setAttribute("src",scene.content.html);article.append(frame)}else if(scene.type==="quiz")renderQuizScene(scene,article);else if(scene.type==="pbl")renderPblScene(scene,article);else renderSlideScene(scene,article);renderActions(scene,article);root.append(article);position.textContent=(index+1)+" / "+scenes.length;document.getElementById("previous").disabled=index===0;document.getElementById("next").disabled=index===scenes.length-1};
document.getElementById("previous").onclick=()=>{if(index>0){index--;render()}};document.getElementById("next").onclick=()=>{if(index+1<scenes.length){index++;render()}};render();
})();</script>
</body></html>`;
  assertOfflineHtmlSelfContained(html);
  return { bytes: new TextEncoder().encode(html) };
}

function assertNoExternalLocations(
  value: unknown,
  label: string,
  key = "",
): void {
  if (typeof value === "string") {
    if (
      key === "sourceLocation" ||
      key === "url" ||
      key === "href" ||
      key === "src" ||
      key === "html" ||
      key === "externalLocation"
    ) {
      if (
        EXTERNAL_LOCATION.test(value) ||
        value.startsWith("/") ||
        /(?:^|\/)\.\.(?:\/|$)/.test(value)
      ) {
        throw new Error(`${label} contains a forbidden external link`);
      }
    }
    return;
  }
  if (Array.isArray(value)) {
    value.forEach((item, index) =>
      assertNoExternalLocations(item, `${label}[${index}]`, key),
    );
    return;
  }
  if (value === null || typeof value !== "object") {
    return;
  }
  for (const [childKey, child] of Object.entries(value)) {
    if (FORBIDDEN_RUNTIME_INPUT_KEYS.has(childKey)) {
      throw new Error(
        `${label} contains a forbidden browser or local runtime field`,
      );
    }
    assertNoExternalLocations(child, `${label}.${childKey}`, childKey);
  }
}

export function isPrivateRenderEndpoint(value: string): boolean {
  try {
    const endpoint = new URL(value);
    return (
      endpoint.protocol === "http:" &&
      endpoint.hostname === "openmaic-render" &&
      endpoint.username === "" &&
      endpoint.password === "" &&
      (endpoint.pathname === "" || endpoint.pathname === "/") &&
      endpoint.search === "" &&
      endpoint.hash === ""
    );
  } catch {
    return false;
  }
}

export function controlledArtifactDownloadPath(
  jobId: string,
  relativePath: string,
): string {
  const normalized = normalizeArtifactPath(relativePath);
  return (
    `/api/yfeistai/v1/artifacts/${encodeURIComponent(
      nonEmptyString(jobId, "source job id"),
    )}/` + normalized.split("/").map(encodeURIComponent).join("/")
  );
}

function isControlledArtifactDownloadPath(
  value: unknown,
  relativePath: string,
): boolean {
  if (typeof value !== "string") {
    return false;
  }
  const prefix = "/api/yfeistai/v1/artifacts/";
  if (!value.startsWith(prefix)) {
    return false;
  }
  const separator = value.indexOf("/", prefix.length);
  if (separator < 0) {
    return false;
  }
  const encodedJobId = value.slice(prefix.length, separator);
  let jobId: string;
  try {
    jobId = decodeURIComponent(encodedJobId);
  } catch {
    return false;
  }
  return (
    encodeURIComponent(jobId) === encodedJobId &&
    value === controlledArtifactDownloadPath(jobId, relativePath)
  );
}

function validateMediaManifest(
  value: unknown,
  sourceJobId?: string | null,
): void {
  if (!Array.isArray(value)) {
    throw new Error("media manifest must be an array");
  }
  if (value.length > 0 && !sourceJobId) {
    throw new Error("media manifest requires a controlled source job");
  }
  if (value.length > MAX_ARCHIVE_ENTRIES) {
    throw new Error("media manifest contains too many artifacts");
  }
  let totalMediaBytes = 0;
  for (const [index, item] of value.entries()) {
    const entry = asRecord(item, `media manifest artifact ${index}`);
    exactKeys(entry, `media manifest artifact ${index}`, [
      "mediaId",
      "relativePath",
      "mimeType",
      "sha256",
      "sizeBytes",
      "temporaryDownloadPath",
      "expiresAt",
    ]);
    nonEmptyString(entry.mediaId, "media id");
    const relativePath = normalizeArtifactPath(
      nonEmptyString(entry.relativePath, "media artifact path"),
    );
    requireSha256(entry.sha256, "media artifact hash");
    if (
      !Number.isSafeInteger(entry.sizeBytes) ||
      (entry.sizeBytes as number) < 0 ||
      typeof entry.mimeType !== "string" ||
      !/^[^\s/]+\/[^\s/]+(?:\s*;\s*[^\r\n]+)?$/.test(entry.mimeType) ||
      !isControlledArtifactDownloadPath(
        entry.temporaryDownloadPath,
        relativePath,
      ) ||
      !/(?:Z|[+-]\d{2}:\d{2})$/.test(
        nonEmptyString(entry.expiresAt, "media expiry"),
      ) ||
      !Number.isFinite(
        Date.parse(nonEmptyString(entry.expiresAt, "media expiry")),
      )
    ) {
      throw new Error("media manifest artifact metadata is invalid");
    }
    totalMediaBytes += entry.sizeBytes as number;
    if (totalMediaBytes > MAX_ARTIFACT_BYTES) {
      throw new Error("aggregate media exceeds the artifact size limit");
    }
  }
}

function validateFormat(value: unknown): ExportFormat {
  if (
    typeof value !== "string" ||
    !OPENMAIC_EXPORT_FORMATS.includes(value as ExportFormat)
  ) {
    throw new Error("export format is unsupported");
  }
  return value as ExportFormat;
}

function fixedArtifact(
  format: ExportFormat,
  jobId: string,
): {
  relativePath: string;
  mime: string;
} {
  switch (format) {
    case "classroom_zip":
      return {
        relativePath: `exports/${jobId}.maic.zip`,
        mime: "application/zip",
      };
    case "pptx":
      return {
        relativePath: `exports/${jobId}.pptx`,
        mime: "application/vnd.openxmlformats-officedocument.presentationml.presentation",
      };
    case "offline_html":
      return {
        relativePath: `exports/${jobId}.html`,
        mime: "text/html; charset=utf-8",
      };
    case "mp4":
      return {
        relativePath: `exports/${jobId}.mp4`,
        mime: "video/mp4",
      };
  }
}

async function canceled(
  check?: () => boolean | Promise<boolean>,
): Promise<boolean> {
  return check ? Boolean(await check()) : false;
}

function failure(
  format: ExportFormat,
  code: string,
  message: string,
): ExportGenerationResult {
  return { status: "failed", format, error: { code, message } };
}

async function validateExportOutput(
  format: ExportFormat,
  output: ExportArtifactOutput,
): Promise<void> {
  if (!(output.bytes instanceof Uint8Array) || output.bytes.byteLength === 0) {
    throw new Error("export output is empty");
  }
  if (output.archiveEntries) {
    validateArchiveEntries(output.archiveEntries);
  }
  if (format === "classroom_zip") {
    if (!output.archiveEntries) {
      throw new Error("classroom archive output is invalid");
    }
    const actualEntries = await inspectAndValidateZipArchive(output.bytes);
    if (canonicalJson(actualEntries) !== canonicalJson(output.archiveEntries)) {
      throw new Error(
        "archive descriptor does not match its central directory",
      );
    }
  } else if (format === "pptx") {
    await validatePptxArchive(output.bytes);
  } else if (format === "offline_html") {
    const html = new TextDecoder("utf-8", { fatal: true }).decode(output.bytes);
    assertOfflineHtmlSelfContained(html);
  } else {
    validateMp4Artifact(output.bytes);
  }
}

export async function generateExportJob(
  request: ExportGenerationRequest,
  dependencies: ExportGenerationDependencies,
): Promise<ExportGenerationResult> {
  const format = validateFormat(request.format);
  nonEmptyString(request.tenantId, "tenant id");
  nonEmptyString(request.jobId, "job id");
  nonEmptyString(request.idempotencyKey, "idempotency key");
  if (request.schemaVersion !== "1.0") {
    throw new Error("export request schemaVersion is unsupported");
  }
  nonEmptyString(request.language, "export language");
  if (
    request.classroomDocument === undefined ||
    request.mediaManifest === undefined
  ) {
    throw new Error("controlled export inputs are required");
  }
  assertNoExternalLocations(request, "export request");
  validateExportInputs(request);
  const document = asPortableDocument(request.classroomDocument);
  validateMediaManifest(request.mediaManifest, request.sourceJobId);
  if (digest(document.mediaManifest) !== request.mediaManifestSha256) {
    throw new Error(
      "media manifest hash does not match the classroom document",
    );
  }
  if (request.exportPolicy?.allowExternalLinks) {
    throw new Error("external links are not allowed in controlled exports");
  }
  if (await canceled(dependencies.isCanceled)) {
    return failure(format, "JOB_CANCELED", "The export job was canceled.");
  }
  const now = (dependencies.now ?? (() => new Date()))();
  const fixed = fixedArtifact(format, request.jobId);
  if (dependencies.readArtifact) {
    try {
      const checkpoint = await dependencies.readArtifact({
        relativePath: fixed.relativePath,
        now,
      });
      if (checkpoint) {
        const expected = createArtifactEntry({
          ...fixed,
          bytes: checkpoint.bytes,
          expiresAt: checkpoint.entry.expiresAt,
        });
        await validateExportOutput(format, {
          bytes: checkpoint.bytes,
          ...(format === "classroom_zip"
            ? { archiveEntries: inspectZipArchive(checkpoint.bytes) }
            : {}),
        });
        if (
          checkpoint.entry.relativePath !== expected.relativePath ||
          checkpoint.entry.sha256 !== expected.sha256 ||
          checkpoint.entry.bytes !== expected.bytes ||
          checkpoint.entry.mime !== expected.mime ||
          checkpoint.entry.expiresAt !== expected.expiresAt ||
          checkpoint.entry.downloadPath !==
            controlledArtifactDownloadPath(
              request.jobId,
              checkpoint.entry.relativePath,
            )
        ) {
          throw new Error("export checkpoint integrity binding failed");
        }
        return { status: "succeeded", format, artifact: checkpoint.entry };
      }
    } catch {
      return failure(
        format,
        "EXPORT_ARTIFACT_INVALID",
        "Export artifact validation failed.",
      );
    }
  }
  if (format === "mp4" && !dependencies.renderEndpoint) {
    return failure(
      format,
      "MP4_RENDER_UNAVAILABLE",
      "MP4 rendering is not configured.",
    );
  }
  if (
    format === "mp4" &&
    !isPrivateRenderEndpoint(dependencies.renderEndpoint as string)
  ) {
    return failure(
      format,
      "MP4_RENDER_UNTRUSTED",
      "MP4 renderer must use the private openmaic-render service.",
    );
  }

  let exporter:
    | ((
        request: ExportGenerationRequest,
        context: ExporterContext,
      ) => Promise<ExportArtifactOutput>)
    | undefined;
  if (format === "classroom_zip") {
    exporter = dependencies.exportClassroomZip;
  } else if (format === "pptx") {
    exporter = dependencies.exportPptx;
  } else if (format === "offline_html") {
    exporter = dependencies.exportOfflineHtml;
  } else {
    exporter = dependencies.renderMp4;
  }
  if (!exporter) {
    return format === "mp4"
      ? failure(
          format,
          "MP4_RENDER_UNAVAILABLE",
          "MP4 rendering is not configured.",
        )
      : failure(
          format,
          "EXPORTER_UNAVAILABLE",
          "The requested exporter is not configured.",
        );
  }

  let output: ExportArtifactOutput;
  try {
    output = await exporter(request, {
      isCanceled: dependencies.isCanceled,
    });
    await validateExportOutput(format, output);
  } catch (error) {
    if (error instanceof ExportPipelineError) {
      return failure(format, error.code, error.publicMessage);
    }
    return failure(
      format,
      format === "mp4" ? "MP4_RENDER_FAILED" : "EXPORT_FAILED",
      format === "mp4" ? "MP4 rendering failed." : "Export generation failed.",
    );
  }
  if (await canceled(dependencies.isCanceled)) {
    return failure(format, "JOB_CANCELED", "The export job was canceled.");
  }

  const expiresAt = new Date(
    now.getTime() +
      (dependencies.artifactTtlMilliseconds ?? 24 * 60 * 60 * 1_000),
  ).toISOString();
  const expectedArtifact = createArtifactEntry({
    ...fixed,
    bytes: output.bytes,
    expiresAt,
  });
  let artifact: ArtifactEntry;
  try {
    if (dependencies.writeArtifact) {
      dependencies.assertPublicationActive?.();
    }
    artifact = dependencies.writeArtifact
      ? await dependencies.writeArtifact({
          ...fixed,
          bytes: output.bytes,
          expiresAt,
        })
      : expectedArtifact;
    const artifactExpiry = Date.parse(artifact.expiresAt);
    if (
      artifact.relativePath !== expectedArtifact.relativePath ||
      artifact.sha256 !== expectedArtifact.sha256 ||
      artifact.bytes !== expectedArtifact.bytes ||
      artifact.mime !== expectedArtifact.mime ||
      !/(?:Z|[+-]\d{2}:\d{2})$/.test(artifact.expiresAt) ||
      !Number.isFinite(artifactExpiry) ||
      artifactExpiry <= now.getTime() ||
      typeof artifact.downloadPath !== "string" ||
      artifact.downloadPath.length === 0 ||
      (dependencies.writeArtifact !== undefined &&
        artifact.downloadPath !==
          controlledArtifactDownloadPath(
            request.jobId,
            expectedArtifact.relativePath,
          ))
    ) {
      throw new Error("export writer integrity binding failed");
    }
  } catch {
    return failure(
      format,
      "EXPORT_ARTIFACT_INVALID",
      "Export artifact validation failed.",
    );
  }
  if (await canceled(dependencies.isCanceled)) {
    return failure(format, "JOB_CANCELED", "The export job was canceled.");
  }
  return { status: "succeeded", format, artifact };
}

const EXPORT_STORE_KEY = Symbol.for("yfeistai.openmaic.export-job-store");
const exportGlobal = globalThis as typeof globalThis & {
  [EXPORT_STORE_KEY]?: ContentJobStore<ExportGenerationResult>;
};

export const exportJobStore =
  exportGlobal[EXPORT_STORE_KEY] ??
  (exportGlobal[EXPORT_STORE_KEY] = new ContentJobStore<ExportGenerationResult>(
    configuredOpenMaicStateRoot(),
    "export-jobs",
  ));

function parseExportRequest(value: unknown): ExportSubmissionRequest {
  const record = asRecord(value, "export request");
  exactKeys(record, "export request", [
    "schemaVersion",
    "tenantId",
    "jobId",
    "idempotencyKey",
    "classroomDocumentSha256",
    "mediaManifestSha256",
    "format",
    "language",
    "exportPolicy",
  ]);
  if (record.schemaVersion !== "1.0") {
    throw new Error("export request schemaVersion is unsupported");
  }
  const policy = asRecord(record.exportPolicy, "export policy");
  exactKeys(policy, "export policy", [
    "includeSourceAttribution",
    "allowExternalLinks",
  ]);
  if (
    typeof policy.includeSourceAttribution !== "boolean" ||
    typeof policy.allowExternalLinks !== "boolean"
  ) {
    throw new Error("export policy flags must be boolean");
  }
  const parsed = {
    schemaVersion: "1.0" as const,
    tenantId: nonEmptyString(record.tenantId, "tenant id"),
    jobId: nonEmptyString(record.jobId, "job id"),
    idempotencyKey: nonEmptyString(record.idempotencyKey, "idempotency key"),
    classroomDocumentSha256: requireSha256(
      record.classroomDocumentSha256,
      "classroom document hash",
    ),
    mediaManifestSha256: requireSha256(
      record.mediaManifestSha256,
      "media manifest hash",
    ),
    format: validateFormat(record.format),
    language: nonEmptyString(record.language, "export language"),
    exportPolicy: {
      includeSourceAttribution: policy.includeSourceAttribution,
      allowExternalLinks: policy.allowExternalLinks,
    },
  } satisfies ExportSubmissionRequest;
  if (parsed.exportPolicy.allowExternalLinks) {
    throw new Error("external links are not allowed in controlled exports");
  }
  return parsed;
}

export interface ExportPostHandlerDependencies
  extends ExportGenerationDependencies, ServiceBoundaryDependencies {
  store: ContentJobStore<ExportGenerationResult>;
  artifactStore?: ArtifactStore;
  inputRegistry?: ContentOutputRegistry;
}

export function createExportPostHandler(
  dependencies: ExportPostHandlerDependencies,
): (request: Request) => Promise<Response> {
  return async (request: Request): Promise<Response> => {
    const body = await request.text();
    const signed = authenticateServiceRequest(request, body, dependencies);
    if (!signed) {
      return serviceError(
        401,
        "AUTHENTICATION_FAILED",
        "Service authentication failed.",
      );
    }
    if (
      request.method !== "POST" ||
      new URL(request.url).pathname !== "/api/yfeistai/v1/exports"
    ) {
      return serviceError(404, "ROUTE_NOT_FOUND", "Route not found.");
    }

    let parsed: ReturnType<typeof parseExportRequest>;
    try {
      parsed = parseExportRequest(JSON.parse(body));
    } catch {
      return serviceError(400, "INVALID_REQUEST", "Request body is invalid.");
    }
    if (!hasSignedBodyBinding(signed, parsed)) {
      return serviceError(
        403,
        "REQUEST_BINDING_MISMATCH",
        "Signed request metadata does not match the request body.",
      );
    }
    const resolved = (
      dependencies.inputRegistry ?? contentOutputRegistry
    ).resolve(
      parsed.tenantId,
      parsed.classroomDocumentSha256,
      parsed.mediaManifestSha256,
    );
    if (!resolved) {
      return serviceError(
        404,
        "EXPORT_INPUT_NOT_FOUND",
        "Controlled export inputs were not found.",
      );
    }
    const generationRequest: ExportGenerationRequest = {
      ...parsed,
      ...resolved,
    };

    try {
      void dependencies.store.start(
        {
          tenantId: parsed.tenantId,
          jobId: parsed.jobId,
          idempotencyKey: parsed.idempotencyKey,
          canonicalBody: canonicalJson(parsed),
          phase: "export",
          failureCode: "EXPORT_FAILED",
        },
        (publication) =>
          generateExportJob(generationRequest, {
            ...dependencies,
            assertPublicationActive: publication.assertActive,
            isCanceled: () =>
              dependencies.store.isCanceled(parsed.tenantId, parsed.jobId),
            readArtifact: dependencies.artifactStore
              ? ({ relativePath, now }) =>
                  dependencies.artifactStore!.read(
                    parsed.tenantId,
                    parsed.jobId,
                    relativePath,
                    now,
                  )
              : dependencies.readArtifact,
            writeArtifact: dependencies.artifactStore
              ? (input) =>
                  dependencies.artifactStore!.put({
                    tenantId: parsed.tenantId,
                    jobId: parsed.jobId,
                    assertPublicationActive: publication.assertActive,
                    ...input,
                  })
              : dependencies.writeArtifact,
          }),
      );
      const job = await dependencies.store.read(parsed.tenantId, parsed.jobId);
      return Response.json(job, { status: 202 });
    } catch (error) {
      if (error instanceof ContentIdempotencyConflictError) {
        return serviceError(
          409,
          "IDEMPOTENCY_CONFLICT",
          "The idempotency key conflicts with an existing job.",
        );
      }
      return serviceError(500, "EXPORT_FAILED", "Export generation failed.");
    }
  };
}

export function createExportGetHandler(
  dependencies: ServiceBoundaryDependencies & {
    store: ContentJobStore<ExportGenerationResult>;
  },
): (request: Request, context: JobRouteContext) => Promise<Response> {
  return async (
    request: Request,
    context: JobRouteContext,
  ): Promise<Response> => {
    const signed = authenticateServiceRequest(request, "", dependencies);
    if (!signed) {
      return serviceError(
        401,
        "AUTHENTICATION_FAILED",
        "Service authentication failed.",
      );
    }
    const { jobId } = await context.params;
    const expectedPath = `/api/yfeistai/v1/exports/${encodeURIComponent(jobId)}`;
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
        "Signed request metadata does not match the requested job.",
      );
    }
    const job: EngineJob<ExportGenerationResult> | null =
      await dependencies.store.read(signed.tenantId, jobId);
    if (!job) {
      return serviceError(404, "JOB_NOT_FOUND", "Export job was not found.");
    }
    return Response.json(job, { status: 200 });
  };
}

export function asPortableDocument(value: unknown): PortableClassroomDocument {
  const portable = strictAsPortableDocument(value);
  if (portable) {
    return portable;
  }
  const document = asRecord(portable, "classroom document");
  exactKeys(document, "classroom document", [
    "schemaVersion",
    "classroomId",
    "classroomVersionId",
    "contentMode",
    "openCreation",
    "openmaic",
    "interactionIds",
    "sourceRefs",
    "knowledgePointMappings",
    "mediaManifest",
    "fileSha256",
    "exportManifest",
    "generationMetadata",
    "auditMetadata",
    "validationResult",
    "migrationRecords",
  ]);
  if (
    document.schemaVersion !== "1.0" ||
    !["source_grounded", "open_creation"].includes(
      document.contentMode as string,
    ) ||
    typeof document.openCreation !== "boolean" ||
    document.openCreation !== (document.contentMode === "open_creation")
  ) {
    throw new Error(
      "classroom document is not a portable version 1.0 document",
    );
  }
  nonEmptyString(document.classroomId, "classroom id");
  nonEmptyString(document.classroomVersionId, "classroom version id");
  requireSha256(document.fileSha256, "classroom file hash");

  const openmaic = asRecord(document.openmaic, "classroom openmaic");
  exactKeys(openmaic, "classroom openmaic", ["dslVersion", "stage", "scenes"]);
  if (openmaic.dslVersion !== "0.1.0") {
    throw new Error("classroom DSL version is unsupported");
  }
  const stage = asRecord(openmaic.stage, "classroom stage");
  exactKeys(stage, "classroom stage", ["id", "name", "createdAt", "updatedAt"]);
  const stageId = nonEmptyString(stage.id, "stage id");
  nonEmptyString(stage.name, "stage name");
  if (
    !Number.isFinite(
      Date.parse(nonEmptyString(stage.createdAt, "stage createdAt")),
    ) ||
    !Number.isFinite(
      Date.parse(nonEmptyString(stage.updatedAt, "stage updatedAt")),
    )
  ) {
    throw new Error("classroom stage timestamps are invalid");
  }
  if (!Array.isArray(openmaic.scenes) || openmaic.scenes.length === 0) {
    throw new Error("classroom scenes must be a non-empty array");
  }
  const sceneIds = new Set<string>();
  const interactiveIds = new Set<string>();
  for (const [index, rawScene] of openmaic.scenes.entries()) {
    const scene = asRecord(rawScene, `classroom scene ${index}`);
    exactKeys(scene, `classroom scene ${index}`, [
      "id",
      "stageId",
      "title",
      "order",
      "type",
      "content",
      "actions",
    ]);
    const sceneId = nonEmptyString(scene.id, "scene id");
    if (
      sceneIds.has(sceneId) ||
      scene.stageId !== stageId ||
      scene.order !== index ||
      !Number.isSafeInteger(scene.order)
    ) {
      throw new Error("classroom scene identity or order is invalid");
    }
    sceneIds.add(sceneId);
    nonEmptyString(scene.title, "scene title");
    if (
      !["slide", "quiz", "interactive", "pbl"].includes(scene.type as string)
    ) {
      throw new Error("classroom scene type is unsupported");
    }
    const content = asRecord(scene.content, "scene content");
    if (content.type !== scene.type) {
      throw new Error("classroom scene content type is inconsistent");
    }
    if (scene.type === "slide") {
      exactKeys(content, "slide content", ["type", "canvas"]);
      asRecord(content.canvas, "slide canvas");
    } else if (scene.type === "quiz") {
      exactKeys(content, "quiz content", ["type", "questions"]);
      if (!Array.isArray(content.questions) || content.questions.length === 0) {
        throw new Error("quiz content requires questions");
      }
      for (const questionValue of content.questions) {
        const question = asRecord(questionValue, "quiz question");
        exactKeys(question, "quiz question", [
          "id",
          "prompt",
          "questionType",
          "options",
          "correctOptionIds",
          "explanation",
        ]);
        nonEmptyString(question.id, "quiz question id");
        nonEmptyString(question.prompt, "quiz prompt");
        nonEmptyString(question.explanation, "quiz explanation");
        if (
          !["single_choice", "multiple_choice", "short_answer"].includes(
            question.questionType as string,
          ) ||
          !Array.isArray(question.options) ||
          !Array.isArray(question.correctOptionIds)
        ) {
          throw new Error("quiz question shape is invalid");
        }
        for (const optionValue of question.options) {
          const option = asRecord(optionValue, "quiz option");
          exactKeys(option, "quiz option", ["id", "label"]);
          nonEmptyString(option.id, "quiz option id");
          nonEmptyString(option.label, "quiz option label");
        }
        if (
          question.correctOptionIds.some(
            (optionId) => typeof optionId !== "string",
          )
        ) {
          throw new Error("quiz correct option identifiers are invalid");
        }
      }
    } else if (scene.type === "interactive") {
      exactKeys(content, "interactive content", [
        "type",
        "html",
        "bridgeVersion",
        "sandbox",
      ]);
      nonEmptyString(content.html, "interactive HTML");
      const sandbox = asRecord(content.sandbox, "interactive sandbox");
      exactKeys(sandbox, "interactive sandbox", [
        "allowScripts",
        "allowSameOrigin",
      ]);
      if (
        content.bridgeVersion !== "1.0" ||
        sandbox.allowScripts !== true ||
        sandbox.allowSameOrigin !== false
      ) {
        throw new Error("interactive sandbox contract is invalid");
      }
      interactiveIds.add(sceneId);
    } else {
      exactKeys(content, "PBL content", [
        "type",
        "scenario",
        "roles",
        "milestones",
      ]);
      nonEmptyString(content.scenario, "PBL scenario");
      if (
        !Array.isArray(content.roles) ||
        content.roles.length === 0 ||
        !Array.isArray(content.milestones) ||
        content.milestones.length === 0
      ) {
        throw new Error("PBL content shape is invalid");
      }
      for (const roleValue of content.roles) {
        const role = asRecord(roleValue, "PBL role");
        exactKeys(role, "PBL role", ["id", "name", "brief"]);
        nonEmptyString(role.id, "PBL role id");
        nonEmptyString(role.name, "PBL role name");
        nonEmptyString(role.brief, "PBL role brief");
      }
      for (const milestoneValue of content.milestones) {
        const milestone = asRecord(milestoneValue, "PBL milestone");
        exactKeys(milestone, "PBL milestone", ["id", "title", "rubric"]);
        nonEmptyString(milestone.id, "PBL milestone id");
        nonEmptyString(milestone.title, "PBL milestone title");
        nonEmptyString(milestone.rubric, "PBL milestone rubric");
      }
    }
    if (!Array.isArray(scene.actions)) {
      throw new Error("classroom scene actions must be an array");
    }
    scene.actions.forEach((action) => asRecord(action, "scene action"));
    assertNoExternalLocations(content, "scene content");
    assertNoExternalLocations(scene.actions, "scene actions");
  }

  if (
    !Array.isArray(document.interactionIds) ||
    document.interactionIds.some((item) => typeof item !== "string") ||
    document.interactionIds.length !== interactiveIds.size ||
    document.interactionIds.some((item) => !interactiveIds.has(item))
  ) {
    throw new Error("classroom interaction identifiers are invalid");
  }
  if (!Array.isArray(document.sourceRefs)) {
    throw new Error("classroom source references must be an array");
  }
  for (const referenceValue of document.sourceRefs) {
    const reference = asRecord(referenceValue, "source reference");
    exactKeys(reference, "source reference", [
      "citationId",
      "sourceId",
      "fragmentId",
    ]);
    nonEmptyString(reference.citationId, "source citation id");
    nonEmptyString(reference.sourceId, "source id");
    nonEmptyString(reference.fragmentId, "source fragment id");
  }
  if (
    !Array.isArray(document.knowledgePointMappings) ||
    document.knowledgePointMappings.length === 0
  ) {
    throw new Error("classroom knowledge mappings must be non-empty");
  }
  for (const mappingValue of document.knowledgePointMappings) {
    const mapping = asRecord(mappingValue, "knowledge mapping");
    exactKeys(mapping, "knowledge mapping", [
      "knowledgePointId",
      "sceneIds",
      "sourceRefs",
    ]);
    nonEmptyString(mapping.knowledgePointId, "knowledge point id");
    if (
      !Array.isArray(mapping.sceneIds) ||
      mapping.sceneIds.length === 0 ||
      mapping.sceneIds.some(
        (sceneIdValue) =>
          typeof sceneIdValue !== "string" || !sceneIds.has(sceneIdValue),
      ) ||
      !Array.isArray(mapping.sourceRefs)
    ) {
      throw new Error("classroom knowledge mapping is invalid");
    }
  }
  validateMediaManifest(document.mediaManifest);
  for (const field of ["exportManifest", "migrationRecords"] as const) {
    if (!Array.isArray(document[field]) || document[field].length !== 0) {
      throw new Error(`classroom ${field} must be an array`);
    }
    assertNoExternalLocations(document[field], `classroom ${field}`);
  }
  for (const [label, value, keys] of [
    [
      "generation metadata",
      document.generationMetadata,
      [
        "generator",
        "generatorVersion",
        "modelId",
        "generatedAt",
        "teachingBriefId",
        "teachingBriefSha256",
        "templateId",
        "templateVersion",
      ],
    ],
    [
      "audit metadata",
      document.auditMetadata,
      [
        "templateId",
        "templateVersion",
        "teachingBriefId",
        "teachingBriefSha256",
        "parentClassroomVersionId",
      ],
    ],
    [
      "validation result",
      document.validationResult,
      ["valid", "issues", "validatedAt"],
    ],
  ] as const) {
    const record = asRecord(value, label);
    exactKeys(record, label, keys);
  }
  const generation = document.generationMetadata as Record<string, unknown>;
  for (const field of [
    "generator",
    "generatorVersion",
    "modelId",
    "teachingBriefId",
    "templateId",
    "templateVersion",
  ]) {
    nonEmptyString(generation[field], `generation metadata ${field}`);
  }
  requireSha256(
    generation.teachingBriefSha256,
    "generation teaching brief hash",
  );
  if (
    !Number.isFinite(
      Date.parse(nonEmptyString(generation.generatedAt, "generatedAt")),
    )
  ) {
    throw new Error("generation timestamp is invalid");
  }
  const audit = document.auditMetadata as Record<string, unknown>;
  for (const field of ["templateId", "templateVersion", "teachingBriefId"]) {
    nonEmptyString(audit[field], `audit metadata ${field}`);
  }
  requireSha256(audit.teachingBriefSha256, "audit teaching brief hash");
  if (audit.parentClassroomVersionId !== null) {
    throw new Error("export accepts only root classroom versions");
  }
  const validation = document.validationResult as Record<string, unknown>;
  if (
    validation.valid !== true ||
    !Array.isArray(validation.issues) ||
    !Number.isFinite(
      Date.parse(nonEmptyString(validation.validatedAt, "validatedAt")),
    )
  ) {
    throw new Error("classroom validation result is invalid");
  }
  assertNoExternalLocations(document, "classroom document");
  return document as unknown as PortableClassroomDocument;
}

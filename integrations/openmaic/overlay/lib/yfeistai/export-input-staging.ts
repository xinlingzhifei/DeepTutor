import { createHash } from "node:crypto";

import {
  type ArtifactStore,
  MAX_ARTIFACT_BYTES,
  artifactStore,
  normalizeArtifactPath,
} from "./artifact-manifest";
import {
  type ContentOutputRegistry,
  contentOutputRegistry,
} from "./content-generation";
import {
  configuredOpenMaicStateRoot,
  durableFile,
  exactDurableRecord,
  readDurableJson,
  writeDurableJsonExclusive,
} from "./durable-state";
import { canonicalJson } from "./outline-generation";
import {
  asPortableDocument,
  type PortableClassroomDocument,
} from "./portable-classroom";
import {
  type ServiceBoundaryDependencies,
  authenticatePrehashedServiceRequest,
  authenticateServiceRequest,
  serviceError,
} from "./service-boundary";

const SHA256_HEX = /^[0-9a-f]{64}$/;
const SAFE_ID = /^[A-Za-z0-9][A-Za-z0-9._~-]{0,127}$/;
const MIME_TYPE = /^[^\s/]+\/[^\s/]+(?:\s*;\s*[^\r\n]+)?$/;
const MAX_DECLARATION_BYTES = 1024 * 1024;
const MAX_DOCUMENT_BYTES = 8 * 1024 * 1024;
const MAX_MEDIA_BYTES = 128 * 1024 * 1024;
const MAX_TOTAL_BYTES = 512 * 1024 * 1024;
const MAX_MEDIA_FILES = 256;
const STAGING_ARTIFACT_TTL_MS = 7 * 24 * 60 * 60 * 1_000;

export interface ExportInputFileDeclaration {
  fileId: string;
  kind: "document" | "media";
  mediaId: string | null;
  relativePath: string;
  mimeType: string;
  sha256: string;
  sizeBytes: number;
}

export interface ExportInputDeclaration {
  schemaVersion: 1;
  tenantId: string;
  jobId: string;
  idempotencyKey: string;
  classroomDocumentSha256: string;
  mediaManifestSha256: string;
  sourceManifestSha256: string;
  files: ExportInputFileDeclaration[];
}

export interface ExportInputCommitReceipt {
  schemaVersion: 1;
  tenantId: string;
  jobId: string;
  idempotencyKey: string;
  declarationSha256: string;
  classroomDocumentSha256: string;
  mediaManifestSha256: string;
  status: "committed";
  receiptSha256: string;
}

interface ExportInputRouteContext {
  params:
    | { jobId: string }
    | Promise<{ jobId: string }>;
}

interface ExportInputFileRouteContext {
  params:
    | { jobId: string; fileId: string }
    | Promise<{ jobId: string; fileId: string }>;
}

interface ReservationRecord {
  version: 1;
  tenantId: string;
  jobId: string;
  idempotencyKey: string;
  declarationSha256: string;
  declaration: ExportInputDeclaration;
}

export class ExportInputStagingError extends Error {
  constructor(
    readonly status: number,
    readonly code: string,
    message: string,
  ) {
    super(message);
  }
}

function sha256(value: string | Uint8Array): string {
  return createHash("sha256").update(value).digest("hex");
}

function exactKeys(
  value: Record<string, unknown>,
  expected: readonly string[],
): void {
  const actual = Object.keys(value).sort();
  const sortedExpected = [...expected].sort();
  if (
    actual.length !== sortedExpected.length ||
    actual.some((key, index) => key !== sortedExpected[index])
  ) {
    throw new Error("export input declaration has unexpected fields");
  }
}

function record(value: unknown): Record<string, unknown> {
  if (value === null || typeof value !== "object" || Array.isArray(value)) {
    throw new Error("export input declaration must be an object");
  }
  return value as Record<string, unknown>;
}

function nonEmpty(value: unknown): string {
  if (typeof value !== "string" || value.length === 0 || /[\r\n\0]/.test(value)) {
    throw new Error("export input declaration string is invalid");
  }
  return value;
}

function digest(value: unknown): string {
  const candidate = nonEmpty(value);
  if (!SHA256_HEX.test(candidate)) {
    throw new Error("export input declaration hash is invalid");
  }
  return candidate;
}

function safeId(value: unknown): string {
  const candidate = nonEmpty(value);
  if (!SAFE_ID.test(candidate) || candidate.endsWith(".")) {
    throw new Error("export input identifier is invalid");
  }
  return candidate;
}

export function parseExportInputDeclaration(
  value: unknown,
): ExportInputDeclaration {
  const input = record(value);
  exactKeys(input, [
    "schemaVersion",
    "tenantId",
    "jobId",
    "idempotencyKey",
    "classroomDocumentSha256",
    "mediaManifestSha256",
    "sourceManifestSha256",
    "files",
  ]);
  if (input.schemaVersion !== 1 || !Array.isArray(input.files)) {
    throw new Error("export input declaration version is invalid");
  }
  const tenantId = safeId(input.tenantId);
  const jobId = safeId(input.jobId);
  const idempotencyKey = nonEmpty(input.idempotencyKey);
  const classroomDocumentSha256 = digest(input.classroomDocumentSha256);
  const mediaManifestSha256 = digest(input.mediaManifestSha256);
  const sourceManifestSha256 = digest(input.sourceManifestSha256);
  if (input.files.length < 1 || input.files.length > MAX_MEDIA_FILES + 1) {
    throw new Error("export input file count is invalid");
  }
  const files = input.files.map((raw, index): ExportInputFileDeclaration => {
    const file = record(raw);
    exactKeys(file, [
      "fileId",
      "kind",
      "mediaId",
      "relativePath",
      "mimeType",
      "sha256",
      "sizeBytes",
    ]);
    const kind = file.kind;
    const fileId = safeId(file.fileId);
    const relativePath = normalizeArtifactPath(nonEmpty(file.relativePath));
    const mimeType = nonEmpty(file.mimeType);
    if (
      (kind !== "document" && kind !== "media") ||
      !MIME_TYPE.test(mimeType) ||
      !Number.isSafeInteger(file.sizeBytes) ||
      (file.sizeBytes as number) <= 0
    ) {
      throw new Error("export input file metadata is invalid");
    }
    const mediaId = file.mediaId;
    if (
      (kind === "document" && mediaId !== null) ||
      (kind === "media" && typeof mediaId !== "string")
    ) {
      throw new Error("export input file binding is invalid");
    }
    if (
      index === 0 &&
      (kind !== "document" ||
        relativePath !== "classroom.json" ||
        mimeType !== "application/json")
    ) {
      throw new Error("export input document declaration is invalid");
    }
    if (index > 0 && kind !== "media") {
      throw new Error("export input media declaration is invalid");
    }
    const sizeBytes = file.sizeBytes as number;
    if (
      (kind === "document" && sizeBytes > MAX_DOCUMENT_BYTES) ||
      (kind === "media" && sizeBytes > MAX_MEDIA_BYTES) ||
      sizeBytes > MAX_ARTIFACT_BYTES
    ) {
      throw new Error("export input file exceeds its size limit");
    }
    return {
      fileId,
      kind,
      mediaId: mediaId as string | null,
      relativePath,
      mimeType,
      sha256: digest(file.sha256),
      sizeBytes,
    };
  });
  if (
    files[0].sha256 !== classroomDocumentSha256 ||
    files.reduce((total, file) => total + file.sizeBytes, 0) > MAX_TOTAL_BYTES ||
    new Set(files.map((file) => file.fileId)).size !== files.length ||
    new Set(files.map((file) => file.relativePath)).size !== files.length ||
    new Set(
      files
        .filter((file) => file.kind === "media")
        .map((file) => file.mediaId),
    ).size !==
      files.length - 1
  ) {
    throw new Error("export input file declarations conflict");
  }
  return {
    schemaVersion: 1,
    tenantId,
    jobId,
    idempotencyKey,
    classroomDocumentSha256,
    mediaManifestSha256,
    sourceManifestSha256,
    files,
  };
}

async function requestChunks(request: Request): Promise<AsyncIterable<Uint8Array>> {
  if (!request.body) {
    throw new ExportInputStagingError(
      400,
      "INVALID_EXPORT_INPUT",
      "Export input body is missing.",
    );
  }
  const body = request.body;
  return {
    async *[Symbol.asyncIterator]() {
      const reader = body.getReader();
      try {
        for (;;) {
          const result = await reader.read();
          if (result.done) {
            return;
          }
          yield result.value;
        }
      } finally {
        reader.releaseLock();
      }
    },
  };
}

async function readBodyLimited(request: Request, maximum: number): Promise<string> {
  const chunks = await requestChunks(request);
  const values: Uint8Array[] = [];
  let bytes = 0;
  for await (const chunk of chunks) {
    bytes += chunk.byteLength;
    if (bytes > maximum) {
      throw new ExportInputStagingError(
        413,
        "EXPORT_INPUT_TOO_LARGE",
        "Export input request body is too large.",
      );
    }
    values.push(chunk);
  }
  const body = new Uint8Array(bytes);
  let offset = 0;
  for (const chunk of values) {
    body.set(chunk, offset);
    offset += chunk.byteLength;
  }
  try {
    return new TextDecoder("utf-8", { fatal: true }).decode(body);
  } catch {
    throw new ExportInputStagingError(
      400,
      "INVALID_EXPORT_INPUT",
      "Export input request body is invalid.",
    );
  }
}

function parseCanonicalJson(body: string): unknown {
  let value: unknown;
  try {
    value = JSON.parse(body) as unknown;
  } catch {
    throw new ExportInputStagingError(
      400,
      "INVALID_EXPORT_INPUT",
      "Export input request body is invalid.",
    );
  }
  if (canonicalJson(value) !== body) {
    throw new ExportInputStagingError(
      400,
      "NONCANONICAL_EXPORT_INPUT",
      "Export input request body is not canonical JSON.",
    );
  }
  return value;
}

export class ExportInputStagingStore {
  constructor(
    private readonly stateRoot = configuredOpenMaicStateRoot(),
    private readonly artifacts: ArtifactStore = artifactStore,
    private readonly outputs: ContentOutputRegistry = contentOutputRegistry,
    private readonly now = () => new Date(),
  ) {}

  private reservationPath(tenantId: string, jobId: string): string {
    return durableFile(
      this.stateRoot,
      "export-inputs",
      "reservations",
      [tenantId, jobId],
      "reservation.json",
    );
  }

  private receiptPath(tenantId: string, jobId: string): string {
    return durableFile(
      this.stateRoot,
      "export-inputs",
      "receipts",
      [tenantId, jobId],
      "receipt.json",
    );
  }

  private readReservation(tenantId: string, jobId: string): ReservationRecord | null {
    const value = readDurableJson(this.reservationPath(tenantId, jobId));
    if (!value) {
      return null;
    }
    const raw = exactDurableRecord(value, "export input reservation", [
      "version",
      "tenantId",
      "jobId",
      "idempotencyKey",
      "declarationSha256",
      "declaration",
    ]);
    const declaration = parseExportInputDeclaration(raw.declaration);
    const declarationSha256 = sha256(canonicalJson(declaration));
    if (
      raw.version !== 1 ||
      raw.tenantId !== tenantId ||
      raw.jobId !== jobId ||
      raw.idempotencyKey !== declaration.idempotencyKey ||
      raw.declarationSha256 !== declarationSha256 ||
      declaration.tenantId !== tenantId ||
      declaration.jobId !== jobId
    ) {
      throw new Error("export input reservation binding is invalid");
    }
    return {
      version: 1,
      tenantId,
      jobId,
      idempotencyKey: declaration.idempotencyKey,
      declarationSha256,
      declaration,
    };
  }

  reserve(declaration: ExportInputDeclaration): ReservationRecord {
    const normalized = parseExportInputDeclaration(declaration);
    const declarationSha256 = sha256(canonicalJson(normalized));
    const record: ReservationRecord = {
      version: 1,
      tenantId: normalized.tenantId,
      jobId: normalized.jobId,
      idempotencyKey: normalized.idempotencyKey,
      declarationSha256,
      declaration: normalized,
    };
    if (
      !writeDurableJsonExclusive(
        this.reservationPath(normalized.tenantId, normalized.jobId),
        record,
      )
    ) {
      const existing = this.readReservation(normalized.tenantId, normalized.jobId);
      if (!existing || canonicalJson(existing) !== canonicalJson(record)) {
        throw new ExportInputStagingError(
          409,
          "EXPORT_INPUT_CONFLICT",
          "Export input reservation conflicts with durable state.",
        );
      }
      return existing;
    }
    return this.readReservation(normalized.tenantId, normalized.jobId) ?? record;
  }

  async upload(
    tenantId: string,
    jobId: string,
    idempotencyKey: string,
    fileId: string,
    contentType: string,
    body: AsyncIterable<Uint8Array>,
  ): Promise<ExportInputFileDeclaration> {
    const reservation = this.readReservation(tenantId, jobId);
    if (!reservation) {
      throw new ExportInputStagingError(
        404,
        "EXPORT_INPUT_NOT_FOUND",
        "Export input reservation was not found.",
      );
    }
    const file = reservation.declaration.files.find(
      (candidate) => candidate.fileId === fileId,
    );
    if (
      !file ||
      reservation.idempotencyKey !== idempotencyKey ||
      contentType !== file.mimeType
    ) {
      throw new ExportInputStagingError(
        409,
        "EXPORT_INPUT_BINDING_MISMATCH",
        "Export input upload does not match its reservation.",
      );
    }
    await this.artifacts.putStream({
      tenantId,
      jobId,
      relativePath: file.relativePath,
      body,
      mime: file.mimeType,
      expiresAt: new Date(this.now().getTime() + STAGING_ARTIFACT_TTL_MS).toISOString(),
      expectedSha256: file.sha256,
      expectedBytes: file.sizeBytes,
    });
    return file;
  }

  private readReceipt(tenantId: string, jobId: string): ExportInputCommitReceipt | null {
    const value = readDurableJson(this.receiptPath(tenantId, jobId));
    if (!value) {
      return null;
    }
    const raw = exactDurableRecord(value, "export input receipt", [
      "schemaVersion",
      "tenantId",
      "jobId",
      "idempotencyKey",
      "declarationSha256",
      "classroomDocumentSha256",
      "mediaManifestSha256",
      "status",
      "receiptSha256",
    ]);
    const base = {
      schemaVersion: 1 as const,
      tenantId: nonEmpty(raw.tenantId),
      jobId: nonEmpty(raw.jobId),
      idempotencyKey: nonEmpty(raw.idempotencyKey),
      declarationSha256: digest(raw.declarationSha256),
      classroomDocumentSha256: digest(raw.classroomDocumentSha256),
      mediaManifestSha256: digest(raw.mediaManifestSha256),
      status: raw.status,
    };
    if (
      base.tenantId !== tenantId ||
      base.jobId !== jobId ||
      base.status !== "committed" ||
      raw.receiptSha256 !== sha256(canonicalJson(base))
    ) {
      throw new Error("export input receipt binding is invalid");
    }
    return {
      ...base,
      status: "committed",
      receiptSha256: raw.receiptSha256 as string,
    };
  }

  async commit(
    tenantId: string,
    jobId: string,
    idempotencyKey: string,
    declarationSha256: string,
  ): Promise<ExportInputCommitReceipt> {
    const reservation = this.readReservation(tenantId, jobId);
    if (!reservation) {
      throw new ExportInputStagingError(
        404,
        "EXPORT_INPUT_NOT_FOUND",
        "Export input reservation was not found.",
      );
    }
    if (
      reservation.idempotencyKey !== idempotencyKey ||
      reservation.declarationSha256 !== declarationSha256
    ) {
      throw new ExportInputStagingError(
        409,
        "EXPORT_INPUT_BINDING_MISMATCH",
        "Export input commit does not match its reservation.",
      );
    }
    const existing = this.readReceipt(tenantId, jobId);
    if (existing) {
      if (existing.declarationSha256 !== declarationSha256) {
        throw new ExportInputStagingError(
          409,
          "EXPORT_INPUT_CONFLICT",
          "Export input commit conflicts with durable state.",
        );
      }
      return existing;
    }
    for (const file of reservation.declaration.files) {
      const artifact = await this.artifacts.verify(
        tenantId,
        jobId,
        file.relativePath,
      );
      if (
        !artifact ||
        artifact.sha256 !== file.sha256 ||
        artifact.bytes !== file.sizeBytes ||
        artifact.mime !== file.mimeType
      ) {
        throw new ExportInputStagingError(
          409,
          "EXPORT_INPUT_INCOMPLETE",
          "Export input files are incomplete.",
        );
      }
    }
    const documentFile = reservation.declaration.files[0];
    const storedDocument = await this.artifacts.read(
      tenantId,
      jobId,
      documentFile.relativePath,
      new Date(0),
    );
    if (!storedDocument) {
      throw new ExportInputStagingError(
        409,
        "EXPORT_INPUT_INCOMPLETE",
        "Export input document is unavailable.",
      );
    }
    let document: PortableClassroomDocument;
    try {
      const body = new TextDecoder("utf-8", { fatal: true }).decode(
        storedDocument.bytes,
      );
      const parsed = JSON.parse(body) as unknown;
      if (canonicalJson(parsed) !== body) {
        throw new Error("classroom document canonical JSON is unstable");
      }
      document = asPortableDocument(parsed);
    } catch {
      throw new ExportInputStagingError(
        422,
        "INVALID_CLASSROOM_DOCUMENT",
        "Export input classroom document is invalid.",
      );
    }
    const declaration = reservation.declaration;
    if (
      sha256(canonicalJson(document)) !== declaration.classroomDocumentSha256 ||
      sha256(canonicalJson(document.mediaManifest)) !==
        declaration.mediaManifestSha256 ||
      document.mediaManifest.length !== declaration.files.length - 1
    ) {
      throw new ExportInputStagingError(
        422,
        "EXPORT_INPUT_HASH_MISMATCH",
        "Export input hashes do not match the classroom document.",
      );
    }
    const mediaFiles = new Map(
      declaration.files.slice(1).map((file) => [file.mediaId, file]),
    );
    for (const media of document.mediaManifest) {
      const file = mediaFiles.get(media.mediaId);
      if (
        !file ||
        file.relativePath !== media.relativePath ||
        file.mimeType !== media.mimeType ||
        file.sha256 !== media.sha256 ||
        file.sizeBytes !== media.sizeBytes
      ) {
        throw new ExportInputStagingError(
          422,
          "EXPORT_INPUT_MEDIA_MISMATCH",
          "Export input media do not match the classroom document.",
        );
      }
    }
    const registered = this.outputs.registerPayload(
      tenantId,
      document,
      document.mediaManifest,
      jobId,
      jobId,
    );
    if (
      registered.classroomDocumentSha256 !== declaration.classroomDocumentSha256 ||
      registered.mediaManifestSha256 !== declaration.mediaManifestSha256
    ) {
      throw new Error("export input registration hash mismatch");
    }
    const base = {
      schemaVersion: 1 as const,
      tenantId,
      jobId,
      idempotencyKey,
      declarationSha256,
      classroomDocumentSha256: declaration.classroomDocumentSha256,
      mediaManifestSha256: declaration.mediaManifestSha256,
      status: "committed" as const,
    };
    const receipt: ExportInputCommitReceipt = {
      ...base,
      receiptSha256: sha256(canonicalJson(base)),
    };
    writeDurableJsonExclusive(this.receiptPath(tenantId, jobId), receipt);
    const persisted = this.readReceipt(tenantId, jobId);
    if (!persisted || canonicalJson(persisted) !== canonicalJson(receipt)) {
      throw new Error("export input receipt conflicts with durable state");
    }
    return persisted;
  }
}

function stagingError(error: unknown): Response {
  if (error instanceof ExportInputStagingError) {
    return serviceError(error.status, error.code, error.message);
  }
  return serviceError(
    500,
    "EXPORT_INPUT_STAGING_FAILED",
    "Export input staging failed.",
  );
}

export function createExportInputReserveHandler(
  dependencies: ServiceBoundaryDependencies & { store: ExportInputStagingStore },
): (request: Request, context: ExportInputRouteContext) => Promise<Response> {
  return async (request, context) => {
    try {
      const body = await readBodyLimited(request, MAX_DECLARATION_BYTES);
      const signed = authenticateServiceRequest(request, body, dependencies);
      if (!signed) {
        return serviceError(401, "AUTHENTICATION_FAILED", "Service authentication failed.");
      }
      const { jobId } = await context.params;
      if (
        request.method !== "POST" ||
        new URL(request.url).pathname !==
          `/api/yfeistai/v1/export-inputs/${encodeURIComponent(jobId)}`
      ) {
        return serviceError(404, "ROUTE_NOT_FOUND", "Route not found.");
      }
      const declaration = parseExportInputDeclaration(parseCanonicalJson(body));
      if (
        signed.tenantId !== declaration.tenantId ||
        signed.jobId !== jobId ||
        signed.jobId !== declaration.jobId ||
        signed.idempotencyKey !== declaration.idempotencyKey
      ) {
        return serviceError(
          403,
          "REQUEST_BINDING_MISMATCH",
          "Signed request metadata does not match the request body.",
        );
      }
      const reserved = dependencies.store.reserve(declaration);
      return Response.json({
        tenantId: reserved.tenantId,
        jobId: reserved.jobId,
        idempotencyKey: reserved.idempotencyKey,
        declarationSha256: reserved.declarationSha256,
        status: "reserved",
      });
    } catch (error) {
      return stagingError(error);
    }
  };
}

export function createExportInputUploadHandler(
  dependencies: ServiceBoundaryDependencies & { store: ExportInputStagingStore },
): (request: Request, context: ExportInputFileRouteContext) => Promise<Response> {
  return async (request, context) => {
    const signed = authenticatePrehashedServiceRequest(request, dependencies);
    if (!signed) {
      return serviceError(401, "AUTHENTICATION_FAILED", "Service authentication failed.");
    }
    try {
      const { jobId, fileId } = await context.params;
      if (
        request.method !== "PUT" ||
        new URL(request.url).pathname !==
          `/api/yfeistai/v1/export-inputs/${encodeURIComponent(jobId)}/files/${encodeURIComponent(fileId)}` ||
        signed.jobId !== jobId
      ) {
        return serviceError(404, "ROUTE_NOT_FOUND", "Route not found.");
      }
      const file = await dependencies.store.upload(
        signed.tenantId,
        jobId,
        signed.idempotencyKey,
        fileId,
        request.headers.get("content-type") ?? "",
        await requestChunks(request),
      );
      return Response.json({
        tenantId: signed.tenantId,
        jobId,
        fileId: file.fileId,
        sha256: file.sha256,
        sizeBytes: file.sizeBytes,
        status: "uploaded",
      });
    } catch (error) {
      return stagingError(error);
    }
  };
}

export function createExportInputCommitHandler(
  dependencies: ServiceBoundaryDependencies & { store: ExportInputStagingStore },
): (request: Request, context: ExportInputRouteContext) => Promise<Response> {
  return async (request, context) => {
    try {
      const body = await readBodyLimited(request, 1024);
      const signed = authenticateServiceRequest(request, body, dependencies);
      if (!signed) {
        return serviceError(401, "AUTHENTICATION_FAILED", "Service authentication failed.");
      }
      const { jobId } = await context.params;
      if (
        request.method !== "POST" ||
        new URL(request.url).pathname !==
          `/api/yfeistai/v1/export-inputs/${encodeURIComponent(jobId)}/commit` ||
        signed.jobId !== jobId
      ) {
        return serviceError(404, "ROUTE_NOT_FOUND", "Route not found.");
      }
      const parsed = record(parseCanonicalJson(body));
      exactKeys(parsed, ["declarationSha256"]);
      const receipt = await dependencies.store.commit(
        signed.tenantId,
        jobId,
        signed.idempotencyKey,
        digest(parsed.declarationSha256),
      );
      return Response.json(receipt);
    } catch (error) {
      return stagingError(error);
    }
  };
}

const EXPORT_INPUT_STAGING_KEY = Symbol.for(
  "yfeistai.openmaic.export-input-staging",
);
const stagingGlobal = globalThis as typeof globalThis & {
  [EXPORT_INPUT_STAGING_KEY]?: ExportInputStagingStore;
};

export const exportInputStagingStore =
  stagingGlobal[EXPORT_INPUT_STAGING_KEY] ??
  (stagingGlobal[EXPORT_INPUT_STAGING_KEY] = new ExportInputStagingStore());

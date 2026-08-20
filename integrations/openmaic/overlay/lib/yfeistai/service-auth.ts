import { createHash, createHmac, timingSafeEqual } from "node:crypto";
import { readFileSync } from "node:fs";

export const SERVICE_SECRET_PATH = "/run/secrets/openmaic_service_secret" as const;
export const MAX_CLOCK_SKEW_SECONDS = 60 as const;

const SAFE_METHODS = new Set(["GET", "HEAD", "OPTIONS"]);
const SHA256_HEX = /^[0-9a-f]{64}$/;

export interface ServiceRequestParts {
  method: string;
  path: string;
  tenantId: string;
  jobId: string;
  timestamp: number;
  idempotencyKey?: string;
  body: string | Uint8Array;
}

type ServiceRequestIdentityParts = Omit<ServiceRequestParts, "body">;

export interface ServiceRequestToSign extends ServiceRequestParts {
  secret: string;
}

export interface SignedServiceRequest {
  method: string;
  path: string;
  tenantId: string;
  jobId: string;
  timestamp: number;
  idempotencyKey: string;
  signature: string;
}

export type ServiceAuthResult =
  | { ok: true }
  | {
      ok: false;
      reason: "expired" | "idempotency" | "invalid" | "signature";
    };

export interface ServiceVerificationOptions {
  secret: string;
  nowSeconds: number;
  body: string | Uint8Array;
}

export interface ServiceDigestVerificationOptions {
  secret: string;
  nowSeconds: number;
  bodySha256: string;
}

export interface ServiceRequestDigestParts {
  method: string;
  path: string;
  tenantId: string;
  jobId: string;
  timestamp: number;
  idempotencyKey?: string;
  bodySha256: string;
}

function requireCanonicalLine(
  name: string,
  value: string,
  options: { allowEmpty?: boolean } = {},
): string {
  if (typeof value !== "string" || (!options.allowEmpty && value.length === 0)) {
    throw new Error(`${name} must be a non-empty string`);
  }
  if (value.includes("\n") || value.includes("\r")) {
    throw new Error(`${name} cannot contain a newline`);
  }
  return value;
}

function normalizeMethod(method: string): string {
  const normalized = requireCanonicalLine("method", method).toUpperCase();
  if (!/^[A-Z]+$/.test(normalized)) {
    throw new Error("method must contain only ASCII letters");
  }
  return normalized;
}

function normalizeTimestamp(timestamp: number): number {
  if (!Number.isSafeInteger(timestamp) || timestamp < 0) {
    throw new Error("timestamp must be a non-negative Unix timestamp");
  }
  return timestamp;
}

function requiresIdempotencyKey(method: string): boolean {
  return !SAFE_METHODS.has(method);
}

function normalizeRequestParts(input: ServiceRequestIdentityParts) {
  const method = normalizeMethod(input.method);
  const path = requireCanonicalLine("path", input.path);
  if (!path.startsWith("/")) {
    throw new Error("path must be absolute");
  }
  const tenantId = requireCanonicalLine("tenantId", input.tenantId);
  const jobId = requireCanonicalLine("jobId", input.jobId);
  const timestamp = normalizeTimestamp(input.timestamp);
  const idempotencyKey = requireCanonicalLine(
    "idempotencyKey",
    input.idempotencyKey ?? "",
    { allowEmpty: !requiresIdempotencyKey(method) },
  );

  return {
    method,
    path,
    tenantId,
    jobId,
    timestamp,
    idempotencyKey,
  };
}

function sha256Body(body: string | Uint8Array): string {
  return createHash("sha256").update(body).digest("hex");
}

export function canonicalServiceRequest(input: ServiceRequestParts): string {
  return canonicalServiceRequestDigest({
    ...input,
    bodySha256: sha256Body(input.body),
  });
}

export function canonicalServiceRequestDigest(
  input: ServiceRequestDigestParts,
): string {
  const normalized = normalizeRequestParts(input);
  if (!SHA256_HEX.test(input.bodySha256)) {
    throw new Error("body digest must be a lowercase SHA-256 digest");
  }
  const canonicalParts = [
    normalized.method,
    normalized.path,
    normalized.tenantId,
    normalized.jobId,
    String(normalized.timestamp),
    normalized.idempotencyKey,
    input.bodySha256,
  ];
  return canonicalParts.join("\n");
}

export function signServiceRequest(input: ServiceRequestToSign): SignedServiceRequest {
  const secret = requireCanonicalLine("secret", input.secret);
  const normalized = normalizeRequestParts(input);
  const signature = createHmac("sha256", secret)
    .update(canonicalServiceRequest(input), "utf8")
    .digest("hex");

  return {
    ...normalized,
    signature,
  };
}

export function verifyServiceRequest(
  signed: SignedServiceRequest,
  options: ServiceVerificationOptions,
): ServiceAuthResult {
  let normalized;
  try {
    normalized = normalizeRequestParts(signed);
    requireCanonicalLine("secret", options.secret);
    normalizeTimestamp(options.nowSeconds);
  } catch (error) {
    if (
      error instanceof Error &&
      error.message.toLowerCase().includes("idempotency")
    ) {
      return { ok: false, reason: "idempotency" };
    }
    return { ok: false, reason: "invalid" };
  }

  if (Math.abs(options.nowSeconds - normalized.timestamp) > MAX_CLOCK_SKEW_SECONDS) {
    return { ok: false, reason: "expired" };
  }

  if (!SHA256_HEX.test(signed.signature)) {
    return { ok: false, reason: "signature" };
  }

  const expected = createHmac("sha256", options.secret)
    .update(canonicalServiceRequest({ ...normalized, body: options.body }), "utf8")
    .digest();
  const received = Buffer.from(signed.signature, "hex");

  return timingSafeEqual(expected, received)
    ? { ok: true }
    : { ok: false, reason: "signature" };
}

export function verifyServiceRequestDigest(
  signed: SignedServiceRequest,
  options: ServiceDigestVerificationOptions,
): ServiceAuthResult {
  let normalized;
  try {
    normalized = normalizeRequestParts(signed);
    requireCanonicalLine("secret", options.secret);
    normalizeTimestamp(options.nowSeconds);
    if (!SHA256_HEX.test(options.bodySha256)) {
      throw new Error("body digest is invalid");
    }
  } catch (error) {
    if (
      error instanceof Error &&
      error.message.toLowerCase().includes("idempotency")
    ) {
      return { ok: false, reason: "idempotency" };
    }
    return { ok: false, reason: "invalid" };
  }
  if (Math.abs(options.nowSeconds - normalized.timestamp) > MAX_CLOCK_SKEW_SECONDS) {
    return { ok: false, reason: "expired" };
  }
  if (!SHA256_HEX.test(signed.signature)) {
    return { ok: false, reason: "signature" };
  }
  const expected = createHmac("sha256", options.secret)
    .update(
      canonicalServiceRequestDigest({
        ...normalized,
        bodySha256: options.bodySha256,
      }),
      "utf8",
    )
    .digest();
  const received = Buffer.from(signed.signature, "hex");
  return timingSafeEqual(expected, received)
    ? { ok: true }
    : { ok: false, reason: "signature" };
}

export function readServiceSecret(): string {
  const secret = readFileSync(SERVICE_SECRET_PATH, "utf8").replace(/\r?\n$/, "");
  if (secret.length === 0) {
    throw new Error("OpenMAIC service secret is empty");
  }
  return secret;
}

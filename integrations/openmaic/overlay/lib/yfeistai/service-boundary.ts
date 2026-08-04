import {
  type SignedServiceRequest,
  verifyServiceRequest,
  verifyServiceRequestDigest,
} from "./service-auth";

export interface ServiceBoundaryDependencies {
  readSecret(): string;
  nowSeconds?: () => number;
}

export interface JobRouteContext {
  params:
    | { jobId: string }
    | Promise<{ jobId: string }>;
}

export interface ArtifactRouteContext {
  params:
    | { jobId: string; path: string[] }
    | Promise<{ jobId: string; path: string[] }>;
}

export function serviceError(
  status: number,
  code: string,
  message: string,
): Response {
  return Response.json({ error: { code, message } }, { status });
}

export function authenticateServiceRequest(
  request: Request,
  body: string,
  dependencies: ServiceBoundaryDependencies,
): SignedServiceRequest | null {
  let secret: string;
  try {
    secret = dependencies.readSecret();
  } catch {
    return null;
  }

  const signed: SignedServiceRequest = {
    method: request.method,
    path: new URL(request.url).pathname,
    tenantId: request.headers.get("x-yfeistai-tenant-id") ?? "",
    jobId: request.headers.get("x-yfeistai-job-id") ?? "",
    timestamp: Number(request.headers.get("x-yfeistai-timestamp")),
    idempotencyKey: request.headers.get("x-yfeistai-idempotency-key") ?? "",
    signature: request.headers.get("x-yfeistai-signature") ?? "",
  };
  const result = verifyServiceRequest(signed, {
    secret,
    nowSeconds: dependencies.nowSeconds?.() ?? Math.floor(Date.now() / 1_000),
    body,
  });
  return result.ok ? signed : null;
}

export function authenticatePrehashedServiceRequest(
  request: Request,
  dependencies: ServiceBoundaryDependencies,
): SignedServiceRequest | null {
  let secret: string;
  try {
    secret = dependencies.readSecret();
  } catch {
    return null;
  }
  const signed: SignedServiceRequest = {
    method: request.method,
    path: new URL(request.url).pathname,
    tenantId: request.headers.get("x-yfeistai-tenant-id") ?? "",
    jobId: request.headers.get("x-yfeistai-job-id") ?? "",
    timestamp: Number(request.headers.get("x-yfeistai-timestamp")),
    idempotencyKey: request.headers.get("x-yfeistai-idempotency-key") ?? "",
    signature: request.headers.get("x-yfeistai-signature") ?? "",
  };
  const result = verifyServiceRequestDigest(signed, {
    secret,
    nowSeconds: dependencies.nowSeconds?.() ?? Math.floor(Date.now() / 1_000),
    bodySha256: request.headers.get("x-yfeistai-content-sha256") ?? "",
  });
  return result.ok ? signed : null;
}

export function hasSignedBodyBinding(
  signed: SignedServiceRequest,
  body: {
    tenantId: string;
    jobId: string;
    idempotencyKey: string;
  },
): boolean {
  return (
    body.tenantId === signed.tenantId &&
    body.jobId === signed.jobId &&
    body.idempotencyKey === signed.idempotencyKey
  );
}

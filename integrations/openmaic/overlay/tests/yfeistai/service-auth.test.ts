import { createHash, createHmac } from "node:crypto";

import { describe, expect, test } from "vitest";

import { GET as getHealth } from "../../app/api/yfeistai/v1/health/route";
import {
  SERVICE_SECRET_PATH,
  canonicalServiceRequest,
  canonicalServiceRequestDigest,
  signServiceRequest,
  verifyServiceRequest,
  verifyServiceRequestDigest,
} from "../../lib/yfeistai/service-auth";

const SECRET = "test-service-secret";
const NOW_SECONDS = 1_800_000_000;
const BODY = '{"schemaVersion":"1.0"}';

function signedPost() {
  return signServiceRequest({
    secret: SECRET,
    method: "POST",
    path: "/api/yfeistai/v1/outlines",
    tenantId: "tenant-a",
    jobId: "job-a",
    timestamp: NOW_SECONDS,
    idempotencyKey: "idem-a",
    body: BODY,
  });
}

describe("yFeiSTAI service authentication", () => {
  test("uses the exact canonical request and HMAC-SHA256 signature", () => {
    const bodySha256 = createHash("sha256").update(BODY, "utf8").digest("hex");
    const expectedCanonical = [
      "POST",
      "/api/yfeistai/v1/outlines",
      "tenant-a",
      "job-a",
      String(NOW_SECONDS),
      "idem-a",
      bodySha256,
    ].join("\n");

    expect(
      canonicalServiceRequest({
        method: "POST",
        path: "/api/yfeistai/v1/outlines",
        tenantId: "tenant-a",
        jobId: "job-a",
        timestamp: NOW_SECONDS,
        idempotencyKey: "idem-a",
        body: BODY,
      }),
    ).toBe(expectedCanonical);

    const signed = signedPost();
    expect(signed.signature).toBe(
      createHmac("sha256", SECRET).update(expectedCanonical, "utf8").digest("hex"),
    );
    expect(signed).not.toHaveProperty("secret");
  });

  test("rejects stale, future-dated, and body-mismatched signatures", () => {
    const signed = signedPost();

    expect(
      verifyServiceRequest(signed, {
        secret: SECRET,
        nowSeconds: NOW_SECONDS + 61,
        body: BODY,
      }),
    ).toEqual({ ok: false, reason: "expired" });
    expect(
      verifyServiceRequest(signed, {
        secret: SECRET,
        nowSeconds: NOW_SECONDS - 61,
        body: BODY,
      }),
    ).toEqual({ ok: false, reason: "expired" });
    expect(
      verifyServiceRequest(signed, {
        secret: SECRET,
        nowSeconds: NOW_SECONDS,
        body: '{"schemaVersion":"2.0"}',
      }),
    ).toEqual({ ok: false, reason: "signature" });
  });

  test("accepts the inclusive 60-second boundary", () => {
    const signed = signedPost();

    expect(
      verifyServiceRequest(signed, {
        secret: SECRET,
        nowSeconds: NOW_SECONDS + 60,
        body: BODY,
      }),
    ).toEqual({ ok: true });
  });

  test("authenticates a streamed request from its predeclared body digest", () => {
    const bodySha256 = createHash("sha256").update("streamed-body").digest("hex");
    const canonical = canonicalServiceRequestDigest({
      method: "PUT",
      path: "/api/yfeistai/v1/export-inputs/job-a/files/file-a",
      tenantId: "tenant-a",
      jobId: "job-a",
      timestamp: NOW_SECONDS,
      idempotencyKey: "idem-a",
      bodySha256,
    });
    const signed = {
      method: "PUT",
      path: "/api/yfeistai/v1/export-inputs/job-a/files/file-a",
      tenantId: "tenant-a",
      jobId: "job-a",
      timestamp: NOW_SECONDS,
      idempotencyKey: "idem-a",
      signature: createHmac("sha256", SECRET)
        .update(canonical, "utf8")
        .digest("hex"),
    };

    expect(
      verifyServiceRequestDigest(signed, {
        secret: SECRET,
        nowSeconds: NOW_SECONDS,
        bodySha256,
      }),
    ).toEqual({ ok: true });
    expect(
      verifyServiceRequestDigest(signed, {
        secret: SECRET,
        nowSeconds: NOW_SECONDS,
        bodySha256: "f".repeat(64),
      }),
    ).toEqual({ ok: false, reason: "signature" });
  });

  test.each([
    ["method", "PUT"],
    ["path", "/api/yfeistai/v1/classrooms"],
    ["tenantId", "tenant-b"],
    ["jobId", "job-b"],
    ["idempotencyKey", "idem-b"],
  ] as const)("rejects a changed signed %s", (field, value) => {
    const signed = { ...signedPost(), [field]: value };

    expect(
      verifyServiceRequest(signed, {
        secret: SECRET,
        nowSeconds: NOW_SECONDS,
        body: BODY,
      }),
    ).toEqual({ ok: false, reason: "signature" });
  });

  test("requires idempotency keys for writes but not safe reads", () => {
    expect(() =>
      signServiceRequest({
        secret: SECRET,
        method: "POST",
        path: "/api/yfeistai/v1/outlines",
        tenantId: "tenant-a",
        jobId: "job-a",
        timestamp: NOW_SECONDS,
        body: BODY,
      }),
    ).toThrow(/idempotency/i);

    const signedGet = signServiceRequest({
      secret: SECRET,
      method: "GET",
      path: "/api/yfeistai/v1/outlines/job-a",
      tenantId: "tenant-a",
      jobId: "job-a",
      timestamp: NOW_SECONDS,
      body: "",
    });
    expect(
      verifyServiceRequest(signedGet, {
        secret: SECRET,
        nowSeconds: NOW_SECONDS,
        body: "",
      }),
    ).toEqual({ ok: true });
  });

  test("rejects ambiguous canonical fields and malformed signatures", () => {
    expect(() =>
      canonicalServiceRequest({
        method: "POST",
        path: "/api/yfeistai/v1/outlines",
        tenantId: "tenant-a\njob-a",
        jobId: "job-a",
        timestamp: NOW_SECONDS,
        idempotencyKey: "idem-a",
        body: BODY,
      }),
    ).toThrow(/newline/i);

    expect(
      verifyServiceRequest(
        { ...signedPost(), signature: "not-a-sha256-hmac" },
        {
          secret: SECRET,
          nowSeconds: NOW_SECONDS,
          body: BODY,
        },
      ),
    ).toEqual({ ok: false, reason: "signature" });
  });

  test("uses only the Docker secret mount path", () => {
    expect(SERVICE_SECRET_PATH).toBe("/run/secrets/openmaic_service_secret");
  });
});

test("health route returns the exact private service capability contract", async () => {
  const response = getHealth();

  expect(response.status).toBe(200);
  await expect(response.json()).resolves.toEqual({
    service: "openmaic",
    upstreamCommit: "0cf2a330411681190e89f48e20f305345ff99f87",
    appVersion: "0.3.1",
    contractVersions: ["1.0"],
    capabilities: [
      "outline",
      "content",
      "micro",
      "export",
      "cancel",
      "artifact-manifest",
    ],
    exportFormats: ["classroom_zip", "pptx", "offline_html", "mp4"],
  });
});

import assert from "node:assert/strict";
import test from "node:test";
import {
  isLivePlaywrightSelected,
  resolveLiveBaseUrl,
} from "../playwright.live-policy";

function captureError(callback: () => unknown): Error {
  try {
    callback();
  } catch (error) {
    assert.ok(error instanceof Error);
    return error;
  }
  assert.fail("expected live Playwright policy to reject the URL");
}

test("live project selection accepts only supported argv before the separator", () => {
  const cases = [
    {
      name: "equals form",
      argv: ["node", "playwright", "test", "--project=first-release-live"],
      expected: true,
    },
    {
      name: "separate form",
      argv: [
        "node",
        "playwright",
        "test",
        "--project",
        "first-release-live",
      ],
      expected: true,
    },
    {
      name: "equals form after separator",
      argv: [
        "node",
        "playwright",
        "test",
        "--",
        "--project=first-release-live",
      ],
      expected: false,
    },
    {
      name: "separate form after separator",
      argv: [
        "node",
        "playwright",
        "test",
        "--",
        "--project",
        "first-release-live",
      ],
      expected: false,
    },
    {
      name: "unrelated project",
      argv: ["node", "playwright", "test", "--project=teaching-flow"],
      expected: false,
    },
  ] as const;

  for (const scenario of cases) {
    assert.equal(
      isLivePlaywrightSelected(scenario.argv, undefined),
      scenario.expected,
      scenario.name,
    );
  }
});

test("live project selection accepts only the fixed evidence values", () => {
  const fixedEvidence = [
    "teacher_flow",
    "student_micro_flow",
    "student_full_flow",
    "content_operations_flow",
    "tailwind4_visual_matrix",
  ] as const;

  for (const evidence of fixedEvidence) {
    assert.equal(
      isLivePlaywrightSelected(["node", "playwright"], evidence),
      true,
    );
  }
  assert.equal(
    isLivePlaywrightSelected(["node", "playwright"], "attacker-controlled"),
    false,
  );
});

test("live URL resolution is disabled outside live mode", () => {
  assert.equal(resolveLiveBaseUrl(false, undefined), undefined);
  assert.equal(resolveLiveBaseUrl(false, "http://localhost"), undefined);
});

test("live URL resolution rejects missing, invalid, and loopback inputs safely", () => {
  const cases = [
    {
      name: "missing",
      rawBaseUrl: undefined,
      token: "missing-token",
      expectedMessage: "WEB_BASE_URL is required for live release evidence",
    },
    {
      name: "empty",
      rawBaseUrl: "",
      token: "empty-token",
      expectedMessage: "WEB_BASE_URL is required for live release evidence",
    },
    {
      name: "blank",
      rawBaseUrl: "  \t  ",
      token: "blank-token",
      expectedMessage: "WEB_BASE_URL is required for live release evidence",
    },
    {
      name: "relative",
      rawBaseUrl: "relative/token-relative",
      token: "token-relative",
      expectedMessage: "WEB_BASE_URL must identify a non-loopback HTTP(S) host",
    },
    {
      name: "ftp",
      rawBaseUrl: "ftp://token-ftp@candidate.example.test/release",
      token: "token-ftp",
      expectedMessage: "WEB_BASE_URL must identify a non-loopback HTTP(S) host",
    },
    {
      name: "localhost",
      rawBaseUrl: "http://token-localhost@localhost/release",
      token: "token-localhost",
      expectedMessage: "WEB_BASE_URL must identify a non-loopback HTTP(S) host",
    },
    {
      name: "subdomain localhost",
      rawBaseUrl: "http://token-subdomain@sub.localhost/release",
      token: "token-subdomain",
      expectedMessage: "WEB_BASE_URL must identify a non-loopback HTTP(S) host",
    },
    {
      name: "IPv4 loopback start",
      rawBaseUrl: "http://token-ipv4-start@127.0.0.1/release",
      token: "token-ipv4-start",
      expectedMessage: "WEB_BASE_URL must identify a non-loopback HTTP(S) host",
    },
    {
      name: "IPv4 loopback range",
      rawBaseUrl: "http://token-ipv4-range@127.0.0.2/release",
      token: "token-ipv4-range",
      expectedMessage: "WEB_BASE_URL must identify a non-loopback HTTP(S) host",
    },
    {
      name: "IPv6 loopback",
      rawBaseUrl: "http://token-ipv6@[::1]/release",
      token: "token-ipv6",
      expectedMessage: "WEB_BASE_URL must identify a non-loopback HTTP(S) host",
    },
  ] as const;

  for (const scenario of cases) {
    const error = captureError(() =>
      resolveLiveBaseUrl(true, scenario.rawBaseUrl),
    );
    assert.equal(error.message, scenario.expectedMessage, scenario.name);
    if (scenario.rawBaseUrl) {
      assert.equal(
        error.message.includes(scenario.rawBaseUrl),
        false,
        scenario.name,
      );
    }
    assert.equal(error.message.includes(scenario.token), false, scenario.name);
  }
});

test("live URL resolution rejects IPv4-mapped IPv6 127/8 candidates", () => {
  const candidates = [
    "http://[::ffff:127.0.0.1]",
    "http://[::ffff:127.42.3.4]",
  ] as const;
  const accepted = candidates.filter((candidate) => {
    try {
      resolveLiveBaseUrl(true, candidate);
      return true;
    } catch (error) {
      assert.ok(error instanceof Error);
      assert.equal(
        error.message,
        "WEB_BASE_URL must identify a non-loopback HTTP(S) host",
      );
      assert.equal(error.message.includes(candidate), false);
      return false;
    }
  });

  assert.deepEqual(accepted, []);
});

test("live URL resolution accepts a remote HTTPS URL", () => {
  assert.equal(
    resolveLiveBaseUrl(
      true,
      "  https://candidate.example.test/release?token=remote-allowed  ",
    ),
    "https://candidate.example.test/release?token=remote-allowed",
  );
  assert.equal(
    resolveLiveBaseUrl(true, "https://[2001:db8::1]/release"),
    "https://[2001:db8::1]/release",
  );
});

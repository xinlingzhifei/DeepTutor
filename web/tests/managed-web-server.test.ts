import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

test("managed Next descendants do not inherit the Playwright runner heap limit", () => {
  const source = readFileSync(
    "tests/e2e/support/managed-web-server.ts",
    "utf8",
  );
  const forkStart = source.indexOf("const child = fork(");
  const forkEnd = source.indexOf("let pids:", forkStart);

  assert.ok(forkStart >= 0 && forkEnd > forkStart);
  assert.match(
    source.slice(forkStart, forkEnd),
    /NODE_OPTIONS:\s*`--max-old-space-size=\$\{MANAGED_HEAP_LIMIT_MB\}`/,
  );
});

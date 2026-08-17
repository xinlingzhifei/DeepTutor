import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";

const contextPath = path.join(
  process.cwd(),
  "context",
  "UnifiedChatContext.tsx",
);

test("new, edited, and regenerated replies use the currently selected language", () => {
  const source = fs.readFileSync(contextPath, "utf8");

  assert.match(source, /const effectiveLanguage = readStoredResponseLanguage\(\);/);
  assert.match(
    source,
    /\{ \.\.\.replaySnapshot, language: effectiveLanguage \}/,
  );
  assert.match(
    source,
    /type: "regenerate"[\s\S]*?language: readStoredResponseLanguage\(\)/,
  );
});

import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { existsSync, readFileSync } from "node:fs";
import path from "node:path";
import test from "node:test";

const WEB_ROOT = path.resolve(process.cwd());

test("production fonts are self-hosted and do not require Google Fonts", () => {
  const layout = readFileSync(path.join(WEB_ROOT, "app", "layout.tsx"), "utf8");

  assert.doesNotMatch(layout, /next\/font\/google/);
  assert.match(layout, /next\/font\/local/);

  const fonts = {
    "geist-latin.woff2":
      "19f9c92546aa300c312235e3125af1b81394d8db9a4bc4a425cd5b641d2d54e1",
    "lora-latin.woff2":
      "ddb8c66035104e233fc024669183aad3738b6daa16deee2ebb1241bd0f98ace1",
  } as const;

  for (const [filename, expectedSha256] of Object.entries(fonts)) {
    const fontPath = path.join(WEB_ROOT, "app", "fonts", filename);
    assert.equal(existsSync(fontPath), true, `${filename} must be bundled`);
    const digest = createHash("sha256").update(readFileSync(fontPath)).digest("hex");
    assert.equal(digest, expectedSha256, `${filename} must match the reviewed asset`);
    assert.match(layout, new RegExp(`\\./fonts/${filename.replace(".", "\\.")}`));
  }
});

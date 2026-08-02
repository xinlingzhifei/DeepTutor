import { readdirSync, statSync, writeFileSync } from "node:fs";
import { spawnSync } from "node:child_process";
import path from "node:path";
import { fileURLToPath } from "node:url";

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);
const webRoot = path.resolve(__dirname, "..");
const distRoot = path.join(webRoot, "dist", "node-tests");
const sourceTestRoot = path.join(webRoot, "tests");

function run(cmd, args) {
  const result = spawnSync(cmd, args, {
    cwd: webRoot,
    stdio: "inherit",
    env: process.env,
  });
  if (result.status !== 0) {
    process.exit(result.status ?? 1);
  }
}

function collectTests(dir) {
  const entries = readdirSync(dir)
    .map((name) => path.join(dir, name))
    .sort((a, b) => a.localeCompare(b));
  const files = [];
  for (const entry of entries) {
    const stats = statSync(entry);
    if (stats.isDirectory()) {
      files.push(...collectTests(entry));
      continue;
    }
    if (entry.endsWith(".test.ts")) {
      files.push(entry);
    }
  }
  return files;
}

run(process.execPath, [
  path.join(webRoot, "node_modules", "typescript", "bin", "tsc"),
  "-p",
  "tsconfig.node-tests.json",
]);

writeFileSync(path.join(distRoot, "package.json"), '{"type":"module"}\n');

const testFiles = collectTests(sourceTestRoot).map((source) =>
  path.join(distRoot, path.relative(webRoot, source)).replace(/\.ts$/, ".js"),
);
if (testFiles.length === 0) {
  console.error("No compiled node tests found.");
  process.exit(1);
}

run(process.execPath, [
  "--import",
  "./scripts/register-node-test-esm.mjs",
  "--test",
  ...testFiles,
]);

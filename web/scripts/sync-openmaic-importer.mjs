import { createHash } from "node:crypto";
import {
  copyFileSync,
  cpSync,
  existsSync,
  mkdirSync,
  readFileSync,
  statSync,
} from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const webRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const sourceDir = path.join(
  webRoot,
  "node_modules",
  "@openmaic",
  "importer",
  "dist",
);
const sourceEntrypoint = path.join(sourceDir, "index.js");
const targetDir = path.join(webRoot, "public", "vendor", "maic-importer");
const targetEntrypoint = path.join(targetDir, "index.js");

if (!existsSync(sourceEntrypoint) || !statSync(sourceEntrypoint).isFile()) {
  throw new Error(
    "@openmaic/importer dist is unavailable; run npm ci before building the web app",
  );
}

mkdirSync(targetDir, { recursive: true });
cpSync(sourceDir, targetDir, { recursive: true, force: true });
// Write the executable entrypoint last so a failed recursive copy cannot publish it early.
copyFileSync(sourceEntrypoint, targetEntrypoint);

const digest = file =>
  createHash("sha256").update(readFileSync(file)).digest("hex");
if (!existsSync(targetEntrypoint) || digest(sourceEntrypoint) !== digest(targetEntrypoint)) {
  throw new Error("The vendored OpenMAIC importer failed its integrity check");
}

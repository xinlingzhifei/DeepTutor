import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

test("Tailwind 4 and the OpenMAIC renderer source are configured", () => {
  const packageJson = JSON.parse(readFileSync("package.json", "utf8")) as {
    devDependencies: Record<string, string>;
  };
  const css = readFileSync("app/globals.css", "utf8");
  const postcss = readFileSync("postcss.config.js", "utf8");

  assert.equal(packageJson.devDependencies.tailwindcss, "4.2.1");
  assert.equal(packageJson.devDependencies["@tailwindcss/postcss"], "4.2.1");
  assert.equal(packageJson.devDependencies.autoprefixer, undefined);
  assert.match(css, /^@import "tailwindcss";/);
  assert.match(css, /@config "\.\.\/tailwind\.config\.js";/);
  assert.match(
    css,
    /@source "\.\.\/node_modules\/@openmaic\/renderer\/dist\/\*\*\/\*\.\{js,mjs\}";/,
  );
  assert.doesNotMatch(css, /@tailwind\s+(?:base|components|utilities)/);
  assert.match(css, /--background:/);
  assert.match(css, /@layer base/);
  assert.match(postcss, /["']@tailwindcss\/postcss["']/);
  assert.doesNotMatch(postcss, /\bautoprefixer\b/);
  assert.doesNotMatch(postcss, /\btailwindcss:\s*\{\}/);
});

import test from "node:test";
import assert from "node:assert/strict";

import {
  DEFAULT_APP_LANGUAGE,
  normalizeLanguage,
} from "../i18n/language";

test("application language defaults to Chinese", () => {
  assert.equal(DEFAULT_APP_LANGUAGE, "zh");
  assert.equal(normalizeLanguage(undefined), "zh");
  assert.equal(normalizeLanguage(null), "zh");
  assert.equal(normalizeLanguage("invalid"), "zh");
});

test("application language preserves supported aliases", () => {
  assert.equal(normalizeLanguage("zh-CN"), "zh");
  assert.equal(normalizeLanguage("cn"), "zh");
  assert.equal(normalizeLanguage("Chinese"), "zh");
  assert.equal(normalizeLanguage("en"), "en");
  assert.equal(normalizeLanguage("English"), "en");
  assert.equal(normalizeLanguage("en-US"), "en");
});

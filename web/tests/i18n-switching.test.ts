import test from "node:test";
import assert from "node:assert/strict";
import i18n from "i18next";

import { ensureLanguage, initI18n } from "../i18n/init";

test("the same i18n instance switches between Chinese and English", async () => {
  initI18n();

  await i18n.changeLanguage("zh");
  assert.equal(i18n.t("Sign out"), "退出登录");
  assert.equal(i18n.t("Add files & context"), "添加文件和上下文");
  assert.equal(i18n.t("Select persona"), "选择角色");
  assert.equal(i18n.t("Record voice"), "录制语音");

  await ensureLanguage("en");
  await i18n.changeLanguage("en");
  assert.equal(i18n.t("Sign out"), "Sign out");
  assert.equal(i18n.t("Add files & context"), "Add files & context");
  assert.equal(i18n.t("Select persona"), "Select persona");
  assert.equal(i18n.t("Record voice"), "Record voice");
});

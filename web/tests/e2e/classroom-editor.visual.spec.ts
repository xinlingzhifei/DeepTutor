import { expect, test } from "@playwright/test";

import {
  CLASSROOM_BASE_URL,
  classroomVisualCasesFor,
  classroomSnapshotName,
  expectCleanClassroomAudit,
  installClassroomNetworkAudit,
  openClassroomVisualFixture,
} from "./support/classroom-visual-support";

const EDITOR_CASES = classroomVisualCasesFor("editor");

test.use({
  baseURL: CLASSROOM_BASE_URL,
  channel: "chromium",
  locale: "en-US",
  timezoneId: "UTC",
});
test.describe.configure({ mode: "serial" });

test.describe("Classroom editor visual matrix", () => {
  for (const visualCase of EDITOR_CASES) {
    test(`${visualCase.theme.name} / ${visualCase.scene} / ${visualCase.viewport.name} / ${visualCase.language} / ${visualCase.motion}`, async ({
      page,
    }) => {
      test.setTimeout(90_000);
      const audit = await installClassroomNetworkAudit(
        page,
        visualCase.theme.stored,
      );
      const root = await openClassroomVisualFixture(page, visualCase);

      expectCleanClassroomAudit(audit);
      await expect(root).toHaveScreenshot(
        classroomSnapshotName(visualCase),
        {
          animations: "disabled",
          caret: "hide",
          scale: "css",
        },
      );
      expectCleanClassroomAudit(audit);
    });
  }
});

test("PPTX importer loads only the same-origin vendored module", async ({
  page,
}) => {
  test.setTimeout(90_000);
  const visualCase = EDITOR_CASES[0];
  const audit = await installClassroomNetworkAudit(
    page,
    visualCase.theme.stored,
  );
  await openClassroomVisualFixture(page, visualCase);

  const vendorPath = "/vendor/maic-importer/index.js";
  expect(
    audit.requests.filter(item => new URL(item.url).pathname === vendorPath),
  ).toEqual([]);

  const submitInvalidPptx = async (name: string) => {
    const root = page.getByTestId("classroom-visual-root");
    const trigger = page.getByTestId("classroom-import-trigger");
    await expect(trigger).toBeVisible();
    await trigger.click();
    await expect(root).toHaveAttribute("data-import-open", "true");
    const dialog = page.getByRole("dialog", { name: "导入课堂" });
    await expect(dialog).toBeVisible();
    await dialog.locator('input[type="file"]').setInputFiles({
      name,
      mimeType:
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
      buffer: Buffer.from("not-a-valid-pptx"),
    });
    await dialog
      .getByRole("button", { name: "导入 PPTX", exact: true })
      .click();
    await expect(dialog.getByRole("alert")).toHaveText("课堂导入失败", {
      timeout: 60_000,
    });
    await dialog.getByRole("button", { name: "取消", exact: true }).click();
    await expect(dialog).toBeHidden();
    await expect(root).toHaveAttribute("data-import-open", "false");
  };

  await submitInvalidPptx("invalid-fixture-1.pptx");
  await submitInvalidPptx("invalid-fixture-2.pptx");

  const vendorRequests = audit.requests.filter(
    item => new URL(item.url).pathname === vendorPath,
  );
  expect(vendorRequests.map(item => item.method)).toEqual(["HEAD", "GET"]);
  expect(
    vendorRequests.every(
      item => new URL(item.url).origin === new URL(CLASSROOM_BASE_URL).origin,
    ),
  ).toBe(true);
  const vendorResponses = audit.responses.filter(
    item => new URL(item.url).pathname === vendorPath,
  );
  expect(
    vendorResponses.map(item => ({ method: item.method, status: item.status })),
  ).toEqual([
    { method: "HEAD", status: 200 },
    { method: "GET", status: 200 },
  ]);
  expect(
    vendorResponses.every(item =>
      /^(?:application|text)\/javascript(?:;|$)/i.test(
        item.contentType ?? "",
      ),
    ),
  ).toBe(true);
  expect(
    audit.requests.some(item =>
      new URL(item.url).pathname.includes("/draft-media"),
    ),
  ).toBe(false);
  expectCleanClassroomAudit(audit);
});

test("draft export uses the controlled API contract and exposes the download route", async ({
  page,
}) => {
  test.setTimeout(90_000);
  const visualCase = EDITOR_CASES[1];
  const audit = await installClassroomNetworkAudit(
    page,
    visualCase.theme.stored,
  );
  await openClassroomVisualFixture(page, visualCase);

  await page.getByRole("button", { name: "PowerPoint" }).click();
  const downloadLink = page.getByRole("link", { name: "下载导出产物" });
  await expect(downloadLink).toHaveAttribute(
    "href",
    "/api/v1/classroom-exports/visual-export/download",
  );
  expect(audit.exportCreates).toHaveLength(1);
  expect(audit.exportCreates[0].body).toEqual({ format: "pptx" });
  expect(audit.exportCreates[0].headers["if-match"]).toBe(
    '"visual-revision-1"',
  );
  expect(audit.exportCreates[0].headers["idempotency-key"]).toMatch(
    /^classroom-export-/,
  );
  expect(audit.exportStatusGets).toEqual([
    "/api/v1/classroom-exports/visual-export",
  ]);
  expect(
    audit.requests
      .filter(item => {
        const pathname = new URL(item.url).pathname;
        return (
          pathname ===
            "/api/v1/classrooms/visual-classroom/draft/exports" ||
          pathname.startsWith("/api/v1/classroom-exports/")
        );
      })
      .map(item => `${item.method} ${new URL(item.url).pathname}`),
  ).toEqual([
    "POST /api/v1/classrooms/visual-classroom/draft/exports",
    "GET /api/v1/classroom-exports/visual-export",
  ]);

  expectCleanClassroomAudit(audit);
});

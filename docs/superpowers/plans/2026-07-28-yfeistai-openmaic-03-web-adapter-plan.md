# Tailwind 4 与 OpenMAIC 前端适配 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 完成 yFeiSTAI Web 全站 Tailwind 4 升级，并以单一 `openmaic-adapter` 接入 DSL、渲染、编辑、导入和课堂播放能力，不引入 iframe 应用壳或 OpenMAIC 全局 Store。

**Architecture:** 所有 OpenMAIC 包只由 `web/lib/openmaic-adapter` 引用。业务页面消费 yFeiSTAI 自有类型和组件。Renderer 负责幻灯片与效果，`EditableSlideCanvas` 只发出编辑意图，yFeiSTAI 持有文档、选择、撤销和保存；播放器宿主解释 OpenMAIC DSL Action，并把学习行为转换成 yFeiSTAI 事件。

**Tech Stack:** Next.js 16.2.3、React 19、TypeScript 5、Tailwind CSS 4.2.1、`@openmaic/dsl` 0.4.0、`@openmaic/renderer` 0.0.3、`@openmaic/importer` 0.1.0、motion 12.27.5、ECharts 6.0.0、Shiki 3.21.0、Node test、Playwright

---

## Task 1: 冻结 Tailwind 3 视觉和行为基线

**Files:**

- Create: `web/tests/e2e/tailwind-migration-baseline.spec.ts`
- Create: `web/tests/e2e/tailwind-migration-baseline.spec.ts-snapshots/`
- Modify: `web/playwright.config.ts`

- [ ] Step 1: 增加稳定的页面矩阵

测试矩阵至少覆盖：

```typescript
const routes = [
  "/login",
  "/home",
  "/knowledge",
  "/settings/appearance",
  "/settings/llm",
  "/space/learning",
] as const;

const viewports = [
  { name: "desktop", width: 1440, height: 900 },
  { name: "mobile", width: 390, height: 844 },
] as const;

const themes = ["snow", "cream", "dark", "glass"] as const;
```

测试固定动画、时间、网络响应和字体加载，等待 `document.fonts.ready` 后截图。

- [ ] Step 2: 在 Tailwind 3 上生成快照

Run:

```powershell
npm --prefix web exec playwright -- test tests/e2e/tailwind-migration-baseline.spec.ts --update-snapshots
```

Expected: 每个路由、视口和主题都有基线快照。

- [ ] Step 3: 运行现有前端基线

Run:

```powershell
npm --prefix web run test:node
npm --prefix web run i18n:check
npm --prefix web run build
```

Expected: PASS，或将既有失败单独记录。

- [ ] Step 4: 提交快照基线

```powershell
git add web/tests/e2e/tailwind-migration-baseline.spec.ts web/tests/e2e/tailwind-migration-baseline.spec.ts-snapshots web/playwright.config.ts
git commit -m "test(web): freeze Tailwind migration baseline"
```

## Task 2: 升级 Tailwind 4 构建链

**Files:**

- Modify: `web/package.json`
- Modify: `web/package-lock.json`
- Modify: `web/postcss.config.js`
- Modify: `web/tailwind.config.js`
- Modify: `web/app/globals.css`
- Create: `web/tests/tailwind4-contract.test.ts`

- [ ] Step 1: 写构建配置失败测试

```typescript
test("Tailwind 4 and the OpenMAIC renderer source are configured", () => {
  const packageJson = JSON.parse(readFileSync("package.json", "utf8"));
  const css = readFileSync("app/globals.css", "utf8");
  const postcss = readFileSync("postcss.config.js", "utf8");

  assert.equal(packageJson.devDependencies.tailwindcss, "4.2.1");
  assert.equal(packageJson.devDependencies["@tailwindcss/postcss"], "4.2.1");
  assert.match(css, /@import "tailwindcss"/);
  assert.match(css, /@source ".+@openmaic\/renderer\/dist"/);
  assert.match(postcss, /@tailwindcss\/postcss/);
});
```

- [ ] Step 2: 运行并确认失败

Run:

```powershell
npm --prefix web run test:node
```

Expected: FAIL，当前仍为 Tailwind 3。

- [ ] Step 3: 精确安装依赖

Run:

```powershell
npm --prefix web install --save-dev --save-exact tailwindcss@4.2.1 @tailwindcss/postcss@4.2.1
npm --prefix web uninstall autoprefixer
```

Expected: `package-lock.json` 精确锁定 Tailwind 4.2.1。

- [ ] Step 4: 迁移 PostCSS 和 CSS 入口

`postcss.config.js`：

```javascript
module.exports = {
  plugins: {
    "@tailwindcss/postcss": {},
  },
};
```

`globals.css` 顶部：

```css
@import "tailwindcss";
@config "../tailwind.config.js";
@source "../node_modules/@openmaic/renderer/dist/**/*.{js,mjs}";
```

保留现有主题变量和 `@layer` 定义。Tailwind 4 中失效的工具类逐项修复，不批量重写无关组件。

- [ ] Step 5: 运行构建和视觉差异

Run:

```powershell
npm --prefix web run test:node
npm --prefix web run build
npm --prefix web exec playwright -- test tests/e2e/tailwind-migration-baseline.spec.ts
```

Expected: 构建和测试通过；只有经过人工确认的 Tailwind 4 渲染差异更新快照。

- [ ] Step 6: 提交

```powershell
git add web/package.json web/package-lock.json web/postcss.config.js web/tailwind.config.js web/app/globals.css web/tests/tailwind4-contract.test.ts web/tests/e2e/tailwind-migration-baseline.spec.ts-snapshots
git commit -m "feat(web): migrate the site to Tailwind 4"
```

## Task 3: 安装 OpenMAIC 包并建立唯一适配入口

**Files:**

- Modify: `web/package.json`
- Modify: `web/package-lock.json`
- Modify: `THIRD_PARTY_NOTICES.md`
- Create: `web/lib/openmaic-adapter/contracts.ts`
- Create: `web/lib/openmaic-adapter/dsl.ts`
- Create: `web/lib/openmaic-adapter/index.ts`
- Create: `web/lib/openmaic-adapter/styles.css`
- Create: `web/tests/openmaic-adapter-contract.test.ts`

- [ ] Step 1: 写依赖和入口约束失败测试

```typescript
test("OpenMAIC packages are exact and only imported by the adapter", () => {
  const packageJson = JSON.parse(readFileSync("package.json", "utf8"));
  assert.equal(packageJson.dependencies["@openmaic/dsl"], "0.4.0");
  assert.equal(packageJson.dependencies["@openmaic/renderer"], "0.0.3");
  assert.equal(packageJson.dependencies["@openmaic/importer"], "0.1.0");

  const offenders = findImportsOutside(
    "lib/openmaic-adapter",
    /@openmaic\/(dsl|renderer|importer)/,
  );
  assert.deepEqual(offenders, []);
});
```

- [ ] Step 2: 运行并确认失败

Run:

```powershell
npm --prefix web run test:node
```

Expected: FAIL。

- [ ] Step 3: 安装精确版本

Run:

```powershell
npm --prefix web install --save-exact @openmaic/dsl@0.4.0 @openmaic/renderer@0.0.3 @openmaic/importer@0.1.0 motion@12.27.5 echarts@6.0.0 shiki@3.21.0
```

- [ ] Step 4: 实现 DSL 边界

```typescript
export function readClassroomDocument(input: unknown): ClassroomDocument {
  const parsed = parseYFeClassroomDocument(input);
  const migrated = migrateOpenMaicDocument(parsed.openmaic);
  const report = validateOpenMaicDocument(migrated);
  if (!report.valid) throw new ClassroomCompatibilityError(report.errors);
  return { ...parsed, openmaic: migrated };
}
```

适配器集中处理 DSL 迁移、场景联合类型、媒体 URL、主题映射和上游不兼容错误。业务组件不得直接引用 OpenMAIC 类型。

- [ ] Step 5: 禁止外部字体请求

不得引入 `@openmaic/renderer/fonts.css`，因为该文件引用外部字体域名。`styles.css` 把课堂范围内的字体族映射到 yFeiSTAI 已打包的 `--font-sans`、`--font-serif` 和系统中文字体：

```css
.openmaic-surface {
  --openmaic-font-sans: var(--font-sans), "PingFang SC", "Microsoft YaHei", sans-serif;
  --openmaic-font-serif: var(--font-serif), "Songti SC", serif;
}
```

更新 `THIRD_PARTY_NOTICES.md`，记录三个 OpenMAIC 包的 MIT 许可证及实际打包字体的许可证。

- [ ] Step 6: 运行测试和网络检查

Run:

```powershell
npm --prefix web run test:node
npm --prefix web run build
```

Expected: PASS；产物和 CSS 中不包含 `file.maic.chat`。

- [ ] Step 7: 提交

```powershell
git add web/package.json web/package-lock.json web/lib/openmaic-adapter/contracts.ts web/lib/openmaic-adapter/dsl.ts web/lib/openmaic-adapter/index.ts web/lib/openmaic-adapter/styles.css web/tests/openmaic-adapter-contract.test.ts THIRD_PARTY_NOTICES.md
git commit -m "feat(web): add the OpenMAIC adapter boundary"
```

## Task 4: 实现受控编辑器、撤销和草稿保存

**Files:**

- Create: `web/lib/openmaic-adapter/edit-intents.ts`
- Create: `web/lib/openmaic-adapter/editor-history.ts`
- Create: `web/lib/openmaic-adapter/scene-operations.ts`
- Create: `web/components/classroom/ClassroomEditor.tsx`
- Create: `web/components/classroom/ClassroomEditorToolbar.tsx`
- Create: `web/components/classroom/SceneNavigator.tsx`
- Create: `web/components/classroom/ScenePropertiesPanel.tsx`
- Create: `web/components/classroom/QuizEditor.tsx`
- Create: `web/components/classroom/InteractiveEditor.tsx`
- Create: `web/components/classroom/PblEditor.tsx`
- Create: `web/tests/openmaic-edit-intents.test.ts`
- Create: `web/tests/openmaic-editor-history.test.ts`
- Create: `web/tests/openmaic-scene-operations.test.ts`

- [ ] Step 1: 写编辑意图和撤销失败测试

```typescript
test("one gesture creates one undo entry", () => {
  const initial = classroomWithOneTextElement();
  const next = applyEditIntents(initial, [
    { type: "element.update", id: "title", props: { left: 120, top: 40 } },
  ]);
  const history = pushHistory(createHistory(initial), next);

  assert.deepEqual(undo(history).present, initial);
  assert.deepEqual(redo(undo(history)).present, next);
});

test("unknown element ids fail without mutating the document", () => {
  const initial = classroomWithOneTextElement();
  assert.throws(() =>
    applyEditIntents(initial, [
      { type: "element.delete", ids: ["missing"] },
    ]),
  );
  assert.deepEqual(initial, classroomWithOneTextElement());
});

test("scene reorder and quiz edits stay inside the draft aggregate", () => {
  const initial = classroomWithSlideAndQuiz();
  const next = applySceneOperations(initial, [
    { type: "scene.reorder", sceneId: "quiz-1", toIndex: 0 },
    {
      type: "quiz.update",
      sceneId: "quiz-1",
      question: "Updated?",
      options: ["Yes", "No"],
      correctOption: 0,
    },
  ]);
  assert.equal(next.scenes[0].id, "quiz-1");
  assert.equal(initial.scenes[1].content.question, "Original?");
});
```

- [ ] Step 2: 运行并确认失败

Run:

```powershell
npm --prefix web run test:node
```

Expected: FAIL。

- [ ] Step 3: 实现纯 Reducer

完整处理 `EditableSlideCanvas` 的公开 `EditIntent` 联合类型：

```text
element.update
element.updateMany
element.add
element.delete
element.reorder
element.align
element.removeProps
text.updateContent
```

每批意图做不可变更新、元素 ID 校验、边界校验和一次撤销记录。保存时把 yFeiSTAI 草稿修订号作为 `If-Match` 发送，冲突返回可比较的服务端版本，不静默覆盖。

- [ ] Step 4: 实现场景级编辑

`scene-operations.ts` 负责场景新增、复制、删除、重排和类型保持；Quiz 编辑题干、选项、正确答案、解析及知识点；Interactive 编辑经过安全校验的 HTML/配置；PBL 编辑情境、角色、里程碑和评分量规。任何操作都进入同一课堂文档历史，不允许组件各自维护不可回放的私有状态。互动 HTML 在保存前执行与发布校验相同的脚本、外链和 iframe 策略。

- [ ] Step 5: 实现编辑器宿主

`ClassroomEditor` 动态导入：

```typescript
const EditableSlideCanvas = dynamic(
  () =>
    import("@/lib/openmaic-adapter").then(
      (module) => module.EditableClassroomCanvas,
    ),
  { ssr: false },
);
```

适配器内部才引用 `@openmaic/renderer/editing`。yFeiSTAI 持有 selection、history、scene、draft revision 和保存状态；`SceneNavigator` 与场景属性面板覆盖 Slide、Quiz、Interactive 和 PBL，不把“可编辑”缩减为仅编辑幻灯片元素。

- [ ] Step 6: 运行测试和构建

Run:

```powershell
npm --prefix web run test:node
npm --prefix web run build
```

Expected: PASS。

- [ ] Step 7: 提交

```powershell
git add web/lib/openmaic-adapter/edit-intents.ts web/lib/openmaic-adapter/editor-history.ts web/lib/openmaic-adapter/scene-operations.ts web/components/classroom/ClassroomEditor.tsx web/components/classroom/ClassroomEditorToolbar.tsx web/components/classroom/SceneNavigator.tsx web/components/classroom/ScenePropertiesPanel.tsx web/components/classroom/QuizEditor.tsx web/components/classroom/InteractiveEditor.tsx web/components/classroom/PblEditor.tsx web/tests/openmaic-edit-intents.test.ts web/tests/openmaic-editor-history.test.ts web/tests/openmaic-scene-operations.test.ts
git commit -m "feat(web): host OpenMAIC classroom editing"
```

## Task 5: 实现课堂播放宿主和场景渲染器

**Files:**

- Create: `web/lib/openmaic-adapter/playback/types.ts`
- Create: `web/lib/openmaic-adapter/playback/controller.ts`
- Create: `web/lib/openmaic-adapter/playback/action-reducer.ts`
- Create: `web/lib/openmaic-adapter/playback/events.ts`
- Create: `web/components/classroom/ClassroomPlayer.tsx`
- Create: `web/components/classroom/QuizScene.tsx`
- Create: `web/components/classroom/InteractiveScene.tsx`
- Create: `web/components/classroom/PblScene.tsx`
- Create: `web/components/classroom/WhiteboardLayer.tsx`
- Create: `web/tests/openmaic-playback.test.ts`
- Create: `web/tests/openmaic-scene-renderers.test.ts`

- [ ] Step 1: 写动作顺序、断点和完成事件失败测试

```typescript
test("playback resumes from the persisted cursor without replaying quiz grading", async () => {
  const ports = fakePlaybackPorts();
  const controller = createPlaybackController(documentFixture(), ports);
  controller.restore({ sceneIndex: 1, actionIndex: 2, consumed: ["quiz-1"] });

  await controller.start();

  assert.equal(ports.firstExecutedAction.id, "scene-2-action-3");
  assert.equal(ports.events.filter((e) => e.type === "quiz.graded").length, 0);
});

test("completion is emitted once", async () => {
  const ports = fakePlaybackPorts();
  const controller = createPlaybackController(shortDocument(), ports);
  await controller.start();
  await controller.start();
  assert.equal(ports.events.filter((e) => e.type === "classroom.completed").length, 1);
});
```

- [ ] Step 2: 运行并确认失败

Run:

```powershell
npm --prefix web run test:node
```

Expected: FAIL。

- [ ] Step 3: 实现无 UI 的播放控制器

控制器解释 `@openmaic/dsl` Action 的可移植形状，通过 Ports 调用 UI 和媒体：

```typescript
export interface PlaybackPorts {
  renderScene(sceneId: string): void;
  speak(action: SpeechAction, signal: AbortSignal): Promise<void>;
  playVideo(action: PlayVideoAction, signal: AbortSignal): Promise<void>;
  applyWhiteboard(action: WhiteboardAction): Promise<void>;
  applyEffect(action: SpotlightAction | LaserAction): void;
  openDiscussion(action: DiscussionAction): Promise<void>;
  postWidgetAction(action: WidgetAction): Promise<void>;
  persistCursor(cursor: PlaybackCursor): Promise<void>;
  emit(event: ClassroomLearningEvent): Promise<void>;
}
```

支持开始、暂停、恢复、停止、场景切换、动作游标和 consumed 集合；浏览器状态只作为断点缓存，服务端会话才是正式状态。

- [ ] Step 4: 实现场景组件

- Slide：使用 `SlideCanvas` 和 Renderer effects。
- Quiz：使用 DSL `QuizContent`，答案提交后等待服务端评分结果。
- Interactive：使用 `sandbox="allow-scripts"` 且不含 `allow-same-origin` 的内容 iframe；消息桥只接受带会话 nonce 的白名单事件。
- PBL：使用可移植 `scenario + roles + milestones + rubric`，不依赖 OpenMAIC 应用路由或 Store。
- Whiteboard：由 Action Reducer 生成当前白板 Slide，再交给 `SlideCanvas`。

- [ ] Step 5: 运行测试和构建

Run:

```powershell
npm --prefix web run test:node
npm --prefix web run build
```

Expected: PASS。

- [ ] Step 6: 提交

```powershell
git add web/lib/openmaic-adapter/playback/types.ts web/lib/openmaic-adapter/playback/controller.ts web/lib/openmaic-adapter/playback/action-reducer.ts web/lib/openmaic-adapter/playback/events.ts web/components/classroom/ClassroomPlayer.tsx web/components/classroom/QuizScene.tsx web/components/classroom/InteractiveScene.tsx web/components/classroom/PblScene.tsx web/components/classroom/WhiteboardLayer.tsx web/tests/openmaic-playback.test.ts web/tests/openmaic-scene-renderers.test.ts
git commit -m "feat(web): play portable OpenMAIC classrooms"
```

## Task 6: 实现浏览器导入、媒体和导出边界

**Files:**

- Create: `web/lib/openmaic-adapter/importer.ts`
- Create: `web/lib/classroom-api.ts`
- Create: `web/components/classroom/ImportClassroomDialog.tsx`
- Create: `web/components/classroom/ClassroomExportMenu.tsx`
- Create: `web/tests/openmaic-importer.test.ts`
- Create: `web/tests/classroom-api.test.ts`

- [ ] Step 1: 写客户端导入和服务端媒体失败测试

```typescript
test("PPTX importer is loaded only after a browser action", async () => {
  const load = createImporterLoader();
  assert.equal(load.loaded, false);
  await load.importPptx(new ArrayBuffer(8), fakeUpload);
  assert.equal(load.loaded, true);
});

test("media URLs are always yFeiSTAI routes", () => {
  const url = classroomMediaUrl("version-1", "asset-2");
  assert.equal(url, "/api/v1/classrooms/versions/version-1/media/asset-2");
  assert.equal(url.includes("openmaic"), false);
});
```

- [ ] Step 2: 运行并确认失败

Run:

```powershell
npm --prefix web run test:node
```

Expected: FAIL。

- [ ] Step 3: 实现动态 Importer

```typescript
export async function importPptxInBrowser(
  input: ArrayBuffer,
  upload: UploadImportedMedia,
): Promise<ImportedSlides> {
  if (typeof window === "undefined") {
    throw new Error("PPTX import is browser-only");
  }
  const { importPptx } = await import("@openmaic/importer");
  return { slides: await importPptx(input, { upload }) };
}
```

上传回调只调用 `POST /api/v1/classrooms/{asset_id}/draft-media`；客户端不能构造对象存储键。返回值只能是媒体 ID、受控读取 URL、MIME、大小和 SHA-256。

- [ ] Step 4: 实现导出菜单

菜单精确提供 `classroom_zip`、`pptx`、`offline_html` 和按租户策略开启的 `mp4`。它调用：

```text
POST /api/v1/classrooms/{asset_id}/draft/exports
POST /api/v1/classroom-versions/{version_id}/exports
GET  /api/v1/classroom-exports/{export_id}
GET  /api/v1/classroom-exports/{export_id}/download
```

导出任务由 yFeiSTAI API 创建并轮询；下载地址也是 yFeiSTAI 的短期受控路由。前端不直连 OpenMAIC 导出 API，不在浏览器伪造“已完成”状态；MP4 服务未启用时展示稳定的策略原因。

- [ ] Step 5: 运行测试和构建

Run:

```powershell
npm --prefix web run test:node
npm --prefix web run build
```

Expected: PASS，Importer 不进入服务端 chunk。

- [ ] Step 6: 提交

```powershell
git add web/lib/openmaic-adapter/importer.ts web/lib/classroom-api.ts web/components/classroom/ImportClassroomDialog.tsx web/components/classroom/ClassroomExportMenu.tsx web/tests/openmaic-importer.test.ts web/tests/classroom-api.test.ts
git commit -m "feat(web): isolate classroom import and export transport"
```

## Task 7: 扩展全站视觉回归并完成迁移门禁

**Files:**

- Create: `web/tests/e2e/classroom-editor.visual.spec.ts`
- Create: `web/tests/e2e/classroom-player.visual.spec.ts`
- Modify: `web/tests/e2e/tailwind-migration-baseline.spec.ts`
- Modify: `web/tests/e2e/tailwind-migration-baseline.spec.ts-snapshots/`

- [ ] Step 1: 增加课堂矩阵

课堂编辑器和播放器覆盖：

```text
desktop + mobile
zh + en
snow + cream + dark + glass
slide + quiz + interactive + pbl
normal motion + reduced motion
```

- [ ] Step 2: 执行前端完整门禁

Run:

```powershell
npm --prefix web run test:node
npm --prefix web run i18n:check
npm --prefix web run lint
npm --prefix web run build
npm --prefix web exec playwright -- test tests/e2e/tailwind-migration-baseline.spec.ts tests/e2e/classroom-editor.visual.spec.ts tests/e2e/classroom-player.visual.spec.ts
```

Expected: PASS；所有快照差异均经过人工核对。

- [ ] Step 3: 检查包边界和外网资源

Run:

```powershell
rg -n "@openmaic/" web --glob "!lib/openmaic-adapter/**" --glob "!package*.json"
rg -n "file\\.maic\\.chat|fonts\\.googleapis\\.com" web/.next
```

Expected: 第一条只出现测试允许列表中的契约检查；第二条无课堂运行时外链。

- [ ] Step 4: 提交

```powershell
git add web/tests/e2e/classroom-editor.visual.spec.ts web/tests/e2e/classroom-player.visual.spec.ts web/tests/e2e/tailwind-migration-baseline.spec.ts web/tests/e2e/tailwind-migration-baseline.spec.ts-snapshots
git commit -m "test(web): gate Tailwind 4 classroom visuals"
```

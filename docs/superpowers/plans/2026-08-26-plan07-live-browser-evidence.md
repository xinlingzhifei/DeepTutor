# Plan07 Live Browser Evidence Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the fixed Plan07 browser probe executable against one deployed immutable candidate by provisioning isolated teacher, student, reviewer, and publisher identities from one platform-admin token through existing formal APIs.

**Architecture:** Keep the Python probe as the fail-closed trust boundary, add a dedicated `first-release-live` Playwright project, and implement a TypeScript fixture client plus one real live spec. The mocked browser suite remains separate. Local tests prove wiring and safety only; browser evidence is authoritative only after a new immutable candidate is published and exercised.

**Tech Stack:** Python 3 / pytest / Ruff, TypeScript, Node's built-in test runner, Playwright, existing yFeiSTAI REST APIs.

---

## Task 1: Fail Closed on the Exact Live Environment

**Files:**

- Modify: `scripts/classroom_release_probe.py`
- Modify: `scripts/classroom_release_probe_contract.py`
- Modify: `tests/scripts/test_classroom_release_probe.py`

- [x] **Step 1: Add focused failing tests**

Add tests proving that the probe:

- rejects a missing or blank `YFEISTAI_LIVE_FIXTURE_TOKEN` before runtime resolution or Node startup;
- forwards only `YFEISTAI_LIVE_FIXTURE_TOKEN` from the live-secret environment;
- does not forward wildcard `YFEISTAI_LIVE_*` values;
- records live environment policy version `2` in its request/receipt contract.

Run:

```powershell
python -m pytest tests/scripts/test_classroom_release_probe.py -q -k "fixture_token or live_environment"
```

Expected: RED only on the missing exact-token allow-list and validation behavior.

- [x] **Step 2: Implement the minimum trust-boundary change**

Define the exact allow-list:

```python
_LIVE_SECRET_ENVIRONMENT = ("YFEISTAI_LIVE_FIXTURE_TOKEN",)
```

Validate the token before candidate/runtime resolution, pass only that key into the Playwright process environment, and bump the live environment policy to version `2`. Do not log, serialize, hash into a receipt, or place the token in argv.

- [x] **Step 3: Verify focused and full Python contracts**

Run serially:

```powershell
python -m pytest tests/scripts/test_classroom_release_probe.py -q
python -m pytest tests/scripts/test_classroom_release_evidence.py tests/scripts/test_verify_classroom_release.py -q
python -m ruff check scripts/classroom_release_probe.py scripts/classroom_release_probe_contract.py tests/scripts/test_classroom_release_probe.py
python -m ruff format --check scripts/classroom_release_probe.py scripts/classroom_release_probe_contract.py tests/scripts/test_classroom_release_probe.py
git diff --check
```

Expected: all selected tests and static checks pass; no secret values appear in output.

- [x] **Step 4: Commit**

```powershell
git add scripts/classroom_release_probe.py scripts/classroom_release_probe_contract.py tests/scripts/test_classroom_release_probe.py
git commit -m "fix(release): isolate live browser credentials"
```

## Task 2: Declare a Live-Only Playwright Project

**Files:**

- Modify: `web/playwright.config.ts`
- Create: `web/playwright.live-policy.ts`
- Create: `web/tests/classroom-release-live-policy.test.ts`
- Modify: `tests/scripts/test_classroom_release_probe.py`

- [x] **Step 1: Add source and runtime contract RED tests**

Add Python source-contract assertions proving that `web/playwright.config.ts`:

- declares exactly one `first-release-live` project for `classroom-first-release.live.spec.ts`;
- conditionally excludes that spec from default all-project runs;
- never starts or falls back to a managed localhost server in live mode;
- fixes `workers: 1`, `retries: 0`, `fullyParallel: false`, Desktop Chromium, `en-US`, UTC, and `trace`/`screenshot`/`video` off.

Add runtime Node tests for a Playwright-independent policy module proving that it:

- selects live mode for both `--project=first-release-live` and `--project first-release-live`, or for one of the five fixed evidence names;
- ignores project-like values after the `--` option terminator;
- requires an absolute HTTP(S) `WEB_BASE_URL` with a hostname;
- rejects `localhost`, `*.localhost`, IPv4 `127/8`, and IPv6 `::1` loopback targets without echoing the input URL.

Run:

```powershell
python -m pytest tests/scripts/test_classroom_release_probe.py -q -k "live_project or live_base_url"
node web/node_modules/typescript/bin/tsc -p web/tsconfig.node-tests.json
Push-Location web
node --import ./scripts/register-node-test-esm.mjs --test dist/node-tests/tests/classroom-release-live-policy.test.js
Pop-Location
```

Expected: RED because the live project and its isolated runtime policy do not exist.

- [x] **Step 2: Implement live project selection and validation**

Implement the selection and URL rules in the pure policy module, then let the Playwright config consume those decisions. Preserve the existing mocked projects and managed server behavior when live mode is not selected, and keep the live project's `testMatch` empty unless live mode was explicitly selected.

- [x] **Step 3: Verify the config without opening a browser**

Run:

```powershell
python -m pytest tests/scripts/test_classroom_release_probe.py -q
node web/node_modules/typescript/bin/tsc -p web/tsconfig.node-tests.json
Push-Location web
node --import ./scripts/register-node-test-esm.mjs --test dist/node-tests/tests/classroom-release-live-policy.test.js
Pop-Location
node web/node_modules/typescript/bin/tsc --noEmit -p web/tsconfig.json
git diff --check
```

The live spec is intentionally absent at this stage, so do not run Playwright collection, a browser, or a local server. All commands must use the existing local dependencies without invoking a package-manager install path.

- [x] **Step 4: Commit**

```powershell
git add web/playwright.config.ts web/playwright.live-policy.ts web/tests/classroom-release-live-policy.test.ts tests/scripts/test_classroom_release_probe.py
git commit -m "test(release): declare live browser project"
```

## Task 3: Provision Deterministic Identities Through Formal APIs

**Files:**

- Create: `web/tests/e2e/support/classroom-first-release-live-fixture.ts`
- Create: `web/tests/classroom-first-release-live-fixture.test.ts`

- [x] **Step 1: Write Node unit RED tests around a fake API context**

Cover these behaviors without network or browser access:

- HMAC-SHA-256 identities are stable for the same policy/release/environment/evidence/role and different across roles;
- usernames end in `@example.invalid`, passwords are strong, and no secret value appears in names, errors, or serialized state;
- user creation `409` is accepted only after successful login with the derived password;
- a `409` followed by `401` fails closed;
- tenant creation supplies a deterministic `Idempotency-Key`, waits for `active`, switches the admin context, and grants the requested scoped role;
- minimum course, class, controlled source, and enrollment calls are idempotently shaped;
- a retained course/class/source/enrollment conflict is accepted only after a formal read proves its deterministic key, tenant/parent ownership, type, and recipe-critical fields;
- a mismatched conflict fails closed instead of selecting a list result or adopting an object by display name.

Run:

```powershell
pnpm --dir web exec tsc -p tsconfig.node-tests.json
Push-Location web
node --import ./scripts/register-node-test-esm.mjs --test dist/node-tests/tests/classroom-first-release-live-fixture.test.js
Pop-Location
```

Expected: RED because the helper does not exist.

- [x] **Step 2: Implement the smallest fixture client**

Export explicit types such as `LiveEvidence`, `LiveRole`, `LiveFixtureContext`, and `LiveIdentity`. Accept an injected Playwright-compatible API request context so tests can provide a fake. Implement only the existing formal endpoints needed to:

1. derive per-run user credentials in memory;
2. create the user or prove ownership after `409`;
3. create/provision the tenant with an idempotency key;
4. wait for `active`, switch the platform-admin request context, and grant tenant roles;
5. create the minimum course/class/source catalog and enrollment, or read and verify every deterministic identity, tenant/parent binding, kind, and critical field after an expected conflict;
6. log a provisioned user into the real `/login` page without exposing credentials.

Do not add production fixture routes, direct database/object-store writes, cleanup calls, or a generic API abstraction.

- [x] **Step 3: Verify unit, type, and static boundaries**

Run:

```powershell
pnpm --dir web exec tsc -p tsconfig.node-tests.json
Push-Location web
node --import ./scripts/register-node-test-esm.mjs --test dist/node-tests/tests/classroom-first-release-live-fixture.test.js
Pop-Location
pnpm --dir web exec tsc --noEmit
git diff --check
```

Expected: the focused Node tests and typecheck pass; no real HTTP requests occur.

- [x] **Step 4: Commit**

```powershell
git add web/tests/e2e/support/classroom-first-release-live-fixture.ts web/tests/classroom-first-release-live-fixture.test.ts
git commit -m "test(release): provision isolated live identities"
```

## Task 4: Implement Real Teacher and Content-Operations Recipes

**Files:**

- Create: `web/tests/e2e/support/classroom-first-release-live-flows.ts`
- Create: `web/tests/e2e/classroom-first-release.live.spec.ts`
- Modify: `tests/scripts/test_classroom_release_probe.py`

- [x] **Step 1: Add live-source boundary RED tests**

Assert across the fixed live spec, fixture helper, and flow helper that:

- the spec imports directly from `@playwright/test`;
- does not import `teaching-flow-test`, baseline fixtures, or mocked flow helpers;
- uses an explicit import allow-list for the two live helpers and production-safe libraries;
- contains no `page.route`, `browserContext.route`, `route.fulfill`, screenshot, trace, video, attachment, or secret serialization path in any of the three live files;
- declares exactly one teacher marker and one content-operations marker.

Run:

```powershell
python -m pytest tests/scripts/test_classroom_release_probe.py -q -k "live_spec"
```

Expected: RED because the fixed live spec is absent.

- [x] **Step 2: Implement the teacher recipe**

Use formal API setup only for tenant/identity/catalog prerequisites. Then use the real UI to sign in, select the provisioned tenant, create a teaching brief at `/teaching/classrooms/new`, wait for the real outline, confirm it, wait for generated draft content, validate it, and submit for review. The terminal assertion is a real pending-review record. Emit exactly `[release-evidence:teacher_flow]` in the test title.

- [x] **Step 3: Implement the content-operations recipe**

Provision distinct author, reviewer, and publisher identities in one tenant. Drive creation and submission as the author, prove that the author cannot see approve/reject actions, approve as the separate reviewer, and publish as the authorized publisher. Emit exactly `[release-evidence:content_operations_flow]`.

- [x] **Step 4: Verify static collection only**

Run:

```powershell
python -m pytest tests/scripts/test_classroom_release_probe.py -q
pnpm --dir web exec tsc -p tsconfig.node-tests.json
Push-Location web
node --import ./scripts/register-node-test-esm.mjs --test dist/node-tests/tests/classroom-first-release-live-fixture.test.js
Pop-Location
pnpm --dir web exec playwright test --project=first-release-live --list
pnpm --dir web exec tsc --noEmit
git diff --check
```

Use synthetic non-secret inputs for collection. Do not run the browser recipes against localhost or a candidate yet.

- [x] **Step 5: Commit**

```powershell
git add web/tests/e2e/support/classroom-first-release-live-flows.ts web/tests/e2e/classroom-first-release.live.spec.ts tests/scripts/test_classroom_release_probe.py
git commit -m "test(release): cover live teacher operations"
```

## Task 5: Implement Real Student Micro and Full Recipes

**Files:**

- Modify: `web/tests/e2e/support/classroom-first-release-live-flows.ts`
- Modify: `web/tests/e2e/classroom-first-release.live.spec.ts`
- Modify: `web/tests/classroom-first-release-live-fixture.test.ts`

- [x] **Step 1: Add focused unit RED tests**

Use fake API/browser collaborators to prove that the bounded poller:

- treats `awaiting_confirmation` as a required full-flow state;
- never pre-seeds an immutable version;
- times out with a redacted diagnostic;
- returns only after the expected real job/version state.

Run:

```powershell
pnpm --dir web exec tsc -p tsconfig.node-tests.json
Push-Location web
node --import ./scripts/register-node-test-esm.mjs --test dist/node-tests/tests/classroom-first-release-live-fixture.test.js
Pop-Location
```

Expected: RED on missing student-flow behavior.

- [x] **Step 2: Implement the micro flow**

Provision and enroll a distinct student, sign in through the real UI, submit a micro-classroom request, wait for the actual generation job and immutable version, open `/learn/classrooms/{versionId}`, and prove the classroom document renders. Emit exactly `[release-evidence:student_micro_flow]`.

- [x] **Step 3: Implement the full flow**

Provision and enroll another student, submit a full-classroom request, wait for `awaiting_confirmation`, edit and confirm the real outline, wait for generation, open the resulting immutable version, and prove the player renders. Emit exactly `[release-evidence:student_full_flow]`.

- [x] **Step 4: Verify unit, collection, and type boundaries**

Run:

```powershell
pnpm --dir web exec tsc -p tsconfig.node-tests.json
Push-Location web
node --import ./scripts/register-node-test-esm.mjs --test dist/node-tests/tests/classroom-first-release-live-fixture.test.js
Pop-Location
python -m pytest tests/scripts/test_classroom_release_probe.py -q
pnpm --dir web exec playwright test --project=first-release-live --list
pnpm --dir web exec tsc --noEmit
git diff --check
```

Expected: static/list output includes one micro and one full marker; no browser is launched.

- [x] **Step 5: Commit**

```powershell
git add web/tests/e2e/support/classroom-first-release-live-flows.ts web/tests/e2e/classroom-first-release.live.spec.ts web/tests/classroom-first-release-live-fixture.test.ts
git commit -m "test(release): cover live student classrooms"
```

## Task 6: Add the 48-Case Tailwind Matrix and Close Local Contracts

**Files:**

- Modify: `web/tests/e2e/classroom-first-release.live.spec.ts`
- Modify: `tests/scripts/test_classroom_release_probe.py`
- Modify: `docs/superpowers/plans/2026-07-28-yfeistai-openmaic-07-deployment-and-acceptance-plan.md`

- [x] **Step 1: Add exact matrix RED tests**

Pin the Cartesian product:

- routes: `/login`, `/home`, `/knowledge`, `/settings/appearance`, `/settings/llm`, `/space/learning`;
- viewports: `1440x900`, `390x844`;
- appearances: `snow`, `light`, `dark`, `glass`.

Require exactly 48 `[release-evidence:tailwind4_visual_matrix]` titles and reject request interception or screenshot APIs.

Run:

```powershell
python -m pytest tests/scripts/test_classroom_release_probe.py -q -k "tailwind or marker_count"
```

Expected: RED until all 48 cases are present.

- [x] **Step 2: Implement the structural matrix**

For each case, establish real login state where required, select the appearance through the supported UI/state seam, navigate to the real route, and assert:

- the expected primary landmark is visible;
- document width does not exceed viewport width;
- no uncaught page error occurs;
- no console error occurs.

Do not capture pixels or add visual snapshots. Keep each title unique while retaining the exact release marker.

- [x] **Step 3: Run the complete local harness verification**

Run each heavy command in its own globally guarded serial window:

```powershell
python -m pytest tests/scripts/test_classroom_release_probe.py tests/scripts/test_classroom_release_evidence.py tests/scripts/test_verify_classroom_release.py -q
pnpm --dir web test:node
pnpm --dir web exec tsc --noEmit
pnpm --dir web exec playwright test --project=first-release-live --list
python -m ruff check scripts/classroom_release_probe.py scripts/classroom_release_probe_contract.py tests/scripts/test_classroom_release_probe.py
python -m ruff format --check scripts/classroom_release_probe.py scripts/classroom_release_probe_contract.py tests/scripts/test_classroom_release_probe.py
git diff --check
```

Expected list result: exactly 52 tests in `first-release-live` — 1 teacher, 1 student micro, 1 student full, 1 content operations, and 48 Tailwind matrix items. No candidate browser execution occurs in this step.

- [x] **Step 4: Update the authoritative plan boundary**

Record that the live harness is statically executable but Plan07 Task7 remains in progress until all five recipes run against the same immutable candidate and their native JSON is accepted. Record that Task8 and the eleven non-Playwright receipt producers remain incomplete.

- [x] **Step 5: Commit**

```powershell
git add web/tests/e2e/classroom-first-release.live.spec.ts tests/scripts/test_classroom_release_probe.py docs/superpowers/plans/2026-07-28-yfeistai-openmaic-07-deployment-and-acceptance-plan.md
git commit -m "test(release): enumerate live visual evidence"
```

## Completion Boundary

Completing this implementation plan proves only that the live browser harness is fail-closed, statically collected, and wired to formal APIs. It does **not** complete Plan07 Task7 or Task8.

After Task 6, freeze the resulting HEAD, publish a new immutable release candidate, and then run the five fixed live recipes against that exact deployment. Separately implement and collect the eleven trusted non-Playwright receipt layers: database revisions, running containers, service health, capacity profile, classroom exports, tenant isolation, learning-event idempotency, OpenMAIC shared plane, OpenMAIC dedicated plane, backup/restore, and gateway-only-public. Only a fully green same-candidate ledger may proceed to the final local merge into `main`.

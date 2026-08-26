# Plan07 Live Browser Evidence Design

**Status:** Approved by the user on 2026-08-26 through option A.

## Goal

Make the fixed Plan07 browser evidence probe executable against one deployed,
immutable yFeiSTAI release candidate without adding a production test backdoor.
The harness must create isolated multi-role identities through existing admin and
tenant APIs, sign in through the real UI, use real application APIs, and emit only
native Playwright JSON that the existing release verifier can validate.

## Scope

This design closes the live Playwright execution seam only:

- add the fixed `classroom-first-release.live.spec.ts` entrypoint;
- add the fixed `first-release-live` Playwright project;
- require one platform-admin bearer token for fixture provisioning;
- provision deterministic per-run users and tenant bindings through existing APIs;
- implement the five fixed Playwright evidence recipes;
- keep the existing mocked first-release suite as a separate fast regression.

It does not manufacture the eleven non-Playwright receipts still required by
`verify_classroom_release.py`. Database, runtime, capacity, export inspection,
tenant isolation, learning-event idempotency, OpenMAIC shared/dedicated smoke,
backup/restore, and gateway receipts remain separate Plan07 work.

## Trust Boundary

The only new secret input is `YFEISTAI_LIVE_FIXTURE_TOKEN`, a bearer token for a
dedicated platform-admin account in the candidate environment. The fixed probe:

- requires the token and rejects an empty value before starting Node;
- passes only the exact allow-listed live secret, never arbitrary
  `YFEISTAI_LIVE_*` variables;
- never includes the token in argv, descriptors, native stdout, receipts, test
  titles, attachments, screenshots, videos, or traces;
- disables Playwright trace, screenshot, and video capture for the live project;
- keeps JSON reporter output as the only browser evidence payload.

The platform-admin token is used only by Playwright's API request context for
formal endpoints that already exist. No route, fixture controller, debug endpoint,
database shortcut, direct object-store write, or browser request interception is
introduced.

## Deterministic Fixture Identity

Every Playwright evidence recipe is a separate process, so setup cannot depend on
another recipe having run first. A live fixture key is derived from:

```text
policyVersion = 2
releaseRunId
environmentId
evidenceName
role
platformAdminToken
```

The helper uses HMAC-SHA-256 with the admin token as the key. The digest supplies:

- a non-secret 12-character account suffix;
- a deterministic strong password that is retained only in process memory;
- stable course, class, tenant-idempotency, and resource identifiers.

Usernames use the reserved `.invalid` domain and contain the sanitized release
run, evidence name, role, and suffix. If account creation returns 409, the helper
must prove ownership by logging in with the derived password; a failed login is a
hard conflict, not a reason to adopt an unrelated existing account.

Tenant creation uses an `Idempotency-Key` derived from the same fixture identity.
The helper waits for provisioning to reach `active`, switches the admin request
context into that tenant, assigns scoped roles through
`/api/v1/tenants/{tenant_id}/members`, and creates the minimum course/class/source
catalog required by the selected recipe. It intentionally does not delete users,
tenants, or business records after the run: those records are candidate evidence
and cleanup would make partial reruns and audit reconstruction unsafe.

Every retained course, class, source, and enrollment uses a deterministic key and
an explicit create-or-read-and-verify rule. A conflict is accepted only after the
formal read API proves the expected deterministic identity, tenant and parent
ownership, object kind, and recipe-critical fields. Enrollment conflicts must
also prove the expected user/class pair. The helper must fail closed on any
mismatch; it must never adopt the first list result, an object selected only by
display name, or an object from another tenant.

## Playwright Project Boundary

`first-release-live` is a dedicated project with exactly one test file. Selecting
that project or setting `YFEISTAI_EVIDENCE` makes live mode active. Live mode:

- requires an absolute HTTP(S) `WEB_BASE_URL` with a hostname;
- never falls back to `127.0.0.1`;
- never starts the managed local mocked server;
- runs one worker, fully serial, with zero retries;
- uses Desktop Chromium, `en-US`, and UTC;
- disables trace, screenshot, and video capture.

The fixed Python probe continues to force JSON reporter, one worker, and zero
retries. The duplicated project settings ensure an operator cannot weaken those
properties by invoking the project directly.

## Live Business Recipes

The spec imports directly from `@playwright/test`; it must not import the mocked
`teaching-flow-test`, baseline API fixtures, or existing route-based flow helpers.
It declares the exact markers already pinned by
`classroom_release_probe_contract.py`:

- one `teacher_flow` marker;
- one `student_micro_flow` marker;
- one `student_full_flow` marker;
- one `content_operations_flow` marker;
- forty-eight `tailwind4_visual_matrix` markers.

The source-boundary contract applies to the spec, live fixture helper, and live
flow helper together. Their imports are allow-listed, and all three are scanned
for browser/context request interception, `page.route()`, `route.fulfill()`,
mocked helper imports, screenshots, traces, videos, and attachment APIs.

Each marker is independently provisioned and uses real browser/UI and API state.

### Teacher Flow

Create a teacher account with tenant-scoped `teacher` permission, a course, a
class, and a controlled source. Sign in through `/login`, select the provisioned
tenant, create a teaching brief in `/teaching/classrooms/new`, wait for the real
outline, confirm it, wait for generated draft content, validate, and submit for
review. The final assertion is a real pending-review record; it does not claim
publication or export inspection.

### Student Micro Flow

Create and enroll a student, sign in through the real UI, submit a micro-classroom
request, wait for the real generation job and immutable version, then open
`/learn/classrooms/{versionId}` and prove the real classroom document renders.

### Student Full Flow

Create and enroll a separate student, submit a full-classroom request, wait for
`awaiting_confirmation`, edit and confirm the real outline, wait for generation,
then open the immutable version in the real player. The helper must not pre-seed a
version or bypass outline confirmation.

### Content Operations Flow

Create distinct author, reviewer, and publisher identities in one tenant. The
author creates and submits content; the same author must not see approve/reject
actions. A separate reviewer approves it, and a teacher or org-admin publishes the
approved version. All role changes use existing scoped tenant grants.

### Tailwind 4 Visual Matrix

Run the existing six approved routes across two viewports and four appearance
variants for 48 exact markers. Use the live environment and real login state, with
no `page.route()` or `route.fulfill()`. The automated evidence is structural:
expected landmark visible, no horizontal overflow, and no page or console errors.
Pixel screenshots remain outside the JSON evidence path because the live project
forbids secret-bearing attachments and cross-host font/rendering differences would
make them unreliable release receipts.

## Fail-Closed Rules

The live probe fails before Node or browser startup when any of these is invalid:

- missing/invalid `WEB_BASE_URL`;
- missing `YFEISTAI_LIVE_FIXTURE_TOKEN`;
- missing release run, environment, candidate root, report path, or evidence name;
- candidate/report paths outside the existing filesystem boundary;
- unrecognized live environment variable;
- missing live spec or project;
- fixture account collision that cannot authenticate with the derived password;
- tenant provisioning not active before the bounded deadline;
- unexpected HTTP status or malformed response;
- mocked request interception detected in the live spec/helper;
- Playwright skipped, retried, flaky, or non-native evidence.

## Evidence Boundary

A green local unit/static suite proves only that the live harness is present,
fail-closed, and wired to formal APIs. It does not prove browser acceptance. A
green live Playwright recipe proves only its named evidence layer on the exact
candidate/environment/release run bound by the surrounding attestation and
receipt pipeline. It does not prove the eleven non-Playwright layers or overall
release readiness.

Any implementation commit changes source HEAD. Therefore no earlier image,
schema-v2 lock, runtime attestation, or browser report may be reused after this
design is implemented; a new immutable candidate must be published afterward.

## Verification Strategy

Use four layers, serially:

1. Python tests for fixed spec/project presence, exact environment allow-list,
   token requirement, and local-server refusal.
2. Node unit tests for deterministic credential derivation, secret redaction,
   conflict ownership, tenant provisioning, and role/catalog request shapes using
   a fake `APIRequestContext` only.
3. Playwright `--list` for the live project with synthetic non-secret environment
   inputs to prove exact marker count and configuration without opening a browser.
4. After publishing a new candidate, run the five fixed recipes against the live
   URL with the real token and pass their native JSON through the existing evidence
   assembler and verifier.

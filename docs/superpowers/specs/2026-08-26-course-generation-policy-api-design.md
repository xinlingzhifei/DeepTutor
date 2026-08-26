# Course Generation Policy API Design

## Context

Plan07 Task5 needs deterministic student `micro` and `full` browser recipes. The live fixture can create a course, class, source, and enrollment through formal APIs, but the learner option query inner-joins `course_generation_policies`. No formal API currently creates or reads that policy, and the model defaults do not authorize `full` or guarantee `open_creation`.

The release harness must not write the database directly, depend on pre-seeded deployment state, or introduce a fixture-only production route.

## Decision

Add a dedicated tenant-scoped nested resource to the existing teaching catalog API:

- `GET /api/v1/teaching/courses/{course_id}/generation-policy`
- `PUT /api/v1/teaching/courses/{course_id}/generation-policy`

The endpoint extends the existing catalog repository and service because that stack already owns active-course lookup and student option bindings. No schema migration or second policy service is needed.

## Authorization

Both operations require the existing `policy.manage` permission against the requested course resource. A tenant-scoped `platform_admin` grant inherits to the course and therefore supports the single-token fixture design. The endpoint does not implicitly accept `tenant.manage`, and it does not widen the `org_admin` role template.

The active tenant comes only from `require_tenant`; request bodies cannot supply `tenantId`, `updatedBy`, or timestamps.

## Contract

`PUT` is a complete replacement with these camelCase fields:

- `allowStudentMicro`
- `allowStudentFull`
- `allowedContentModes`: a non-empty unique list containing only `source_grounded` and/or `open_creation`
- `allowWebSearch`
- `requireApprovalForRestrictedTopics`
- `minorSafetyMode`
- `microSceneLimit` from 1 through 5
- `fullSceneLimit` from 1 through 24
- `dailyStudentUnits`, non-negative
- `monthlyStudentUnits`, non-negative

The request uses `extra="forbid"`. The service constructs the existing `CourseGenerationPolicy` value object so API and persisted validation cannot diverge. Content modes persist in the canonical order `source_grounded,open_creation`.

The response includes the policy fields plus trusted `tenantId`, `courseId`, `updatedBy`, and `updatedAt`.

## Persistence and Idempotency

The repository first verifies an active course in the selected tenant schema, then creates or updates that course's single policy row inside one transaction. Repeating the same complete payload is logically idempotent; no `Idempotency-Key`, optimistic version, or schema change is introduced.

The policy row always records `tenant_id` from the selected context and `updated_by` from the authenticated user. An absent policy returns 404 on `GET`; `PUT` may create it.

## Errors

- authentication failure: 401 through the existing auth stack;
- missing `policy.manage`: 403 before resource lookup, preventing cross-tenant existence disclosure;
- missing/inactive same-tenant course: 404;
- missing policy on `GET`: 404;
- malformed or invalid policy: 422;
- stable repository failure: 503 without raw database text.

## Rejected Alternatives

Extending course creation with an optional policy conflates two resources and cannot repair a retained course. A fixture-only route or direct database seed would make release evidence non-production and is rejected. Broadly changing the Node test loader is unrelated to the business prerequisite and remains out of scope.

## Task5 Follow-up

After this API is green, the live fixture will use its single platform-admin token to `PUT` a deterministic policy with micro and full enabled, `open_creation` allowed, and positive quotas. It will then `GET` and verify every trusted field before either student browser context starts.

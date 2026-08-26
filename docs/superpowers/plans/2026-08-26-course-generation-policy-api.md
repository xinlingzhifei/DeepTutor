# Course Generation Policy API Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add the formal tenant-scoped policy resource required to provision deterministic student micro/full release evidence.

**Architecture:** Extend the existing teaching catalog repository, service, and router. Reuse `CourseGenerationPolicy` for validation and `CourseGenerationPolicyRecord` for persistence; authorize both operations with course-scoped `policy.manage`.

**Tech Stack:** FastAPI, Pydantic v2, SQLAlchemy async, pytest, SQLite integration fixtures.

---

### Task 1: Pin the HTTP and authorization contract

**Files:**

- Modify: `tests/api/test_teaching_catalog.py`

- [ ] **Step 1: Extend the fake catalog repository**

Add an immutable fake policy record carrying the complete policy plus `tenant_id`, `course_id`, `updated_by`, and `updated_at`. Add `get_course_generation_policy(course_id)` and `replace_course_generation_policy(course_id, policy, updated_by)` methods that preserve the existing fake course-not-found behavior.

- [ ] **Step 2: Add focused RED tests**

Add exact tests that prove:

```python
def test_platform_admin_replaces_and_reads_course_generation_policy(): ...
def test_course_generation_policy_requires_policy_manage(): ...
def test_course_generation_policy_rejects_untrusted_and_invalid_fields(): ...
def test_course_generation_policy_missing_resources_have_stable_statuses(): ...
```

The first test repeats the same `PUT` and compares all semantic response fields. The second checks that `org_admin`, teacher, and student receive 403. The third covers `tenantId` injection, duplicate/unknown content modes, invalid scene limits, and negative quotas. The fourth distinguishes missing active course and missing policy as stable 404 responses.

- [ ] **Step 3: Run the focused RED**

Run in one globally guarded window:

```powershell
python -m pytest tests/api/test_teaching_catalog.py -q -k "course_generation_policy"
```

Expected: the selected tests fail only because the GET/PUT routes and catalog policy methods are absent. Collection, fixtures, and environment must succeed.

### Task 2: Implement the minimal catalog policy resource

**Files:**

- Modify: `deeptutor/teaching/repositories/catalog.py`
- Modify: `deeptutor/teaching/services/catalog.py`
- Modify: `deeptutor/api/routers/teaching_catalog.py`
- Test: `tests/api/test_teaching_catalog.py`

- [ ] **Step 1: Add repository records and operations**

Define a frozen `CourseGenerationPolicyView` and add:

```python
async def get_course_generation_policy(self, course_id: str) -> CourseGenerationPolicyView: ...
async def replace_course_generation_policy(
    self,
    course_id: str,
    policy: CourseGenerationPolicy,
    updated_by: str,
) -> CourseGenerationPolicyView: ...
```

Both methods bind to the selected tenant schema and tenant ID. `GET` requires an active course and an existing policy. `PUT` locks/verifies the active course, creates or updates the single policy row, persists canonical content modes, trusted updater metadata, and returns the flushed record.

- [ ] **Step 2: Add service authorization**

Extend the catalog repository protocol and add `get_course_generation_policy` and `replace_course_generation_policy` to `CatalogService`. Both must call `_has_permission(context, "policy.manage", course_id=course_id)` before repository lookup. Construct the domain policy only from validated request fields.

- [ ] **Step 3: Add API DTOs and routes**

Add strict request/response models to `teaching_catalog.py`, map domain/repository errors to the existing stable HTTP categories, and expose the two nested routes. `PUT` returns 200 for both create and replacement.

- [ ] **Step 4: Run focused GREEN and the full catalog API file**

Run each command in a separate globally guarded window:

```powershell
python -m pytest tests/api/test_teaching_catalog.py -q -k "course_generation_policy"
python -m pytest tests/api/test_teaching_catalog.py -q
```

Expected: all selected and full-file tests pass with native exit 0.

### Task 3: Prove persistence and tenant isolation

**Files:**

- Create: `tests/teaching/integration/test_course_generation_policies.py`
- Modify if fixture registration requires it: `tests/teaching/integration/conftest.py`

- [ ] **Step 1: Add repository integration tests**

Use the existing translated SQLite tenant schema fixture pattern to prove:

```python
async def test_course_policy_replace_is_tenant_scoped_and_canonical(): ...
async def test_course_policy_replace_updates_one_row_without_cross_tenant_leakage(): ...
async def test_course_policy_get_rejects_missing_or_inactive_course(): ...
```

Assert the persisted content-mode string is canonical, updater metadata is trusted, replacement keeps exactly one row, and the same course ID in another tenant cannot be observed.

- [ ] **Step 2: Run focused integration GREEN**

Run in one globally guarded window:

```powershell
python -m pytest tests/teaching/integration/test_course_generation_policies.py -q
```

Expected: all tests pass with native exit 0.

- [ ] **Step 3: Run static checks**

```powershell
python -m ruff check deeptutor/teaching/repositories/catalog.py deeptutor/teaching/services/catalog.py deeptutor/api/routers/teaching_catalog.py tests/api/test_teaching_catalog.py tests/teaching/integration/test_course_generation_policies.py
python -m ruff format --check deeptutor/teaching/repositories/catalog.py deeptutor/teaching/services/catalog.py deeptutor/api/routers/teaching_catalog.py tests/api/test_teaching_catalog.py tests/teaching/integration/test_course_generation_policies.py
git diff --check
```

- [ ] **Step 4: Commit the prerequisite**

```powershell
git add deeptutor/teaching/repositories/catalog.py deeptutor/teaching/services/catalog.py deeptutor/api/routers/teaching_catalog.py tests/api/test_teaching_catalog.py tests/teaching/integration/test_course_generation_policies.py docs/superpowers/specs/2026-08-26-course-generation-policy-api-design.md docs/superpowers/plans/2026-08-26-course-generation-policy-api.md
git commit -m "feat(teaching): manage course generation policy"
```

After this commit, resume Plan07 Task5. The live fixture must use this API and verify the retained policy before the micro/full browser contexts start.

# Plan07 Release Candidate Binding Implementation Plan

> **Execution rule:** Complete each task with RED, minimal GREEN, and the
> listed focused verification before moving to the next task. Do not push a Git
> tag or run a real registry publication until all local contracts are GREEN.

**Goal:** Bind one immutable yFeiSTAI source commit, one release tag, three GHCR
image digests, `deploy/image-lock.json`, and both production Compose files into
one fail-closed Plan07 release candidate.

**Architecture:** Extend the image-lock writer with schema-version-2 candidate
metadata, require that metadata in production wrapper/preflight/verifier paths,
and harden the private-image workflow so an exact candidate tag is validated
before registry login. Keep legacy locks readable only through an explicit
historical-inspection mode.

**Tech stack:** Python 3, pytest, PyYAML, GitHub Actions YAML, Docker Compose
configuration text.

---

## Task 1: Add Candidate-Binding RED Contracts

**Files:**

- Modify: `tests/scripts/test_platform_compose.py`
- Modify: `tests/scripts/test_docker_compose.py`
- Modify: `tests/scripts/test_platform_preflight.py`
- Modify: `tests/scripts/test_verify_classroom_release.py`

1. Add a workflow contract proving a branch/manual ref is rejected before the
   GHCR login step, while an exact `yfeistai-first-release-YYYYMMDD-<HEAD8>` tag
   is accepted only when its suffix matches the checkout commit.
2. Extend the workflow publication contract to require the immutable candidate
   tag plus existing compatibility tags for all three custom images.
3. Add writer contracts requiring `sourceRepository`, `sourceHead`,
   `releaseTag`, `openmaicHead`, and three matching non-zero image digests.
4. Parameterize drift contracts for source HEAD, release tag, and candidate
   image digests; every failure must preserve the lock and both Compose files
   byte-for-byte.
5. Add wrapper and preflight contracts proving a legacy schema-version-1 lock
   is rejected before any subprocess invocation.
6. Add verifier contracts proving evidence cannot bind to a different source
   HEAD, release tag, OpenMAIC HEAD, or image digest.

Run only the new exact node IDs and confirm they fail for missing candidate
support rather than collection, fixture, Docker, or environment errors.

## Task 2: Implement Schema-Version-2 Atomic Rendering

**Files:**

- Modify: `scripts/render_platform_compose.py`

1. Add a small immutable candidate value model and strict validation helpers.
2. Require the writer to receive candidate source repository, source HEAD,
   release tag, and OpenMAIC HEAD.
3. Resolve all custom images using the release tag, reject zero/missing
   digests, and emit schema version 2 with duplicated digest equality checks.
4. Preserve the existing staged-write rollback behavior for the lock and both
   Compose files.
5. Extend `load_image_lock()` with an explicit `require_candidate` boundary:
   historical callers may opt out, while production callers default to strict
   candidate validation.
6. Add CLI arguments for the candidate fields and fail before writes if any
   field is invalid.

Run the writer-focused new nodes, then the existing atomic writer and remote
manifest tests.

## Task 3: Fail Closed in Wrapper and Preflight

**Files:**

- Modify: `scripts/docker_compose.py`
- Modify: `scripts/platform_preflight.py`

1. Make the production platform wrapper require a candidate-bound lock before
   constructing or invoking any Docker Compose subprocess.
2. Make the data-plane wrapper enforce the same lock boundary where it uses
   custom platform images.
3. Make preflight report legacy, missing, or drifting candidate metadata as a
   blocking image-lock failure.
4. Keep offline contract checks network-free and preserve existing topology and
   environment sanitization behavior.

Run the new exact wrapper/preflight nodes, then the two affected script test
files.

## Task 4: Harden the Private Image Workflow

**Files:**

- Modify: `.github/workflows/private-platform-images.yml`
- Modify: `tests/scripts/test_platform_compose.py`

1. Add a first step that validates the exact release tag and checkout commit
   before Buildx setup or GHCR login.
2. Give every build both the immutable candidate tag and the existing
   compatibility tag; retain `linux/amd64`, pinned contexts, and pinned
   Dockerfiles.
3. After all pushes, resolve candidate-tag manifests and call the writer with
   exact repository, source HEAD, release tag, and pinned OpenMAIC HEAD.
4. Validate the generated candidate lock against the workflow source values
   before artifact upload.
5. Upload the schema-version-2 lock and two rendered Compose files without any
   repository commit or push step.
6. Retain a single workflow concurrency group and least-privilege
   `contents: read`, `packages: write` permissions.

Run an offline YAML parse, the exact workflow contract node, and scoped
`git diff --check`.

## Task 5: Bind Release Verification Evidence

**Files:**

- Modify: `scripts/verify_classroom_release.py`
- Modify: `tests/scripts/test_verify_classroom_release.py`

1. Require receipt candidate identity to include `sourceHead`, `releaseTag`,
   `openmaicHead`, and the three image digests.
2. Compare all candidate fields against schema-version-2 image-lock metadata
   and the selected Compose references.
3. Report source/tag/digest drift as a blocking candidate-binding failure; do
   not convert a missing production receipt into synthetic success.
4. Preserve the existing eighteen evidence-layer requirements and honest
   `not_ready` result when evidence is absent.

Run focused binding nodes and the complete verifier test file.

## Task 6: Run Affected Verification and Commit

Run serially from the worktree:

```powershell
python -m pytest tests/scripts/test_platform_compose.py tests/scripts/test_docker_compose.py tests/scripts/test_platform_preflight.py tests/scripts/test_verify_classroom_release.py -q
python -m ruff check scripts/render_platform_compose.py scripts/docker_compose.py scripts/platform_preflight.py scripts/verify_classroom_release.py tests/scripts/test_platform_compose.py tests/scripts/test_docker_compose.py tests/scripts/test_platform_preflight.py tests/scripts/test_verify_classroom_release.py
python -m ruff format --check scripts/render_platform_compose.py scripts/docker_compose.py scripts/platform_preflight.py scripts/verify_classroom_release.py tests/scripts/test_platform_compose.py tests/scripts/test_docker_compose.py tests/scripts/test_platform_preflight.py tests/scripts/test_verify_classroom_release.py
git diff --check
```

Inspect the complete scoped diff. Confirm no branch/tag push, Docker operation,
image-lock publication, or main merge occurred. Commit only the candidate
binding implementation after all local checks are GREEN.

## Task 7: Publish One New Immutable Candidate

This task requires the user's standing tag/GHCR authorization and a healthy
runner. Never reuse or move an existing tag.

1. Confirm the worktree is clean and all required local commits are included.
2. Derive a new exact tag from the final HEAD and verify the name is absent
   locally and remotely.
3. Create and push only that tag; do not push a branch and do not merge main.
4. Observe the exact tag-triggered Actions run to native completion.
5. Download the artifact and verify its source HEAD, release tag, OpenMAIC HEAD,
   three remote manifest digests, and both Compose references.

If the run fails, retain the immutable failed tag as historical evidence, fix
on a new commit, and create a new tag. Never repoint the failed tag.

## Task 8: Continue Plan07 Acceptance on the Same Candidate

Use only the successful candidate artifact for Plan07 Tasks 2-8 and the twelve
named acceptance suites. Do not mix evidence from another commit, tag, image,
or Compose set. Only after every required layer is GREEN may the branch be
safely merged into local `main` and reverified there.

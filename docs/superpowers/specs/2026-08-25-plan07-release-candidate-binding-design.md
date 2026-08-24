# Plan07 Source-Bound Release Candidate Design

**Date:** 2026-08-25

## Problem

The first-release image workflow can build three private images and write their
registry digests into `deploy/image-lock.json`, but the current lock format does
not prove which yFeiSTAI commit produced those images. The workflow also pushes
only mutable compatibility tags. A non-zero digest therefore proves that an
image exists, not that the three images, two production Compose files, and the
checked-out source all belong to one immutable release candidate.

The historical tag `yfeistai-first-release-20260820-05605067` already exists and
must not be moved. Its failed workflow run cannot be repaired by repointing the
tag.

## Decision

Every publishable candidate uses a new immutable Git tag:

```text
yfeistai-first-release-YYYYMMDD-<HEAD8>
```

The workflow must fail before registry login unless the triggering ref is an
exact tag matching this format and the `<HEAD8>` suffix matches the checked-out
commit. Manual dispatch remains available only when the workflow itself is run
from such a tag.

Each of the three images is first pushed with:

- the immutable release-candidate tag.

After all three builds succeed, their candidate-tag remote digests match the
three build outputs, and the generated lock and Compose files validate, the
workflow promotes each existing compatibility tag from the corresponding
verified digest. A later build failure therefore cannot split compatibility
aliases across candidates.

The candidate tag is the authoritative source for resolving the final remote
OCI manifest digest. Compatibility tags are aliases only and are never used to
establish candidate identity.

## Candidate-Bound Image Lock

The writer emits schema version 2. Alongside the existing image entries it
records one candidate object:

```json
{
  "schemaVersion": 2,
  "candidate": {
    "sourceRepository": "xinlingzhifei/DeepTutor",
    "sourceHead": "<40-character commit>",
    "releaseTag": "yfeistai-first-release-YYYYMMDD-<HEAD8>",
    "openmaicHead": "0cf2a330...",
    "imageDigests": {
      "deeptutor": "sha256:...",
      "openmaic": "sha256:...",
      "openmaic_render": "sha256:..."
    }
  },
  "images": {}
}
```

For every custom image, `images.<name>.tag` is the immutable release tag and
`images.<name>.digest` equals `candidate.imageDigests.<name>`. Both production
Compose files use `<registry>/<image>:<releaseTag>@<digest>` exactly.

The writer updates the lock and both Compose files atomically. Missing or zero
digests, mismatched candidate fields, or any publish failure leave all three
files byte-for-byte unchanged.

## Validation Boundaries

Legacy schema-version-1 locks may still be parsed explicitly for historical
inspection, but they are not valid production candidates. The following paths
must require schema version 2 and fail closed before invoking Docker:

- the platform wrapper;
- platform preflight;
- the release verifier; and
- the CI post-push validation step.

Validation binds all of these values together:

- checkout HEAD;
- release tag and its HEAD suffix;
- pinned OpenMAIC commit;
- three remote image manifest digests;
- image-lock image entries; and
- both production Compose references.

## Generated Artifact and Commit Cycle

The workflow uploads the generated lock and two Compose files as one Actions
artifact after all three image pushes and candidate validation succeed. It does
not commit those generated files back to the repository. This avoids an
impossible cycle where writing the digest lock creates a new commit that is no
longer the source commit recorded in the lock.

Deployment consumes the artifact produced by the exact immutable tag. The
checked-in schema-version-1 lock remains historical until a candidate artifact
is selected; its non-zero digests cannot be treated as proof for current HEAD.

## Safety and Compatibility

- Existing Git tags are immutable and are never force-updated.
- The workflow has one global concurrency group so candidate publications do
  not overlap.
- Registry login occurs only after candidate-ref validation.
- Existing candidate image tags are rejected before any build, and generated
  lock digests must equal the three build-action output digests.
- The workflow never commits or pushes repository contents.
- No branch push or local `main` merge is part of publishing a candidate.
- Compatibility image tags remain available, but acceptance evidence uses only
  the immutable candidate tag and digests.

## Verification Strategy

Tests first establish RED contracts for candidate identity, workflow ordering,
digest binding, legacy-lock rejection, and source/tag drift. The implementation
then makes only those contracts GREEN. Static workflow tests do not prove that
GitHub Actions ran or that GHCR accepted a push; those facts require a real
tag-triggered run and remote manifest inspection for the same candidate.

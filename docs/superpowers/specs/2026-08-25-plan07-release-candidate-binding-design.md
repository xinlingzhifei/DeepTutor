# Plan07 Source-Bound Release Candidate Design

**Date:** 2026-08-25

## Problem

Before schema-version-3 candidate binding, the first-release image workflow
could build three workload images and write their registry digests into
`deploy/image-lock.json`, but the legacy lock format did not prove which
yFeiSTAI commit produced those images. The legacy workflow also pushed only
mutable compatibility tags. A non-zero digest therefore proved that an image
existed, not that the three workload images, five base images, the independent
observer signer, exact base Compose file, three rendered production Compose
files, and the checked-out source all belonged to one immutable release
candidate. The
checked-in schema-version-1 lock remains historical baseline input; it does not
describe current candidate publication.

The historical tag `yfeistai-first-release-20260820-05605067` already exists and
must not be moved. Its failed workflow run cannot be repaired by repointing the
tag.

## Decision

Every publishable candidate uses a new immutable Git tag:

```text
yfeistai-first-release-YYYYMMDD-<HEAD8>
```

The workflow must fail before registry login unless it is the first attempt of
a push event for an exact tag matching this format and the `<HEAD8>` suffix
matches the checked-out commit. Manual dispatch and workflow reruns are not
publication paths.

Each of the four images is first pushed under a unique run-scoped staging tag:

- `staging-${GITHUB_RUN_ID}-${GITHUB_RUN_ATTEMPT}`.

After all four builds succeed, the workflow validates their non-zero build
digests and generates the lock and Compose files directly from those digests.
It rejects every pre-existing final tag before the builds and again immediately
before each promotion, promotes each digest once to the immutable
release-candidate tag, and remotely reverifies all four promoted tags before
receipts or artifacts are emitted. No compatibility tag is created or moved.

GHCR does not expose an atomic four-image transaction or a conditional tag
creation primitive to this workflow. Protected repository and package write
permissions are therefore an external single-writer trust boundary. A failed
or partially published candidate is never rerun under the same tag: it remains
historical evidence, while a fix receives a new commit and a new tag. Consumers
use only the digest-pinned artifact from a fully successful run.

## Candidate-Bound Image Lock

The writer emits schema version 3. Alongside the existing image entries it
records one candidate object:

```json
{
  "schemaVersion": 3,
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
  "images": {
    "pocketbase": {
      "repository": "ghcr.io/muchobien/pocketbase",
      "tag": "latest",
      "digest": "sha256:...",
      "reference": "ghcr.io/muchobien/pocketbase:latest@sha256:..."
    }
  },
  "supportImages": {
    "observer_signer": {
      "repository": "ghcr.io/xinlingzhifei/yfeistai-observer-signer",
      "tag": "yfeistai-first-release-YYYYMMDD-<HEAD8>",
      "digest": "sha256:...",
      "reference": "ghcr.io/xinlingzhifei/yfeistai-observer-signer:yfeistai-first-release-YYYYMMDD-<HEAD8>@sha256:..."
    }
  }
}
```

For every custom image, `images.<name>.tag` is the immutable release tag and
`images.<name>.digest` equals `candidate.imageDigests.<name>`. All four
candidate Compose files use the exact digest-qualified `images.<name>.reference`;
PocketBase remains on its fixed `latest` tag but its candidate reference is
`ghcr.io/muchobien/pocketbase:latest@sha256:...`. The observer signer digest is
non-zero, uses the same release tag, and must not equal any workload digest.

The writer updates the lock, base Compose, platform Compose, data-plane Compose,
and observer overlay atomically. Missing or zero digests, mismatched candidate
fields, or any publish failure leave all five files byte-for-byte unchanged.

## Validation Boundaries

Legacy schema-version-1 locks may still be parsed explicitly for historical
inspection, but they are not valid production candidates. The following paths
must require schema version 3 and fail closed before invoking Docker:

- the platform wrapper;
- platform preflight;
- the release verifier; and
- the CI post-push validation step.

Validation binds all of these values together:

- checkout HEAD;
- release tag and its HEAD suffix;
- pinned OpenMAIC commit;
- three workload image digests, all five base-image digests, and the
  independent observer signer digest;
- image-lock image entries; and
- the exact base Compose template plus all three rendered production Compose
  references.

## Generated Artifact and Commit Cycle

The workflow uploads the generated lock, exact base Compose file, and three
rendered Compose files as one Actions artifact after all four image promotions
and remote validation succeed. The artifact also contains the canonical
promotion intent and a schema-version-2 receipt bound to that intent, the
source candidate, every image digest in the lock, the independent observer
signer digest, the exact GitHub run ID, first run attempt, and workflow ref. A
strict local verifier consumes those files before upload and again after the
artifact is downloaded, using the Actions API run ID as an out-of-band input.
The verifier opens and holds one no-follow descriptor lease across the lock,
the base, platform, data-plane, and observer Compose files, promotion intent,
and promotion receipt before semantic validation. Validation consumes only the
cached bytes;
before closing, every descriptor is rewound and completely reread in reverse
artifact order, its SHA-256
must equal the initial digest, and descriptor/path/root/parent identities are
rechecked. This digest reread is mandatory on Windows because change timestamps
alone are insufficient against coordinated pathname ABA. The verifier proves artifact/run binding;
remote manifest inspection remains a separate required Task 7 check. The
workflow does not commit generated files back to the repository. This avoids
an impossible cycle where writing the digest lock creates a new commit that is
no longer the source commit recorded in the lock.

Deployment consumes the artifact produced by the exact immutable tag. The
checked-in schema-version-1 lock remains historical until a candidate artifact
is selected; its non-zero digests cannot be treated as proof for current HEAD.

## Safety and Compatibility

- Existing Git tags are immutable and are never force-updated.
- The workflow has one global concurrency group so candidate publications do
  not overlap.
- Registry login occurs only after candidate-ref validation.
- Every pre-existing candidate image tag is rejected, including one that
  already resolves to the expected digest; no same-tag retry is allowed.
- Generated custom-image digests must equal all four build-action output
  digests, while intent and receipt bind those plus every base-image digest,
  including PocketBase.
- The workflow never commits or pushes repository contents.
- No branch push or local `main` merge is part of publishing a candidate.
- The workflow creates or moves no compatibility image tags; acceptance
  evidence uses only the immutable candidate tag and digests.

## Verification Strategy

Tests first establish RED contracts for candidate identity, workflow ordering,
digest binding, legacy-lock rejection, and source/tag drift. The implementation
then makes only those contracts GREEN. Static workflow tests do not prove that
GitHub Actions ran or that GHCR accepted a push; those facts require a real
tag-triggered run and remote manifest inspection for the same candidate.

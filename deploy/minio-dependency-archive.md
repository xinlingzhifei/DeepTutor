# MinIO dependency archive

On 2026-10-08 the release job could no longer resolve the pinned MinIO server
and client tags. Anonymous manifest requests to Docker Hub and Quay returned
HTTP 401 for both versions. The deployment host retained the previously
verified source images.

The original Linux amd64 manifests were copied into the existing public
platform package. No image was rebuilt. Each archived manifest was fetched
anonymously from GHCR and compared byte for byte with the cached manifest
referenced by its original, SHA-256-verified multi-platform index.

| Image | Original index digest | Preserved amd64 manifest digest |
| --- | --- | --- |
| `minio/minio:RELEASE.2025-04-22T22-12-26Z` | `sha256:a1ea29fa28355559ef137d71fc570e508a214ec84ff8083e39bc5428980b015e` | `sha256:3f97c5651cb6662b880c787a232b6b34fec8d8922e08d6617b25d241a21164bb` |
| `minio/mc:RELEASE.2025-04-16T18-13-26Z` | `sha256:aead63c77f9db9107f1696fb08ecb0faeda23729cde94b0f663edf4fe09728e3` | `sha256:2582c2f48b1e31545143ba5285c67d7b38c8b8f6912142d0630686dc7aaac28b` |

Archive references:

- `ghcr.io/xinlingzhifei/deeptutor:dependency-minio-RELEASE.2025-04-22T22-12-26Z@sha256:3f97c5651cb6662b880c787a232b6b34fec8d8922e08d6617b25d241a21164bb`
- `ghcr.io/xinlingzhifei/deeptutor:dependency-mc-RELEASE.2025-04-16T18-13-26Z@sha256:2582c2f48b1e31545143ba5285c67d7b38c8b8f6912142d0630686dc7aaac28b`

These archive tags are immutable and support Linux amd64 only. The original
image layers, configuration, labels, and licenses are preserved. Existing
candidate archives remain unchanged; new candidates bind the archive references
through the normal image-lock and Observer admission checks.

Backup restoration accepts the exact archived MinIO reference above while
retaining compatibility with previous Docker Hub references. It does not accept
other application images or arbitrary digests from the platform package.

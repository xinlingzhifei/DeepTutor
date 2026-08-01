#!/usr/bin/env python3
"""Verify the pinned OpenMAIC overlay and optionally run its focused tests."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_INTEGRATION_ROOT = ROOT / "integrations" / "openmaic"
EXPECTED_UPSTREAM = {
    "repository": "https://github.com/xinlingzhifei/OpenMAIC.git",
    "commit": "0cf2a330411681190e89f48e20f305345ff99f87",
    "appVersion": "0.3.1",
}
REQUIRED_OVERLAY_FILES = {
    Path("app/api/yfeistai/v1/artifacts/[jobId]/[...path]/route.ts"),
    Path("app/api/yfeistai/v1/classrooms/[jobId]/route.ts"),
    Path("app/api/yfeistai/v1/classrooms/route.ts"),
    Path("app/api/yfeistai/v1/exports/[jobId]/route.ts"),
    Path("app/api/yfeistai/v1/exports/route.ts"),
    Path("app/api/yfeistai/v1/outlines/[jobId]/route.ts"),
    Path("app/api/yfeistai/v1/outlines/route.ts"),
    Path("app/api/yfeistai/v1/health/route.ts"),
    Path("app/api/yfeistai/v1/jobs/[jobId]/cancel/route.ts"),
    Path("lib/yfeistai/artifact-manifest.ts"),
    Path("lib/yfeistai/content-generation.ts"),
    Path("lib/yfeistai/contracts.ts"),
    Path("lib/yfeistai/durable-state.ts"),
    Path("lib/yfeistai/export-generation.ts"),
    Path("lib/yfeistai/job-store.ts"),
    Path("lib/yfeistai/outline-generation.ts"),
    Path("lib/yfeistai/portable-classroom.ts"),
    Path("lib/yfeistai/service-boundary.ts"),
    Path("lib/yfeistai/service-auth.ts"),
    Path("tests/yfeistai/artifact-manifest.test.ts"),
    Path("tests/yfeistai/cancel.test.ts"),
    Path("tests/yfeistai/content-generation.test.ts"),
    Path("tests/yfeistai/export-generation.test.ts"),
    Path("tests/yfeistai/outline-generation.test.ts"),
    Path("tests/yfeistai/service-auth.test.ts"),
}
TASK4_SOURCE_FILES = {
    Path("app/api/yfeistai/v1/artifacts/[jobId]/[...path]/route.ts"),
    Path("app/api/yfeistai/v1/classrooms/[jobId]/route.ts"),
    Path("app/api/yfeistai/v1/classrooms/route.ts"),
    Path("app/api/yfeistai/v1/exports/[jobId]/route.ts"),
    Path("app/api/yfeistai/v1/exports/route.ts"),
    Path("app/api/yfeistai/v1/jobs/[jobId]/cancel/route.ts"),
    Path("lib/yfeistai/artifact-manifest.ts"),
    Path("lib/yfeistai/content-generation.ts"),
    Path("lib/yfeistai/durable-state.ts"),
    Path("lib/yfeistai/export-generation.ts"),
    Path("lib/yfeistai/job-store.ts"),
    Path("lib/yfeistai/portable-classroom.ts"),
    Path("lib/yfeistai/service-boundary.ts"),
}
JAVASCRIPT_SUFFIXES = {".cjs", ".cts", ".js", ".jsx", ".mjs", ".mts", ".ts", ".tsx"}
FORBIDDEN_CLIENT_PARAMETER_PATTERNS = (
    re.compile(r"\bapi[-_\s]*key\b", re.IGNORECASE),
    re.compile(r"\bbase[-_\s]*url\b", re.IGNORECASE),
    re.compile(r"\bprovider[-_\s]*id\b", re.IGNORECASE),
    re.compile(r"\bprovider[-_\s]*key\b", re.IGNORECASE),
    re.compile(r"\bprovider[-_\s]*api[-_\s]*key\b", re.IGNORECASE),
    re.compile(r"\bprovider[-_\s]*base[-_\s]*url\b", re.IGNORECASE),
)
FORBIDDEN_IDENTITY_SURFACE_PATTERNS = (
    re.compile(r"\bsign[-_\s]*in(?:[-_\s]*(?:page|form))?\b", re.IGNORECASE),
    re.compile(r"\blogin(?:[-_\s]*(?:page|form))?\b", re.IGNORECASE),
    re.compile(
        r"\baccount(?:s|[-_\s]*(?:store|table|model|entity|record))?\b",
        re.IGNORECASE,
    ),
)
PRODUCTION_TEST_IMPORT_PATTERN = re.compile(
    r"""(?:(?:\bimport\b|\bfrom\b)[^;\n]*|\brequire\s*\(\s*)["']"""
    r"""(?:tests[/\\][^"']*|[^"']*[/\\]tests(?:[/\\][^"']*)?)["']""",
    re.IGNORECASE,
)
VITEST_PACKAGE = "vitest@4.1.8"


class OverlayVerificationError(RuntimeError):
    """Raised when the committed overlay violates its integration contract."""


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise OverlayVerificationError(f"required file is missing: {path}") from exc


def _strip_javascript_comments(source: str) -> str:
    result: list[str] = []
    index = 0
    quote: str | None = None
    while index < len(source):
        char = source[index]
        next_char = source[index + 1] if index + 1 < len(source) else ""
        if quote is not None:
            result.append(char)
            if char == "\\" and index + 1 < len(source):
                index += 1
                result.append(source[index])
            elif char == quote:
                quote = None
            index += 1
            continue
        if char in {'"', "'", "`"}:
            quote = char
            result.append(char)
            index += 1
            continue
        if char == "/" and next_char == "/":
            index += 2
            while index < len(source) and source[index] not in "\r\n":
                index += 1
            continue
        if char == "/" and next_char == "*":
            index += 2
            while index + 1 < len(source) and source[index : index + 2] != "*/":
                if source[index] in "\r\n":
                    result.append(source[index])
                index += 1
            index = min(index + 2, len(source))
            continue
        result.append(char)
        index += 1
    return "".join(result)


def _verify_upstream_manifest(integration_root: Path) -> None:
    path = integration_root / "UPSTREAM.json"
    try:
        manifest = json.loads(_read_text(path))
    except json.JSONDecodeError as exc:
        raise OverlayVerificationError(f"invalid JSON in {path}: {exc}") from exc
    if manifest != EXPECTED_UPSTREAM:
        raise OverlayVerificationError(
            f"{path} must exactly pin the verified repository, commit, and app version"
        )


def _verify_dockerfile(integration_root: Path) -> None:
    path = integration_root / "Dockerfile"
    source = _read_text(path)
    overlay_copy = "COPY integrations/openmaic/overlay/"
    commit = EXPECTED_UPSTREAM["commit"]
    short_commit = commit[:12]
    required_tokens = (
        (f'git clone --filter=blob:none --no-checkout "{EXPECTED_UPSTREAM["repository"]}" /src'),
        f'git fetch --depth=1 origin "{commit}"',
        f'git checkout --detach "{commit}"',
        f'test "$(git rev-parse HEAD)" = "{commit}"',
        (
            'test "$(node -p "require(\'./package.json\').version")" = '
            f'"{EXPECTED_UPSTREAM["appVersion"]}"'
        ),
        overlay_copy,
        f'org.opencontainers.image.revision="{commit}"',
        f'org.opencontainers.image.revision-short="{short_commit}"',
        f'org.opencontainers.image.version="{EXPECTED_UPSTREAM["appVersion"]}"',
    )
    missing = [token for token in required_tokens if token not in source]
    if missing:
        raise OverlayVerificationError(
            f"{path} is missing required pin/build tokens: {', '.join(missing)}"
        )
    if re.search(r"^\s*ARG\s+OPENMAIC_", source, re.MULTILINE):
        raise OverlayVerificationError("OpenMAIC release pins cannot be Docker build arguments")
    if "$OPENMAIC_" in source or "${OPENMAIC_" in source:
        raise OverlayVerificationError("OpenMAIC release pins cannot be variable substitutions")
    if source.index("git rev-parse HEAD") > source.index(overlay_copy):
        raise OverlayVerificationError(
            "Dockerfile must verify the checkout before applying overlay"
        )
    runner_marker = " AS runner"
    if runner_marker not in source:
        raise OverlayVerificationError("Dockerfile must contain a final runner stage")
    runner = source[source.index(runner_marker) :]
    if "git clone" in runner or re.search(r"\bapk add\b[^\n]*\bgit\b", runner):
        raise OverlayVerificationError("final image must not clone Git or install Git")


def find_forbidden_overlay_surface(overlay_root: Path) -> list[str]:
    """Return login/account paths and client-controlled provider parameters."""

    violations: list[str] = []
    for path in sorted(overlay_root.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(overlay_root)
        relative_text = relative.as_posix()
        for pattern in FORBIDDEN_IDENTITY_SURFACE_PATTERNS:
            match = pattern.search(relative_text)
            if match:
                violations.append(f"{relative}: forbidden {match.group(0)} surface")
        if path.suffix.lower() not in JAVASCRIPT_SUFFIXES:
            continue
        source = _strip_javascript_comments(path.read_text(encoding="utf-8"))
        for pattern in FORBIDDEN_IDENTITY_SURFACE_PATTERNS:
            match = pattern.search(source)
            if match:
                violations.append(f"{relative}: forbidden {match.group(0)} surface")
        for pattern in FORBIDDEN_CLIENT_PARAMETER_PATTERNS:
            match = pattern.search(source)
            if match:
                violations.append(f"{relative}: forbidden client parameter {match.group(0)}")
        if (
            not relative.parts or relative.parts[0].lower() != "tests"
        ) and PRODUCTION_TEST_IMPORT_PATTERN.search(source):
            violations.append(f"{relative}: production source imports overlay tests")
        if (
            relative.parts
            and relative.parts[0].lower() != "tests"
            and re.search(r"\bgenerateClassroom\s*\(", source)
        ):
            violations.append(
                f"{relative}: outline overlay must not invoke full classroom generation"
            )
    return violations


def verify_service_auth_source(source: str) -> None:
    source = _strip_javascript_comments(source)
    required_patterns = {
        "Node crypto imports": (
            r"import\s*\{(?=[^}]*\bcreateHash\b)(?=[^}]*\bcreateHmac\b)"
            r'(?=[^}]*\btimingSafeEqual\b)[^}]*\}\s*from\s*["\']node:crypto["\']'
        ),
        "SHA-256 body digest": (
            r'createHash\(["\']sha256["\']\)\s*\.update\(body\)'
            r'\s*\.digest\(["\']hex["\']\)'
        ),
        "HMAC-SHA256 signer": (
            r'createHmac\(["\']sha256["\'],\s*secret\)\s*'
            r'\.update\(canonicalServiceRequest\(input\),\s*["\']utf8["\']\)'
            r'\s*\.digest\(["\']hex["\']\)'
        ),
        "60-second clock window": (
            r"export\s+const\s+MAX_CLOCK_SKEW_SECONDS\s*=\s*60"
            r"(?:\s+as\s+const)?\s*;"
        ),
        "clock-skew enforcement": (
            r"Math\.abs\(options\.nowSeconds\s*-\s*normalized\.timestamp\)"
            r"\s*>\s*MAX_CLOCK_SKEW_SECONDS"
        ),
        "fixed secret mount": (
            r"export\s+const\s+SERVICE_SECRET_PATH\s*=\s*"
            r'["\']/run/secrets/openmaic_service_secret["\']'
        ),
        "secret file read": (
            r"readFileSync\(SERVICE_SECRET_PATH,\s*"
            r'["\']utf8["\']\)'
        ),
        "write idempotency enforcement": (r"allowEmpty:\s*!requiresIdempotencyKey\(method\)"),
        "canonical field order": (
            r"const\s+canonicalParts\s*=\s*\[\s*"
            r"normalized\.method,\s*"
            r"normalized\.path,\s*"
            r"normalized\.tenantId,\s*"
            r"normalized\.jobId,\s*"
            r"String\(normalized\.timestamp\),\s*"
            r"normalized\.idempotencyKey,\s*"
            r"sha256Body\(input\.body\),?\s*\]"
        ),
        "canonical newline join": (r'return\s+canonicalParts\.join\(["\']\\n["\']\)'),
        "signature shape validation": (r"!SHA256_HEX\.test\(signed\.signature\)"),
        "constant-time signature comparison": (r"timingSafeEqual\(expected,\s*received\)"),
    }
    missing = [
        label
        for label, pattern in required_patterns.items()
        if re.search(pattern, source, re.DOTALL) is None
    ]
    if missing:
        raise OverlayVerificationError(
            "service-auth.ts is missing security-critical structures: " + ", ".join(missing)
        )
    if "process.env" in source:
        raise OverlayVerificationError(
            "service-auth.ts must not read its service secret from process.env"
        )
    if len(re.findall(r"\breadFileSync\s*\(", source)) != 1:
        raise OverlayVerificationError(
            "service-auth.ts must read exactly one fixed service-secret file"
        )


def _verify_health_contract(overlay_root: Path) -> None:
    route = _read_text(overlay_root / "app/api/yfeistai/v1/health/route.ts")
    contracts = _read_text(overlay_root / "lib/yfeistai/contracts.ts")
    if "OPENMAIC_HEALTH_RESPONSE" not in route or "Response.json" not in route:
        raise OverlayVerificationError("health route must return the typed health response")
    required_literals = (
        '"openmaic"',
        f'"{EXPECTED_UPSTREAM["commit"]}"',
        f'"{EXPECTED_UPSTREAM["appVersion"]}"',
        '"1.0"',
        '"outline"',
        '"content"',
        '"micro"',
        '"export"',
        '"cancel"',
        '"artifact-manifest"',
        '"classroom_zip"',
        '"pptx"',
        '"offline_html"',
        '"mp4"',
    )
    missing = [token for token in required_literals if token not in contracts]
    if missing:
        raise OverlayVerificationError(
            "contracts.ts is missing health contract literals: " + ", ".join(missing)
        )


def verify_outline_contract_hash(
    source: str,
    schema_path: Path | None = None,
) -> None:
    schema_path = (
        schema_path
        if schema_path is not None
        else ROOT / "contracts" / "classroom" / "outline-bundle.schema.json"
    )
    expected = hashlib.sha256(schema_path.read_bytes()).hexdigest()
    match = re.search(
        r"export\s+const\s+OUTLINE_BUNDLE_CONTRACT_SHA256\s*=\s*"
        r'["\']([0-9a-f]{64})["\']',
        _strip_javascript_comments(source),
    )
    if match is None:
        raise OverlayVerificationError(
            "outline-generation.ts must export the frozen outline schema hash"
        )
    if match.group(1) != expected:
        raise OverlayVerificationError(
            "outline contract hash does not match outline-bundle.schema.json"
        )


def _verify_outline_generation(overlay_root: Path) -> None:
    source = _strip_javascript_comments(
        _read_text(overlay_root / "lib/yfeistai/outline-generation.ts")
    )
    post_route = _strip_javascript_comments(
        _read_text(overlay_root / "app/api/yfeistai/v1/outlines/route.ts")
    )
    get_route = _strip_javascript_comments(
        _read_text(overlay_root / "app/api/yfeistai/v1/outlines/[jobId]/route.ts")
    )
    store = _strip_javascript_comments(_read_text(overlay_root / "lib/yfeistai/job-store.ts"))

    required_source_tokens = (
        "verifyServiceRequest(",
        "JSON.parse(body)",
        "validateGenerationRequest(",
        "validateOutlineBundle(",
        "canonicalJson(generationRequest)",
        'action: "outline"',
        "generationRequest.tenantId !== signed.tenantId",
        "generationRequest.jobId !== signed.jobId",
        "generationRequest.idempotencyKey !== signed.idempotencyKey",
        "dependencies.store.read(signed.tenantId, jobId)",
        "IdempotencyConflictError",
    )
    missing = [token for token in required_source_tokens if token not in source]
    if missing:
        raise OverlayVerificationError(
            "outline-generation.ts is missing boundary controls: " + ", ".join(missing)
        )
    if source.index("verifyServiceRequest(") > source.index("JSON.parse(body)"):
        raise OverlayVerificationError(
            "outline POST must authenticate before parsing its request body"
        )

    required_post_tokens = (
        "@/lib/generation/outline-generator",
        "@/lib/server/resolve-model",
        "@/lib/web-search",
        "generateSceneOutlinesFromRequirements(",
        'resolveModel({ stage: "generate-classroom" })',
        "resolveClassroomWebSearchConfig({})",
        'answer: ""',
        "createOutlinePostHandler(",
        "return postOutline(request)",
    )
    missing = [token for token in required_post_tokens if token not in post_route]
    if missing:
        raise OverlayVerificationError(
            "outline POST route is missing pinned upstream adapters: " + ", ".join(missing)
        )
    required_get_tokens = (
        "createOutlineGetHandler(",
        "params: Promise<{ jobId: string }>",
        "return getOutline(request, context)",
    )
    missing = [token for token in required_get_tokens if token not in get_route]
    if missing:
        raise OverlayVerificationError(
            "outline GET route is missing authenticated polling: " + ", ".join(missing)
        )
    required_store_tokens = (
        "claimDurableLease(",
        "renewDurableLease(",
        "durableLeaseMatches(",
        "writeDurableJsonExclusive(",
        "submission.tenantId",
        "submission.jobId",
        "submission.idempotencyKey",
        "submission.action",
        "bodySha256(submission.canonicalBody)",
        "Symbol.for(",
        "globalThis",
    )
    missing = [token for token in required_store_tokens if token not in store]
    if missing:
        raise OverlayVerificationError(
            "job-store.ts is missing idempotency bindings: " + ", ".join(missing)
        )
    verify_outline_contract_hash(source)


def _require_tokens(source: str, label: str, tokens: tuple[str, ...]) -> None:
    missing = [token for token in tokens if token not in source]
    if missing:
        raise OverlayVerificationError(
            f"{label} is missing required controls: " + ", ".join(missing)
        )


def verify_task4_sources(overlay_root: Path) -> None:
    """Verify content, export, cancellation, artifact, and signed-route controls."""

    content = _strip_javascript_comments(
        _read_text(overlay_root / "lib/yfeistai/content-generation.ts")
    )
    artifact = _strip_javascript_comments(
        _read_text(overlay_root / "lib/yfeistai/artifact-manifest.ts")
    )
    export = _strip_javascript_comments(
        _read_text(overlay_root / "lib/yfeistai/export-generation.ts")
    )
    durable = _strip_javascript_comments(_read_text(overlay_root / "lib/yfeistai/durable-state.ts"))
    portable = _strip_javascript_comments(
        _read_text(overlay_root / "lib/yfeistai/portable-classroom.ts")
    )
    boundary = _strip_javascript_comments(
        _read_text(overlay_root / "lib/yfeistai/service-boundary.ts")
    )
    classroom_post = _strip_javascript_comments(
        _read_text(overlay_root / "app/api/yfeistai/v1/classrooms/route.ts")
    )
    classroom_get = _strip_javascript_comments(
        _read_text(overlay_root / "app/api/yfeistai/v1/classrooms/[jobId]/route.ts")
    )
    export_post = _strip_javascript_comments(
        _read_text(overlay_root / "app/api/yfeistai/v1/exports/route.ts")
    )
    export_get = _strip_javascript_comments(
        _read_text(overlay_root / "app/api/yfeistai/v1/exports/[jobId]/route.ts")
    )
    cancel_route = _strip_javascript_comments(
        _read_text(overlay_root / "app/api/yfeistai/v1/jobs/[jobId]/cancel/route.ts")
    )
    artifact_route = _strip_javascript_comments(
        _read_text(overlay_root / "app/api/yfeistai/v1/artifacts/[jobId]/[...path]/route.ts")
    )

    _require_tokens(
        content,
        "content-generation.ts",
        (
            "canonicalConfirmedOutlineJson(request.confirmedOutline)",
            "actual !== request.confirmedOutlineSha256",
            'confirmationMetadata?.status !== "confirmed"',
            "for (const [order, outlineScene] of outline.scenes.entries())",
            "await dependencies.generateScenes(",
            "await assertNotCanceled(dependencies.isCanceled)",
            'dslVersion: "0.1.0"',
            'bridgeVersion = "1.0"',
            "allowSameOrigin: false",
            "mediaManifest",
            "fileSha256",
            'relativePath: "classroom/classroom.json"',
            "claimExecution(",
            "persistTerminal(terminal, claim, publishSucceeded)",
            "configuredOpenMaicStateRoot()",
            "asPortableDocument(classroomDocumentCandidate)",
            "generated media writer integrity binding failed",
            "dependencies.store.isCanceled(parsed.tenantId, parsed.jobId)",
            "hasSignedBodyBinding(signed, parsed)",
            'validateGenerationRequest(value, "classroom")',
            "buildMicroOutline(",
            "contentOutputRegistry",
            "generated media reference is missing from the artifact manifest",
            "classroomDocumentSha256",
            "mediaManifestSha256",
            "resultFailure(result)",
            "assertPublicationActive",
            "publishSucceeded(job.result)",
            "sceneTypeFor(",
            "sourceFragments: request.teachingBrief.sourceFragments.map",
        ),
    )
    if content.count("await assertNotCanceled(dependencies.isCanceled)") < 4:
        raise OverlayVerificationError(
            "content-generation.ts must check cancellation around every scene and publication"
        )
    if content.index("actual !== request.confirmedOutlineSha256") > content.index(
        "await dependencies.generateScenes("
    ):
        raise OverlayVerificationError(
            "content generation must verify the confirmed outline before generating scenes"
        )
    if "generateOutlines(" in content or "generateClassroom(" in content:
        raise OverlayVerificationError(
            "content generation must not regenerate an outline or invoke the full pipeline"
        )
    if content.index("authenticateServiceRequest(request, body") > content.index(
        "JSON.parse(body)"
    ):
        raise OverlayVerificationError(
            "classroom POST must authenticate before parsing its request body"
        )

    _require_tokens(
        durable,
        "durable-state.ts",
        (
            "YFEISTAI_OPENMAIC_STATE_ROOT",
            "linkSync(temporary, target)",
            "fsyncSync(",
            "const owner = randomUUID()",
            'path.join(lockPath, "owner")',
            "fstatSync(ownerDescriptor)",
            "claimDurableLease(",
            "renewDurableLease(",
            "durableLeaseMatches(",
            "syncParentDirectory(target)",
            "PROCESS_INSTANCE_ID",
            'record.hostname === hostname()',
        ),
    )
    _require_tokens(
        portable,
        "portable-classroom.ts",
        (
            'exactKeys(content, "slide content"',
            "validateQuiz(content)",
            "interactive sandbox contract is invalid",
            "PBL content shape is invalid",
            'sha256(document.fileSha256, "classroom file hash")',
            "assertOfflineHtmlSelfContained(",
            "srcset|action|poster",
            'document.contentMode === "source_grounded"',
            "source-grounded classroom requires at least one source ref",
        ),
    )

    _require_tokens(
        artifact,
        "artifact-manifest.ts",
        (
            "/%[0-9A-Fa-f]{2}/.test(value)",
            'value.includes("\\\\")',
            'value.startsWith("/")',
            "path.relative(root, target)",
            "manifestEntry",
            "parseExpiry(manifestEntry.expiresAt)",
            "for (const segment of relativePath.split",
            "if (stat.isSymbolicLink())",
            'createHash("sha256").update(input.bytes).digest("hex")',
            "bytes.byteLength !== stored.entry.bytes",
            'createHash("sha256").update(bytes).digest("hex") !==',
            "dependencies.store.read(",
            "readArtifactFromSameHandle(",
            "fsConstants.O_NOFOLLOW",
            "handle.readFile()",
            '".yfeistai-manifest"',
            "assertArtifactWriteTarget(",
            "artifact write path contains an unsafe parent",
            "WINDOWS_DEVICE_NAME",
        ),
    )

    _require_tokens(
        export,
        "export-generation.ts",
        (
            "digest(input.classroomDocument)",
            "classroom document hash mismatch",
            "digest(input.mediaManifest)",
            "media manifest hash mismatch",
            "validateArchiveEntries(",
            "MAX_ARCHIVE_ENTRIES",
            "MAX_ARCHIVE_UNCOMPRESSED_BYTES",
            "MAX_ARCHIVE_COMPRESSION_RATIO",
            'entry.kind === "symlink"',
            "archive external links are forbidden",
            "FORBIDDEN_RUNTIME_INPUT_KEYS",
            "assertNoExternalLocations(request",
            'endpoint.protocol === "http:"',
            'endpoint.hostname === "openmaic-render"',
            "MP4_RENDER_UNAVAILABLE",
            "MP4_RENDER_UNTRUSTED",
            "MP4_RENDER_TIMEOUT",
            'case "classroom_zip"',
            'case "pptx"',
            'case "offline_html"',
            'case "mp4"',
            "hasSignedBodyBinding(signed, parsed)",
            "controlled export inputs are required",
            'exactKeys(record, "export request"',
            "contentOutputRegistry",
            "validateExportOutput(",
            "MP4_RENDER_INVALID_ARTIFACT",
            "controlledArtifactDownloadPath(",
            "inspectAndValidateZipArchive(",
            "createInflateRaw(",
            "archive entry CRC validation failed",
            "PPTX OOXML package is missing required entries",
            "validateMp4Artifact(",
            "readResponseBytesLimited(",
            "cancelRemoteRenderIfRequested(",
            "createOfflineHtmlArtifact(",
            "Content-Security-Policy",
            "connect-src 'none'",
            "assertPublicationActive: publication.assertActive",
        ),
    )
    if "dependencies.fetchExternal(" in export:
        raise OverlayVerificationError(
            "controlled exports must never fetch client-selected locations"
        )
    if export.index("authenticateServiceRequest(request, body") > export.index("JSON.parse(body)"):
        raise OverlayVerificationError(
            "export POST must authenticate before parsing its request body"
        )

    _require_tokens(
        boundary,
        "service-boundary.ts",
        (
            "verifyServiceRequest(signed",
            'request.headers.get("x-yfeistai-tenant-id")',
            'request.headers.get("x-yfeistai-job-id")',
            'request.headers.get("x-yfeistai-idempotency-key")',
            "body.tenantId === signed.tenantId",
            "body.jobId === signed.jobId",
            "body.idempotencyKey === signed.idempotencyKey",
        ),
    )
    _require_tokens(
        classroom_post,
        "classroom POST route",
        (
            "@/lib/generation/scene-generator",
            "@/lib/server/resolve-model",
            "generateSceneContent(",
            "generateSceneActions(",
            "materializeEmbeddedMedia(",
            "buildOpenMaicSourcePrompt(",
            "toPortableOpenMaicSceneContent(",
            "type: context.sceneType",
            "createClassroomPostHandler(",
            "readServiceSecret",
            "return postClassroom(request)",
        ),
    )
    if "generateClassroom(" in classroom_post or "generateOutlines(" in classroom_post:
        raise OverlayVerificationError(
            "classroom POST route must consume the confirmed outline without regenerating it"
        )
    _require_tokens(
        classroom_get,
        "classroom GET route",
        ("createClassroomGetHandler(", "readServiceSecret", "return getClassroom("),
    )
    _require_tokens(
        export_post,
        "export POST route",
        (
            'from "jszip"',
            'from "pptxgenjs"',
            "artifactStore.read(",
            "request.sourceJobId",
            "YFEISTAI_OPENMAIC_RENDER_ENDPOINT",
            "/yfeistai/v1/render",
            'redirect: "error"',
            "await assertRenderActive(context)",
            "MP4_RENDER_INVALID_ARTIFACT",
            "createOfflineHtmlArtifact(",
            "readResponseBytesLimited(",
            "validateMp4Artifact(",
            "cancelRemoteRenderIfRequested(",
            "isMp4MediaType(",
            "createExportPostHandler(",
            "readServiceSecret",
            "return postExport(request)",
        ),
    )
    forbidden_export_adapter_tokens = (
        '"use client"',
        "'use client'",
        "useStageStore",
        "indexedDB",
        "db.",
        "file-saver",
    )
    present = [token for token in forbidden_export_adapter_tokens if token in export_post]
    if present:
        raise OverlayVerificationError(
            "export POST route reads browser/client state: " + ", ".join(present)
        )
    _require_tokens(
        export_get,
        "export GET route",
        ("createExportGetHandler(", "readServiceSecret", "return getExport("),
    )
    _require_tokens(
        cancel_route,
        "cancel route",
        (
            "createJobCancelHandler(",
            "contentJobStore",
            "exportJobStore",
            "readServiceSecret",
            "return cancelJob(",
        ),
    )
    _require_tokens(
        artifact_route,
        "artifact route",
        (
            "createArtifactGetHandler(",
            "artifactStore",
            "readServiceSecret",
            "return getArtifact(",
        ),
    )


def verify_overlay(integration_root: Path = DEFAULT_INTEGRATION_ROOT) -> None:
    integration_root = integration_root.resolve()
    overlay_root = integration_root / "overlay"
    _verify_upstream_manifest(integration_root)
    _verify_dockerfile(integration_root)
    for relative in sorted(REQUIRED_OVERLAY_FILES):
        if not (overlay_root / relative).is_file():
            raise OverlayVerificationError(f"required overlay file is missing: {relative}")
    violations = find_forbidden_overlay_surface(overlay_root)
    if violations:
        raise OverlayVerificationError("\n".join(violations))
    verify_service_auth_source(_read_text(overlay_root / "lib/yfeistai/service-auth.ts"))
    _verify_health_contract(overlay_root)
    _verify_outline_generation(overlay_root)
    verify_task4_sources(overlay_root)


def resolve_package_runner() -> list[str]:
    corepack_name = "corepack.cmd" if os.name == "nt" else "corepack"
    corepack = shutil.which(corepack_name)
    if corepack:
        return [corepack, "pnpm"]
    pnpm_name = "pnpm.cmd" if os.name == "nt" else "pnpm"
    pnpm = shutil.which(pnpm_name)
    if pnpm:
        return [pnpm]
    raise OverlayVerificationError("corepack or pnpm is required to run overlay tests")


def _run_service_auth_tests(integration_root: Path) -> int:
    try:
        runner = resolve_package_runner()
    except OverlayVerificationError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    command = [
        *runner,
        "dlx",
        VITEST_PACKAGE,
        "run",
        "tests/yfeistai/service-auth.test.ts",
        "--environment",
        "node",
    ]
    completed = subprocess.run(command, cwd=integration_root / "overlay", check=False)
    return completed.returncode


def _run_outline_generation_tests(integration_root: Path) -> int:
    try:
        runner = resolve_package_runner()
    except OverlayVerificationError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    command = [
        *runner,
        "dlx",
        VITEST_PACKAGE,
        "run",
        "tests/yfeistai/outline-generation.test.ts",
        "--environment",
        "node",
    ]
    completed = subprocess.run(command, cwd=integration_root / "overlay", check=False)
    return completed.returncode


def _run_task4_tests(integration_root: Path, test_name: str) -> int:
    try:
        runner = resolve_package_runner()
    except OverlayVerificationError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    command = [
        *runner,
        "dlx",
        VITEST_PACKAGE,
        "run",
        f"tests/yfeistai/{test_name}.test.ts",
        "--environment",
        "node",
    ]
    completed = subprocess.run(command, cwd=integration_root / "overlay", check=False)
    return completed.returncode


def _run_pinned_image_build(integration_root: Path) -> int:
    docker = shutil.which("docker")
    if not docker:
        print("docker is required to build the pinned OpenMAIC image", file=sys.stderr)
        return 2
    command = [
        docker,
        "build",
        "--file",
        str(integration_root / "Dockerfile"),
        "--tag",
        f"yfeistai/openmaic:verify-{EXPECTED_UPSTREAM['commit'][:12]}",
        str(ROOT),
    ]
    completed = subprocess.run(command, cwd=ROOT, check=False)
    return completed.returncode


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--test",
        choices=(
            "service-auth",
            "outline-generation",
            "content-generation",
            "cancel",
            "artifact-manifest",
            "export-generation",
            "static",
        ),
        default="static",
        help="verification surface to run",
    )
    parser.add_argument(
        "--build",
        action="store_true",
        help="build the pinned OpenMAIC image after static verification",
    )
    args = parser.parse_args(argv)
    try:
        verify_overlay()
    except OverlayVerificationError as exc:
        print(f"OpenMAIC overlay verification failed: {exc}", file=sys.stderr)
        return 1
    if args.test == "service-auth":
        result = _run_service_auth_tests(DEFAULT_INTEGRATION_ROOT)
        if result:
            return result
    if args.test == "outline-generation":
        result = _run_outline_generation_tests(DEFAULT_INTEGRATION_ROOT)
        if result:
            return result
    if args.test in {
        "content-generation",
        "cancel",
        "artifact-manifest",
        "export-generation",
    }:
        result = _run_task4_tests(DEFAULT_INTEGRATION_ROOT, args.test)
        if result:
            return result
    if args.build:
        result = _run_pinned_image_build(DEFAULT_INTEGRATION_ROOT)
        if result:
            return result
    print("OpenMAIC overlay verified.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

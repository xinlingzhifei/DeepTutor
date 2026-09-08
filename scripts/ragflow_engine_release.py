"""Offline build inputs and local receipt contracts for the branded RAGFlow engine.

This is independent of the platform image lock/archive. The caller must verify
the Actions run and fetch the final manifest through a trusted channel. Passing
their self-reported values here proves consistency, not publication, an applied
branding checkout, image contents, or deployment.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import sys

SOURCE_REPOSITORY = "xinlingzhifei/DeepTutor"
IMAGE_REPOSITORY = "ghcr.io/xinlingzhifei/yfeistai-ragflow"
WORKFLOW = ".github/workflows/private-ragflow-engine-image.yml"
UPSTREAM = {
    "repository": "https://github.com/infiniflow/ragflow.git",
    "tag": "v0.27.1",
    "commit": "b9df87c4c75a5b0d35c90d15329fc0f6f91cb73e",
}
# Raw Dockerfile_ci bytes at UPSTREAM["commit"], independently checked against
# the pinned checkout; line ending changes are deliberately not normalized.
UPSTREAM_RECIPE_SHA256 = "ae86c755169894288eb235e0fba82643bb3550dc93a2b8073e7cbd532102c33e"
BASE_PATTERN = re.compile(
    r"infiniflow/ragflow-base(?::[A-Za-z0-9_][A-Za-z0-9_.-]{0,127})?"
    r"@sha256:([0-9a-f]{64})"
)
RELEASE_PATTERN = re.compile(r"yfeistai-first-release-[0-9]{8}-([0-9a-f]{8})")


def canonical_bytes(document: dict) -> bytes:
    """Canonical UTF-8 JSON: sorted keys, compact separators, one final LF."""
    return (
        json.dumps(
            document, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
        )
        + "\n"
    ).encode("utf-8")


def _sha256(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def _json_object(body: bytes, label: str) -> dict:
    document = json.loads(body)
    if not isinstance(document, dict):
        raise ValueError(f"{label} must be a JSON object")
    return document


def _build_contract(
    repository_root: Path, upstream_recipe: Path, base_image: str
) -> tuple[dict, bytes]:
    base_match = BASE_PATTERN.fullmatch(base_image) if isinstance(base_image, str) else None
    if base_match is None or base_match[1] == "0" * 64:
        raise ValueError("base image must pin a nonzero digest of infiniflow/ragflow-base")

    integration = Path(repository_root) / "integrations/ragflow"
    pin_body = (integration / "UPSTREAM.json").read_bytes()
    pin = _json_object(pin_body, "upstream pin")
    if (
        type(pin.get("schemaVersion")) is not int
        or pin["schemaVersion"] != 1
        or any(pin.get(key) != value for key, value in UPSTREAM.items())
    ):
        raise ValueError("upstream pin differs from the independently pinned engine release")
    manifest_body = (integration / "branding/manifest.json").read_bytes()
    manifest = _json_object(manifest_body, "branding manifest")
    if (
        type(manifest.get("schemaVersion")) is not int
        or manifest["schemaVersion"] != 1
        or manifest.get("brand") != "yFei"
        or manifest.get("upstreamCommit") != UPSTREAM["commit"]
        or manifest.get("textHashNormalization") != "UTF-8 with CRLF normalized to LF"
    ):
        raise ValueError("branding manifest differs from the pinned engine contract")
    patch_body = (integration / "branding/yfei.patch").read_bytes()
    if manifest.get("patchSha256") != _sha256(patch_body):
        raise ValueError("branding patch checksum mismatch")
    apply_body = (integration / "branding/apply-branding.mjs").read_bytes()

    recipe = Path(upstream_recipe).read_bytes()
    if _sha256(recipe) != UPSTREAM_RECIPE_SHA256:
        raise ValueError("upstream recipe bytes do not match the pinned Dockerfile_ci")
    stage_prefix = b"FROM infiniflow/ragflow-base:v2.1 "
    stages = re.findall(rb"^FROM ([^\r\n]+)", recipe, re.MULTILINE)
    if stages != [
        b"infiniflow/ragflow-base:v2.1 AS builder",
        b"infiniflow/ragflow-base:v2.1 AS production",
    ]:
        raise ValueError("upstream recipe must contain exactly the two pinned base stages")
    generated = recipe.replace(stage_prefix, f"FROM {base_image} ".encode("ascii"))
    contract = {
        "schemaVersion": 1,
        "kind": "ragflow-engine-build-inputs",
        "upstream": dict(UPSTREAM),
        "inputs": {
            "upstreamPinSha256": _sha256(pin_body),
            "brandingManifestSha256": _sha256(manifest_body),
            "brandingPatchSha256": _sha256(patch_body),
            "brandingApplySha256": _sha256(apply_body),
            "upstreamRecipePath": "Dockerfile_ci",
            "upstreamRecipeSha256": _sha256(recipe),
            "generatedRecipeSha256": _sha256(generated),
        },
        "baseImage": base_image,
        "platform": "linux/amd64",
        "backendMode": "python",
    }
    return contract, generated


def prepare_build_inputs(
    *, repository_root: Path, upstream_recipe: Path, base_image: str, output_dir: Path
) -> dict:
    """Write a new recipe and input contract, never modify the source recipe.

    This binds the branding tools, not the applied checkout. CI must apply and
    verify the branding separately before building from that checkout.
    """
    contract, generated = _build_contract(repository_root, upstream_recipe, base_image)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=False)
    with (output_dir / "Dockerfile").open("xb") as handle:
        handle.write(generated)
    with (output_dir / "build-inputs.json").open("xb") as handle:
        handle.write(canonical_bytes(contract))
    return contract


def create_receipt(
    *,
    repository_root: Path,
    upstream_recipe: Path,
    base_image: str,
    prepared_dir: Path,
    source_repository: str,
    source_head: str,
    release_tag: str,
    run_id: str,
    run_attempt: int,
    image: str,
    build_digest: str,
    remote_manifest: bytes,
) -> dict:
    """Bind explicit candidate/run identities and caller-supplied manifest bytes.

    No caller parameter is an authentication mechanism. The platform head must
    independently identify these source files; the Actions run, applied branding
    checkout, and final remote fetch must be checked outside this local contract.
    """
    if source_repository != SOURCE_REPOSITORY:
        raise ValueError("source repository must be xinlingzhifei/DeepTutor")
    if (
        not isinstance(source_head, str)
        or re.fullmatch(r"[0-9a-f]{40}", source_head) is None
        or source_head == "0" * 40
    ):
        raise ValueError("source head must be a nonzero full commit")
    tag_match = RELEASE_PATTERN.fullmatch(release_tag) if isinstance(release_tag, str) else None
    if tag_match is None or tag_match[1] != source_head[:8]:
        raise ValueError("release tag must identify the same source head")
    if not isinstance(run_id, str) or re.fullmatch(r"[1-9][0-9]*", run_id) is None:
        raise ValueError("Actions run ID must be a canonical positive decimal string")
    if type(run_attempt) is not int or run_attempt != 1:
        raise ValueError("Actions run attempt must be exactly 1")
    if (
        not isinstance(build_digest, str)
        or re.fullmatch(r"sha256:[0-9a-f]{64}", build_digest) is None
        or build_digest == "sha256:" + "0" * 64
    ):
        raise ValueError("build digest must be a nonzero sha256 digest")
    if (
        not isinstance(remote_manifest, bytes)
        or not remote_manifest
        or "sha256:" + _sha256(remote_manifest) != build_digest
    ):
        raise ValueError("final raw remote manifest bytes do not match the build digest")
    if image != f"{IMAGE_REPOSITORY}:{release_tag}@{build_digest}":
        raise ValueError("output image must bind the engine repository, candidate tag, and digest")

    contract, generated = _build_contract(repository_root, upstream_recipe, base_image)
    prepared_dir = Path(prepared_dir)
    if (prepared_dir / "Dockerfile").read_bytes() != generated or (
        prepared_dir / "build-inputs.json"
    ).read_bytes() != canonical_bytes(contract):
        raise ValueError("prepared build inputs differ from the current pinned source contract")
    return {
        **contract,
        "kind": "ragflow-engine-publication",
        "sourceRepository": source_repository,
        "sourceHead": source_head,
        "releaseTag": release_tag,
        "image": image,
        "remoteManifestSha256": _sha256(remote_manifest),
        "ci": {"workflow": WORKFLOW, "runId": run_id, "runAttempt": run_attempt},
    }


def write_receipt(*, output: Path, **receipt_inputs) -> dict:
    """Create an independent local receipt; existing outputs are never replaced."""
    receipt = create_receipt(**receipt_inputs)
    with Path(output).open("xb") as handle:
        handle.write(canonical_bytes(receipt))
    return receipt


def validate_receipt(body: bytes, **receipt_inputs) -> dict:
    """Validate ONLY the local contract against explicit expected inputs.

    Byte equality with the reconstructed schema rejects duplicate/unknown keys,
    changed scalar types, BOMs, reordered keys, and whitespace/noncanonical JSON.
    This never asserts trusted Actions provenance or live remote publication.
    """
    expected = create_receipt(**receipt_inputs)
    if not isinstance(body, bytes) or body != canonical_bytes(expected):
        raise ValueError("engine receipt is noncanonical or differs from the expected contract")
    return expected


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for command in ("prepare", "write-receipt", "validate-receipt"):
        subparser = commands.add_parser(command)
        subparser.add_argument("--repository-root", type=Path, required=True)
        subparser.add_argument("--upstream-recipe", type=Path, required=True)
        subparser.add_argument("--base-image", required=True)
        if command == "prepare":
            subparser.add_argument("--output-dir", type=Path, required=True)
            continue
        subparser.add_argument("--prepared-dir", type=Path, required=True)
        subparser.add_argument("--source-repository", required=True)
        subparser.add_argument("--source-head", required=True)
        subparser.add_argument("--release-tag", required=True)
        subparser.add_argument("--run-id", required=True)
        subparser.add_argument("--run-attempt", type=int, required=True)
        subparser.add_argument("--image", required=True)
        subparser.add_argument("--build-digest", required=True)
        subparser.add_argument("--remote-manifest", type=Path, required=True)
        if command == "write-receipt":
            subparser.add_argument("--output", type=Path, required=True)
        else:
            subparser.add_argument("--receipt", type=Path, required=True)
    arguments = vars(parser.parse_args(argv))
    command = arguments.pop("command")
    try:
        if command == "prepare":
            prepare_build_inputs(**arguments)
        else:
            arguments["remote_manifest"] = arguments["remote_manifest"].read_bytes()
            if command == "write-receipt":
                write_receipt(**arguments)
            else:
                body = arguments.pop("receipt").read_bytes()
                validate_receipt(body, **arguments)
    except (OSError, ValueError) as exc:
        print(f"RAGFlow engine release rejected: {exc}", file=sys.stderr)
        return 1
    print(
        "Local contract only; applied branding, trusted Actions and publication are not verified."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

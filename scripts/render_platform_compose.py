#!/usr/bin/env python
"""Render and verify digest-pinned private platform Compose configuration."""

from __future__ import annotations

import argparse
from collections.abc import Callable, Mapping, Sequence
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
IMAGE_LOCK_PATH = PROJECT_ROOT / "deploy" / "image-lock.json"
DIGEST_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
COMPOSE_IMAGE_NAMES = {
    "docker-compose.platform.yml": (
        "deeptutor",
        "openmaic",
        "openmaic_render",
        "nginx",
        "postgres",
        "minio",
        "minio_client",
    ),
    "docker-compose.data-plane.yml": ("openmaic", "openmaic_render"),
}

IMAGE_SPECS: dict[str, dict[str, Any]] = {
    "deeptutor": {
        "repository": "ghcr.io/xinlingzhifei/deeptutor",
        "tag": "first-release",
    },
    "openmaic": {
        "repository": "ghcr.io/xinlingzhifei/openmaic",
        "tag": "0.3.1-0cf2a330",
        "source": {
            "repository": "https://github.com/xinlingzhifei/OpenMAIC.git",
            "revision": "0cf2a330411681190e89f48e20f305345ff99f87",
            "dockerfile": "integrations/openmaic/Dockerfile",
        },
    },
    "openmaic_render": {
        "repository": "ghcr.io/xinlingzhifei/openmaic-render",
        "tag": "0.3.1-0cf2a330",
        "source": {
            "repository": "https://github.com/xinlingzhifei/OpenMAIC.git",
            "revision": "0cf2a330411681190e89f48e20f305345ff99f87",
            "dockerfile": "render-service/Dockerfile",
        },
    },
    "nginx": {
        "repository": "nginx",
        "tag": "1.29.8-alpine3.23",
    },
    "postgres": {
        "repository": "postgres",
        "tag": "16.14-alpine3.24",
    },
    "minio": {
        "repository": "minio/minio",
        "tag": "RELEASE.2025-04-22T22-12-26Z",
    },
    "minio_client": {
        "repository": "minio/mc",
        "tag": "RELEASE.2025-04-16T18-13-26Z",
    },
}


def _tagged_reference(spec: Mapping[str, Any]) -> str:
    return f"{spec['repository']}:{spec['tag']}"


def _registry_digest(reference: str) -> str | None:
    docker = shutil.which("docker")
    if not docker:
        raise ValueError("docker was not found on PATH while resolving registry digest")
    result = subprocess.run(
        [
            docker,
            "buildx",
            "imagetools",
            "inspect",
            "--raw",
            reference,
        ],
        cwd=str(PROJECT_ROOT),
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        return None
    if not isinstance(result.stdout, bytes):
        return None
    try:
        manifest = json.loads(result.stdout)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(manifest, dict):
        return None
    return "sha256:" + hashlib.sha256(result.stdout).hexdigest()


def _render_compose_references(
    compose_path: Path,
    images: Mapping[str, Mapping[str, Any]],
) -> str:
    image_names = COMPOSE_IMAGE_NAMES.get(compose_path.name)
    if image_names is None:
        raise ValueError(f"unsupported production Compose file: {compose_path.name}")
    try:
        rendered = compose_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise ValueError(f"production Compose file could not be read: {compose_path.name}") from exc
    for name in image_names:
        tagged_reference = _tagged_reference(IMAGE_SPECS[name])
        pattern = re.compile(rf"{re.escape(tagged_reference)}@sha256:[0-9a-f]{{64}}")
        rendered, count = pattern.subn(str(images[name]["reference"]), rendered)
        if count != 1:
            raise ValueError(f"production Compose image reference is invalid for {name}")
    return rendered


def _stage_text(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        newline="\n",
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
        delete=False,
    ) as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())
        return Path(handle.name)


def _restore_bytes(path: Path, content: bytes) -> None:
    with path.open("wb") as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())


def write_image_lock(
    output_path: Path = IMAGE_LOCK_PATH,
    *,
    digest_resolver: Callable[[str], str | None] = _registry_digest,
    compose_paths: Sequence[Path] = (),
) -> dict[str, Any]:
    """Record one resolved image set in the lock and selected Compose files."""
    images: dict[str, dict[str, Any]] = {}
    for name, spec in IMAGE_SPECS.items():
        tagged_reference = _tagged_reference(spec)
        digest = digest_resolver(tagged_reference)
        if digest is None or not DIGEST_PATTERN.fullmatch(digest):
            raise ValueError(f"registry digest is unavailable for image {name}")
        record: dict[str, Any] = {
            "repository": spec["repository"],
            "tag": spec["tag"],
            "digest": digest,
            "reference": f"{tagged_reference}@{digest}",
        }
        source = spec.get("source")
        if isinstance(source, dict):
            record["source"] = dict(source)
        images[name] = record

    document = {"schemaVersion": 1, "images": images}
    rendered_compose = {
        Path(compose_path): _render_compose_references(Path(compose_path), images)
        for compose_path in compose_paths
    }
    publish_paths = (*rendered_compose, output_path)
    original_bytes: dict[Path, bytes | None] = {}
    for publish_path in publish_paths:
        try:
            original_bytes[publish_path] = publish_path.read_bytes()
        except FileNotFoundError:
            original_bytes[publish_path] = None
    temporary_paths: dict[Path, Path] = {}
    published_paths: list[Path] = []
    try:
        for compose_path, rendered in rendered_compose.items():
            temporary_paths[compose_path] = _stage_text(compose_path, rendered)
        lock_text = (
            json.dumps(
                document,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            + "\n"
        )
        temporary_paths[output_path] = _stage_text(output_path, lock_text)
        for compose_path in rendered_compose:
            os.replace(temporary_paths[compose_path], compose_path)
            published_paths.append(compose_path)
            temporary_paths.pop(compose_path)
        os.replace(temporary_paths[output_path], output_path)
        published_paths.append(output_path)
        temporary_paths.pop(output_path)
    except BaseException as publish_error:
        rollback_errors: list[OSError] = []
        for published_path in reversed(published_paths):
            try:
                original = original_bytes[published_path]
                if original is None:
                    published_path.unlink(missing_ok=True)
                else:
                    _restore_bytes(published_path, original)
            except OSError as rollback_error:
                rollback_errors.append(rollback_error)
        if rollback_errors:
            raise RuntimeError(
                "image lock publish failed and rollback was incomplete"
            ) from publish_error
        raise
    finally:
        for temporary_path in temporary_paths.values():
            temporary_path.unlink(missing_ok=True)
    return document


def load_image_lock(lock_path: Path = IMAGE_LOCK_PATH) -> dict[str, Any]:
    try:
        document = json.loads(lock_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("image lock could not be read") from exc
    images = document.get("images") if isinstance(document, dict) else None
    if not isinstance(images, dict):
        raise ValueError("image lock is invalid")
    for name, spec in IMAGE_SPECS.items():
        record = images.get(name)
        if not isinstance(record, dict):
            raise ValueError(f"image lock is missing {name}")
        digest = record.get("digest")
        expected = (
            f"{spec['repository']}:{spec['tag']}@{digest}" if isinstance(digest, str) else None
        )
        if (
            not isinstance(digest, str)
            or not DIGEST_PATTERN.fullmatch(digest)
            or digest == "sha256:" + ("0" * 64)
            or record.get("repository") != spec["repository"]
            or record.get("tag") != spec["tag"]
            or record.get("reference") != expected
        ):
            raise ValueError(f"image lock entry is invalid for {name}")
    return document


def validate_image_lock_bindings(
    lock_path: Path = IMAGE_LOCK_PATH,
    *,
    compose_paths: Sequence[Path] = (),
) -> dict[str, Any]:
    lock = load_image_lock(lock_path)
    images = lock["images"]
    for compose_path in compose_paths:
        path = Path(compose_path)
        try:
            current = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise ValueError(f"production Compose file could not be read: {path.name}") from exc
        expected = _render_compose_references(path, images)
        if current != expected:
            raise ValueError("production Compose image references do not match image lock")
    return lock


def image_reference(name: str, *, lock_path: Path = IMAGE_LOCK_PATH) -> str:
    if name not in IMAGE_SPECS:
        raise ValueError(f"unknown image name: {name}")
    lock = load_image_lock(lock_path)
    return str(lock["images"][name]["reference"])


def load_rendered_compose(
    *compose_files: str | Path,
    project_root: Path = PROJECT_ROOT,
    environment: Mapping[str, str] | None = None,
    profiles: Sequence[str] = (),
) -> dict[str, Any]:
    """Return Docker Compose's authoritative merged model as JSON."""
    if not compose_files:
        raise ValueError("at least one Compose file is required")
    docker = shutil.which("docker")
    if not docker:
        raise RuntimeError("docker was not found on PATH")
    command = [
        docker,
        "compose",
        "--project-name",
        "yfeistai-config-check",
        "--project-directory",
        str(project_root),
    ]
    for compose_file in compose_files:
        path = Path(compose_file)
        if not path.is_absolute():
            path = project_root / path
        command.extend(("-f", str(path)))
    for profile in profiles:
        command.extend(("--profile", profile))
    command.extend(("config", "--format", "json"))

    process_environment = os.environ.copy()
    process_environment["COMPOSE_DISABLE_ENV_FILE"] = "1"
    if environment:
        process_environment.update(environment)
    result = subprocess.run(
        command,
        cwd=str(project_root),
        env=process_environment,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        diagnostic = result.stderr.strip().splitlines()
        detail = diagnostic[-1] if diagnostic else "unknown Compose error"
        raise RuntimeError(f"docker compose config failed: {detail}")
    try:
        rendered = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("docker compose config did not return JSON") from exc
    if not isinstance(rendered, dict):
        raise RuntimeError("docker compose config returned an invalid model")
    return rendered


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--write-image-lock", action="store_true")
    action.add_argument("--print-image", choices=tuple(IMAGE_SPECS))
    parser.add_argument("--lock-path", type=Path, default=IMAGE_LOCK_PATH)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    if args.write_image_lock:
        write_image_lock(
            args.lock_path,
            compose_paths=(
                PROJECT_ROOT / "docker-compose.platform.yml",
                PROJECT_ROOT / "docker-compose.data-plane.yml",
            ),
        )
        print(args.lock_path)
        return 0
    print(image_reference(args.print_image, lock_path=args.lock_path))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

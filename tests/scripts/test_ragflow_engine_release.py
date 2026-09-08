"""Offline contracts for the independent Python RAGFlow engine publication."""

from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil

import pytest

ROOT = Path(__file__).resolve().parents[2]
SOURCE_HEAD = "a" * 40
RELEASE_TAG = f"yfeistai-first-release-20260907-{SOURCE_HEAD[:8]}"
BASE_IMAGE = "infiniflow/ragflow-base:v2.1@sha256:" + "b" * 64
MANIFEST = b'{"schemaVersion":2,"mediaType":"application/vnd.oci.image.manifest.v1+json"}'
BUILD_DIGEST = "sha256:" + hashlib.sha256(MANIFEST).hexdigest()
IMAGE = f"ghcr.io/xinlingzhifei/yfeistai-ragflow:{RELEASE_TAG}@{BUILD_DIGEST}"
# Exact bytes of Dockerfile_ci at the pinned upstream commit, not an ignored checkout dependency.
PINNED_RECIPE = r"""# builder stage
FROM infiniflow/ragflow-base:v2.1 AS builder
USER root
SHELL ["/bin/bash", "-c"]
ARG NEED_MIRROR=0

WORKDIR /ragflow

# install dependencies from uv.lock file
COPY pyproject.toml uv.lock ./

# https://github.com/astral-sh/uv/issues/10462
# uv records index url into uv.lock but doesn't failover among multiple indexes
# Also rewrite pypi.tuna.tsinghua.edu.cn to mirrors.aliyun.com/pypi so locks
# that were resolved against the Tsinghua mirror (e.g. when UV_INDEX pointed
# there) get normalized to the Aliyun mirror in NEED_MIRROR=1 builds. Without
# this, stale Tsinghua URLs slip through and `uv sync --frozen` 404s on
# packages that the Tsinghua mirror no longer carries.
RUN --mount=type=cache,id=ragflow_uv,target=/root/.cache/uv,sharing=locked \
    if [ "$NEED_MIRROR" == "1" ]; then \
        sed -i 's|pypi.org|mirrors.aliyun.com/pypi|g' uv.lock; \
        sed -i 's|pypi.tuna.tsinghua.edu.cn|mirrors.aliyun.com/pypi|g' uv.lock; \
    else \
        sed -i 's|mirrors.aliyun.com/pypi|pypi.org|g' uv.lock; \
        sed -i 's|pypi.tuna.tsinghua.edu.cn|pypi.org|g' uv.lock; \
        sed -i 's|gitee.com|github.com|g' uv.lock; \
    fi; \
    # --refresh-package litellm forces a re-download of litellm from the
    # (post-sed) URLs in uv.lock even if BuildKit's persistent uv cache mount
    # holds a stale wheel from a previous build. litellm 1.88.x has had
    # multiple internal ImportError issues (1.88.1 missing
    # DEFAULT_HEALTH_CHECK_STALENESS_MULTIPLIER, 1.88.0 wheel pulled via
    # some proxies missing RedisPipelineLpopOperation) — always re-fetching
    # the locked version avoids serving a half-broken cached copy.
    uv sync --python 3.13 --frozen --refresh-package litellm && \
    # Ensure pip is available in the venv for runtime package installation (fixes #12651)
    .venv/bin/python3 -m ensurepip --upgrade

# Install frontend dependencies — depends only on package manifests so
# web source / docs changes don't invalidate this layer.
COPY web/package.json web/package-lock.json web/.npmrc ./web/
RUN --mount=type=cache,id=ragflow_npm,target=/root/.npm,sharing=locked \
    cd web && NODE_OPTIONS="--max-old-space-size=8192" npm install

# Copy full web source and docs for the frontend build.
COPY web web
COPY docs docs
RUN --mount=type=cache,id=ragflow_npm,target=/root/.npm,sharing=locked \
    cd web && NODE_OPTIONS="--max-old-space-size=8192" VITE_BUILD_SOURCEMAP=false VITE_MINIFY=esbuild npm run build

# Get version from git (mount .git directory to compute version dynamically)
RUN --mount=type=bind,source=.git,target=/ragflow/.git \
    version_info=$(git describe --tags --match=v* --first-parent --always); \
    echo "RAGFlow version: $version_info"; \
    echo "$version_info" > /ragflow/VERSION

# production stage
FROM infiniflow/ragflow-base:v2.1 AS production
USER root

WORKDIR /ragflow

# Copy Python environment and packages
ENV VIRTUAL_ENV=/ragflow/.venv
COPY --from=builder ${VIRTUAL_ENV} ${VIRTUAL_ENV}
ENV PATH="${VIRTUAL_ENV}/bin:${PATH}"

ENV PYTHONPATH=/ragflow/
# copy entrypoint.sh and entrypoint-pasar.sh
COPY docker/service_conf.yaml.template ./conf/service_conf.yaml.template
COPY docker/entrypoint*.sh ./
RUN chmod +x ./entrypoint*.sh


ARG NGINX_VERSION=1.31.3-1~noble
RUN --mount=type=cache,id=ragflow_apt,target=/var/cache/apt,sharing=locked \
    mkdir -p /etc/apt/keyrings && \
    curl --retry 5 --retry-delay 2 --retry-all-errors -fsSL https://nginx.org/keys/nginx_signing.key | gpg --dearmor -o /etc/apt/keyrings/nginx-archive-keyring.gpg && \
    echo "deb [signed-by=/etc/apt/keyrings/nginx-archive-keyring.gpg] https://nginx.org/packages/mainline/ubuntu/ noble nginx" > /etc/apt/sources.list.d/nginx.list && \
    apt -o Acquire::Retries=5 update && \
    apt -o Acquire::Retries=5 install -y nginx=${NGINX_VERSION} && \
    apt-mark hold nginx


# Copy nginx configuration for frontend serving
# OpenResty installs to /usr/local/openresty/nginx/; create /etc/nginx/ symlink tree
RUN mkdir -p /etc/nginx/conf.d /var/log/nginx

COPY docker/nginx/nginx.conf docker/nginx/proxy.conf /etc/nginx/
COPY docker/nginx/ragflow.conf.golang \
     docker/nginx/ragflow.conf.python \
     docker/nginx/ragflow.conf.hybrid \
     /etc/nginx/conf.d/

RUN rm -f /etc/nginx/sites-enabled/default

COPY admin admin
COPY api api
COPY conf conf
COPY deepdoc deepdoc
COPY rag rag
COPY agent agent
COPY pyproject.toml uv.lock ./
COPY mcp mcp
COPY common common
COPY memory memory
COPY bin bin
COPY tools/scripts tools/scripts

# Copy compiled web pages
COPY --from=builder /ragflow/web/dist /ragflow/web/dist

# Copy version info
COPY --from=builder /ragflow/VERSION /ragflow/VERSION

# Set environment variables
ENV HF_ENDPOINT=https://hf-mirror.com

ENTRYPOINT ["./entrypoint.sh"]
""".encode("utf-8")


def _release():
    path = ROOT / "scripts/ragflow_engine_release.py"
    assert path.is_file(), "independent RAGFlow engine release implementation is missing"
    spec = importlib.util.spec_from_file_location("ragflow_engine_release", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def inputs(tmp_path):
    root = tmp_path / "platform"
    integration = root / "integrations/ragflow"
    (integration / "branding").mkdir(parents=True)
    for relative in (
        "UPSTREAM.json",
        "branding/manifest.json",
        "branding/yfei.patch",
        "branding/apply-branding.mjs",
    ):
        shutil.copyfile(ROOT / "integrations/ragflow" / relative, integration / relative)
    recipe = tmp_path / "Dockerfile_ci"
    recipe.write_bytes(PINNED_RECIPE)
    return {
        "repository_root": root,
        "upstream_recipe": recipe,
        "base_image": BASE_IMAGE,
        "output_dir": tmp_path / "prepared",
    }


def _prepared(inputs):
    release = _release()
    release.prepare_build_inputs(**inputs)
    return release, {
        "repository_root": inputs["repository_root"],
        "upstream_recipe": inputs["upstream_recipe"],
        "base_image": inputs["base_image"],
        "prepared_dir": inputs["output_dir"],
        "source_repository": "xinlingzhifei/DeepTutor",
        "source_head": SOURCE_HEAD,
        "release_tag": RELEASE_TAG,
        "run_id": "123456789",
        "run_attempt": 1,
        "image": IMAGE,
        "build_digest": BUILD_DIGEST,
        "remote_manifest": MANIFEST,
    }


def test_prepare_pins_exactly_two_stages_without_editing_original(inputs):
    release = _release()
    original = inputs["upstream_recipe"].read_bytes()
    contract = release.prepare_build_inputs(**inputs)
    generated = (inputs["output_dir"] / "Dockerfile").read_bytes()
    expected = original.replace(b"infiniflow/ragflow-base:v2.1", BASE_IMAGE.encode())
    assert generated == expected
    assert generated.count(BASE_IMAGE.encode()) == 2
    assert inputs["upstream_recipe"].read_bytes() == original
    assert contract["platform"] == "linux/amd64"
    assert contract["backendMode"] == "python"
    assert contract["upstream"]["commit"] == "b9df87c4c75a5b0d35c90d15329fc0f6f91cb73e"
    assert contract["inputs"]["upstreamRecipeSha256"] == hashlib.sha256(original).hexdigest()
    assert contract["inputs"]["generatedRecipeSha256"] == hashlib.sha256(generated).hexdigest()
    assert (
        contract["inputs"]["brandingApplySha256"]
        == hashlib.sha256(
            (
                inputs["repository_root"] / "integrations/ragflow/branding/apply-branding.mjs"
            ).read_bytes()
        ).hexdigest()
    )
    assert (inputs["output_dir"] / "build-inputs.json").read_bytes() == release.canonical_bytes(
        contract
    )
    assert "brandingVerified" not in contract


@pytest.mark.parametrize(
    "base",
    [
        "infiniflow/ragflow-base:v2.1",
        "infiniflow/ragflow-base:v2.1@sha256:" + "0" * 64,
        "infiniflow/ragflow-base@sha256:" + "b" * 63,
        "evil.example/infiniflow/ragflow-base@sha256:" + "b" * 64,
        "infiniflow/ragflow-base-extra@sha256:" + "b" * 64,
        "infiniflow/ragflow-base@sha256:" + "B" * 64,
        BASE_IMAGE + "\n",
    ],
)
def test_prepare_rejects_unpinned_zero_or_wrong_repository_base(inputs, base):
    with pytest.raises(ValueError, match="base image"):
        _release().prepare_build_inputs(**{**inputs, "base_image": base})
    assert not inputs["output_dir"].exists()


@pytest.mark.parametrize("base", ["infiniflow/ragflow-base@sha256:" + "b" * 64, BASE_IMAGE])
def test_prepare_accepts_digest_reference_with_optional_tag(inputs, base):
    result = _release().prepare_build_inputs(**{**inputs, "base_image": base})
    assert result["baseImage"] == base


@pytest.mark.parametrize(
    "mutate",
    [
        lambda recipe: recipe + b"\n# tampered\n",
        lambda recipe: recipe.replace(b"FROM infiniflow", b"FROM attacker", 1),
        lambda recipe: recipe.replace(b"v2.1", b"v2.2"),
        lambda recipe: recipe.replace(b"\n", b"\r\n"),
    ],
)
def test_prepare_rejects_recipe_bytes_not_at_pinned_commit(inputs, mutate):
    inputs["upstream_recipe"].write_bytes(mutate(PINNED_RECIPE))
    with pytest.raises(ValueError, match="recipe"):
        _release().prepare_build_inputs(**inputs)
    assert not inputs["output_dir"].exists()


@pytest.mark.parametrize(
    ("relative", "field", "value"),
    [
        ("UPSTREAM.json", "commit", "c" * 40),
        ("UPSTREAM.json", "tag", "v0.27.2"),
        ("UPSTREAM.json", "repository", "https://example.test/ragflow.git"),
        ("branding/manifest.json", "upstreamCommit", "d" * 40),
        ("branding/manifest.json", "patchSha256", "0" * 64),
    ],
)
def test_prepare_rejects_pin_or_branding_mismatch(inputs, relative, field, value):
    path = inputs["repository_root"] / "integrations/ragflow" / relative
    document = json.loads(path.read_bytes())
    document[field] = value
    path.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(ValueError, match="pin|branding"):
        _release().prepare_build_inputs(**inputs)
    assert not inputs["output_dir"].exists()


def test_prepare_rejects_tampered_patch(inputs):
    patch = inputs["repository_root"] / "integrations/ragflow/branding/yfei.patch"
    patch.write_bytes(patch.read_bytes() + b"\n")
    with pytest.raises(ValueError, match="patch"):
        _release().prepare_build_inputs(**inputs)


def test_prepare_preserves_existing_output(inputs):
    inputs["output_dir"].mkdir()
    sentinel = inputs["output_dir"] / "existing.txt"
    sentinel.write_text("user material", encoding="utf-8")
    with pytest.raises(FileExistsError):
        _release().prepare_build_inputs(**inputs)
    assert sentinel.read_text() == "user material"
    assert list(inputs["output_dir"].iterdir()) == [sentinel]


def test_receipt_binds_candidate_inputs_and_raw_remote_manifest(inputs, tmp_path):
    release, args = _prepared(inputs)
    output = tmp_path / "receipt.json"
    receipt = release.write_receipt(output=output, **args)
    assert receipt["schemaVersion"] == 1
    assert receipt["kind"] == "ragflow-engine-publication"
    assert receipt["sourceRepository"] == "xinlingzhifei/DeepTutor"
    assert receipt["sourceHead"] == SOURCE_HEAD
    assert receipt["releaseTag"] == RELEASE_TAG
    assert receipt["image"] == IMAGE
    assert receipt["remoteManifestSha256"] == BUILD_DIGEST.removeprefix("sha256:")
    assert receipt["ci"] == {
        "workflow": ".github/workflows/private-ragflow-engine-image.yml",
        "runId": "123456789",
        "runAttempt": 1,
    }
    assert release.validate_receipt(output.read_bytes(), **args) == receipt
    assert "publicationVerified" not in receipt


@pytest.mark.parametrize(
    ("field", "value", "error"),
    [
        ("source_repository", "xinlingzhifei/yFeiSTAI", "source repository"),
        ("source_head", "0" * 40, "source head"),
        ("source_head", "b" * 40, "release tag"),
        ("release_tag", "candidate-elsewhere", "release tag"),
        ("run_id", "0", "run ID"),
        ("run_id", "001", "run ID"),
        ("run_id", 123, "run ID"),
        ("run_attempt", 2, "attempt"),
        ("run_attempt", True, "attempt"),
        ("image", IMAGE.replace(RELEASE_TAG, "another-tag"), "image"),
        ("image", IMAGE.replace("yfeistai-ragflow", "deeptutor"), "image"),
        ("build_digest", "sha256:" + "0" * 64, "digest"),
        ("build_digest", "sha256:" + "c" * 64, "manifest"),
        ("remote_manifest", MANIFEST + b"\n", "manifest"),
    ],
)
def test_receipt_rejects_candidate_attempt_digest_or_manifest_drift(inputs, field, value, error):
    release, args = _prepared(inputs)
    with pytest.raises(ValueError, match=error):
        release.create_receipt(**{**args, field: value})


@pytest.mark.parametrize("filename", ["Dockerfile", "build-inputs.json"])
def test_receipt_rejects_modified_prepared_outputs(inputs, filename):
    release, args = _prepared(inputs)
    path = args["prepared_dir"] / filename
    path.write_bytes(path.read_bytes() + b"\n")
    with pytest.raises(ValueError, match="prepared"):
        release.create_receipt(**args)


@pytest.mark.parametrize(
    "relative",
    ["UPSTREAM.json", "branding/manifest.json", "branding/apply-branding.mjs"],
)
def test_receipt_rejects_changed_bound_source_files(inputs, relative):
    release, args = _prepared(inputs)
    path = inputs["repository_root"] / "integrations/ragflow" / relative
    path.write_bytes(path.read_bytes() + b"\n")
    with pytest.raises(ValueError, match="prepared"):
        release.create_receipt(**args)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda body: body.rstrip(b"\n"),
        lambda body: body + b"\n",
        lambda body: b"\xef\xbb\xbf" + body,
        lambda body: json.dumps(json.loads(body), indent=2).encode(),
        lambda body: body.replace(b'"schemaVersion":1', b'"schemaVersion":true'),
        lambda body: body.replace(b'"schemaVersion":1', b'"schemaVersion":1,"schemaVersion":1'),
        lambda body: body.replace(b'"schemaVersion":1', b'"schemaVersion":1,"extra":true'),
        lambda body: body.replace(b'"linux/amd64"', b'"linux/arm64"'),
        lambda body: body.replace(b'"python"', b'"golang"'),
        lambda body: body.replace(b'"runAttempt":1', b'"runAttempt":2'),
        lambda body: body.replace(b'"runId":"123456789"', b'"runId":"123456780"'),
    ],
)
def test_receipt_validation_rejects_noncanonical_or_tampered_bytes(inputs, mutate):
    release, args = _prepared(inputs)
    body = release.canonical_bytes(release.create_receipt(**args))
    with pytest.raises(ValueError, match="receipt"):
        release.validate_receipt(mutate(body), **args)


def test_receipt_validation_rejects_rebound_candidate(inputs):
    release, args = _prepared(inputs)
    body = release.canonical_bytes(release.create_receipt(**args))
    changed = copy.deepcopy(args)
    changed["source_head"] = "a" * 8 + "b" * 32
    with pytest.raises(ValueError, match="receipt"):
        release.validate_receipt(body, **changed)


def test_receipt_write_does_not_overwrite_existing_file(inputs, tmp_path):
    release, args = _prepared(inputs)
    output = tmp_path / "receipt.json"
    output.write_bytes(b"existing user receipt")
    with pytest.raises(FileExistsError):
        release.write_receipt(output=output, **args)
    assert output.read_bytes() == b"existing user receipt"


def test_cli_prepare_write_and_validate_are_offline_local_contract_checks(inputs, tmp_path, capsys):
    release = _release()
    common = [
        "--repository-root",
        str(inputs["repository_root"]),
        "--upstream-recipe",
        str(inputs["upstream_recipe"]),
        "--base-image",
        BASE_IMAGE,
    ]
    assert release.main(["prepare", *common, "--output-dir", str(inputs["output_dir"])]) == 0
    manifest = tmp_path / "remote-manifest.json"
    manifest.write_bytes(MANIFEST)
    output = tmp_path / "receipt.json"
    receipt_args = [
        *common,
        "--prepared-dir",
        str(inputs["output_dir"]),
        "--source-repository",
        "xinlingzhifei/DeepTutor",
        "--source-head",
        SOURCE_HEAD,
        "--release-tag",
        RELEASE_TAG,
        "--run-id",
        "123456789",
        "--run-attempt",
        "1",
        "--image",
        IMAGE,
        "--build-digest",
        BUILD_DIGEST,
        "--remote-manifest",
        str(manifest),
    ]
    assert release.main(["write-receipt", *receipt_args, "--output", str(output)]) == 0
    assert release.main(["validate-receipt", *receipt_args, "--receipt", str(output)]) == 0
    assert "local contract only" in capsys.readouterr().out.lower()


def test_cli_rejects_bad_inputs_without_publication_claim(inputs, capsys):
    release = _release()
    code = release.main(
        [
            "prepare",
            "--repository-root",
            str(inputs["repository_root"]),
            "--upstream-recipe",
            str(inputs["upstream_recipe"]),
            "--base-image",
            "infiniflow/ragflow-base:v2.1",
            "--output-dir",
            str(inputs["output_dir"]),
        ]
    )
    assert code == 1
    assert "base image" in capsys.readouterr().err

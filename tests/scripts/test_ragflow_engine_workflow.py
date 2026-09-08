"""Static and native Linux guardrails for the once-only RAGFlow engine build.

These checks do not build an image, contact a registry, or prove runtime health.
"""

from __future__ import annotations

import ast
from pathlib import Path
import re
import shutil
import subprocess
import sys

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github/workflows/private-ragflow-engine-image.yml"
IMAGE = "ghcr.io/xinlingzhifei/yfeistai-ragflow"
UPSTREAM = "b9df87c4c75a5b0d35c90d15329fc0f6f91cb73e"
CHECKOUT = "actions/checkout@11d5960a326750d5838078e36cf38b85af677262"
BUILD = "docker/build-push-action@10e90e3645eae34f1e60eeb005ba3a3d33f178e8"
UPLOAD = "actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02"


def _workflow():
    assert WORKFLOW.is_file(), "independent RAGFlow engine workflow is missing"
    source = WORKFLOW.read_text(encoding="utf-8")
    workflow = yaml.safe_load(source)
    assert isinstance(workflow, dict)
    assert len(workflow["jobs"]) == 1
    job = next(iter(workflow["jobs"].values()))
    return source, workflow, job, job["steps"]


def _step(steps, name):
    return next(step for step in steps if step.get("name") == name)


def test_only_once_only_release_tag_pushes_run_on_serialized_standard_runner():
    source, workflow, job, steps = _workflow()
    assert workflow.get("on", workflow.get(True)) == {
        "push": {"tags": ["yfeistai-first-release-*"]}
    }
    assert workflow["permissions"] == {"contents": "read", "packages": "write"}
    assert job["runs-on"] == "ubuntu-latest"
    assert job["timeout-minutes"] == 120
    validation = _step(steps, "Validate immutable release candidate")
    for constraint in (
        'test "$GITHUB_EVENT_NAME" = "push"',
        'test "$GITHUB_RUN_ATTEMPT" = "1"',
        'test "$GITHUB_REPOSITORY" = "xinlingzhifei/DeepTutor"',
        'test "$GITHUB_REF_TYPE" = "tag"',
        "^yfeistai-first-release-[0-9]{8}-[0-9a-f]{8}$",
        'test "$(git rev-parse HEAD)" = "$GITHUB_SHA"',
        'test "${GITHUB_REF_NAME##*-}" = "${GITHUB_SHA:0:8}"',
        'test "$(git rev-list -n 1 "$GITHUB_REF_NAME")" = "$GITHUB_SHA"',
    ):
        assert constraint in validation["run"]
    assert steps.index(validation) < next(
        index for index, step in enumerate(steps) if "docker/login-action@" in step.get("uses", "")
    )
    assert "workflow_dispatch" not in source
    assert "continue-on-error" not in source


@pytest.mark.parametrize(
    "filename",
    [
        "private-ragflow-engine-image.yml",
        "private-platform-images.yml",
    ],
)
def test_both_publication_workflows_queue_without_replacing_pending_candidates(filename):
    workflow = yaml.safe_load((ROOT / ".github/workflows" / filename).read_text(encoding="utf-8"))
    assert workflow["concurrency"] == {
        "group": "private-platform-images",
        "cancel-in-progress": False,
        "queue": "max",
    }


def test_preflight_fails_closed_without_runner_cleanup_or_paid_resources():
    source, _, _, steps = _workflow()
    preflight = _step(steps, "Check standard runner resources")
    for constraint in (
        "os.cpu_count()",
        'os.sysconf("SC_PHYS_PAGES")',
        'os.sysconf("SC_PAGE_SIZE")',
        "shutil.disk_usage",
        "cores < 4",
        "memory_bytes < 16_000_000_000",
        "disk_free < 50_000_000_000",
        "raise SystemExit",
    ):
        assert constraint in preflight["run"]
    assert steps.index(preflight) < steps.index(_step(steps, "Checkout pinned RAGFlow"))
    for forbidden in (
        "rm -",
        "Remove-Item",
        "docker system prune",
        "sudo",
        "apt-get",
        "self-hosted",
        "larger-runner",
        "ssh ",
        "scp ",
        "workflow_dispatch",
        "git push",
        "git commit",
        "render_platform_compose",
        "image-lock.json",
    ):
        assert forbidden not in source


def test_actions_and_upstream_are_immutable_and_node22_replays_branding_before_build():
    _, _, _, steps = _workflow()
    for step in steps:
        if "uses" in step:
            assert re.fullmatch(r"[A-Za-z0-9_./-]+@[0-9a-f]{40}", step["uses"])
    checkout = _step(steps, "Checkout yFeiSTAI")
    assert checkout["uses"] == CHECKOUT
    assert checkout["with"] == {"fetch-depth": 0, "persist-credentials": False}
    upstream = _step(steps, "Checkout pinned RAGFlow")
    assert upstream["uses"] == CHECKOUT
    assert upstream["with"] == {
        "repository": "infiniflow/ragflow",
        "ref": UPSTREAM,
        "path": "integrations/ragflow/upstream",
        "fetch-depth": 0,
        "persist-credentials": False,
    }
    node = next(step for step in steps if step.get("uses", "").startswith("actions/setup-node@"))
    assert str(node["with"]["node-version"]) == "22"
    branding = _step(steps, "Replay and verify yFei branding")
    command = "node integrations/ragflow/branding/apply-branding.mjs integrations/ragflow/upstream"
    assert command + "\n" in branding["run"]
    assert command + " --check\n" in branding["run"]
    assert "test -d integrations/ragflow/upstream/.git" in branding["run"]
    assert steps.index(upstream) < steps.index(branding)
    assert steps.index(node) < steps.index(branding)
    assert steps.index(branding) < steps.index(_step(steps, "Build and push staging engine"))


def test_preparation_uses_strict_pinned_config_and_external_generated_recipe():
    _, _, _, steps = _workflow()
    prepare = _step(steps, "Prepare pinned engine build inputs")
    for constraint in (
        'Path("integrations/ragflow/ENGINE-BUILD.json")',
        "if config != expected",
        'type(config["schemaVersion"]) is not int',
        '"schemaVersion": 1',
        '"platform": "linux/amd64"',
        '"backendMode": "python"',
        '"upstreamRecipe": "Dockerfile_ci"',
        "infiniflow/ragflow-base:v2.1@sha256:21b5afcfb32f57f2cb38d197254fcac8be5a81d7dd80adeac8523d277cb83e80",
        "ae86c755169894288eb235e0fba82643bb3550dc93a2b8073e7cbd532102c33e",
        "python scripts/ragflow_engine_release.py prepare",
        "--repository-root .",
        "--upstream-recipe integrations/ragflow/upstream/Dockerfile_ci",
        '--base-image "$BASE_IMAGE"',
        "--output-dir ragflow-engine-release",
    ):
        assert constraint in prepare["run"]
    base = _step(steps, "Verify pinned base manifest and platform")
    assert 'docker buildx imagetools inspect --raw "$BASE_IMAGE"' in base["run"]
    assert "hashlib.sha256" in base["run"]
    assert '"architecture": "amd64"' in base["run"]
    assert '"os": "linux"' in base["run"]
    assert steps.index(base) < steps.index(_step(steps, "Build and push staging engine"))


def test_engine_is_one_independent_filesystem_build_not_a_fifth_platform_image():
    source, _, _, steps = _workflow()
    builds = [step for step in steps if step.get("uses") == BUILD]
    assert len(builds) == 1
    build = builds[0]
    assert build["id"] == "build_engine"
    settings = build["with"]
    for key, value in {
        "context": "./integrations/ragflow/upstream",
        "file": "./ragflow-engine-release/Dockerfile",
        "target": "production",
        "platforms": "linux/amd64",
        "provenance": False,
        "push": True,
        "tags": f"{IMAGE}:staging-${{{{ github.run_id }}}}-${{{{ github.run_attempt }}}}",
    }.items():
        assert settings[key] == value
    assert (
        "org.opencontainers.image.source=https://github.com/xinlingzhifei/DeepTutor"
        in settings["labels"]
    )
    assert "org.opencontainers.image.revision=${{ github.sha }}" in settings["labels"]
    assert "org.opencontainers.image.version=${{ github.ref_name }}" in settings["labels"]
    assert "defaultContext" not in source
    assert "private-platform-image-lock" not in source
    assert "secrets." not in source.replace("secrets.GITHUB_TOKEN", "")


def test_typed_final_guard_runs_only_after_verified_staging_with_env_only_credentials():
    source, _, _, steps = _workflow()
    assert "Reject existing final engine tag before build" not in source
    promote = _step(steps, "Promote verified staging manifest once")
    assert promote["env"] == {
        "BUILD_DIGEST": "${{ steps.build_engine.outputs.digest }}",
        "GITHUB_TOKEN": "${{ secrets.GITHUB_TOKEN }}",
        "GITHUB_ACTOR": "${{ github.actor }}",
    }
    assert source.count("python scripts/ghcr_tag_guard.py") == 1
    assert "python scripts/ghcr_tag_guard.py" in promote["run"]
    assert '--image-repository "$IMAGE_REPOSITORY"' in promote["run"]
    assert '--release-tag "$GITHUB_REF_NAME"' in promote["run"]
    assert "GITHUB_TOKEN" not in promote["run"]
    assert "GITHUB_ACTOR" not in promote["run"]
    assert "imagetools inspect" not in promote["run"]
    assert "manifest unknown" not in promote["run"]
    assert "prebuild.err" not in source and "prepromotion.err" not in source
    assert steps.index(_step(steps, "Build and push staging engine")) < steps.index(
        _step(steps, "Verify staging digest and platform")
    )
    assert steps.index(_step(steps, "Verify staging digest and platform")) < steps.index(promote)
    assert promote["run"].index("ghcr_tag_guard.py") < promote["run"].index("imagetools create")
    assert "docker buildx imagetools create --prefer-index=false" in promote["run"]
    assert '--tag "$FINAL_REF" "$IMAGE_REPOSITORY@$BUILD_DIGEST"' in promote["run"]


def test_staging_and_final_raw_manifest_digests_bind_to_action_output_before_receipt():
    _, _, _, steps = _workflow()
    for name, manifest in (
        ("Verify staging digest and platform", "staging-manifest.raw.json"),
        ("Verify final engine publication", "final-manifest.raw.json"),
    ):
        step = _step(steps, name)
        assert step["env"]["BUILD_DIGEST"] == "${{ steps.build_engine.outputs.digest }}"
        assert "imagetools inspect --raw" in step["run"]
        assert manifest in step["run"]
        assert '"sha256:" + hashlib.sha256' in step["run"]
        assert 'os.environ["BUILD_DIGEST"]' in step["run"]
        assert "raise SystemExit" in step["run"]
    staging = _step(steps, "Verify staging digest and platform")
    assert "imagetools inspect --format '{{json .Image}}'" in staging["run"]
    assert 'image.get("architecture") != "amd64"' in staging["run"]
    assert 'image.get("os") != "linux"' in staging["run"]
    receipt = _step(steps, "Write independent engine receipt")
    for constraint in (
        "python scripts/ragflow_engine_release.py write-receipt",
        "--repository-root .",
        "--upstream-recipe integrations/ragflow/upstream/Dockerfile_ci",
        '--base-image "$BASE_IMAGE"',
        "--prepared-dir ragflow-engine-release",
        '--source-repository "$GITHUB_REPOSITORY"',
        '--source-head "$GITHUB_SHA"',
        '--release-tag "$GITHUB_REF_NAME"',
        '--run-id "$GITHUB_RUN_ID"',
        '--run-attempt "$GITHUB_RUN_ATTEMPT"',
        '--image "$IMAGE_REPOSITORY:$GITHUB_REF_NAME@$BUILD_DIGEST"',
        '--build-digest "$BUILD_DIGEST"',
        "--remote-manifest ragflow-engine-release/final-manifest.raw.json",
        "--output ragflow-engine-release/engine-receipt.json",
    ):
        assert constraint in receipt["run"]
    assert steps.index(_step(steps, "Verify final engine publication")) < steps.index(receipt)


def test_artifact_contains_only_independent_engine_evidence_and_safe_build_metadata():
    _, _, _, steps = _workflow()
    receipt = _step(steps, "Write independent engine receipt")
    assert receipt["env"]["BUILD_METADATA"] == "${{ steps.build_engine.outputs.metadata }}"
    assert 'os.environ["BUILD_METADATA"]' in receipt["run"]
    assert "json.loads" in receipt["run"]
    assert "build-metadata.json" in receipt["run"]
    uploads = [step for step in steps if step.get("uses") == UPLOAD]
    assert len(uploads) == 1
    settings = uploads[0]["with"]
    assert settings["name"] == "private-ragflow-engine-${{ github.ref_name }}-${{ github.sha }}"
    assert settings["if-no-files-found"] == "error"
    assert set(settings["path"].splitlines()) == {
        "ragflow-engine-release/Dockerfile",
        "ragflow-engine-release/build-inputs.json",
        "ragflow-engine-release/engine-receipt.json",
        "ragflow-engine-release/staging-manifest.raw.json",
        "ragflow-engine-release/final-manifest.raw.json",
        "ragflow-engine-release/build-metadata.json",
    }
    assert steps.index(receipt) < steps.index(uploads[0])


def test_inline_python_parses_and_shell_never_interpolates_actions_expressions():
    _, _, _, steps = _workflow()
    snippets = []
    for step in steps:
        run = step.get("run", "")
        assert "${{" not in run, step["name"]
        assert not step.get("continue-on-error"), step["name"]
        if run:
            assert "set -euo pipefail" in run, step["name"]
        for snippet in re.findall(r"<<'PY'\n(.*?)\nPY(?:\n|$)", run, re.DOTALL):
            ast.parse(snippet)
            snippets.append(snippet)
    assert len(snippets) >= 6


@pytest.mark.skipif(sys.platform != "linux", reason="Requires native Linux Bash (including WSL)")
def test_all_engine_workflow_run_steps_pass_native_bash_syntax_check():
    bash = shutil.which("bash")
    assert bash, "Native Linux workflow checks require Bash"
    _, _, _, steps = _workflow()
    for step in steps:
        if "run" not in step:
            continue
        result = subprocess.run(
            [bash, "--noprofile", "--norc", "-n"],
            input=step["run"],
            text=True,
            capture_output=True,
            env={"PATH": ""},
            timeout=10,
            check=False,
        )
        assert result.returncode == 0, f"{step['name']}: {result.stderr}"


@pytest.mark.skipif(sys.platform != "linux", reason="Requires native Linux Bash (including WSL)")
@pytest.mark.parametrize("guard_status", [0, 1, 2, 127])
def test_native_promotion_never_runs_after_guard_failure(tmp_path, guard_status):
    bash = shutil.which("bash")
    assert bash, "Native Linux workflow checks require Bash"
    _, _, _, steps = _workflow()
    release_tag = "yfeistai-first-release-20260907-aaaaaaaa"
    final_ref = f"{IMAGE}:{release_tag}"
    build_digest = "sha256:" + "b" * 64
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    python = fake_bin / "python"
    python.write_text(
        "#!/bin/sh\n"
        'test "$GITHUB_TOKEN" = "fixture-token" || exit 98\n'
        'test "$GITHUB_ACTOR" = "fixture-actor" || exit 98\n'
        'printf \'python %s\\n\' "$*" >> "$FAKE_CALLS"\n'
        'exit "$FAKE_GUARD_STATUS"\n',
        encoding="utf-8",
    )
    python.chmod(0o755)
    docker = fake_bin / "docker"
    docker.write_text(
        "#!/bin/sh\n"
        'printf \'docker %s\\n\' "$*" >> "$FAKE_CALLS"\n'
        'case "$1 $2 $3" in\n'
        '  "buildx imagetools create") exit 0 ;;\n'
        "  *) exit 99 ;;\n"
        "esac\n",
        encoding="utf-8",
    )
    docker.chmod(0o755)
    calls_path = tmp_path / "calls.txt"
    # Only fake executables and fixture credentials are available. The helper's
    # HTTP/parser behavior is covered independently without a real registry.
    environment = {
        "PATH": str(fake_bin),
        "RUNNER_TEMP": str(tmp_path),
        "IMAGE_REPOSITORY": IMAGE,
        "GITHUB_REF_NAME": release_tag,
        "GITHUB_TOKEN": "fixture-token",
        "GITHUB_ACTOR": "fixture-actor",
        "BUILD_DIGEST": build_digest,
        "FAKE_CALLS": str(calls_path),
        "FAKE_GUARD_STATUS": str(guard_status),
    }
    result = subprocess.run(
        [bash, "--noprofile", "--norc", "-e", "-o", "pipefail", "-s"],
        input=_step(steps, "Promote verified staging manifest once")["run"],
        cwd=tmp_path,
        env=environment,
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == guard_status, result.stdout + result.stderr
    expected_calls = [
        f"python scripts/ghcr_tag_guard.py --image-repository {IMAGE} --release-tag {release_tag}"
    ]
    if guard_status == 0:
        expected_calls.append(
            f"docker buildx imagetools create --prefer-index=false --tag {final_ref} {IMAGE}@{build_digest}"
        )
    assert calls_path.read_text(encoding="utf-8").splitlines() == expected_calls

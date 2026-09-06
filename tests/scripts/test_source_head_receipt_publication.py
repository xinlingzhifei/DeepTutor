from __future__ import annotations

from contextlib import contextmanager
import os
from pathlib import Path
import subprocess

import pytest

from tests.scripts import test_classroom_release_evidence as support
from tests.scripts.test_classroom_release_evidence import (
    RELEASE_RUN,
    SOURCE_HEAD,
    _intercept_image_receipt_publication,
    _load_evidence_module,
    _write_candidate_root,
)


def _case(tmp_path):
    module = _load_evidence_module()
    root, _candidate = _write_candidate_root(tmp_path, include_runtime_attestation=False)
    source = tmp_path / "source"
    source.mkdir()
    output = tmp_path / "receipts" / "source_head.json"
    output.parent.mkdir()
    sentinel = output.parent / "unrelated.json"
    sentinel.write_bytes(b"preserved input")
    state = {
        "head": SOURCE_HEAD,
        "status": "",
        "origin": "https://github.com/xinlingzhifei/DeepTutor.git",
    }
    calls = []

    def git(arguments, *, cwd):
        assert cwd == source
        calls.append(tuple(arguments))
        if arguments == ["rev-parse", "HEAD"]:
            stdout = state["head"]
        elif arguments == ["status", "--porcelain=v1", "--untracked-files=all"]:
            stdout = state["status"]
        elif arguments == ["remote", "get-url", "origin"]:
            stdout = state["origin"]
        else:
            raise AssertionError(arguments)
        return subprocess.CompletedProcess(arguments, 0, stdout=stdout, stderr="")

    arguments = dict(
        output_path=output,
        candidate_root=root,
        source_root=source,
        release_run=RELEASE_RUN,
        observed_at="2026-08-25T00:00:00Z",
        git_runner=git,
    )
    return module, root, output, arguments, sentinel, state, calls


@pytest.mark.parametrize("phase", ("probe-returned", "receipt-published"))
@pytest.mark.parametrize(
    "relative_input",
    (
        "deploy/image-lock.json",
        "docker-compose.yml",
        "docker-compose.platform.yml",
        "docker-compose.data-plane.yml",
        "deploy/docker-compose.openmaic-shared-ingress-observer.yml",
    ),
    ids=("lock", "base", "platform", "data-plane", "observer"),
)
def test_source_receipt_rejects_same_byte_candidate_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
    relative_input: str,
) -> None:
    module, root, output, arguments, sentinel, _state, calls = _case(tmp_path)
    target = root / relative_input
    replacement = tmp_path / "same-byte-candidate"
    replacement.write_bytes(target.read_bytes())
    stat, lstat = os.stat, os.lstat
    identity = module._file_identity(replacement.stat())
    assert identity != module._file_identity(target.stat())
    key = os.path.normcase(os.path.abspath(target))
    mutated = False
    published = []

    def redirected(path):
        if mutated:
            try:
                if os.path.normcase(os.path.abspath(os.fspath(path))) == key:
                    return replacement
            except (TypeError, ValueError):
                pass
        return path

    if os.name == "nt":
        monkeypatch.setattr(os, "stat", lambda path, *a, **kw: stat(redirected(path), *a, **kw))
        monkeypatch.setattr(os, "lstat", lambda path, *a, **kw: lstat(redirected(path), *a, **kw))

    def mutate():
        nonlocal mutated
        assert not mutated
        if os.name != "nt":
            os.replace(replacement, target)
        mutated = True
        assert module._file_identity(os.lstat(target)) == identity

    git = arguments["git_runner"]

    def probe(*args, **kwargs):
        result = git(*args, **kwargs)
        if len(calls) == 4 and phase == "probe-returned":
            mutate()
        return result

    arguments["git_runner"] = probe

    def publish_then_mutate(publish):
        publish()
        published.append(True)
        if phase == "receipt-published":
            mutate()

    _intercept_image_receipt_publication(module, monkeypatch, publish_then_mutate)
    with pytest.raises(ValueError, match="(?i)candidate|artifact|changed|image lock|Compose"):
        module.write_source_head_receipt(**arguments)
    assert mutated
    assert published == ([True] if phase == "receipt-published" else [])
    assert list(output.parent.iterdir()) == [sentinel]


@pytest.mark.parametrize("phase", ("published", "lease-exited"))
@pytest.mark.parametrize(
    "field,value",
    (
        ("head", "b" * 40),
        ("status", " M dirty.py"),
        ("origin", "https://github.com/foreign/repo.git"),
    ),
)
def test_source_receipt_rechecks_git_through_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
    field: str,
    value: str,
) -> None:
    module, _root, output, arguments, sentinel, state, _calls = _case(tmp_path)
    mutations = []

    def mutate():
        mutations.append(True)
        state[field] = value

    def publish(publish_real):
        publish_real()
        if phase == "published":
            mutate()

    lease = module.validated_candidate_artifact_lease

    @contextmanager
    def exit_then_mutate(*args, **kwargs):
        with lease(*args, **kwargs) as artifact:
            yield artifact
        if phase == "lease-exited":
            assert output.exists()
            mutate()

    monkeypatch.setattr(module, "validated_candidate_artifact_lease", exit_then_mutate)
    _intercept_image_receipt_publication(module, monkeypatch, publish)
    with pytest.raises(ValueError, match="(?i)Git"):
        module.write_source_head_receipt(**arguments)
    assert mutations == [True]
    assert list(output.parent.iterdir()) == [sentinel]


@pytest.mark.parametrize("same_content", (False, True))
def test_source_receipt_preserves_existing_output(tmp_path: Path, same_content: bool) -> None:
    module, _root, output, arguments, _sentinel, _state, _calls = _case(tmp_path)
    module.write_source_head_receipt(**arguments)
    if not same_content:
        output.write_bytes(b"prior evidence")
    original = (output.read_bytes(), module._file_identity(output.stat()))
    with pytest.raises(FileExistsError):
        module.write_source_head_receipt(**arguments)
    assert (output.read_bytes(), module._file_identity(output.stat())) == original


def test_source_receipt_lease_exit_failure_retracts_only_own_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module, _root, output, arguments, sentinel, _state, _calls = _case(tmp_path)
    lease = module.validated_candidate_artifact_lease
    reached = []

    @contextmanager
    def fail_after_exit(*args, **kwargs):
        with lease(*args, **kwargs) as artifact:
            yield artifact
        reached.append(output.exists())
        raise OSError("injected source candidate lease close failure")

    monkeypatch.setattr(module, "validated_candidate_artifact_lease", fail_after_exit)
    with pytest.raises(OSError, match="injected source candidate lease close failure"):
        module.write_source_head_receipt(**arguments)
    assert reached == [True]
    assert list(output.parent.iterdir()) == [sentinel]


@pytest.mark.parametrize("dirty_after_publish", (False, True))
def test_source_receipt_checks_real_local_git_through_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    dirty_after_publish: bool,
) -> None:
    module = _load_evidence_module()
    source = tmp_path / "isolated-git-fixture"
    source.mkdir()
    template = tmp_path / "empty-git-template"
    template.mkdir()
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.delenv("GIT_CONFIG_PARAMETERS", raising=False)
    monkeypatch.setenv("GIT_CONFIG_COUNT", "0")

    def git(*arguments):
        result = module._run_git(list(arguments), cwd=source)
        assert result.returncode == 0, result.stderr
        return result.stdout.strip()

    git("init", "--quiet", f"--template={template}")
    tracked = source / "fixture.txt"
    tracked.write_text("committed fixture\n", encoding="utf-8")
    git("add", "fixture.txt")
    git(
        "-c",
        "user.name=Receipt fixture",
        "-c",
        "user.email=fixture@example.invalid",
        "commit",
        "--no-gpg-sign",
        "--quiet",
        "-m",
        "isolated receipt fixture",
    )
    git("remote", "add", "origin", "https://github.com/xinlingzhifei/DeepTutor.git")
    head = git("rev-parse", "HEAD")
    monkeypatch.setattr(support, "SOURCE_HEAD", head)
    monkeypatch.setattr(support, "RELEASE_TAG", f"yfeistai-first-release-20260906-{head[:8]}")
    root, candidate = _write_candidate_root(tmp_path, include_runtime_attestation=False)
    output = tmp_path / "receipts" / "source_head.json"
    output.parent.mkdir()
    published = []

    def publish_then_dirty(publish):
        publish()
        published.append(True)
        if dirty_after_publish:
            tracked.write_text("concurrent uncommitted change\n", encoding="utf-8")

    _intercept_image_receipt_publication(module, monkeypatch, publish_then_dirty)
    arguments = dict(
        output_path=output,
        candidate_root=root,
        source_root=source,
        release_run=RELEASE_RUN,
        observed_at="2026-08-25T00:00:00Z",
    )
    if dirty_after_publish:
        with pytest.raises(ValueError, match="Git worktree is not clean"):
            module.write_source_head_receipt(**arguments)
        assert list(output.parent.iterdir()) == []
        assert tracked.read_text(encoding="utf-8") == "concurrent uncommitted change\n"
    else:
        document = module.write_source_head_receipt(**arguments)
        assert document["candidate"] == candidate
        assert output.read_bytes() == module._json_bytes(document)
        assert list(output.parent.iterdir()) == [output]
        assert git("status", "--porcelain=v1", "--untracked-files=all") == ""
    assert published == [True]
    assert git("rev-parse", "HEAD") == head

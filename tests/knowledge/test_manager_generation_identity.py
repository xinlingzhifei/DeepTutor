from __future__ import annotations

import builtins
import json
from pathlib import Path
import subprocess
import sys
import textwrap
import threading
import time
import uuid

import pytest

from deeptutor.knowledge import manager as manager_module
from deeptutor.knowledge.manager import KnowledgeBaseConfigError, KnowledgeBaseManager


def _assert_generation(value: object) -> str:
    generation = str(value or "")
    assert str(uuid.UUID(generation)) == generation
    return generation


def test_legacy_entries_receive_stable_persisted_generation_ids(tmp_path: Path) -> None:
    external = tmp_path / "external"
    external.mkdir()
    config_path = tmp_path / "kb_config.json"
    config_path.write_text(
        json.dumps(
            {
                "knowledge_bases": {
                    "ordinary": {"path": "ordinary"},
                    "connected": {
                        "path": "connected",
                        "type": "linked",
                        "external_path": str(external),
                    },
                }
            }
        ),
        encoding="utf-8",
    )

    first = KnowledgeBaseManager(base_dir=tmp_path)
    first_ids = {
        name: _assert_generation(entry["generation_id"])
        for name, entry in first.config["knowledge_bases"].items()
    }
    persisted = json.loads(config_path.read_text(encoding="utf-8"))
    assert {
        name: entry["generation_id"]
        for name, entry in persisted["knowledge_bases"].items()
    } == first_ids

    second = KnowledgeBaseManager(base_dir=tmp_path)
    assert {
        name: entry["generation_id"]
        for name, entry in second.config["knowledge_bases"].items()
    } == first_ids


def test_legacy_generation_migration_fails_closed_when_publish_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "kb_config.json"
    legacy_config = {"knowledge_bases": {"legacy": {"path": "legacy"}}}
    config_path.write_text(json.dumps(legacy_config), encoding="utf-8")

    def fail_publish(_path: Path, _payload: dict) -> None:
        raise OSError("simulated config publication failure")

    monkeypatch.setattr(manager_module, "atomic_write_json", fail_publish)

    with pytest.raises(OSError, match="publication failure"):
        KnowledgeBaseManager(base_dir=tmp_path)

    assert json.loads(config_path.read_text(encoding="utf-8")) == legacy_config


def test_mutation_does_not_overwrite_existing_invalid_config(tmp_path: Path) -> None:
    config_path = tmp_path / "kb_config.json"
    invalid_bytes = b'{"knowledge_bases":'
    config_path.write_bytes(invalid_bytes)
    (tmp_path / "new-kb").mkdir()
    manager = KnowledgeBaseManager(base_dir=tmp_path)

    with pytest.raises(KnowledgeBaseConfigError, match="config is unreadable or invalid"):
        manager.register_knowledge_base("new-kb")

    assert config_path.read_bytes() == invalid_bytes
    assert b"generation_id" not in config_path.read_bytes()

    config_path.write_text(json.dumps({"knowledge_bases": {}}), encoding="utf-8")
    manager.register_knowledge_base("new-kb")
    persisted = json.loads(config_path.read_text(encoding="utf-8"))
    _assert_generation(persisted["knowledge_bases"]["new-kb"]["generation_id"])


def test_stale_save_does_not_overwrite_existing_invalid_config(tmp_path: Path) -> None:
    config_path = tmp_path / "kb_config.json"
    invalid_bytes = b"not-json"
    config_path.write_bytes(invalid_bytes)
    manager = KnowledgeBaseManager(base_dir=tmp_path)
    manager.config["knowledge_bases"]["new-kb"] = {"path": "new-kb"}

    with pytest.raises(KnowledgeBaseConfigError, match="config is unreadable or invalid"):
        manager._save_config()

    assert config_path.read_bytes() == invalid_bytes
    assert b"generation_id" not in config_path.read_bytes()


def test_mutation_does_not_overwrite_existing_unreadable_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "kb_config.json"
    original_bytes = json.dumps({"knowledge_bases": {}}).encode()
    config_path.write_bytes(original_bytes)
    (tmp_path / "new-kb").mkdir()
    manager = KnowledgeBaseManager(base_dir=tmp_path)
    real_open = builtins.open

    def fail_config_read(file, mode="r", *args, **kwargs):
        if Path(file) == config_path and "r" in mode:
            raise PermissionError("C:/secret/config is unavailable")
        return real_open(file, mode, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", fail_config_read)

    with pytest.raises(KnowledgeBaseConfigError) as exc_info:
        manager.register_knowledge_base("new-kb")

    assert str(exc_info.value) == "Knowledge-base config is unreadable or invalid."
    assert config_path.read_bytes() == original_bytes


@pytest.mark.parametrize("path_method", ["exists", "stat"])
def test_mutation_wraps_config_path_metadata_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    path_method: str,
) -> None:
    config_path = tmp_path / "kb_config.json"
    original_bytes = json.dumps({"knowledge_bases": {}}).encode()
    config_path.write_bytes(original_bytes)
    (tmp_path / "new-kb").mkdir()
    manager = KnowledgeBaseManager(base_dir=tmp_path)
    original_method = getattr(Path, path_method)

    def fail_config_metadata(path: Path, *args, **kwargs):
        if path == config_path:
            raise PermissionError("C:/secret/config metadata denied")
        return original_method(path, *args, **kwargs)

    monkeypatch.setattr(Path, path_method, fail_config_metadata)

    with pytest.raises(KnowledgeBaseConfigError) as exc_info:
        manager.register_knowledge_base("new-kb")

    assert str(exc_info.value) == "Knowledge-base config is unreadable or invalid."
    assert config_path.read_bytes() == original_bytes
    assert b"generation_id" not in config_path.read_bytes()


def test_open_file_not_found_race_bootstraps_missing_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "kb_config.json"
    (tmp_path / "new-kb").mkdir()
    manager = KnowledgeBaseManager(base_dir=tmp_path)
    original_exists = Path.exists

    def stale_exists(path: Path) -> bool:
        if path == config_path:
            return True
        return original_exists(path)

    monkeypatch.setattr(Path, "exists", stale_exists)

    manager.register_knowledge_base("new-kb")

    persisted = json.loads(config_path.read_text(encoding="utf-8"))
    _assert_generation(persisted["knowledge_bases"]["new-kb"]["generation_id"])


def test_concurrent_legacy_loaders_publish_one_shared_generation(tmp_path: Path) -> None:
    config_path = tmp_path / "kb_config.json"
    config_path.write_text(
        json.dumps({"knowledge_bases": {"legacy": {"path": "legacy"}}}),
        encoding="utf-8",
    )
    start_path = tmp_path / "start.barrier"
    script = textwrap.dedent(
        """
        import sys
        import time
        from pathlib import Path
        from deeptutor.knowledge import manager as manager_module

        base_dir = Path(sys.argv[1])
        ready_path = Path(sys.argv[2])
        start_path = Path(sys.argv[3])
        original_publish = manager_module.atomic_write_json

        def slow_publish(path, payload):
            time.sleep(0.5)
            original_publish(path, payload)

        manager_module.atomic_write_json = slow_publish
        ready_path.write_text("ready", encoding="utf-8")
        while not start_path.exists():
            time.sleep(0.01)
        manager = manager_module.KnowledgeBaseManager(base_dir=base_dir)
        print(manager.config["knowledge_bases"]["legacy"]["generation_id"])
        """
    )
    project_root = Path(__file__).resolve().parents[2]
    processes = [
        subprocess.Popen(
            [
                sys.executable,
                "-c",
                script,
                str(tmp_path),
                str(tmp_path / f"worker-{index}.ready"),
                str(start_path),
            ],
            cwd=project_root,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for index in range(2)
    ]
    deadline = time.monotonic() + 10
    while not all((tmp_path / f"worker-{index}.ready").exists() for index in range(2)):
        assert time.monotonic() < deadline
        time.sleep(0.01)
    start_path.write_text("start", encoding="utf-8")

    outputs = [process.communicate(timeout=30) for process in processes]
    assert [(process.returncode, stderr) for process, (_stdout, stderr) in zip(processes, outputs)] == [
        (0, ""),
        (0, ""),
    ]
    generations = [stdout.strip() for stdout, _stderr in outputs]
    persisted = json.loads(config_path.read_text(encoding="utf-8"))
    persisted_generation = persisted["knowledge_bases"]["legacy"]["generation_id"]
    assert generations == [persisted_generation, persisted_generation]


def test_concurrent_unrelated_registrations_do_not_lose_config_updates(
    tmp_path: Path,
) -> None:
    for name in ("alpha", "beta"):
        (tmp_path / name).mkdir()
    start_path = tmp_path / "register.start"
    script = textwrap.dedent(
        """
        import sys
        import time
        from pathlib import Path
        from deeptutor.knowledge import manager as manager_module

        base_dir = Path(sys.argv[1])
        name = sys.argv[2]
        ready_path = Path(sys.argv[3])
        start_path = Path(sys.argv[4])
        manager = manager_module.KnowledgeBaseManager(base_dir=base_dir)
        original_publish = manager_module.atomic_write_json

        def slow_publish(path, payload):
            time.sleep(0.5)
            original_publish(path, payload)

        manager_module.atomic_write_json = slow_publish
        ready_path.write_text("ready", encoding="utf-8")
        while not start_path.exists():
            time.sleep(0.01)
        manager.register_knowledge_base(name)
        print(manager.config["knowledge_bases"][name]["generation_id"])
        """
    )
    project_root = Path(__file__).resolve().parents[2]
    processes = [
        subprocess.Popen(
            [
                sys.executable,
                "-c",
                script,
                str(tmp_path),
                name,
                str(tmp_path / f"register-{name}.ready"),
                str(start_path),
            ],
            cwd=project_root,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for name in ("alpha", "beta")
    ]
    deadline = time.monotonic() + 10
    while not all(
        (tmp_path / f"register-{name}.ready").exists() for name in ("alpha", "beta")
    ):
        assert time.monotonic() < deadline
        time.sleep(0.01)
    start_path.write_text("start", encoding="utf-8")

    outputs = [process.communicate(timeout=30) for process in processes]
    assert [process.returncode for process in processes] == [0, 0]
    assert [stderr for _stdout, stderr in outputs] == ["", ""]
    generations = {
        name: stdout.strip()
        for name, (stdout, _stderr) in zip(("alpha", "beta"), outputs)
    }
    persisted = json.loads((tmp_path / "kb_config.json").read_text(encoding="utf-8"))
    assert {
        name: entry["generation_id"]
        for name, entry in persisted["knowledge_bases"].items()
    } == generations


def test_stale_same_name_writer_merges_with_concurrent_generation_publication(
    tmp_path: Path,
) -> None:
    (tmp_path / "shared").mkdir()
    stale_manager = KnowledgeBaseManager(base_dir=tmp_path)
    config_path = tmp_path / "kb_config.json"
    config_path.write_text(
        json.dumps({"knowledge_bases": {"shared": {"path": "shared"}}}),
        encoding="utf-8",
    )
    ready_path = tmp_path / "normalize.ready"
    release_path = tmp_path / "normalize.release"
    script = textwrap.dedent(
        """
        import sys
        import time
        from pathlib import Path
        from deeptutor.knowledge import manager as manager_module

        base_dir = Path(sys.argv[1])
        ready_path = Path(sys.argv[2])
        release_path = Path(sys.argv[3])
        original_publish = manager_module.atomic_write_json

        def controlled_publish(path, payload):
            ready_path.write_text("ready", encoding="utf-8")
            while not release_path.exists():
                time.sleep(0.01)
            original_publish(path, payload)

        manager_module.atomic_write_json = controlled_publish
        manager = manager_module.KnowledgeBaseManager(base_dir=base_dir)
        print(manager.config["knowledge_bases"]["shared"]["generation_id"])
        """
    )
    process = subprocess.Popen(
        [
            sys.executable,
            "-c",
            script,
            str(tmp_path),
            str(ready_path),
            str(release_path),
        ],
        cwd=Path(__file__).resolve().parents[2],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    deadline = time.monotonic() + 10
    while not ready_path.exists():
        assert time.monotonic() < deadline
        time.sleep(0.01)

    stale_manager.config["knowledge_bases"]["shared"] = {
        "path": "shared",
        "description": "registered concurrently",
    }
    errors: list[BaseException] = []

    def save_stale_config() -> None:
        try:
            stale_manager._save_config()
        except BaseException as exc:
            errors.append(exc)

    writer = threading.Thread(target=save_stale_config)
    writer.start()
    time.sleep(0.2)
    release_path.write_text("release", encoding="utf-8")
    writer.join(timeout=30)
    assert not writer.is_alive()
    stdout, stderr = process.communicate(timeout=30)
    assert process.returncode == 0, stderr
    assert errors == []

    persisted = json.loads(config_path.read_text(encoding="utf-8"))
    entry = persisted["knowledge_bases"]["shared"]
    assert entry["generation_id"] == stdout.strip()
    assert stale_manager.config["knowledge_bases"]["shared"]["generation_id"] == stdout.strip()
    assert entry["description"] == "registered concurrently"


def test_stale_save_rejects_conflicting_scalar_update(tmp_path: Path) -> None:
    (tmp_path / "shared").mkdir()
    seed = KnowledgeBaseManager(base_dir=tmp_path)
    seed.register_knowledge_base("shared", description="base")
    first = KnowledgeBaseManager(base_dir=tmp_path)
    stale = KnowledgeBaseManager(base_dir=tmp_path)

    first.config["knowledge_bases"]["shared"]["description"] = "first"
    first._save_config()
    stale.config["knowledge_bases"]["shared"]["description"] = "stale"

    with pytest.raises(RuntimeError, match="config.*description"):
        stale._save_config()

    persisted = json.loads((tmp_path / "kb_config.json").read_text(encoding="utf-8"))
    assert persisted["knowledge_bases"]["shared"]["description"] == "first"


def test_recreating_same_name_allocates_a_new_generation(tmp_path: Path) -> None:
    kb_dir = tmp_path / "notes"
    kb_dir.mkdir()
    manager = KnowledgeBaseManager(base_dir=tmp_path)
    manager.register_knowledge_base("notes")
    first_generation = _assert_generation(
        manager.get_kb_entry("notes")["generation_id"]
    )

    manager.config["knowledge_bases"].pop("notes")
    manager._save_config()
    manager.register_knowledge_base("notes")
    second_generation = _assert_generation(
        manager.get_kb_entry("notes")["generation_id"]
    )

    assert second_generation != first_generation


def test_connected_registration_persists_generation_in_public_metadata(
    tmp_path: Path,
) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    manager = KnowledgeBaseManager(base_dir=tmp_path / "managed")

    entry = manager.register_obsidian_vault("vault", str(vault))
    generation = _assert_generation(entry["generation_id"])

    assert manager.get_metadata("vault")["generation_id"] == generation
    persisted = json.loads(manager.config_file.read_text(encoding="utf-8"))
    assert persisted["knowledge_bases"]["vault"]["generation_id"] == generation

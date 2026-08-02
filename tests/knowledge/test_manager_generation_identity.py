from __future__ import annotations

import json
from pathlib import Path
import uuid

from deeptutor.knowledge.manager import KnowledgeBaseManager


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

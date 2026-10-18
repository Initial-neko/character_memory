"""Offline evidence only; real registry/files/SQLite, no live runtime/providers."""
import json
import hashlib
import os
from pathlib import Path
import subprocess
import sys
import threading
import pytest
from types import SimpleNamespace

from character_memory.api_character_service import ApiCharacterService
from character_memory.config import discover_character_profiles
from character_memory.ensemble_builder import EnsembleBuilderService, EnsembleRepository
from character_memory.storage.sqlite import SQLiteStore


def compose(root):
    settings = SimpleNamespace(persona_path=str(root / "personas" / "seed" / "persona.yaml"), db_path=str(root / "evidence.db"))
    store = SQLiteStore(str(root / "evidence.db"))
    characters = ApiCharacterService(settings=settings, current_bundle=lambda: None,
        character_write_lock=threading.RLock(), register_runtime_character=lambda profile: None)
    access = SimpleNamespace(read_store=store, store=lambda: store,
        character_profiles=characters.profiles,
        check_character_capacity=characters.check_capacity,
        create_character_from_draft=characters.create_from_draft,
        rollback_created_character=characters.rollback_created)
    repository = EnsembleRepository(store)
    return store, settings, repository, EnsembleBuilderService(access, repository)


class KnownConfirmRecoveryDuplication(AssertionError):
    pass


def evidence_path():
    root = next(parent for parent in Path(__file__).resolve().parents if (parent / "pyproject.toml").is_file())
    target = Path(os.getenv("ENSEMBLE_CONFIRM_EVIDENCE_DIR", str(root / "artifacts/core-visual-evidence/ensemble-confirm-recovery")))
    target.mkdir(parents=True, exist_ok=True)
    return target


def test_uninterrupted_confirm_is_idempotent_across_recomposition(tmp_path):
    seed = tmp_path / "personas" / "seed" / "persona.yaml"
    seed.parent.mkdir(parents=True)
    seed.write_text("id: seed\nname: Seed\nidentity: Observer\n", encoding="utf-8")
    store, settings, repository, service = compose(tmp_path)
    build = service.prepare_template("warm-isekai")
    first = service.confirm(build["group_id"], [0, 1, 2, 3])
    store.close()
    store, settings, repository, service = compose(tmp_path)
    repository.recover_interrupted()
    second = service.confirm(build["group_id"], [0, 1, 2, 3])
    assert first["group_id"] == second["group_id"]
    assert len(discover_character_profiles(settings)) == 5
    assert len(service.groups.list_groups()) == 1
    store.close()
    (evidence_path() / "normal-control.json").write_text(json.dumps({"status": "PASS", "characters_including_seed": 5, "groups": 1}), encoding="utf-8")


def test_interrupted_confirm_preserves_one_people_set_and_one_group(tmp_path):
    seed = tmp_path / "personas" / "seed" / "persona.yaml"
    seed.parent.mkdir(parents=True)
    seed.write_text("id: seed\nname: Seed\nidentity: Observer\n", encoding="utf-8")
    store, settings, repository, service = compose(tmp_path)
    build = service.prepare_template("warm-isekai")
    build_id = build["group_id"]
    store.close()
    env = dict(os.environ)
    env["PYTHONPATH"] = str(next(parent for parent in Path(__file__).resolve().parents if (parent / "pyproject.toml").is_file()) / "src")
    child = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--crash", str(tmp_path), build_id],
        env=env, capture_output=True, text=True, timeout=30)
    assert child.returncode == 77, (child.returncode, child.stdout, child.stderr)

    store, settings, repository, service = compose(tmp_path)
    recovered = repository.recover_interrupted()
    before = {"build_status": repository.get(build_id)["status"],
        "recovered_count": recovered,
        "profiles": [{"id": p["id"], "name": p["name"]} for p in discover_character_profiles(settings)],
        "groups": [{"id": g.id, "members": g.member_ids} for g in service.groups.list_groups()]}
    result = service.confirm(build_id, [0, 1, 2, 3])
    after = {"build_status": result["status"],
        "profiles": [{"id": p["id"], "name": p["name"]} for p in discover_character_profiles(settings)],
        "groups": [{"id": g.id, "members": g.member_ids} for g in service.groups.list_groups()]}
    repo_root = next(parent for parent in Path(__file__).resolve().parents if (parent / "pyproject.toml").is_file())
    checkout_sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo_root, capture_output=True, text=True, check=True, timeout=30).stdout.strip()
    source_paths = ["src/character_memory/ensemble_builder.py", "src/character_memory/api_character_service.py", "src/character_memory/group_store.py", "src/character_memory/persona_builder.py"]
    evidence = {"checkout_sha": checkout_sha,
        "source_sha256": {name: hashlib.sha256((repo_root / name).read_bytes()).hexdigest() for name in source_paths},
        "fixture_boundaries": {"current_bundle": None, "runtime_registration": "no-op", "onboarding_provider": None, "real_persona_files": True, "real_sqlite_repositories": True},
        "child_exitcode": child.returncode, "before_retry": before, "after_retry": after,
        "expected": "same build must yield one group and four new people across interrupted confirmation retries",
        "actual": {"people_excluding_seed": len(after["profiles"]) - 1, "groups": len(after["groups"])}}
    (evidence_path() / "interrupted-confirm.json").write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
    assert before["build_status"] == "READY"
    assert len(before["profiles"]) == 5 and len(before["groups"]) == 1
    assert before["recovered_count"] == 0
    assert len(before["groups"][0]["members"]) == 4
    assert len(result["group"]["member_ids"]) == 4
    first_members = set(before["groups"][0]["members"])
    second_members = set(result["group"]["member_ids"])
    known_signature = len(after["profiles"]) == 9 and len(after["groups"]) == 2 and first_members.isdisjoint(second_members)
    store.close()
    if known_signature:
        raise KnownConfirmRecoveryDuplication("confirmed exact defect: eight new people and two disjoint groups")
    assert len(after["profiles"]) == 5
    assert len(after["groups"]) == 1


if __name__ == "__main__":
    root, build_id = Path(sys.argv[2]), sys.argv[3]
    store, settings, repository, service = compose(root)
    # Crash after actual four persona writes and group commit, immediately before
    # the production activation/alias commit. os._exit skips exception rollback.
    repository.activate = lambda *args, **kwargs: os._exit(77)
    service.confirm(build_id, [0, 1, 2, 3])


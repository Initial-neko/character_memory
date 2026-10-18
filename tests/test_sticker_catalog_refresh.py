from __future__ import annotations

from types import SimpleNamespace

from character_memory import api_resource_service


def test_refresh_snapshots_runtimes_before_iterating(tmp_path, monkeypatch):
    """Character creation inserts into the same dict from another thread.

    The refresh must iterate a snapshot; otherwise a concurrent registration
    raises "dictionary changed size during iteration" and the sticker import or
    group send turns into a 5xx.
    """
    runtimes = {"a": SimpleNamespace(sticker_catalog=None)}
    bundle = SimpleNamespace(runtimes=runtimes)
    service = api_resource_service.ApiResourceService(
        settings=SimpleNamespace(sticker_dir=str(tmp_path)),
        read_store=None,
        media_storage=None,
        current_bundle=lambda: bundle,
        require_bundle=lambda: bundle,
        character_profiles=lambda: [],
        ensure_character=lambda character_id: {"id": character_id},
    )

    seen: list[str] = []

    def fake_catalog(catalog, global_dir, character_id):
        if not seen:
            # Simulates POST /v1/characters registering a runtime mid-refresh.
            runtimes["b"] = SimpleNamespace(sticker_catalog=None)
        seen.append(character_id)
        return f"catalog-{character_id}"

    monkeypatch.setattr(api_resource_service, "character_sticker_catalog", fake_catalog)

    service.refresh_runtime_sticker_catalog(object())

    assert seen == ["a"]
    assert runtimes["a"].sticker_catalog == "catalog-a"
    assert runtimes["b"].sticker_catalog is None

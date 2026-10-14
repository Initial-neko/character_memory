from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from character_memory.vocation import Vocation, read_vocation, render_vocation
from character_memory.world_activity import WorldActivityService, WorldPulseRepository
from character_memory.storage.sqlite import SQLiteStore
from test_world_activity import NOW, make_access


def _persona(tmp_path: Path, name: str) -> Path:
    folder = tmp_path / name
    folder.mkdir()
    persona = folder / "persona.yaml"
    persona.write_text("id: " + name + "\nname: " + name + "\n", encoding="utf-8")
    return persona


def test_vocation_is_optional_strict_bounded_and_disabled_is_absent(tmp_path):
    persona = _persona(tmp_path, "researcher")
    assert read_vocation(persona) is None
    path = persona.parent / "vocation.yaml"
    path.write_text(
        "enabled: true\ntitle: 十月新番研究\nobjective: 跟进有来源的新番信息\n"
        "topics:\n  - 2026年10月新番\n  - 偏好匹配\n",
        encoding="utf-8",
    )
    vocation = read_vocation(persona)
    assert vocation is not None and vocation.title == "十月新番研究"
    assert "不是每次必须执行的任务" in render_vocation(vocation)

    path.write_text(
        "enabled: false\ntitle: 十月新番研究\nobjective: 研究番剧\n",
        encoding="utf-8",
    )
    assert read_vocation(persona) is None
    path.write_text(
        "title: 研究\nobjective: 任务\npermissions: write_repo\n",
        encoding="utf-8",
    )
    with pytest.raises(ValidationError):
        read_vocation(persona)
    path.write_text("a" * 8200, encoding="utf-8")
    with pytest.raises(ValueError, match="8192"):
        read_vocation(persona)


@pytest.mark.parametrize("topics", [["x", "x"], [""], ["a" * 121]])
def test_vocation_rejects_bad_topics(topics):
    with pytest.raises(ValidationError):
        Vocation(title="研究", objective="查找真实资料", topics=topics)


def test_vocation_is_character_local_and_uses_existing_world_browse_receipts(tmp_path):
    store, access, model = make_access(tmp_path, count=2)
    profiles = access.character_profiles()
    first = _persona(tmp_path, "c00")
    second = _persona(tmp_path, "c01")
    (first.parent / "vocation.yaml").write_text(
        "title: 十月新番研究员\nobjective: 调研2026年10月新番并寻找有据可查的作品\n"
        "topics:\n  - 2026年秋季番剧\n  - 动画角色喜好\n",
        encoding="utf-8",
    )
    profiles[0]["persona_path"] = str(first)
    profiles[1]["persona_path"] = str(second)
    access.character_profiles = lambda: profiles
    service = WorldActivityService(access, WorldPulseRepository(store))
    try:
        first_result = service.browse_character("c00", now=NOW, opportunity_id="vocation-c00")
        second_result = service.browse_character("c01", now=NOW, opportunity_id="vocation-c01")
        first_prompt = next(prompt for kind, session, prompt in model.calls
                            if kind == "PersonalBrowsePlan" and session.endswith("vocation-c00"))
        second_prompt = next(prompt for kind, session, prompt in model.calls
                             if kind == "PersonalBrowsePlan" and session.endswith("vocation-c01"))
        assert "十月新番研究员" in first_prompt
        assert "2026年秋季番剧" in first_prompt
        assert "不是事实" in first_prompt
        assert "十月新番研究员" not in second_prompt
        assert first_result["opportunity_status"] == "APPLIED"
        assert second_result["opportunity_status"] == "APPLIED"
        # Replaying a durable opportunity never causes another model call.
        previous_calls = len(model.calls)
        repeated = service.browse_character("c00", now=NOW, opportunity_id="vocation-c00")
        assert repeated["source_event_id"] == first_result["source_event_id"]
        assert len(model.calls) == previous_calls
    finally:
        store.close()


def test_invalid_vocation_does_not_break_character_world(tmp_path):
    store, access, model = make_access(tmp_path, count=1)
    profile = _persona(tmp_path, "c00")
    (profile.parent / "vocation.yaml").write_text(
        "title: 任务\nobjective: 研究\ncommand: rm -rf /\n",
        encoding="utf-8",
    )
    access.character_profiles = lambda: [
        {"id": "c00", "name": "c00", "persona_path": str(profile)}
    ]
    try:
        outcome = WorldActivityService(access, WorldPulseRepository(store)).browse_character(
            "c00", now=NOW, opportunity_id="invalid-vocation"
        )
        assert outcome["opportunity_status"] == "APPLIED"
        assert not any("rm -rf" in prompt for _, _, prompt in model.calls)
    finally:
        store.close()

"""Characters a batch build creates start with no direct chat of their own.

The ensemble flow's point is a *group*. Dropping five new names into the sidebar
buries the characters the user actually talks to, so the ones a build creates
are deferred: fully alive -- they speak in the group, in Space, in voice -- and
absent from the sidebar until the user opens a direct chat with them.

The polarity matters and is what these tests mostly pin. The marker's *absence*
is the normal state, so a character the user made one at a time, and every
character that existed before this feature did, keeps showing up unchanged.
"""

from pathlib import Path

import pytest
import yaml

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from character_memory.api import create_api
from character_memory.avatar_web import attach_avatar_routes
from character_memory.config import DIRECT_PENDING_FILENAME, load_settings
from character_memory.group_web import attach_group_routes


def _draft(name: str) -> dict:
    return {
        "name": name,
        "age": 24,
        "identity": "独立游戏音效设计师",
        "tagline": "会收集城市里奇怪的声音",
        "description": "很好奇，也有自己的判断。",
        "personality": ["好奇", "独立"],
        "conversation": "自然短句。",
        "expression": "偶尔用感叹号。",
        "questions": "真的好奇才追问。",
        "silence": "没想说的话时可以沉默。",
        "initiative": "有后续时可能主动提起。",
        "disagreement": "不同意会直接说理由。",
        "care": "记住具体事情。",
        "boundaries": ["不无条件迎合", "关系通过共同经历形成"],
    }


def _config(tmp_path: Path) -> Path:
    root = tmp_path / "personas"
    for name in ("rin", "momo"):
        directory = root / name
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "persona.yaml").write_text(
            yaml.safe_dump(
                {"id": name, "name": name.title(), "identity": "测试"},
                allow_unicode=True,
                sort_keys=False,
            ),
            encoding="utf-8",
        )
    path = tmp_path / "config.yaml"
    path.write_text(
        "\n".join(
            [
                'api_key: ""',
                'embedding_provider: "deterministic"',
                f'db_path: "{(tmp_path / "direct.db").as_posix()}"',
                f'persona_path: "{(root / "rin" / "persona.yaml").as_posix()}"',
            ]
        ),
        encoding="utf-8",
    )
    return path


def _create(client, name: str, *, source: str) -> dict:
    response = client.post(
        "/v1/characters",
        json={
            "draft": _draft(name),
            "creation": {"source": source, "prompt": "测试", "name_hint": name},
        },
    )
    assert response.status_code == 200, response.text
    return response.json()["character"]


def _sidebar(client) -> list[str]:
    return [item["id"] for item in client.get("/v1/characters").json()["characters"]]


def test_a_batch_built_character_stays_out_of_the_sidebar_until_asked(tmp_path: Path):
    config = _config(tmp_path)
    app = create_api(str(config))
    attach_avatar_routes(app)

    with TestClient(app) as client:
        before = _sidebar(client)
        assert "rin" in before

        created = _create(client, "Nova", source="ENSEMBLE_BUILDER")
        nova = created["id"]

        # Not in the sidebar...
        assert nova not in _sidebar(client)
        # ...but not hidden either: the picker can still see it, and it is a
        # perfectly ordinary active character as far as capacity is concerned.
        deferred = client.get("/v1/characters?include_deferred=true").json()
        assert nova in [item["id"] for item in deferred["characters"]]
        assert client.get("/v1/characters").json()["active_total"] == len(before) + 1
        assert (tmp_path / "personas" / nova / DIRECT_PENDING_FILENAME).is_file()

        # The avatar manager's list republishes itself into the sidebar, so it
        # must not answer with a deferred character even though it does answer
        # with every active one.
        assert nova not in [
            item["id"]
            for item in client.get("/v1/character-profiles").json()["characters"]
        ]

        # Opening it is a one-way, idempotent move, and the second press is not
        # an error: the button lives on a list that can be seconds stale.
        for _ in range(2):
            opened = client.post(f"/v1/characters/{nova}/open-direct")
            assert opened.status_code == 200
            assert opened.json()["direct_pending"] is False
        assert opened.json()["was_pending"] is False, "the second press had nothing left to do"

        assert nova in _sidebar(client)
        assert not (tmp_path / "personas" / nova / DIRECT_PENDING_FILENAME).exists()


def test_a_hand_made_character_is_never_deferred(tmp_path: Path):
    """Absence of the marker is the normal state -- the backward-compatible half.

    A character built one at a time goes straight into the sidebar, exactly as
    every character did before this marker existed.
    """

    config = _config(tmp_path)
    app = create_api(str(config))

    with TestClient(app) as client:
        made = _create(client, "Nova", source="PERSONA_BUILDER")
        assert made["id"] in _sidebar(client)
        assert not (tmp_path / "personas" / made["id"] / DIRECT_PENDING_FILENAME).exists()
        # And no marker means no key at all, so the payload is unchanged for
        # every caller that predates this feature.
        listed = next(
            item
            for item in client.get("/v1/characters").json()["characters"]
            if item["id"] == made["id"]
        )
        assert "direct_pending" not in listed


def test_a_deferred_character_is_still_offered_everywhere_else(tmp_path: Path):
    """Deferring is a sidebar decision, not a demotion.

    The group member payload has to say so explicitly: the browser can no longer
    infer "archived" from "absent from the sidebar", because deferred characters
    are absent from it too.
    """

    config = _config(tmp_path)
    app = create_api(str(config))
    attach_group_routes(app, str(config))

    with TestClient(app) as client:
        nova = _create(client, "Nova", source="ENSEMBLE_BUILDER")["id"]
        client.post("/v1/groups", json={"name": "三人行", "member_ids": ["rin", "momo", nova]})

        group = client.get("/v1/groups").json()["groups"][0]
        members = {item["id"]: item for item in group["members"]}
        assert set(members) == {"rin", "momo", nova}
        assert members[nova]["direct_pending"] is True
        assert members[nova]["archived"] is False
        assert members["rin"]["direct_pending"] is False


def test_the_sidebar_falls_back_rather_than_going_empty(tmp_path: Path):
    """An empty sidebar is not a state the browser can boot from.

    ``CM.loadCharacters`` throws "没有发现任何 Persona" on an empty list, so if
    deferring would hide every remaining character the list shows them anyway.
    """

    config = _config(tmp_path)
    app = create_api(str(config))

    with TestClient(app) as client:
        nova = _create(client, "Nova", source="ENSEMBLE_BUILDER")["id"]
        # Archive everything that is not deferred, leaving only Nova active.
        for character_id in ("momo", "rin"):
            assert client.post(f"/v1/characters/{character_id}/archive").status_code == 200

        assert _sidebar(client) == [nova], (
            "the one character left cannot be the one we hide"
        )


def test_an_archived_character_cannot_be_given_a_direct_chat(tmp_path: Path):
    """Restore first: the archive drawer is the surface for archived characters."""

    config = _config(tmp_path)
    app = create_api(str(config))

    with TestClient(app) as client:
        assert client.post("/v1/characters/momo/archive").status_code == 200
        response = client.post("/v1/characters/momo/open-direct")
        assert response.status_code == 409
        assert client.post("/v1/characters/nobody/open-direct").status_code == 404


def test_the_group_settings_offers_the_open_direct_action():
    """The chosen entry point for "later, add them to my conversations"."""

    root = Path(__file__).resolve().parents[1]
    script = (root / "src" / "character_memory" / "web" / "group_settings.js").read_text(
        encoding="utf-8"
    )

    assert "data-group-member-open-direct" in script
    assert "/open-direct" in script
    assert "加入我的对话" in script
    # The picker must ask for deferred characters explicitly, or the character
    # the user just built could not be added to a second group.
    assert "include_deferred=true" in script
    # And the notes must come from the payload, never from the sidebar list.
    assert "member?.archived" in script
    assert "member?.direct_pending" in script
    assert "pickerProfiles" in script

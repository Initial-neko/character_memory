"""Behavioural tests for the group ChatTurn folding in web/groups.js.

Grepping the script for a class name says nothing about whether consecutive
character events actually collapse into one turn, and a rename that flips the
merge semantics must fail loudly. So each test extracts the real
``foldMessages`` source out of ``web/groups.js``, evaluates it in Node, and
asserts on the turns it produces for concrete event lists.
"""

from __future__ import annotations

from json import dumps, loads
from pathlib import Path
import shutil
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]
GROUPS_JS = ROOT / "src" / "character_memory" / "web" / "groups.js"

HARNESS = r"""
const fs = require("fs");

const source = fs.readFileSync(process.argv[2], "utf8");
const anchor = source.indexOf("function foldMessages(messages)");
if (anchor < 0) {
  console.error("foldMessages is missing from groups.js");
  process.exit(2);
}
// Brace-match the function body so the extraction survives edits elsewhere in
// the IIFE. foldMessages contains no brace-bearing string literals by design.
let depth = 0;
let end = -1;
for (let i = source.indexOf("{", anchor); i < source.length; i++) {
  if (source[i] === "{") depth++;
  else if (source[i] === "}") {
    depth--;
    if (depth === 0) { end = i + 1; break; }
  }
}
if (end < 0) {
  console.error("could not brace-match foldMessages");
  process.exit(2);
}
const foldMessages = eval("(" + source.slice(anchor, end) + ")");

const input = JSON.parse(fs.readFileSync(process.argv[3], "utf8"));
process.stdout.write(JSON.stringify(foldMessages(input)));
"""


def run_fold(tmp_path: Path, messages) -> list[dict]:
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required to execute foldMessages behaviourally")
    harness = tmp_path / "fold_harness.cjs"
    harness.write_text(HARNESS, encoding="utf-8")
    payload = tmp_path / "messages.json"
    payload.write_text(dumps(messages), encoding="utf-8")
    completed = subprocess.run(
        [node, str(harness), str(GROUPS_JS), str(payload)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
    )
    assert completed.returncode == 0, completed.stderr
    return loads(completed.stdout)


def character(event_id, turn_id, actor_id, content, event_time="2026-09-10T20:13:05+00:00"):
    return {
        "id": event_id,
        "turn_id": turn_id,
        "role": "assistant",
        "actor_id": actor_id,
        "actor_name": actor_id.upper(),
        "content": content,
        "event_time": event_time,
    }


def user(event_id, turn_id, content, event_time="2026-09-10T20:13:00+00:00"):
    return {
        "id": event_id,
        "turn_id": turn_id,
        "role": "user",
        "actor_id": "user",
        "actor_name": "我",
        "content": content,
        "event_time": event_time,
    }


def test_consecutive_text_events_from_one_speaker_fold_into_one_turn(tmp_path):
    messages = [
        user(1, "turn-1", "大家怎么看"),
        character(2, "turn-1", "hashida", "审人先往后放放……"),
        character(3, "turn-1", "hashida", "至于那个「超级可爱」……"),
    ]
    turns = run_fold(tmp_path, messages)
    assert [entry["kind"] for entry in turns] == ["message", "turn"]
    turn = turns[1]
    assert turn["actor_id"] == "hashida"
    assert turn["actor_name"] == "HASHIDA"
    assert turn["turn_id"] == "turn-1"
    assert [item["id"] for item in turn["items"]] == [2, 3]
    assert turn["timestamp"] == messages[1]["event_time"]


def test_a_different_speaker_or_user_message_breaks_the_run(tmp_path):
    """Different characters must never merge, not even inside one turn.

    Interleaved A-B-A in a single turn keeps three visual turns, because
    merging across the B events would falsify the order people actually read.
    """
    messages = [
        character(1, "turn-1", "hashida", "第一句"),
        character(2, "turn-1", "momo", "另一人的反应"),
        character(3, "turn-1", "hashida", "回到同一轮的补充"),
    ]
    turns = run_fold(tmp_path, messages)
    assert [entry["kind"] for entry in turns] == ["turn", "turn", "turn"]
    assert [entry["actor_id"] for entry in turns] == ["hashida", "momo", "hashida"]
    assert all(len(entry["items"]) == 1 for entry in turns)

    interrupted = [
        character(1, "turn-1", "hashida", "第一句"),
        user(2, "turn-2", "插一句"),
        character(3, "turn-2", "hashida", "下一轮的回应"),
    ]
    turns = run_fold(tmp_path, interrupted)
    assert [entry["kind"] for entry in turns] == ["turn", "message", "turn"]
    assert turns[0]["turn_id"] == "turn-1"
    assert turns[2]["turn_id"] == "turn-2"


def test_same_speaker_across_two_turns_stays_separate(tmp_path):
    """The fold key is (turn_id, actor_id), not actor_id alone.

    One character replying in two different user turns produces two visual
    turns; collapsing them would attach one reply to the wrong question.
    """
    messages = [
        character(1, "turn-1", "hashida", "第一轮的回复"),
        character(2, "turn-2", "hashida", "第二轮的回复"),
    ]
    turns = run_fold(tmp_path, messages)
    assert len(turns) == 2
    assert {entry["turn_id"] for entry in turns} == {"turn-1", "turn-2"}


def test_media_events_are_turn_items_not_independent_turns(tmp_path):
    """A sticker between two text lines joins the same turn.

    The screenshot bug was a sticker rendering as its own bubble with its own
    speaker name; at the data level it must land inside the speaker's turn so
    the renderer can put it inside the one row.
    """
    sticker_event = character(2, "turn-1", "hashida", "[表情]")
    sticker_event["action"] = "STICKER"
    sticker_event["sticker"] = {"id": "s1", "label": "表情", "url": "/v1/stickers/s1/asset"}
    image_event = character(4, "turn-1", "hashida", "")
    image_event["action"] = "IMAGE"
    image_event["image"] = {"id": "i1", "label": "图", "url": "/v1/images/1"}
    messages = [
        user(0, "turn-1", "来点图"),
        character(1, "turn-1", "hashida", "看这个"),
        sticker_event,
        image_event,
        character(5, "turn-1", "hashida", "还有补充"),
    ]
    turns = run_fold(tmp_path, messages)
    assert [entry["kind"] for entry in turns] == ["message", "turn"]
    turn = turns[1]
    assert [item["id"] for item in turn["items"]] == [1, 2, 4, 5]


def test_empty_and_missing_turn_ids_do_not_crash_or_over_merge(tmp_path):
    messages = [
        character(1, None, "hashida", "没有轮次的事实"),
        user(2, None, "用户"),
        character(3, None, "hashida", "另一条"),
    ]
    turns = run_fold(tmp_path, messages)
    assert [entry["kind"] for entry in turns] == ["turn", "message", "turn"]
    assert turns[0]["turn_id"] is None

    assert run_fold(tmp_path, []) == []
    assert run_fold(tmp_path, [None]) == []

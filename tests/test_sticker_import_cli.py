from __future__ import annotations

import json
from pathlib import Path
import sys
import zipfile

import yaml

from character_memory.sticker_import_cli import main


def _write_sticker_zip(path: Path) -> None:
    rows = [
        {
            "id": "wave",
            "filename": "wave.png",
            "tag_zh": "挥手",
            "aliases": ["你好", "打招呼"],
            "set_id": "demo",
            "display_name": "Demo",
        }
    ]
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("all_tags.json", json.dumps(rows, ensure_ascii=False))
        archive.writestr("wave.png", b"not-a-real-png-but-valid-import-bytes")


def _write_config(tmp_path: Path) -> tuple[Path, Path]:
    persona = tmp_path / "personas" / "rin" / "persona.yaml"
    persona.parent.mkdir(parents=True)
    persona.write_text("id: rin\nname: Rin\n", encoding="utf-8")
    config = tmp_path / "config.yaml"
    config.write_text(
        yaml.safe_dump(
            {
                "db_path": str(tmp_path / "data" / "character-memory.db"),
                "persona_path": str(persona),
            },
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    return config, persona


def test_sticker_import_cli_writes_global_library(tmp_path, monkeypatch, capsys) -> None:
    config, persona = _write_config(tmp_path)
    archive = tmp_path / "stickers.zip"
    _write_sticker_zip(archive)

    monkeypatch.setattr(sys, "argv", ["sticker-import", str(archive), "--config", str(config)])
    assert main() == 0

    global_manifest = tmp_path / "data" / "stickers" / "manifest.yaml"
    legacy_manifest = persona.parent / "stickers" / "manifest.yaml"
    assert global_manifest.is_file()
    assert not legacy_manifest.exists()
    output = capsys.readouterr().out
    assert "Imported 1 stickers globally." in output


def test_sticker_import_cli_accepts_deprecated_character_flag(tmp_path, monkeypatch, capsys) -> None:
    config, _ = _write_config(tmp_path)
    archive = tmp_path / "stickers.zip"
    _write_sticker_zip(archive)

    monkeypatch.setattr(
        sys,
        "argv",
        ["sticker-import", str(archive), "--character", "rin", "--config", str(config)],
    )
    assert main() == 0
    output = capsys.readouterr().out
    assert "--character is deprecated" in output
    assert (tmp_path / "data" / "stickers" / "manifest.yaml").is_file()

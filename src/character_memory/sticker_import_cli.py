from __future__ import annotations

import argparse
from pathlib import Path

from character_memory.config import discover_character_profiles, load_settings
from character_memory.stickers import import_sticker_bundle, load_sticker_catalog


def main() -> int:
    parser = argparse.ArgumentParser(description="Import a tagged sticker ZIP into one character.")
    parser.add_argument("archive", help="Path to the sticker ZIP archive")
    parser.add_argument("--character", required=True, help="Character id, for example rin or character-c70f8f95")
    parser.add_argument("--config", default="config.yaml", help="Character Memory config path")
    args = parser.parse_args()

    settings = load_settings(args.config)
    profiles = discover_character_profiles(settings)
    profile = next((item for item in profiles if item["id"] == args.character), None)
    if profile is None:
        known = ", ".join(item["id"] for item in profiles)
        raise SystemExit(f"Unknown character: {args.character}. Known: {known}")

    archive_path = Path(args.archive)
    if not archive_path.is_file():
        raise SystemExit(f"Sticker archive not found: {archive_path}")

    result = import_sticker_bundle(profile["persona_path"], archive_path.read_bytes())
    catalog = load_sticker_catalog(profile["persona_path"])
    pack_text = ", ".join(f"{item['name']}({item['id']})" for item in result["packs"])
    print(f"Imported {result['imported']} stickers for {args.character}.")
    print(f"Packs: {pack_text}")
    print(f"Manifest: {result['manifest']}")
    print(f"Available stickers after merge: {len(catalog.public_items(args.character))}")
    print("Restart the web server if it is already running so the loaded character runtime refreshes its sticker catalog.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

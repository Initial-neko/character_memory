from __future__ import annotations

import argparse
from pathlib import Path

from character_memory.config import discover_character_profiles, load_settings, resolve_sticker_dir
from character_memory.stickers import import_sticker_bundle, load_global_sticker_catalog


def main() -> int:
    parser = argparse.ArgumentParser(description="Import a tagged sticker ZIP into the global sticker library.")
    parser.add_argument("archive", help="Path to the sticker ZIP archive")
    parser.add_argument(
        "--character",
        default="",
        help="Deprecated compatibility option. Sticker imports are global; the value is ignored after validation.",
    )
    parser.add_argument("--config", default="config.yaml", help="Character Memory config path")
    args = parser.parse_args()

    settings = load_settings(args.config)
    profiles = discover_character_profiles(settings)
    if args.character:
        known_ids = {item["id"] for item in profiles}
        if args.character not in known_ids:
            known = ", ".join(sorted(known_ids))
            raise SystemExit(f"Unknown character: {args.character}. Known: {known}")
        print("Warning: --character is deprecated; Sticker imports are global and shared by all characters/groups.")

    archive_path = Path(args.archive)
    if not archive_path.is_file():
        raise SystemExit(f"Sticker archive not found: {archive_path}")

    sticker_dir = resolve_sticker_dir(settings)
    persona_paths = [item["persona_path"] for item in profiles]
    result = import_sticker_bundle(
        settings.persona_path,
        archive_path.read_bytes(),
        target_dir=sticker_dir,
    )
    catalog = load_global_sticker_catalog(sticker_dir, persona_paths=persona_paths)
    pack_text = ", ".join(f"{item['name']}({item['id']})" for item in result["packs"])
    print(f"Imported {result['imported']} stickers globally.")
    print(f"Packs: {pack_text}")
    print(f"Manifest: {result['manifest']}")
    print(f"Available stickers after merge: {len(catalog.public_items())}")
    print("Restart the web server if it is already running so loaded runtimes refresh the global sticker catalog.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

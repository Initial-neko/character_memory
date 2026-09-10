# P0.9 — Clipboard Images and Custom Sticker Packs

## Scope

P0.9 keeps the existing P0.8 media protocol and improves input / sticker ergonomics:

- paste a real image from the clipboard into the main chat textarea;
- use the same preview + Vision path as file-picked images;
- make the final P0.8 renderer preserve Sticker rendering instead of overriding P0.7 behavior;
- group stickers into small WeChat-like pack grids;
- import already-tagged sticker ZIP bundles without manually authoring `manifest.yaml`.

## Clipboard image behavior

Focus the main chat textarea and paste an image (`Ctrl+V`). If the clipboard contains a supported image, the browser prevents the raw paste and opens the existing image preview panel. The user can add an optional caption and send it normally.

Supported formats remain JPEG / PNG / GIF / WebP, with the existing 8 MiB per-image limit. Clipboard images use the same P0.8 backend and are sent to the configured Vision model; they are not a separate protocol.

Plain-text clipboard paste is unchanged.

## Sticker pack UX

The sticker picker is intentionally compact:

- 52×52 px picker cells on desktop;
- 46×46 px image preview inside each cell;
- pack tabs across the top when multiple packs are available;
- compact sent Sticker rendering (max 112 px desktop, 96 px mobile);
- broken assets show an explicit small fallback instead of a silent empty slot.

The final `p0_8.js` renderer contains the Sticker fallback logic because P0.8 loads after P0.7 and therefore owns the final `addMessage()` implementation.

## Tagged ZIP format

The importer supports the generated bundle layout used by `chat_ai_sticker_packs_7sets.zip` and equivalent future packs.

Metadata can be either:

- one `all_tags.json` anywhere in the ZIP; or
- one or more per-pack `tags.json` files.

Each metadata row should contain a `filename` (or `file`) and may contain:

```json
{
  "set_id": "set_01",
  "set_name": "base_emotions",
  "display_name": "基础情绪包",
  "id": "set_01_01",
  "filename": "set_01_01_happy_smile.png",
  "category": "happy",
  "tag_zh": "开心微笑",
  "tag_en": "happy_smile",
  "aliases": ["开心", "高兴", "微笑"],
  "description": "轻松开心、表示赞同或心情不错。"
}
```

Imported metadata maps to the runtime Sticker contract:

- `id` -> Sticker ID;
- `filename` -> imported local file;
- `tag_zh` -> label;
- `category + tag_zh + tag_en + aliases` -> semantic tags;
- `description` -> description;
- `set_id` -> pack ID;
- `display_name` / `set_name` -> pack name.

## Import command

Stop the web server first (or restart it after import), then run:

```bash
uv run python -m character_memory.sticker_import_cli chat_ai_sticker_packs_7sets.zip --character <character_id>
```

For example:

```bash
uv run python -m character_memory.sticker_import_cli chat_ai_sticker_packs_7sets.zip --character character-c70f8f95
```

The importer resolves the character's Persona through `config.yaml`, writes the images into:

```text
personas/<character_id>/stickers/
```

and creates / updates:

```text
personas/<character_id>/stickers/manifest.yaml
```

Restart `uv run character-memory web` after importing if the runtime was already loaded, because a loaded `PersonRuntime` keeps its Sticker Catalog in memory.

## Default + custom stickers

A character-local manifest no longer hides the built-in test stickers. `load_sticker_catalog()` merges the default and character-local catalogs; a local Sticker with the same ID overrides the default item.

This lets a character use both the small built-in test pack and imported custom packs.

## Safety / archive limits

The importer:

- rejects path traversal such as `../file.png`;
- accepts only supported image extensions;
- limits archive size to 64 MiB;
- limits extracted size to 160 MiB;
- limits the archive to 500 files;
- requires each metadata filename to resolve to exactly one asset;
- writes the manifest only after all selected assets are successfully processed.

The imported Sticker images stay local and are not uploaded to GitHub by the importer.

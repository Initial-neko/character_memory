# Dev Console V0

Character Memory Dev Console is an internal diagnostics UI. It is intentionally separate from the product chat UI and does not own product state or credentials.

## Start

Run the three processes independently:

```bash
uv run character-memory web --port 8000
bash scripts/run-media.sh
uv run character-dev
```

Open:

```text
http://127.0.0.1:8002/dev
```

Defaults:

- Character Runtime: `http://127.0.0.1:8000`
- Media Runtime: `http://127.0.0.1:8001`
- Dev Console: `http://127.0.0.1:8002`

Optional deployment overrides:

```bash
export CHARACTER_DEV_CHARACTER_BASE_URL=http://127.0.0.1:8000
export CHARACTER_DEV_MEDIA_BASE_URL=http://127.0.0.1:8001
export CHARACTER_DEV_HOST=127.0.0.1
export CHARACTER_DEV_PORT=8002
export CHARACTER_CONFIG_PATH=config.yaml
```

## V0 surfaces

- System: probe Character Runtime and Media Runtime health without loading a PersonRuntime.
- LLM: send a raw prompt through the configured OpenAI-compatible provider/model. API keys remain server-side.
- TTS: proxy the Media Runtime TTS endpoint, play WAV output, and expose provider/device/inference/audio/RTF/total timing.
- ASR: upload WAV or record microphone audio in the browser, convert recording to 16 kHz PCM16 WAV, then proxy the Media Runtime ASR endpoint.
- Metrics: display the Media Runtime bounded latency buffer.

## Boundary

The Dev Console is not a generic Postman replacement. It should only expose Character Memory runtime capabilities. Future Vision, image-generation, embedding, and full-pipeline benchmark panels should reuse the same card/result pattern rather than adding arbitrary URL/header scripting.

The browser talks only to the Dev Console origin. The Dev Console keeps provider credentials and cross-service routing server-side.

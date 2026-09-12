# Dev Console V0

Character Memory Dev Console is an internal diagnostics UI. It is intentionally separate from the product chat UI and does not own product state or credentials.

## Recommended start: one local stack

After syncing the environment, start all three independent services with one command:

```bash
uv run character-stack
```

The launcher health-checks and starts only missing services:

- Character Runtime: `http://127.0.0.1:8000`
- Media Runtime: `http://127.0.0.1:8001`
- Dev Console: `http://127.0.0.1:8002`

It opens `http://127.0.0.1:8002/dev` by default. The Dev Console is the local developer front door and links directly to Chat `:8000` and Media health `:8001/health`.

Useful variants:

```bash
uv run character-stack --open chat
uv run character-stack --no-browser
```

`Ctrl+C` stops only processes started by this launcher. A service that was already running before `character-stack` is left alone.

The services remain separate processes. The launcher is orchestration only; it does not merge Character Runtime and Media Runtime into one failure or GPU boundary.

## Independent start for diagnosis

When isolating a failure, the three processes can still be started separately:

```bash
uv run character-memory web --port 8000
bash scripts/run-media.sh
uv run character-dev
```

Open:

```text
http://127.0.0.1:8002/dev
```

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
- Media Live Smoke: execute real `TTS -> WAV -> ASR` inference rather than a mock contract.
- Resource Monitor: sample system RAM, process RSS and NVIDIA VRAM on demand. Default browser refresh is 60 seconds and can be disabled or shortened for diagnosis.
- Metrics: display the Media Runtime bounded latency buffer.

## Boundary

The Dev Console is not a generic Postman replacement. It should only expose Character Memory runtime capabilities. Future Vision, image-generation, embedding, and full-pipeline benchmark panels should reuse the same card/result pattern rather than adding arbitrary URL/header scripting.

The browser talks only to the Dev Console origin for development probes. Provider credentials and cross-service routing remain server-side.

# DSH Companion

Implementation for [Issue #707](https://github.com/Initial-neko/character_memory/issues/707).
The chosen design is an unobtrusive composer button, a collapsible call capsule,
and optional voice, screen, image and avatar surfaces. DSH owns conversations,
memory, prompt submission and model access. TTS uses the existing GSV service.

## Current verification boundary

The implementation contains the actual DSH entry points, native composer slot,
call capsule, settings persistence, call/resource ownership, ASR/GSV HTTP
adapters, explicit microphone capture, and screen-capture lifecycle control.
It has been loaded in an isolated instance of the installed DSH runtime. The
button, menu, configuration save, minimize and end were exercised in that actual
host. Focused Node tests include loopback HTTP and SSE scope/cleanup.

The independent warm ASR service has executed real local recognition, including
a fixed Chinese sample synthesized by GSV and then transcribed through the
actual DSH plugin HTTP route. This proves that wiring, not general recognition
quality. Real microphone-to-chat-to-GSV playback, interruption, screen
permission/vision submission, effective-persona change detection and
image/Live2D integration are not yet accepted. Image and avatar menu entries
currently explain their unfinished state. Do not install this partial package
into a production profile yet.

```sh
node --test test/*.test.mjs
```

Tests start only temporary loopback HTTP servers. They do not load models,
request microphone or screen access, change DSH configuration, or invoke Codex.
Provider simulation is protocol evidence, not ASR accuracy, GSV quality or
actual DSH acceptance.
The same dependency-free command runs in Linux and Windows CI and uploads
JUnit results. It does not start the optional ASR service or load model assets.

During development the source directory may be linked into a disposable DSH
profile using an isolated `DSH_HOME` and `DSH_AGENTS_HOME`. Do not link it into a
production profile as a shortcut. Initial registration needs a host lifecycle
test; the frontend's ESM assets are served without cache, so UI-only revisions
can be checked by refreshing the browser. Backend revisions require reloading
the plugin or the owned test host, and are not frontend HMR.

## Optional independent ASR service

This same package includes `tools/asr-server.mjs`. Run it only when ASR is
needed, with an **already installed** `sherpa-onnx-node` module and an existing
SenseVoice model directory containing `model.int8.onnx` and `tokens.txt`:

```sh
node tools/asr-server.mjs --module /absolute/path/sherpa-onnx.js --model-dir /absolute/path/sensevoice --port 50131
```

Configure `asrEndpoint` to that loopback URL and `asrProtocol` to
`pcm16k-final`. No dependency installation, model download or other TTS engine
is performed. The worker stays warm and each final utterance has a separate
recognizer stream. One inference is admitted at a time; loading, busy, failed
and unknown outcomes do not trigger retries. The service does not send chat
messages or depend on the older voice plugin's global active session.

`GET /health` does not infer. `POST /asr?sessionId=...&final=1` accepts at most
60 seconds of mono 16 kHz Float32 little-endian PCM. Recognition appends to an
unchanged native draft, ignores blank results, and requires user confirmation.
The old `dsh-voice-mode` setting is retained for stored configurations but is
not marked ready by the independent service preflight; explicitly select the
new protocol when using this service.

GSV readiness requires its explicit `ready: true`; HTTP liveness alone is not
enough. Its first synthesis can still fail while loading or inferring. Use a
writable temporary directory in isolated runtime tests, and retain failures.

## Design invariants

- One install, one composer entry, one call owner and one audio playback owner.
- Minimize keeps the call active; end releases this plugin's media resources.
- Stop screen sharing independently of voice. A late frame cannot become current.
- Pause on navigation or configuration changes; do not silently migrate a call.
- Bind asynchronous work to session, generation, persona and configuration.
- Confirm ASR text before sending, without overwriting an edited native draft.
- No automatic screen polling, model downloads, startup inference or TTS fallback.
- No second message database, agent loop or replacement chat interface.

## Source and reuse

DSH composer/settings/session interfaces were inspected in the installed host,
alongside the locally installed `dsh-meme`, `dsh-cosplay` and `dsh-voice-mode`
packages. This package uses their public integration shapes and existing wire
contracts; it does not vendor their application implementations. The GSV wire
matches the existing local `gsv-voice` adapter. The independently tested Codex
image prototype is a future adapter input, not proof that this package has
finished image or host integration.

Screen permission and track lifetime follow the browser MediaDevices and
MediaStreamTrack contracts. Native transparent desktop windows require an
actual supported host capability; a mockup does not establish that support.

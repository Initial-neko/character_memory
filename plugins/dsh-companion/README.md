# DSH Companion

Implementation for [Issue #707](https://github.com/Initial-neko/character_memory/issues/707).
The chosen design is an unobtrusive composer button, a collapsible call capsule,
and optional voice, screen, image and avatar surfaces. DSH owns conversations,
memory, prompt submission and model access. TTS uses the existing GSV service.

## Current verification boundary

The initial implementation contains call/resource ownership, configuration
validation with serialized compare-and-swap persistence, existing ASR/GSV HTTP
adapters, and screen-capture lifecycle control. These modules have focused Node
tests. The plugin entry point, actual DSH UI wiring and complete real-provider
acceptance are still under implementation. Do not install this partial package
into a production profile yet.

```sh
node --test test/*.test.mjs
```

Tests start only temporary loopback HTTP servers. They do not load models,
request microphone or screen access, change DSH configuration, or invoke Codex.
Provider simulation is protocol evidence, not ASR accuracy, GSV quality or
actual DSH acceptance.

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

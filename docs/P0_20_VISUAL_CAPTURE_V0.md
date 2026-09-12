# P0.20 Visual Capture V0

## Goal

Extend the existing persistent voice Call Session with a lightweight visual channel. The character can observe a front camera or a browser-approved display surface (browser tab, application window, or whole screen) without continuously uploading raw video.

The V0 contract is intentionally frame-based:

```text
Camera / Display MediaStream
        ↓ browser-only sampling (~800 ms)
small analysis frame → cheap pixel-change score
        ↓
recent candidate ring buffer (max 18)
        ↓ when one speech turn ends
select up to 3–5 chronological keyframes
        ↓
Vision LLM request together with ASR text
```

Raw video never enters Character Runtime, Media Runtime, SQLite, Runtime Trace, or chat history.

## Browser sources

### Camera

`navigator.mediaDevices.getUserMedia()` is used with `facingMode: {ideal: "user"}`. On mobile this prefers the front camera. Desktop browser/device selection remains under the browser and OS permission model.

### Display

`navigator.mediaDevices.getDisplayMedia()` is used with `displaySurface: "window"` as a hint. The browser still owns the final chooser and authorization. The user can select a tab, a window such as IntelliJ IDEA, or the whole screen depending on browser/OS support.

The application must never silently capture a particular window.

## Call Session semantics

Visual capture belongs to the existing call session, not the currently browsed Character Memory page.

- minimizing the call does **not** stop camera/display capture;
- navigating to another chat does **not** retarget or stop the visual session;
- ending the call stops microphone, SSE, and visual tracks;
- browser/system display-share termination is detected from the video track `ended` event;
- V0 allows one visual source at a time: camera **or** display.

## Sampling and keyframes

`web/visual_capture.js` is dependency-free and reusable by both Chat and Dev Console.

Default behavior:

- sample attempt every 800 ms;
- analysis resolution: 64 px wide grayscale image;
- keep frames after meaningful change or at least once every 4 seconds;
- candidate ring buffer: 18 frames;
- encoded frame: JPEG, max side 512 px, quality 0.72;
- speech turn selection: max 4 frames by default, hard maximum 5;
- selection preserves chronology and prefers endpoints plus high-change frames.

This is intentionally not OpenCV/video understanding. The target is low CPU/RAM use and predictable cloud Vision cost.

## Backend contract

When no keyframes are available, voice keeps using the normal async routes.

When keyframes exist:

- direct: `POST /v1/visual/direct/messages`
- group: `POST /v1/visual/groups/{conversation_id}/messages`

Request frames are transient `data:image/...;base64,...` values. The server:

1. validates 1–5 frames;
2. verifies actual JPEG/PNG/WebP magic bytes;
3. enforces 2 MiB per frame and 6 MiB total;
4. persists only the user text plus small `visual_capture` metadata;
5. passes frame data URLs only through the in-memory ReactionScheduler;
6. sends the ordered frame list to the already-existing multimodal `PersonRuntime` / Vision model path.

The asynchronous scheduler now supports multiple image URLs per persisted user event while remaining backward-compatible with the existing single uploaded-image path.

## TTS speaker pool

While integrating P0.20, the stable speaker fallback was corrected to the locally auditioned female speaker pool:

```text
[0, 2, 5]
```

Character IDs still map deterministically; voices are not randomized per turn.

## Dev Console

`uv run character-stack` opens Dev Console as usual.

The **Visual Capture + Vision** card supports:

1. start front camera;
2. open the browser screen/window/tab chooser;
3. inspect live preview and candidate count;
4. force a sample;
5. select up to 3/4/5 keyframes from the recent 15 seconds;
6. inspect the selected thumbnails;
7. call `POST /v1/dev/vision` against the configured real `vision_model`.

This matters because static unit tests do not prove that the configured cloud Vision provider can actually accept multimodal requests.

## Manual acceptance

Run:

```bash
uv run character-stack
```

### Dev test

1. Open `http://127.0.0.1:8002/dev`.
2. In Visual Capture + Vision, start Camera.
3. Move an object or change the scene; verify candidate count increases.
4. Select keyframes and verify 1–5 thumbnails appear.
5. Run Vision Test and verify the configured Vision model describes only the captured content.
6. Repeat with Screen and select one application window.

### Call test

1. Start a direct voice call.
2. Turn on Camera and speak: “你看看我现在拿的东西是什么？”
3. Verify transcript notes attached visual keyframes and the reply uses visual context.
4. Minimize the call; verify audio and camera remain active and the dock shows `📷`.
5. Navigate to another chat; speak again and verify the original call target still receives the turn.
6. Switch to Screen; pick an IDE/browser window and ask about visible content.
7. End the call; verify browser camera/screen indicators turn off.
8. Repeat in a group call and verify the same keyframes feed the shared group reaction turn.

## Out of scope for V0

- raw video upload or storage;
- continuous cloud video understanding;
- OpenCV / local vision model / GPU video pipeline;
- simultaneous camera + screen capture;
- OCR-specific preprocessing;
- proactive/background visual interpretation without a user speech turn;
- full-duplex voice barge-in.

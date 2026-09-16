# Mobile Access — Tailscale Serve

Status: V1 supported deployment path for private Android/iOS access.

## 1. Boundary

Mobile access uses **Tailscale + Tailscale Serve** only.

- Do not expose router ports to the public Internet.
- Do not use Tailscale Funnel for Character Memory.
- Character Runtime stays on `127.0.0.1:8000`.
- Media Runtime stays on `127.0.0.1:8001`.
- Dev Console `:8002`, Settings Center `:8003`, TTS Provider/Lab `:9002`, and optional CosyVoice `:9012` remain PC-local in V1.

The mobile-facing topology is:

```text
Phone browser
    |
    | private tailnet + HTTPS
    v
Tailscale Serve on PC
    |-- https://<node>.<tailnet>.ts.net:443  -> 127.0.0.1:8000
    `-- https://<node>.<tailnet>.ts.net:8443 -> 127.0.0.1:8001
```

## 2. One-time Tailscale setup

Install Tailscale on the PC and phone, and sign both devices into the same tailnet.

Tailscale Serve requires the tailnet HTTPS/MagicDNS capability. If HTTPS has not been enabled for the tailnet yet, follow the prompt from the Tailscale CLI/admin console and enable HTTPS certificates.

## 3. Configure Tailscale Serve and obtain the hostname

Run from Git Bash on the PC:

```bash
bash scripts/tailscale-serve.sh
```

Equivalent commands are:

```bash
tailscale serve --https=443 --bg 8000
tailscale serve --https=8443 --bg 8001
tailscale serve status
```

The status output provides the HTTPS hostname, for example:

```text
https://<node>.<tailnet>.ts.net
```

`--bg` keeps the Serve configuration active after the terminal exits. To remove these mappings later, use the matching `tailscale serve ... off` commands or manage the Serve configuration explicitly. Do not switch either port to Funnel.

## 4. Start or restart Character Memory with the exact mobile origin

The browser page is served from `https://<node>.<tailnet>.ts.net`, while Media Runtime is on HTTPS port `8443`. Because a different port is a different browser origin, Media Runtime must allow the exact chat origin through CORS.

In Git Bash, after obtaining the hostname from step 3:

```bash
export CHARACTER_MEDIA_CORS_ORIGINS="http://127.0.0.1:8000,http://localhost:8000,https://<node>.<tailnet>.ts.net"
uv run character-stack --no-browser
```

Replace `<node>.<tailnet>.ts.net` with the hostname shown by `tailscale serve status`.

If the stack was already running before this environment variable was set, restart it so Media Runtime receives the new CORS configuration.

Do not use a broad `*.ts.net` CORS rule. V1 deliberately allows the exact Character Memory origin only.

## 5. Open on the phone

With Tailscale connected on the phone, open:

```text
https://<node>.<tailnet>.ts.net
```

The chat frontend automatically resolves Media Runtime as follows:

```text
local/non-Tailscale page          -> http://127.0.0.1:8001
HTTPS *.ts.net Serve page         -> https://same-hostname:8443
explicit localStorage override    -> override wins
```

The automatic `:8443` mapping is intentionally scoped to HTTPS `*.ts.net` pages. Other reverse-proxy/custom-domain deployments must set the existing override explicitly instead of inheriting a Tailscale assumption.

The existing key remains the escape hatch for non-standard deployments:

```text
character-memory:media-base-url
```

## 6. Expected V1 mobile capabilities

Expected to work over the private HTTPS path:

- direct chat and group chat;
- SSE realtime Character messages;
- stickers and generated images;
- incoming message cue sound after browser audio has been unlocked by user interaction;
- dictation ASR;
- TTS playback and voice calls;
- camera capture when the mobile browser exposes `getUserMedia`.

Display/screen sharing is browser/platform-dependent on mobile and is not a V1 compatibility promise. The UI hides that control when `getDisplayMedia` is unavailable.

## 7. Security rationale

Tailscale encrypts node-to-node traffic, while Serve adds browser-visible HTTPS. This matters because microphone/camera APIs require a secure browser context on mobile.

The backend services continue to listen on localhost only. Tailscale Serve is the only mobile-facing entrypoint, and access-control rules in the tailnet remain effective.

## 8. Troubleshooting

If chat works but ASR/TTS fails:

1. Open `tailscale serve status` and confirm both `443 -> 8000` and `8443 -> 8001` exist.
2. Confirm the phone URL is HTTPS, not a raw `http://100.x.x.x` address.
3. Confirm `CHARACTER_MEDIA_CORS_ORIGINS` contains the exact HTTPS chat origin, without `:8443`.
4. Restart Media Runtime after changing CORS.
5. On the PC, verify `http://127.0.0.1:8001/health` is healthy.

If microphone/camera permission is unavailable, first confirm the page is loaded from the HTTPS `*.ts.net` hostname and that the browser has OS-level microphone/camera permission.

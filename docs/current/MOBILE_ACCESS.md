# Mobile Access — Tailscale Serve

Status: V1 supported deployment path for private Android/iOS access.

## 1. Boundary

Mobile access uses **Tailscale + Tailscale Serve** only.

- Do not expose router ports to the public Internet.
- Do not use Tailscale Funnel for Character Memory.
- Character Runtime stays on `127.0.0.1:8000`.
- Media Runtime stays on `127.0.0.1:8001`.
- Dev Console `:8002`, Settings Center `:8003`, TTS Provider/Lab `:9002`, and optional CosyVoice `:9012` remain PC-local in V1.

```text
Phone browser
    |
    | private tailnet + HTTPS
    v
Tailscale Serve on PC
    |-- https://<node>.<tailnet>.ts.net:443  -> 127.0.0.1:8000
    `-- https://<node>.<tailnet>.ts.net:8443 -> 127.0.0.1:8001
```

## 2. One-time prerequisite

Install Tailscale on the PC and phone and sign both devices into the same tailnet.

Tailscale Serve requires the tailnet HTTPS/MagicDNS capability. If HTTPS has not been enabled yet, follow the Tailscale prompt/admin-console flow to enable HTTPS certificates.

## 3. Normal startup

The normal mobile startup path is now one command from Git Bash:

```bash
bash scripts/mobile-start.sh
```

The script:

1. finds `tailscale` / `tailscale.exe`;
2. checks `tailscale status --json` and requires `BackendState=Running`;
3. reads the local node `*.ts.net` DNS name;
4. configures private Tailscale Serve mappings through `scripts/tailscale-serve.sh`;
5. starts `character-stack` with the exact mobile HTTPS origin through `--mobile-origin`;
6. prints the phone URL and the validation command.

The stack remains foreground-managed exactly like the normal `character-stack` command. `Ctrl+C` stops processes started by that launcher.

Example output URL:

```text
https://<node>.<tailnet>.ts.net
```

Open that URL on the phone while Tailscale is connected.

### Config override

Arguments after `mobile-start.sh` are passed to `character-stack`, so a non-default config remains possible:

```bash
bash scripts/mobile-start.sh --config config.yaml
```

## 4. Explicit stack parameter

The script is the preferred path, but the stack also has an explicit startup parameter:

```bash
uv run character-stack \
  --no-browser \
  --mobile-origin "https://<node>.<tailnet>.ts.net"
```

`--mobile-origin`:

- requires an absolute HTTPS origin on the default HTTPS port `443`;
- merges the exact origin into `CHARACTER_MEDIA_CORS_ORIGINS` while preserving the local chat origins;
- does not bind Media Runtime to a public interface;
- refuses to silently reuse an already-running Media Runtime when that process does not return the requested CORS origin.

If an old stack is already running without the mobile CORS origin, stop that stack and run `mobile-start.sh` again. The launcher deliberately does not kill arbitrary Python processes.

## 5. Validation

After the stack reports ready, run in another Git Bash terminal:

```bash
bash scripts/mobile-check.sh
```

This is a read-only diagnostic script. It does not start, stop, reset, or reconfigure services.

It checks:

- Tailscale backend is connected;
- local `*.ts.net` HTTPS hostname exists;
- an online Android/iOS peer is visible when Tailscale reports one;
- Serve contains the `:8000` and `:8001` backends;
- local Character Runtime `/health`;
- local Media Runtime `/health`;
- Media Runtime returns `Access-Control-Allow-Origin` for the exact mobile origin;
- remote Character HTTPS `/health`;
- remote Media HTTPS `:8443/health`.

A healthy setup ends with:

```text
Mobile access validation: PASS
Phone URL: https://<node>.<tailnet>.ts.net
```

The mobile-peer check is informational: a sleeping/offline phone may produce a warning while the server-side configuration itself is still valid.

## 6. Low-level Serve helper

`mobile-start.sh` uses:

```bash
bash scripts/tailscale-serve.sh
```

Equivalent Serve commands are:

```bash
tailscale serve --https=443 --bg 8000
tailscale serve --https=8443 --bg 8001
tailscale serve status
```

`--bg` makes the Serve configuration persist in the background. The helper does **not** run `serve reset` and does **not** enable Funnel, so unrelated Serve configuration is not intentionally cleared and nothing is made public.

## 7. Browser routing behavior

The same Web application serves PC and mobile clients.

```text
local/non-Tailscale page          -> Media http://127.0.0.1:8001
HTTPS *.ts.net Serve page         -> Media https://same-hostname:8443
explicit localStorage override    -> override wins
```

The existing override key remains:

```text
character-memory:media-base-url
```

Automatic `:8443` routing is intentionally limited to HTTPS `*.ts.net` pages. Other reverse-proxy/custom-domain deployments must configure the override explicitly.

## 8. Expected V1 mobile capabilities

Expected over the private HTTPS path:

- direct chat and group chat;
- SSE realtime Character messages;
- stickers and generated images;
- incoming message cue sound after browser audio is unlocked by user interaction;
- dictation ASR;
- TTS playback and voice calls;
- camera capture when the mobile browser exposes `getUserMedia`.

Display/screen sharing is browser/platform-dependent on mobile and is not a V1 compatibility promise. The UI hides that control when `getDisplayMedia` is unavailable.

Settings Center remains PC-local. A remote `*.ts.net` page hides the Settings link rather than pointing the phone at its own `127.0.0.1:8003`.

## 9. Security rationale

Tailscale encrypts node-to-node traffic and Serve gives the browser a valid HTTPS secure context. This allows browser microphone/camera APIs without changing Character or Media Runtime to listen on `0.0.0.0`.

The only mobile-facing entrypoints are the private tailnet Serve endpoints. Funnel and router port-forwarding remain out of scope.

## 10. Targeted troubleshooting

Start with:

```bash
bash scripts/mobile-check.sh
```

If it reports a Serve failure:

```bash
tailscale serve status
```

If local Media is healthy but the CORS check fails, stop the old stack and restart with:

```bash
bash scripts/mobile-start.sh
```

If remote HTTPS checks fail while local checks pass, inspect Tailscale HTTPS/Serve rather than changing Character Memory bind addresses.

If microphone/camera permission is unavailable, confirm the phone page is the HTTPS `*.ts.net` URL and that the mobile OS/browser has microphone/camera permission.

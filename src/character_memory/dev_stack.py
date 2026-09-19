from __future__ import annotations

import argparse
import os
from pathlib import Path
import subprocess
import sys
import time
from urllib.error import URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen
import webbrowser

import yaml


ROOT = Path(__file__).resolve().parents[2]
LOCAL_MEDIA_ORIGINS = ("http://127.0.0.1:8000", "http://localhost:8000")


def _healthy(url: str, timeout: float = 0.8) -> bool:
    try:
        with urlopen(url, timeout=timeout) as response:
            return 200 <= int(response.status) < 300
    except (OSError, URLError):
        return False


def _normalize_mobile_origin(value: str | None) -> str | None:
    raw = str(value or "").strip().rstrip("/")
    if not raw:
        return None
    parsed = urlsplit(raw)
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("mobile origin contains an invalid port") from exc
    if parsed.scheme != "https" or not parsed.hostname:
        raise ValueError("mobile origin must be an absolute HTTPS origin")
    if port not in {None, 443}:
        raise ValueError("mobile origin must use the default HTTPS port 443")
    if parsed.path not in {"", "/"} or parsed.query or parsed.fragment or parsed.username or parsed.password:
        raise ValueError("mobile origin must not contain credentials, path, query, or fragment")
    return f"https://{parsed.hostname}"


def _merge_cors_origins(current: str | None, mobile_origin: str | None) -> str:
    values: list[str] = []
    for item in [*LOCAL_MEDIA_ORIGINS, *(str(current or "").split(",")), mobile_origin or ""]:
        value = str(item or "").strip().rstrip("/")
        if value and value not in values:
            values.append(value)
    return ",".join(values)


def _cors_allows_origin(url: str, origin: str, timeout: float = 0.8) -> bool:
    try:
        request = Request(url, headers={"Origin": origin})
        with urlopen(request, timeout=timeout) as response:
            allowed = str(response.headers.get("Access-Control-Allow-Origin") or "").strip()
            return 200 <= int(response.status) < 300 and allowed == origin
    except (OSError, URLError):
        return False


def _configured_tts_provider(config_path: str) -> str:
    try:
        path = Path(config_path)
        data = yaml.safe_load(path.read_text(encoding="utf-8")) if path.is_file() else {}
        value = str((data or {}).get("tts_provider", "kokoro") or "kokoro").strip().lower()
        return value if value in {"kokoro", "sherpa", "qwen3", "edge", "gsv"} else "kokoro"
    except (OSError, yaml.YAMLError):
        return "kokoro"


def _configured_tts_device(config_path: str) -> str:
    try:
        path = Path(config_path)
        data = yaml.safe_load(path.read_text(encoding="utf-8")) if path.is_file() else {}
        value = str((data or {}).get("tts_device", "cpu") or "cpu").strip().lower()
        return value if value in {"cpu", "cuda"} else "cpu"
    except (OSError, yaml.YAMLError):
        return "cpu"


def _media_env(base: dict[str, str]) -> dict[str, str]:
    env = dict(base)
    model_root = Path(env.get("CHARACTER_MEDIA_MODEL_ROOT", ROOT / "models")).resolve()
    asr_dir = model_root / "sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17"
    tts_dir = model_root / "sherpa-onnx-vits-zh-ll"

    defaults = {
        "CHARACTER_MEDIA_ASR_MODEL": asr_dir / "model.int8.onnx",
        "CHARACTER_MEDIA_ASR_TOKENS": asr_dir / "tokens.txt",
        "CHARACTER_MEDIA_TTS_MODEL": tts_dir / "model.onnx",
        "CHARACTER_MEDIA_TTS_TOKENS": tts_dir / "tokens.txt",
        "CHARACTER_MEDIA_TTS_LEXICON": tts_dir / "lexicon.txt",
        "CHARACTER_MEDIA_TTS_DICT_DIR": tts_dir / "dict",
        "CHARACTER_MEDIA_TTS_RULE_FSTS": f"{tts_dir / 'phone.fst'},{tts_dir / 'number.fst'}",
    }
    for key, value in defaults.items():
        env.setdefault(key, str(value))
    env.setdefault("CHARACTER_MEDIA_ASR_DEVICE", "cpu")
    env.setdefault("CHARACTER_MEDIA_ASR_THREADS", "2")
    env.setdefault("CHARACTER_MEDIA_ASR_LANGUAGE", "auto")
    env.setdefault("CHARACTER_MEDIA_TTS_DEVICE", "cpu")
    env.setdefault("CHARACTER_MEDIA_TTS_THREADS", "2")
    env.setdefault("CHARACTER_MEDIA_HOST", "127.0.0.1")
    env.setdefault("CHARACTER_MEDIA_PORT", "8001")
    env.setdefault("CHARACTER_TTS_LAB_BASE", "http://127.0.0.1:9002")
    env.setdefault("CHARACTER_QWEN3_TTS_BASE", "http://127.0.0.1:9013")
    env.setdefault("CHARACTER_TTS_GSV_VOICE", env.get("GSV_TTS_VOICE", "murasame"))
    return env


def _tts_lab_env(base: dict[str, str], config_path: str) -> dict[str, str]:
    env = dict(base)
    env.setdefault("CHARACTER_TTS_LAB_HOST", "127.0.0.1")
    env.setdefault("CHARACTER_TTS_LAB_PORT", "9002")
    env.setdefault("CHARACTER_TTS_LAB_MEDIA_BASE", "http://127.0.0.1:8001")
    env.setdefault("CHARACTER_TTS_COSYVOICE_BASE", "http://127.0.0.1:9012")
    env.setdefault("CHARACTER_TTS_QWEN3_BASE", "http://127.0.0.1:9013")
    env.setdefault("CHARACTER_TTS_GSV_BASE", "http://127.0.0.1:9014")
    env.setdefault("CHARACTER_TTS_KOKORO_DEVICE", _configured_tts_device(config_path))
    return env


def _settings_env(base: dict[str, str]) -> dict[str, str]:
    env = dict(base)
    env.setdefault("CHARACTER_SETTINGS_HOST", "127.0.0.1")
    env.setdefault("CHARACTER_SETTINGS_PORT", "8003")
    env.setdefault("CHARACTER_SETTINGS_CHARACTER_BASE", "http://127.0.0.1:8000")
    env.setdefault("CHARACTER_SETTINGS_MEDIA_BASE", "http://127.0.0.1:8001")
    env.setdefault("CHARACTER_SETTINGS_DEV_BASE", "http://127.0.0.1:8002")
    env.setdefault("CHARACTER_SETTINGS_TTS_LAB_BASE", "http://127.0.0.1:9002")
    return env


def _spawn(name: str, command: list[str], env: dict[str, str]) -> subprocess.Popen:
    print(f"stack: starting {name}: {' '.join(command)}", flush=True)
    return subprocess.Popen(command, cwd=ROOT, env=env)


def main() -> None:
    parser = argparse.ArgumentParser(prog="character-stack")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--open", choices=("dev", "chat", "settings", "tts"), default="dev")
    parser.add_argument(
        "--mobile-origin",
        default=None,
        help="exact HTTPS browser origin on port 443 allowed to call Media Runtime",
    )
    args = parser.parse_args()

    try:
        mobile_origin = _normalize_mobile_origin(args.mobile_origin)
    except ValueError as exc:
        parser.error(str(exc))

    base_env = os.environ.copy()
    base_env["CHARACTER_MEMORY_CONFIG"] = args.config
    base_env["CHARACTER_CONFIG_PATH"] = args.config
    base_env.setdefault("CHARACTER_DEV_CHARACTER_BASE_URL", "http://127.0.0.1:8000")
    base_env.setdefault("CHARACTER_DEV_MEDIA_BASE_URL", "http://127.0.0.1:8001")
    if mobile_origin:
        base_env["CHARACTER_MEDIA_CORS_ORIGINS"] = _merge_cors_origins(
            base_env.get("CHARACTER_MEDIA_CORS_ORIGINS"),
            mobile_origin,
        )

    python = sys.executable
    tts_provider = _configured_tts_provider(args.config)
    specs = [
        (
            "Character Runtime",
            "http://127.0.0.1:8000/health",
            [python, "-m", "character_memory.cli", "--config", args.config, "web", "--port", "8000"],
            base_env,
        ),
        (
            "Media Runtime",
            "http://127.0.0.1:8001/health",
            [python, "-m", "character_memory.media_bootstrap"],
            _media_env(base_env),
        ),
        (
            "Dev Console",
            "http://127.0.0.1:8002/health",
            [python, "-m", "character_memory.dev_server"],
            base_env,
        ),
        (
            "Settings Center",
            "http://127.0.0.1:8003/health",
            [python, "-m", "character_memory.settings_server"],
            _settings_env(base_env),
        ),
        (
            "TTS Provider Lab",
            # Do not use /health here: that endpoint probes the provider inventory,
            # including the optional CosyVoice sidecar. If that sidecar is absent,
            # the probe can take seconds while the stack readiness timeout is 0.8s.
            # /tts proves the lab process itself is listening without provider I/O.
            "http://127.0.0.1:9002/tts",
            [python, "-m", "character_memory.tts_lab"],
            _tts_lab_env(base_env, args.config),
        ),
    ]

    if tts_provider == "qwen3":
        qwen_venv = Path(os.getenv("QWEN3_TTS_VENV", ROOT / ".venv-qwen3-tts"))
        qwen_python = qwen_venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        if not qwen_python.is_file():
            raise SystemExit(
                f"Qwen3-TTS is selected but its isolated environment is missing: {qwen_python}. "
                "Run: bash scripts/setup-qwen3-tts.sh"
            )
        qwen_env = dict(base_env)
        qwen_env["PYTHONPATH"] = str(ROOT / "src")
        qwen_env.setdefault("HF_HOME", str((ROOT / "models" / "huggingface").resolve()))
        qwen_device = "cuda:0" if _configured_tts_device(args.config) == "cuda" else "cpu"
        qwen_dtype = "auto"
        specs.insert(
            1,
            (
                "Qwen3-TTS Runtime",
                "http://127.0.0.1:9013/health",
                [
                    str(qwen_python),
                    "-m",
                    "character_memory.qwen3_tts_experiment",
                    "--device",
                    qwen_device,
                    "--dtype",
                    qwen_dtype,
                ],
                qwen_env,
            ),
        )

    if tts_provider == "gsv":
        gsv_root = Path(os.getenv("GSV_TTS_ROOT", ROOT / ".external" / "GSV-TTS-Lite"))
        gsv_venv = Path(os.getenv("GSV_TTS_VENV", gsv_root / ".venv"))
        gsv_python = gsv_venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        if not gsv_python.is_file():
            raise SystemExit(
                f"GSV-TTS-Lite is selected but its isolated environment is missing: {gsv_python}. "
                "Keep the validated GSV environment under .external/GSV-TTS-Lite/.venv or set GSV_TTS_VENV."
            )
        missing_gsv = [
            name for name in ("GSV_TTS_GPT_MODEL", "GSV_TTS_SOVITS_MODEL", "GSV_TTS_REF_AUDIO", "GSV_TTS_REF_TEXT")
            if not str(base_env.get(name, "")).strip()
        ]
        if missing_gsv:
            raise SystemExit(
                "GSV-TTS-Lite is selected but required environment variables are missing: "
                + ", ".join(missing_gsv)
            )
        gsv_env = dict(base_env)
        gsv_env["PYTHONPATH"] = os.pathsep.join([str(ROOT / "src"), str(gsv_root)])
        gsv_env.setdefault("GSV_TTS_HOST", "127.0.0.1")
        gsv_env.setdefault("GSV_TTS_PORT", "9014")
        gsv_env.setdefault("GSV_TTS_PRELOAD", "1")
        gsv_env.setdefault("GSV_TTS_VOICE", "murasame")
        specs.insert(
            1,
            (
                "GSV-TTS-Lite Runtime",
                "http://127.0.0.1:9014/health",
                [str(gsv_python), "-m", "character_memory.gsv_tts_experiment"],
                gsv_env,
            ),
        )

    owned: list[tuple[str, subprocess.Popen]] = []
    try:
        for name, health_url, command, env in specs:
            if _healthy(health_url):
                if name == "Media Runtime" and mobile_origin and not _cors_allows_origin(health_url, mobile_origin):
                    raise SystemExit(
                        "Media Runtime is already running without the requested mobile CORS origin. "
                        "Stop the existing Character Memory stack and run mobile-start.sh again."
                    )
                print(f"stack: {name} already running ({health_url})", flush=True)
                continue
            owned.append((name, _spawn(name, command, env)))

        deadline = time.time() + 25.0
        pending = {name: health_url for name, health_url, _command, _env in specs}
        while pending and time.time() < deadline:
            for name, url in list(pending.items()):
                if _healthy(url):
                    print(f"stack: {name} ready -> {url}", flush=True)
                    pending.pop(name, None)
            if pending:
                for name, process in owned:
                    code = process.poll()
                    if code is not None and name in pending:
                        raise SystemExit(f"{name} exited before becoming ready (code={code})")
                time.sleep(0.25)

        if pending:
            missing = ", ".join(pending)
            raise SystemExit(f"Timed out waiting for: {missing}")

        print("\nCharacter Memory stack is ready:", flush=True)
        print("  Chat:     http://127.0.0.1:8000", flush=True)
        print("  Media:    http://127.0.0.1:8001/health", flush=True)
        print("  Dev:      http://127.0.0.1:8002/dev", flush=True)
        print("  Settings: http://127.0.0.1:8003/settings", flush=True)
        print("  TTS Lab:  http://127.0.0.1:9002/tts", flush=True)
        if mobile_origin:
            print(f"  Mobile:   {mobile_origin}", flush=True)
            print(f"  Media HTTPS: {mobile_origin}:8443/health", flush=True)
            print("  Verify:   bash scripts/mobile-check.sh", flush=True)
        print("Press Ctrl+C to stop processes started by this launcher.\n", flush=True)

        targets = {
            "chat": "http://127.0.0.1:8000",
            "dev": "http://127.0.0.1:8002/dev",
            "settings": "http://127.0.0.1:8003/settings",
            "tts": "http://127.0.0.1:9002/tts",
        }
        if not args.no_browser:
            webbrowser.open(targets[args.open])

        while True:
            for name, process in owned:
                code = process.poll()
                if code is not None:
                    raise SystemExit(f"{name} exited (code={code})")
            time.sleep(1.0)
    except KeyboardInterrupt:
        print("\nstack: stopping...", flush=True)
    finally:
        for _name, process in reversed(owned):
            if process.poll() is None:
                process.terminate()
        deadline = time.time() + 4.0
        for _name, process in reversed(owned):
            if process.poll() is not None:
                continue
            remaining = max(0.0, deadline - time.time())
            try:
                process.wait(timeout=remaining)
            except subprocess.TimeoutExpired:
                process.kill()
        if owned:
            print("stack: stopped", flush=True)


if __name__ == "__main__":
    main()

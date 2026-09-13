from __future__ import annotations

import argparse
import os
from pathlib import Path
import subprocess
import sys
import time
from urllib.error import URLError
from urllib.request import urlopen
import webbrowser


ROOT = Path(__file__).resolve().parents[2]


def _healthy(url: str, timeout: float = 0.8) -> bool:
    try:
        with urlopen(url, timeout=timeout) as response:
            return 200 <= int(response.status) < 300
    except (OSError, URLError):
        return False


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
    return env


def _tts_lab_env(base: dict[str, str]) -> dict[str, str]:
    env = dict(base)
    env.setdefault("CHARACTER_TTS_LAB_HOST", "127.0.0.1")
    env.setdefault("CHARACTER_TTS_LAB_PORT", "9002")
    env.setdefault("CHARACTER_TTS_LAB_MEDIA_BASE", "http://127.0.0.1:8001")
    env.setdefault("CHARACTER_TTS_COSYVOICE_BASE", "http://127.0.0.1:9012")
    env.setdefault("CHARACTER_TTS_KOKORO_DEVICE", "cpu")
    return env


def _spawn(name: str, command: list[str], env: dict[str, str]) -> subprocess.Popen:
    print(f"stack: starting {name}: {' '.join(command)}", flush=True)
    return subprocess.Popen(command, cwd=ROOT, env=env)


def main() -> None:
    parser = argparse.ArgumentParser(prog="character-stack")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--open", choices=("dev", "chat", "tts"), default="dev")
    args = parser.parse_args()

    base_env = os.environ.copy()
    base_env["CHARACTER_MEMORY_CONFIG"] = args.config
    base_env["CHARACTER_CONFIG_PATH"] = args.config
    base_env.setdefault("CHARACTER_DEV_CHARACTER_BASE_URL", "http://127.0.0.1:8000")
    base_env.setdefault("CHARACTER_DEV_MEDIA_BASE_URL", "http://127.0.0.1:8001")

    python = sys.executable
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
            "TTS Provider Lab",
            "http://127.0.0.1:9002/health",
            [python, "-m", "character_memory.tts_lab"],
            _tts_lab_env(base_env),
        ),
    ]

    owned: list[tuple[str, subprocess.Popen]] = []
    try:
        for name, health_url, command, env in specs:
            if _healthy(health_url):
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
        print("  Chat:    http://127.0.0.1:8000", flush=True)
        print("  Media:   http://127.0.0.1:8001/health", flush=True)
        print("  Dev:     http://127.0.0.1:8002/dev", flush=True)
        print("  TTS Lab: http://127.0.0.1:9002/tts", flush=True)
        print("Press Ctrl+C to stop processes started by this launcher.\n", flush=True)

        targets = {
            "chat": "http://127.0.0.1:8000",
            "dev": "http://127.0.0.1:8002/dev",
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

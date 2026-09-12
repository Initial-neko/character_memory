from __future__ import annotations

import argparse
import json
from pathlib import Path
import statistics
import subprocess
import time
from urllib import error, request


def percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round((len(ordered) - 1) * fraction)))
    return ordered[index]


def post(url: str, body: bytes, content_type: str) -> tuple[bytes, dict, float]:
    req = request.Request(url, data=body, method="POST", headers={"Content-Type": content_type})
    started = time.perf_counter()
    with request.urlopen(req, timeout=60) as response:
        payload = response.read()
        headers = dict(response.headers.items())
    return payload, headers, (time.perf_counter() - started) * 1000.0


def gpu_snapshot(pid: int | None) -> str:
    if not pid:
        return ""
    try:
        result = subprocess.run(
            [
                "nvidia-smi",
                "--query-compute-apps=pid,used_memory",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            check=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    for line in result.stdout.splitlines():
        parts = [item.strip() for item in line.split(",")]
        if len(parts) >= 2 and parts[0] == str(pid):
            return f"{parts[1]} MiB"
    return "0 MiB / process not listed"


def load_health(base_url: str) -> dict:
    url = f"{base_url.rstrip('/')}/health"
    try:
        with request.urlopen(url, timeout=10) as response:
            return json.loads(response.read().decode("utf-8"))
    except error.URLError as exc:
        raise SystemExit(
            f"Cannot reach Media Runtime at {url}.\n"
            "Start it first with: bash scripts/run-media.sh\n"
            "Then verify in the same environment: curl http://127.0.0.1:8001/health\n"
            f"Underlying error: {exc}"
        ) from exc


def main():
    parser = argparse.ArgumentParser(description="Benchmark Character Memory Media Runtime")
    parser.add_argument("--base-url", default="http://127.0.0.1:8001")
    parser.add_argument("--wav", required=True, help="PCM16 WAV test utterance")
    parser.add_argument("--text", default="你好，今天过得怎么样？")
    parser.add_argument("--iterations", type=int, default=10)
    parser.add_argument("--speaker-id", type=int, default=0)
    parser.add_argument("--speed", type=float, default=1.0)
    args = parser.parse_args()

    wav_path = Path(args.wav)
    if not wav_path.is_file():
        raise SystemExit(f"Benchmark WAV not found: {wav_path}")
    if args.iterations < 1:
        raise SystemExit("--iterations must be >= 1")

    base_url = args.base_url.rstrip("/")
    health = load_health(base_url)
    print(json.dumps(health, ensure_ascii=False, indent=2))
    print("GPU process memory:", gpu_snapshot(health.get("pid")) or "nvidia-smi unavailable")

    wav = wav_path.read_bytes()
    tts_body = json.dumps(
        {"text": args.text, "speaker_id": args.speaker_id, "speed": args.speed},
        ensure_ascii=False,
    ).encode("utf-8")

    asr_total: list[float] = []
    asr_inference: list[float] = []
    tts_total: list[float] = []
    tts_inference: list[float] = []

    try:
        # One explicit warm-up avoids mixing lazy model load into warm latency numbers.
        print("warming models...")
        post(f"{base_url}/v1/asr", wav, "audio/wav")
        post(f"{base_url}/v1/tts", tts_body, "application/json")
        print("GPU after warm-up:", gpu_snapshot(health.get("pid")) or "nvidia-smi unavailable")

        for index in range(args.iterations):
            asr_payload, _, elapsed = post(f"{base_url}/v1/asr", wav, "audio/wav")
            asr_data = json.loads(asr_payload.decode("utf-8"))
            asr_total.append(elapsed)
            asr_inference.append(float(asr_data.get("inference_ms") or 0.0))

            _, headers, elapsed = post(f"{base_url}/v1/tts", tts_body, "application/json")
            tts_total.append(elapsed)
            tts_inference.append(float(headers.get("X-Media-Inference-Ms") or 0.0))
            print(
                f"{index + 1:02d}: "
                f"ASR total={asr_total[-1]:.1f}ms infer={asr_inference[-1]:.1f}ms | "
                f"TTS total={tts_total[-1]:.1f}ms infer={tts_inference[-1]:.1f}ms"
            )
    except error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise SystemExit(f"Media Runtime returned HTTP {exc.code} for {exc.url}: {body}") from exc
    except error.URLError as exc:
        raise SystemExit(
            "Media Runtime became unreachable during benchmark. "
            "Check the run-media terminal for a model/runtime crash.\n"
            f"Underlying error: {exc}"
        ) from exc

    def report(name: str, values: list[float]):
        print(
            f"{name}: mean={statistics.mean(values):.1f}ms "
            f"p50={percentile(values, 0.50):.1f}ms "
            f"p95={percentile(values, 0.95):.1f}ms "
            f"max={max(values):.1f}ms"
        )

    print("\nWarm-path summary")
    report("ASR HTTP total", asr_total)
    report("ASR inference", asr_inference)
    report("TTS HTTP total", tts_total)
    report("TTS inference", tts_inference)
    print("GPU final:", gpu_snapshot(health.get("pid")) or "nvidia-smi unavailable")


if __name__ == "__main__":
    main()

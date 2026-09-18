from __future__ import annotations

import argparse
import json
from pathlib import Path
import statistics
import time

import httpx


CASES = [
    ("short", "嗯，我知道了，我们继续。"),
    ("medium", "这个功能可以加，我先看看现在的实现，然后马上告诉你结果。"),
    (
        "long",
        "我刚刚把这部分重新检查了一遍。当前真正影响体验的不是声音质量，而是模型什么时候开始生成、TTS什么时候拿到第一段文本，以及音频能不能尽快播放。所以这次我们先把加载时间、生成时间、实时系数和显存占用全部测出来，再决定要不要正式接入。",
    ),
]


def percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * p
    low = int(position)
    high = min(low + 1, len(ordered) - 1)
    fraction = position - low
    return ordered[low] * (1.0 - fraction) + ordered[high] * fraction


def header_float(response: httpx.Response, name: str) -> float | None:
    raw = str(response.headers.get(name) or "").strip()
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def summarize(values: list[float]) -> dict[str, float]:
    return {
        "min": round(min(values), 1),
        "mean": round(statistics.fmean(values), 1),
        "p50": round(percentile(values, 0.50), 1),
        "p95": round(percentile(values, 0.95), 1),
        "max": round(max(values), 1),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Benchmark the isolated Character Memory Qwen3-TTS sidecar")
    parser.add_argument("--base-url", default="http://127.0.0.1:9013")
    parser.add_argument("--voice", default="Vivian")
    parser.add_argument("--language", default="Chinese")
    parser.add_argument("--instruct", default="")
    parser.add_argument("--warmup", type=int, default=1)
    parser.add_argument("--repeats", type=int, default=10)
    parser.add_argument("--timeout", type=float, default=180.0)
    parser.add_argument("--output", default="")
    parser.add_argument("--save-dir", default="data/qwen3-tts-benchmark")
    args = parser.parse_args()

    if args.repeats < 1:
        raise SystemExit("--repeats must be >= 1")
    base = args.base_url.rstrip("/")
    save_dir = Path(args.save_dir)
    save_dir.mkdir(parents=True, exist_ok=True)

    with httpx.Client(timeout=args.timeout) as client:
        health = client.get(f"{base}/health")
        health.raise_for_status()
        before = health.json()
        print("Qwen3-TTS benchmark")
        print("===================")
        print(f"model:   {before.get('model')}")
        print(f"mode:    {before.get('mode')}")
        print(f"device:  {before.get('device')}")
        print(f"dtype:   {before.get('dtype')}")
        print(f"attn:    {before.get('attn_implementation')}")
        print(f"loaded:  {before.get('loaded')}")

        load_started = time.perf_counter()
        loaded_response = client.post(f"{base}/v1/load")
        load_client_ms = (time.perf_counter() - load_started) * 1000.0
        loaded_response.raise_for_status()
        loaded = loaded_response.json()
        print(f"load:    {loaded.get('load_ms')} ms model / {load_client_ms:.1f} ms HTTP")
        print(f"load VRAM peak: {loaded.get('load_cuda_peak_mb')} MB")
        if loaded.get("already_loaded"):
            print("NOTE: model was already loaded; restart the sidecar before benchmarking true cold-load time.")

        payload = {
            "text": CASES[0][1],
            "voice": args.voice,
            "language": args.language,
            "instruct": args.instruct,
            "speed": 1.0,
        }
        for _ in range(max(0, args.warmup)):
            response = client.post(f"{base}/v1/tts", json=payload)
            response.raise_for_status()

        case_results = []
        for case_name, text in CASES:
            client_ms_values: list[float] = []
            inference_ms_values: list[float] = []
            audio_ms_values: list[float] = []
            rtf_values: list[float] = []
            peak_values: list[float] = []
            first_audio: bytes | None = None

            for _ in range(args.repeats):
                request = {
                    "text": text,
                    "voice": args.voice,
                    "language": args.language,
                    "instruct": args.instruct,
                    "speed": 1.0,
                }
                started = time.perf_counter()
                response = client.post(f"{base}/v1/tts", json=request)
                client_ms = (time.perf_counter() - started) * 1000.0
                response.raise_for_status()
                if first_audio is None:
                    first_audio = response.content
                client_ms_values.append(client_ms)
                inference_ms_values.append(header_float(response, "x-tts-inference-ms") or client_ms)
                audio_ms_values.append(header_float(response, "x-tts-audio-ms") or 0.0)
                rtf_values.append(header_float(response, "x-tts-rtf") or 0.0)
                peak = header_float(response, "x-tts-cuda-peak-mb")
                if peak is not None:
                    peak_values.append(peak)

            if first_audio:
                (save_dir / f"{case_name}.wav").write_bytes(first_audio)

            result = {
                "name": case_name,
                "chars": len(text),
                "text": text,
                "client_ms": summarize(client_ms_values),
                "inference_ms": summarize(inference_ms_values),
                "audio_ms_mean": round(statistics.fmean(audio_ms_values), 1),
                "rtf": {
                    "mean": round(statistics.fmean(rtf_values), 4),
                    "p50": round(percentile(rtf_values, 0.50), 4),
                    "p95": round(percentile(rtf_values, 0.95), 4),
                },
                "cuda_peak_mb": round(max(peak_values), 1) if peak_values else None,
            }
            case_results.append(result)
            print()
            print(
                f"{case_name:6} chars={result['chars']:3d} "
                f"inference p50={result['inference_ms']['p50']:7.1f} ms "
                f"p95={result['inference_ms']['p95']:7.1f} ms "
                f"RTF p50={result['rtf']['p50']:.3f} "
                f"VRAM peak={result['cuda_peak_mb']} MB"
            )

        final_health = client.get(f"{base}/health").json()

    report = {
        "sidecar": base,
        "model": final_health.get("model"),
        "mode": final_health.get("mode"),
        "device": final_health.get("device"),
        "dtype": final_health.get("dtype"),
        "attn_implementation": final_health.get("attn_implementation"),
        "voice": args.voice,
        "language": args.language,
        "repeats": args.repeats,
        "warmup": args.warmup,
        "cold_load": {
            "already_loaded": bool(loaded.get("already_loaded")),
            "model_load_ms": loaded.get("load_ms"),
            "client_load_ms": round(load_client_ms, 1),
            "cuda_peak_mb": loaded.get("load_cuda_peak_mb"),
        },
        "resident_cuda_allocated_mb": final_health.get("cuda_allocated_mb"),
        "resident_cuda_reserved_mb": final_health.get("cuda_reserved_mb"),
        "cases": case_results,
        "audio_dir": str(save_dir),
    }

    output = Path(args.output) if args.output else save_dir / "benchmark.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print()
    print(f"report: {output}")
    print(f"audio:  {save_dir}")


if __name__ == "__main__":
    main()

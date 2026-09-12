from __future__ import annotations

import argparse
import json
import sys

import httpx


def main() -> int:
    parser = argparse.ArgumentParser(description="Run Character Memory live Dev Console smoke tests.")
    parser.add_argument("--base-url", default="http://127.0.0.1:8002", help="Dev Console base URL")
    parser.add_argument("--text", default="你好，这是 Character Memory 的媒体自检。", help="TTS text used for TTS→ASR smoke")
    parser.add_argument("--llm", action="store_true", help="Also call the configured cloud LLM")
    args = parser.parse_args()

    base = args.base_url.rstrip("/")
    client = httpx.Client(timeout=120.0)
    failed = False

    def show(name: str, response: httpx.Response) -> dict | None:
        nonlocal failed
        print(f"\n[{name}] HTTP {response.status_code}")
        try:
            payload = response.json()
        except ValueError:
            payload = {"text": response.text}
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        if response.is_error:
            failed = True
        return payload if isinstance(payload, dict) else None

    try:
        try:
            show("status", client.get(f"{base}/v1/dev/status", timeout=10.0))
        except Exception as exc:
            print(f"[status] request failed: {exc}", file=sys.stderr)
            return 2

        try:
            show(
                "media-smoke",
                client.post(
                    f"{base}/v1/dev/media-smoke",
                    json={"text": args.text, "speaker_id": 0, "speed": 1.0},
                ),
            )
        except Exception as exc:
            print(f"[media-smoke] request failed: {exc}", file=sys.stderr)
            failed = True

        if args.llm:
            try:
                show(
                    "llm",
                    client.post(
                        f"{base}/v1/dev/llm",
                        json={"prompt": "Reply exactly with OK", "system_prompt": ""},
                    ),
                )
            except Exception as exc:
                print(f"[llm] request failed: {exc}", file=sys.stderr)
                failed = True
    finally:
        client.close()

    print("\nRESULT:", "FAIL" if failed else "PASS")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())

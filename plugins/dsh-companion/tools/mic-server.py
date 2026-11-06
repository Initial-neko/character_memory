#!/usr/bin/env python3
"""Local microphone service: capture once, stream Float32 PCM over HTTP.

The point is to keep device access out of the browser sandbox. The renderer is
only a client: it opens /stream and receives 16 kHz mono float32 frames.

Endpoints
  GET  /health          -> {"ok", "ready", "device", "rate"}
  GET  /devices         -> {"devices": [{"index", "name", "rate"}]}
  GET  /stream          -> chunked float32 frames, header X-Sample-Rate
  POST /mute?on=1|0     -> drop or resume frames for every stream
"""
import argparse
import json
import queue
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

import numpy as np

TARGET_RATE = 16000
FRAME_SAMPLES = 640
_state = {
    "device": None,
    "rate": TARGET_RATE,
    "ready": False,
    "error": None,
    "muted": False,
    "lastFrame": 0.0,
}
_stream = None
_queues = []
_queues_lock = threading.Lock()


def _resample_average(samples, rate):
    if rate == TARGET_RATE:
        return samples
    ratio = rate / TARGET_RATE
    count = int(len(samples) / ratio)
    if count <= 0:
        return np.zeros(0, dtype=np.float32)
    out = np.empty(count, dtype=np.float32)
    for i in range(count):
        start = int(i * ratio)
        end = max(start + 1, int((i + 1) * ratio))
        segment = samples[start:end]
        out[i] = float(np.mean(segment))
    return out


def _pusher(samples, rate):
    _state["lastFrame"] = time.time()
    data = _resample_average(samples, rate)
    if data.size == 0 or _state["muted"]:
        return
    payload = data.astype(np.float32).tobytes()
    with _queues_lock:
        for q in list(_queues):
            try:
                q.put_nowait(payload)
            except queue.Full:
                try:
                    q.get_nowait()
                    q.put_nowait(payload)
                except queue.Empty:
                    pass


def _open_stream(device_index):
    import sounddevice as sd

    last = None
    for rate in (TARGET_RATE, 48000, 44100):
        try:
            kwargs = {
                "samplerate": rate,
                "channels": 1,
                "dtype": "float32",
                "blocksize": FRAME_SAMPLES if rate == TARGET_RATE else 0,
                "callback": lambda indata, frames, time_info, status: _pusher(indata[:, 0].copy(), rate),
            }
            if device_index is not None:
                kwargs["device"] = device_index
            stream = sd.InputStream(**kwargs)
            stream.start()
            return stream, rate
        except Exception as error:  # device may refuse a rate, try the next one
            last = error
    raise last


def start_capture(device_index):
    global _stream
    try:
        stream, rate = _open_stream(device_index)
    except Exception as error:
        _state.update(ready=False, error=str(error))
        return None
    _stream = stream
    _state.update(ready=True, error=None, rate=rate, device=device_index)
    return stream


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    def _json(self, payload, code=200):
        body = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("content-type", "application/json; charset=utf-8")
        self.send_header("content-length", str(len(body)))
        self.send_header("access-control-allow-origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        route = urlparse(self.path).path
        if route == "/health":
            stale = _state["ready"] and _state["lastFrame"] and time.time() - _state["lastFrame"] > 5
            if stale:
                global _stream
                try:
                    if _stream is not None:
                        _stream.close()
                except Exception:
                    pass
                start_capture(_state["device"])
            self._json({
                "ok": True,
                "ready": _state["ready"],
                "device": _state["device"],
                "rate": _state["rate"],
                "error": _state["error"],
                "stale": bool(stale),
                "lastFrameAge": round(time.time() - _state["lastFrame"], 2) if _state["lastFrame"] else None,
            })
            return
        if route == "/devices":
            import sounddevice as sd
            devices = []
            for index, info in enumerate(sd.query_devices()):
                if info.get("max_input_channels", 0) > 0:
                    devices.append({"index": index, "name": info["name"], "rate": int(info["default_samplerate"])})
            self._json({"devices": devices})
            return
        if route == "/stream":
            self.send_response(200)
            self.send_header("content-type", "application/octet-stream")
            self.send_header("x-sample-rate", str(TARGET_RATE))
            self.send_header("transfer-encoding", "chunked")
            self.send_header("access-control-allow-origin", "*")
            self.end_headers()
            self._stream()
            return
        self._json({"error": "not found"}, 404)

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path == "/mute":
            value = parse_qs(parsed.query).get("on", ["1"])[0]
            _state["muted"] = value not in ("0", "false", "")
            self._json({"ok": True, "muted": _state["muted"]})
            return
        self._json({"error": "not found"}, 404)

    def _stream(self):
        q = queue.Queue(maxsize=64)
        with _queues_lock:
            _queues.append(q)
        try:
            while True:
                try:
                    payload = q.get(timeout=5)
                except queue.Empty:
                    # keep the connection warm; silent frame also proves liveness
                    payload = np.zeros(FRAME_SAMPLES, dtype=np.float32).tobytes()
                self.wfile.write(b"%x\r\n" % len(payload))
                self.wfile.write(payload)
                self.wfile.write(b"\r\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            with _queues_lock:
                if q in _queues:
                    _queues.remove(q)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=50132)
    parser.add_argument("--device", type=int, default=None)
    args = parser.parse_args()

    stream = start_capture(args.device)
    if stream is None:
        print(json.dumps({"ok": False, "error": _state["error"]}), flush=True)
        sys.exit(1)
    print(json.dumps({"ok": True, "rate": _state["rate"], "device": _state["device"]}), flush=True)

    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    server.daemon_threads = True
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stream.stop()
        stream.close()


if __name__ == "__main__":
    main()

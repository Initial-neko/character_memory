"""Loopback screen capture for the desktop companion.

The Electron renderer is denied getDisplayMedia, so the desktop path asks this
local service for a JPEG frame instead. It never runs a model and only binds
127.0.0.1.
"""
import argparse
import io
import json
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

from PIL import ImageGrab


def _encode(max_edge, quality):
    started = time.time()
    image = ImageGrab.grab()
    image = image.convert("RGB")
    width, height = image.size
    scale = min(1.0, float(max_edge) / max(width, height))
    if scale < 1:
        image = image.resize((max(1, int(width * scale)), max(1, int(height * scale))))
    buffer = io.BytesIO()
    image.save(buffer, "JPEG", quality=int(quality * 100))
    data = buffer.getvalue()
    return data, image.size, round((time.time() - started) * 1000)


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
        parsed = urlparse(self.path)
        if parsed.path == "/health":
            return self._json({"ok": True, "ready": True, "ts": time.time()})
        if parsed.path == "/screen/frame":
            query = parse_qs(parsed.query)
            try:
                max_edge = int(query.get("maxEdge", ["1280"])[0])
                quality = float(query.get("quality", ["0.8"])[0])
                max_edge = max(256, min(1920, max_edge))
                quality = max(0.3, min(1.0, quality))
            except ValueError:
                return self._json({"error": "invalid maxEdge/quality"}, 400)
            try:
                data, size, elapsed = _encode(max_edge, quality)
            except Exception as error:
                return self._json({"error": f"screen capture failed: {error}"}, 503)
            self.send_response(200)
            self.send_header("content-type", "image/jpeg")
            self.send_header("cache-control", "no-store")
            self.send_header("x-frame-width", str(size[0]))
            self.send_header("x-frame-height", str(size[1]))
            self.send_header("x-capture-ms", str(elapsed))
            self.send_header("content-length", str(len(data)))
            self.send_header("access-control-allow-origin", "*")
            self.end_headers()
            self.wfile.write(data)
            return
        self._json({"error": "not found"}, 404)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=50134)
    parser.add_argument("--host", default="127.0.0.1")
    args = parser.parse_args()
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(json.dumps({"ok": True, "service": "companion-screen", "port": args.port}), flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()

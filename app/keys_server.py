"""Minimal key delivery server, no directory listing and no key logging."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit
import os
import re

KEYS_DIR = Path(os.getenv("OUTLINE_KEYS_DIR", "/opt/outline-wss/keys"))
PORT = int(os.getenv("OUTLINE_KEYS_PORT", "18081"))
TOKEN_RE = re.compile(r"^/([A-Za-z0-9_-]{24,64})\.yml$")


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        name = urlsplit(self.path).path
        match = TOKEN_RE.fullmatch(name)
        if not match:
            return self.send_error(404)
        path = KEYS_DIR / f"{match.group(1)}.yml"
        try:
            data = path.read_bytes()
        except (FileNotFoundError, PermissionError):
            return self.send_error(404)
        self.send_response(200)
        self.send_header("Content-Type", "application/yaml; charset=utf-8")
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        # A URL contains a bearer capability — never log paths.
        pass


if __name__ == "__main__":
    ThreadingHTTPServer((os.getenv("OUTLINE_KEYS_BIND", "127.0.0.1"), PORT), Handler).serve_forever()

"""Read-only local HTTP viewer. No arbitrary Yomk request endpoint exists."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
import ipaddress
import json
from pathlib import Path
import threading
from urllib.parse import urlsplit

from .collector import validate_snapshot

MAX_SNAPSHOT_BYTES = 16 * 1024 * 1024


def file_provider(path):
    path = Path(path)
    def read():
        with path.open("rb") as stream:
            data = stream.read(MAX_SNAPSHOT_BYTES + 1)
        if len(data) > MAX_SNAPSHOT_BYTES:
            raise ValueError("snapshot exceeds 16 MiB")
        return validate_snapshot(json.loads(data))
    return read


class _ViewerHTTPServer(ThreadingHTTPServer):
    daemon_threads = False
    block_on_close = True

    def get_request(self):
        connection, address = super().get_request()
        connection.settimeout(2)
        return connection, address


class GraphServer:
    """Close on the control thread before detaching the observer.

    close() drains admitted HTTP handlers. Providers must be bounded read-only
    queries and must not call close() from inside an HTTP handler.
    """
    def __init__(self, provider, host="127.0.0.1", port=18765):
        address = ipaddress.ip_address(host)
        if address.version != 4 or not address.is_loopback:
            raise ValueError("viewer host must be an IPv4 loopback IP; use an SSH tunnel for remote access")
        self.provider = provider
        self._thread = None
        self._closed = False
        outer = self
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                # Reject cross-site requests and host rebinding to this local viewer.
                expected = {f"{outer.httpd.server_address[0]}:{outer.httpd.server_port}",
                            f"localhost:{outer.httpd.server_port}"}
                if self.headers.get("Host") not in expected:
                    self.send_error(403)
                    return
                origin = self.headers.get("Origin")
                if origin and origin not in {"http://" + host for host in expected}:
                    self.send_error(403)
                    return
                path = urlsplit(self.path).path
                if path == "/api/snapshot":
                    try:
                        snapshot = validate_snapshot(outer.provider())
                        self.reply(json.dumps(snapshot, ensure_ascii=False, allow_nan=False).encode(), "application/json; charset=utf-8")
                    except Exception:
                        self.reply(b'{"error":"Snapshot unavailable; inspect the application or snapshot file."}', "application/json", 503)
                    return
                assets = {"/": ("index.html", "text/html; charset=utf-8"),
                          "/app.js": ("app.js", "text/javascript; charset=utf-8"),
                          "/app.css": ("app.css", "text/css; charset=utf-8")}
                if path not in assets:
                    self.send_error(404)
                    return
                name, mime = assets[path]
                self.reply(files(__package__).joinpath("web", name).read_bytes(), mime)

            def reply(self, data, mime, status=200):
                self.send_response(status)
                self.send_header("Content-Type", mime)
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; connect-src 'self'; object-src 'none'; frame-ancestors 'none'")
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *_):
                pass
        self.httpd = _ViewerHTTPServer((host, port), Handler)

    @property
    def url(self):
        return f"http://{self.httpd.server_address[0]}:{self.httpd.server_port}"

    def start(self):
        if self._closed:
            raise RuntimeError("viewer is closed")
        if self._thread is None:
            self._thread = threading.Thread(target=self.httpd.serve_forever, name="yomk-graph-http", daemon=True)
            try:
                self._thread.start()
            except BaseException:
                self._thread = None
                self.httpd.server_close()
                self._closed = True
                raise
        return self

    def close(self):
        if self._closed:
            return
        if self._thread is not None:
            self.httpd.shutdown()
            self._thread.join()
        self.httpd.server_close()
        self._closed = True

    def __enter__(self):
        return self.start()

    def __exit__(self, *_):
        self.close()

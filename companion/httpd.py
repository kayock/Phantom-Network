"""Threaded HTTP server, request router and Server-Sent-Events hub.

Design notes
------------
* Uses ``http.server.ThreadingHTTPServer`` — one daemon thread per connection.
  At LAN scale (a handful of clients) this is simple, robust and effectively
  zero-CPU when idle.
* The router maps ``(METHOD, regex)`` to handler callables that receive a
  :class:`Request` and return a :class:`Response`.
* Server push uses **SSE** (``GET /api/stream``).  Browsers reconnect
  automatically and no WebSocket dependency is needed.  Intercom audio is
  carried as base64 PCM inside SSE events.
* CORS is permissive on ``/api/*`` because the browser UI may be opened from
  one companion while talking to companions on other machines.  Filesystem
  access is sandboxed server-side; CORS only affects browser convenience.
"""

from __future__ import annotations

import json
import urllib.parse
import queue
import re
import socket
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, unquote, urlparse

from .errors import FsError

log_host = "pn.http"


class Request:
    """Immutable-ish view of one HTTP request handed to route handlers."""

    def __init__(self, handler: "Handler", params: Dict[str, str]):
        self.handler = handler
        self.method = handler.command
        self.path = handler.pn_path          # decoded path without query
        self.query: Dict[str, List[str]] = handler.pn_query
        self.headers = handler.headers
        self.params = params                 # named regex groups
        self.client_addr = handler.client_address[0]
        self.rfile = handler.rfile

    # -- query helpers --------------------------------------------------
    def q(self, name: str, default: str = "") -> str:
        vals = self.query.get(name)
        return vals[0] if vals else default

    def qint(self, name: str, default: int = 0) -> int:
        try:
            return int(self.q(name, default))
        except (TypeError, ValueError):
            return default

    def qbool(self, name: str, default: bool = False) -> bool:
        v = self.q(name, "").lower()
        if v == "":
            return default
        return v in ("1", "true", "yes", "on")

    # -- body helpers ---------------------------------------------------
    def json(self, default: Any = None) -> Any:
        """Parse request body as JSON (capped at 8 MB)."""
        length = min(int(self.headers.get("Content-Length") or 0), 8 * 1024 * 1024)
        if length <= 0:
            return default
        raw = self.rfile.read(length)
        if not raw:
            return default
        try:
            return json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return default

    def body_bytes(self) -> bytes:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return b""
        return self.rfile.read(length)

    def body_iter(self, chunk: int = 256 * 1024):
        """Iterate over the raw request body without buffering it entirely."""
        remaining = int(self.headers.get("Content-Length") or 0)
        while remaining > 0:
            data = self.rfile.read(min(chunk, remaining))
            if not data:
                break
            remaining -= len(data)
            yield data


class Response:
    def __init__(self, status: int = 200, body: bytes = b"",
                 content_type: str = "application/json; charset=utf-8",
                 headers: Optional[Dict[str, str]] = None):
        self.status = status
        self.body = body
        self.content_type = content_type
        self.headers = headers or {}
        self.stream = None          # optional (fileobj, length, chunk)
        self.sse = None             # optional generator for SSE responses


def json_response(obj: Any, status: int = 200) -> Response:
    return Response(status, json.dumps(obj, ensure_ascii=False).encode("utf-8"))


def error_response(status: int, code: str, message: str, **extra) -> Response:
    payload = {"ok": False, "error": code, "message": message}
    payload.update(extra)
    return json_response(payload, status)


def file_response(fileobj, length: int, content_type: str,
                  filename: Optional[str] = None,
                  range_header: str = "", status: int = 200) -> Response:
    """Stream a file object (supports HTTP Range for partial downloads)."""
    headers: Dict[str, str] = {"Accept-Ranges": "bytes"}
    if filename:
        # HTTP headers are latin-1: raw non-ASCII filenames (Cyrillic, CJK,
        # emoji …) crashed the response with UnicodeEncodeError → empty
        # reply → transfer failed.  RFC 6266/5987: ASCII fallback plus a
        # UTF-8 percent-encoded filename* parameter.
        fallback = filename.replace('"', "'").replace("\\", "_")
        fallback = fallback.replace("\r", "_").replace("\n", "_")
        fallback = fallback.encode("ascii", "replace").decode("ascii")
        quoted = urllib.parse.quote(filename, safe="")
        headers["Content-Disposition"] = (
            f'attachment; filename="{fallback}"; '
            f"filename*=UTF-8''{quoted}")
    if range_header:
        m = re.match(r"bytes=(\d*)-(\d*)$", range_header.strip())
        if m and (m.group(1) or m.group(2)):
            start_s, end_s = m.group(1), m.group(2)
            if start_s == "":                       # suffix range: last N bytes
                n = int(end_s)
                start = max(0, length - n)
                end = length - 1
            else:
                start = int(start_s)
                end = min(int(end_s) if end_s else length - 1, length - 1)
            if start > end or start >= length:
                resp = Response(416, b"")
                resp.headers["Content-Range"] = f"bytes */{length}"
                return resp
            try:
                fileobj.seek(start)
            except OSError:
                pass
            resp = Response(206)
            resp.stream = (fileobj, end - start + 1, 256 * 1024)
            resp.headers.update(headers)
            resp.headers["Content-Range"] = f"bytes {start}-{end}/{length}"
            resp.content_type = content_type
            return resp
    resp = Response(status)
    resp.stream = (fileobj, length, 256 * 1024)
    resp.headers.update(headers)
    resp.content_type = content_type
    return resp


class SSEHub:
    """Fan-out hub for Server-Sent Events.

    ``publish(topic, data)`` queues ``data`` for every subscriber of
    ``topic`` (``*`` subscribers receive everything).
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._subs: Dict[str, List[queue.Queue]] = {}

    def subscribe(self, topic: str = "*") -> Tuple[str, "queue.Queue"]:
        client_id = f"sse_{time.time_ns()}"
        q: "queue.Queue" = queue.Queue(maxsize=512)
        with self._lock:
            self._subs.setdefault(topic, []).append(q)
        return client_id, q

    def unsubscribe(self, topic: str, q: "queue.Queue") -> None:
        with self._lock:
            lst = self._subs.get(topic)
            if lst and q in lst:
                lst.remove(q)

    def publish(self, topic: str, data: Any) -> None:
        item = (topic, data)
        with self._lock:
            queues = list(self._subs.get(topic, [])) + list(self._subs.get("*", []))
        for q in queues:
            try:
                q.put_nowait(item)
            except queue.Full:
                pass

    @staticmethod
    def format_event(topic: str, data: Any) -> bytes:
        payload = json.dumps(data, ensure_ascii=False)
        return f"event: {topic}\ndata: {payload}\n\n".encode("utf-8")


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "PhantomCompanion/1.2"

    # populated by the server factory -------------------------------------
    router: "Router" = None            # type: ignore[assignment]
    sse_hub: SSEHub = None             # type: ignore[assignment]
    api = None                          # companion application context

    # ------------------------------------------------------------------
    def log_message(self, fmt, *args):  # quiet default logging
        pass

    def _log_access(self, status: int) -> None:
        logging = __import__("logging").getLogger(log_host)
        logging.debug("%s %s -> %s (%s)",
                      self.command, self.path, status, self.client_address[0])

    # ------------------------------------------------------------------
    def do_GET(self):    self._dispatch("GET")
    def do_POST(self):   self._dispatch("POST")
    def do_PUT(self):    self._dispatch("PUT")
    def do_DELETE(self): self._dispatch("DELETE")
    def do_HEAD(self):   self._dispatch("GET", head=True)
    def do_OPTIONS(self): self._dispatch("OPTIONS")

    # ------------------------------------------------------------------
    def _cors_headers(self) -> Dict[str, str]:
        return {
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "GET, POST, PUT, DELETE, OPTIONS",
            "Access-Control-Allow-Headers": "Content-Type, X-PN-Token, Range, X-PN-Session",
            "Access-Control-Expose-Headers": "Content-Length, Content-Range, Accept-Ranges, Content-Disposition",
        }

    def _dispatch(self, method: str, head: bool = False) -> None:
        parsed = urlparse(self.path)
        self.pn_path = unquote(parsed.path)
        self.pn_query = parse_qs(parsed.query)

        if method == "OPTIONS":
            self._send(Response(204, b""), head)
            return

        match = self.router.resolve(method, self.pn_path)
        if match is None:
            self._send(error_response(404, "not_found",
                                      f"No route: {method} {self.pn_path}"), head)
            return
        route, params = match

        if route.auth and not self._authorized():
            self._send(error_response(401, "unauthorized",
                                      "Missing or invalid auth token"), head)
            return

        try:
            resp = route.handler(Request(self, params))
        except BrokenPipeError:
            return
        except FsError as exc:                    # domain error → clean JSON
            self._send(error_response(exc.status, exc.code, exc.message), head)
            return
        except Exception as exc:                       # handler crash → 500
            lg = __import__("logging").getLogger(log_host)
            lg.error("handler error on %s %s: %s", method, self.pn_path, exc)
            lg.debug(traceback.format_exc())
            try:
                self.api.events.emit("companion_error",
                                     f"Handler error {self.pn_path}: {exc}",
                                     severity="error")
            except Exception:
                pass
            self._send(error_response(500, "internal_error", str(exc)), head)
            return

        try:
            self._send(resp, head)
            self._log_access(resp.status)
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True

    # ------------------------------------------------------------------
    def _authorized(self) -> bool:
        api = self.api
        if api is None or not api.config.get("require_token"):
            return True
        token = self.headers.get("X-PN-Token", "")
        return bool(token) and token == api.config.get("auth_token")

    # ------------------------------------------------------------------
    def _send(self, resp: Response, head: bool = False) -> None:
        try:
            if resp.sse is not None:
                self._send_sse(resp)
                return

            headers = dict(resp.headers)
            headers.update(self._cors_headers())
            body = resp.body

            if resp.stream is not None:
                fileobj, length, chunk = resp.stream
                self.send_response(resp.status)
                headers.setdefault("Content-Type", resp.content_type)
                for key, value in headers.items():
                    self.send_header(key, value)
                self.send_header("Content-Length", str(length))
                self.end_headers()
                if head or self.command == "HEAD":
                    fileobj.close()
                    return
                sent = 0
                while sent < length:
                    data = fileobj.read(min(chunk, length - sent))
                    if not data:
                        break
                    self.wfile.write(data)
                    sent += len(data)
                fileobj.close()
                return

            self.send_response(resp.status)
            headers.setdefault("Content-Type", resp.content_type)
            for key, value in headers.items():
                self.send_header(key, value)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if not head and self.command != "HEAD" and body:
                self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            self.close_connection = True

    # ------------------------------------------------------------------
    def _send_sse(self, resp: Response) -> None:
        """Stream SSE events until the client disconnects or server stops."""
        try:
            self.send_response(200)
            headers = {
                "Content-Type": "text/event-stream",
                "Cache-Control": "no-cache",
                "Connection": "close",
                "X-Accel-Buffering": "no",
            }
            headers.update(self._cors_headers())
            for key, value in headers.items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(b": phantom-stream\n\n")
            self.wfile.flush()
            resp.sse(self.wfile)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError,
                socket.timeout, OSError):
            self.close_connection = True


class Route:
    def __init__(self, method: str, pattern: str, handler: Callable,
                 auth: bool = True):
        self.method = method
        self.regex = re.compile("^" + pattern + "$")
        self.handler = handler
        self.auth = auth


class Router:
    def __init__(self):
        self._routes: List[Route] = []

    def add(self, method: str, pattern: str, handler: Callable,
            auth: bool = True) -> None:
        self._routes.append(Route(method, pattern, handler, auth))

    def resolve(self, method: str, path: str):
        for route in self._routes:
            if route.method != method:
                continue
            m = route.regex.match(path)
            if m:
                params = {k: v for k, v in m.groupdict().items() if v is not None}
                if not params and m.groups():
                    # unnamed capture groups (e.g. r"/api/jobs/([^/]+)") are
                    # exposed under the "" key — handlers read req.params[""]
                    params = {"": m.group(1)}
                return route, params
        return None


class PhantomServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 32

    def __init__(self, addr, api):
        self.api = api
        handler_cls = type("BoundHandler", (Handler,), {
            "router": api.router,
            "sse_hub": api.sse,
            "api": api,
        })
        super().__init__(addr, handler_cls)

    def server_bind(self):
        try:
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        except OSError:
            pass
        super().server_bind()

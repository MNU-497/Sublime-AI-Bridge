"""Streamable-HTTP transport for the MCP server.

Bound to localhost. POST /mcp accepts a JSON-RPC request and returns the
JSON-RPC response. GET /mcp opens a server-sent-events stream that we keep
alive but never push notifications on -- this server is tool-only, so the
stream just exists to satisfy clients that expect the channel. DELETE /mcp
is accepted as a no-op since we're stateless.
"""
import json
import socket
import sys
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .jsonrpc import INTERNAL_ERROR, PARSE_ERROR, make_error, make_response


_ALLOWED_ORIGIN_PREFIXES = (
    "http://localhost", "http://127.0.0.1",
    "https://localhost", "https://127.0.0.1",
)
_SSE_KEEPALIVE_SECONDS = 15.0
_BENIGN_DISCONNECTS = (
    BrokenPipeError, ConnectionResetError, ConnectionAbortedError,
)
_SERVER_SESSION_ID = str(uuid.uuid4())


class HTTPTransport:
    def __init__(self, mcp, host="127.0.0.1", port=8765, path="/mcp",
                 logger=None, allowed_origins=None, auth_token=None):
        self._mcp = mcp
        self.host = host
        self.port = int(port)
        self.path = path
        self._log = logger or (lambda *a, **kw: None)
        # Extra origins (exact match) permitted in addition to the localhost
        # defaults -- e.g. a separate-host web UI that drives this server from
        # the user's browser. The auth_token is the real gate when these are
        # set; CORS only constrains browsers, never native clients.
        self._allowed_origins = frozenset(allowed_origins or ())
        self._auth_token = auth_token or None
        self._httpd = None
        self._thread = None
        self._stop_event = threading.Event()

    @property
    def bound_port(self):
        if self._httpd is None:
            return None
        return self._httpd.server_address[1]

    def start(self):
        self._stop_event.clear()
        handler_cls = _make_handler(
            self._mcp, self.path, self._log, self._stop_event,
            self._allowed_origins, self._auth_token,
        )
        server_cls = _make_server(self._log)
        self._httpd = server_cls((self.host, self.port), handler_cls)
        self._thread = threading.Thread(
            target=self._httpd.serve_forever,
            name="mcp-http",
            daemon=True,
        )
        self._thread.start()
        self._log("MCP HTTP transport listening on %s:%d%s",
                  self.host, self.bound_port, self.path)

    def stop(self, timeout=2.0):
        # Wake up any in-flight SSE keepalive loops so they exit cleanly.
        self._stop_event.set()
        if self._httpd is not None:
            try:
                self._httpd.shutdown()
            except Exception:
                pass
            try:
                self._httpd.server_close()
            except Exception:
                pass
            self._httpd = None
        if self._thread is not None:
            self._thread.join(timeout)
            if self._thread.is_alive():
                self._log("MCP HTTP thread did not exit within %.1fs", timeout)
            self._thread = None


def _make_server(log):
    class _Server(ThreadingHTTPServer):
        # SSE handlers block until the client disconnects; non-daemon threads
        # would prevent the server from shutting down cleanly on plugin reload.
        daemon_threads = True
        allow_reuse_address = True

        def handle_error(self, request, client_address):
            exc = sys.exc_info()[1]
            if isinstance(exc, _BENIGN_DISCONNECTS) or isinstance(exc, OSError):
                # Clients drop the SSE channel routinely; not worth a stack trace.
                return
            try:
                log("server error from %s: %r", client_address, exc)
            except Exception:
                pass

    return _Server


def _make_handler(mcp, mount_path, log, stop_event, allowed_origins, auth_token):
    class _Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, format, *args):
            return

        def log_error(self, format, *args):
            try:
                log("http error: " + (format % args))
            except Exception:
                pass

        def _reject_bad_origin(self):
            origin = self.headers.get("Origin", "")
            if origin and self._allowed_origin() is None:
                self.send_error(403, "Origin not allowed")
                return True
            return False

        def _allowed_origin(self):
            origin = self.headers.get("Origin", "")
            if not origin:
                return None
            if origin.startswith(_ALLOWED_ORIGIN_PREFIXES):
                return origin
            if origin in allowed_origins:
                return origin
            return None

        def _check_auth(self):
            """Return True if the request may proceed. When an auth token is
            configured, every non-preflight request must carry it as
            `Authorization: Bearer <token>`. CORS only constrains browsers, so
            this token is what actually gates native and cross-origin callers."""
            if not auth_token:
                return True
            header = self.headers.get("Authorization", "")
            prefix = "Bearer "
            if header.startswith(prefix) and header[len(prefix):] == auth_token:
                return True
            self.send_error(401, "missing or invalid Authorization token")
            return False

        def _send_cors_headers(self):
            origin = self._allowed_origin()
            if origin:
                self.send_header("Access-Control-Allow-Origin", origin)
                self.send_header("Access-Control-Expose-Headers", "Mcp-Session-Id")
                self.send_header("Vary", "Origin")

        def _send_json(self, status, payload, session_id=None):
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            if session_id:
                self.send_header("Mcp-Session-Id", session_id)
            self._send_cors_headers()
            self.end_headers()
            self.wfile.write(body)

        # ---- OPTIONS: CORS preflight --------------------------------------

        def do_OPTIONS(self):
            if self.path != mount_path:
                self.send_error(404)
                return
            origin = self._allowed_origin()
            if not origin:
                # No Origin or non-local Origin -- reject preflight by
                # omitting CORS headers; the browser will surface this as a
                # "Failed to fetch" with a CORS message.
                self.send_error(403, "non-local Origin rejected")
                return
            requested = self.headers.get(
                "Access-Control-Request-Headers",
                "Authorization, Content-Type, Mcp-Session-Id, Accept, "
                "Mcp-Protocol-Version",
            )
            self.send_response(204)
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Access-Control-Allow-Methods", "POST, GET, DELETE, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", requested)
            self.send_header("Access-Control-Max-Age", "600")
            self.send_header("Vary", "Origin")
            self.send_header("Content-Length", "0")
            self.end_headers()

        # ---- GET: idle SSE stream -----------------------------------------

        def do_GET(self):
            if self.path != mount_path:
                self.send_error(404)
                return
            if self._reject_bad_origin():
                return
            if not self._check_auth():
                return

            # Open the SSE stream. We never emit JSON-RPC frames on it (this
            # server has no notifications to push), but holding the channel
            # open is what most Streamable-HTTP clients expect.
            try:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Connection", "keep-alive")
                self.send_header("X-Accel-Buffering", "no")
                self._send_cors_headers()
                # Streamable-HTTP responses are not chunked transfer encoded
                # by default in stdlib http.server when Content-Length is
                # omitted under HTTP/1.1; force connection: close semantics
                # by switching protocol back to 1.0 for this response.
                self.protocol_version = "HTTP/1.0"
                self.end_headers()
                self.wfile.write(b": stream open\n\n")
                self.wfile.flush()
            except _BENIGN_DISCONNECTS:
                return
            except OSError as e:
                log("sse open failed: %s", e)
                return

            # Keepalive loop. wait() returns True when stop_event is set,
            # which happens on plugin_unloaded.
            try:
                while not stop_event.wait(_SSE_KEEPALIVE_SECONDS):
                    try:
                        self.wfile.write(b": keepalive\n\n")
                        self.wfile.flush()
                    except _BENIGN_DISCONNECTS:
                        return
                    except OSError:
                        return
            finally:
                try:
                    self.wfile.flush()
                except Exception:
                    pass

        def do_DELETE(self):
            if self._reject_bad_origin():
                return
            if not self._check_auth():
                return
            self.send_response(204)
            self.send_header("Content-Length", "0")
            self._send_cors_headers()
            self.end_headers()

        # ---- POST: JSON-RPC -----------------------------------------------

        def do_POST(self):
            if self.path != mount_path:
                self.send_error(404)
                return
            if self._reject_bad_origin():
                return
            if not self._check_auth():
                return

            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                self.send_error(400, "bad Content-Length")
                return
            if length <= 0:
                self.send_error(400, "empty body")
                return

            try:
                raw = self.rfile.read(length)
            except (ConnectionError, socket.timeout) as e:
                log("read body failed: %s", e)
                return

            session_id = self.headers.get("Mcp-Session-Id")

            try:
                msg = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as e:
                self._send_json(200, make_response(None, error=make_error(
                    PARSE_ERROR, "parse error: {}".format(e))),
                    session_id=session_id)
                return

            if isinstance(msg, dict) and msg.get("method") == "initialize":
                session_id = _SERVER_SESSION_ID

            try:
                response = mcp.handle(msg)
            except Exception as e:
                log("dispatch crashed: %s", e)
                req_id = msg.get("id") if isinstance(msg, dict) else None
                self._send_json(200, make_response(req_id, error=make_error(
                    INTERNAL_ERROR, "internal error: {}".format(e))),
                    session_id=session_id)
                return

            if response is None:
                self.send_response(202)
                self.send_header("Content-Length", "0")
                if session_id:
                    self.send_header("Mcp-Session-Id", session_id)
                self._send_cors_headers()
                self.end_headers()
                return

            self._send_json(200, response, session_id=session_id)

    return _Handler

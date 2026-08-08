"""HTTP agent - runs on each node, lets the controller drive local services.

Token-gated (Bearer). Bind/firewall scope to the controller IP (DESIGN.md §18).
"""
from __future__ import annotations

import hmac
import json
import logging
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable

from .ops import ServiceOps

log = logging.getLogger(__name__)

#: allowed POST routes -> (ServiceOps method name, requires model param?)
_ROUTES: dict[str, Callable[[ServiceOps, dict], Any]] = {
    "/lms/start": lambda o, _: o.lms_start(),
    "/lms/load": lambda o, b: o.lms_load(b.get("model"), b.get("gpu_ratio")),
    "/lms/unload": lambda o, _: o.lms_unload(),
    "/lms/status": lambda o, _: o.lms_status(),
    "/lms/get": lambda o, b: o.lms_get(b.get("model", "")),
    "/lms/provision": lambda o, b: o.provision_lmstudio(b.get("model")),
    "/comfy/start": lambda o, b: o.comfy_start(b.get("which")),
    "/comfy/stop": lambda o, b: o.comfy_stop(b.get("which")),
    "/krea2/generate": lambda o, b: o.generate_image(
        b.get("prompt", ""), b.get("seed", 0), b.get("aspect_ratio", "16:9"),
        b.get("out_path", ""), b.get("use_lora", False)),
    "/verify": lambda o, _: o.verify(),
}


def _make_handler(ops, token: str):
    """`ops` may be an instance (tests) or a callable returning a fresh ServiceOps
    (production) so per-request config changes are picked up without a restart."""
    if not callable(ops):
        fixed = ops
        ops = lambda: fixed  # noqa: E731

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):  # quiet
            pass

        def _authed(self) -> bool:
            if not token:
                return True  # dev mode: no token configured
            header = self.headers.get("Authorization", "")
            return hmac.compare_digest(header, f"Bearer {token}")

        def _send(self, code: int, payload: dict | str):
            body = json.dumps(payload).encode() if isinstance(payload, dict) else str(payload).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if not self._authed():
                return self._send(401, {"ok": False, "detail": "unauthorized"})
            o = ops()
            if self.path == "/health":
                return self._send(200, o.health())
            if self.path == "/lms/status":
                return self._send(200, o.lms_status())
            self._send(404, {"ok": False, "detail": f"no GET route {self.path}"})

        def do_POST(self):
            if not self._authed():
                return self._send(401, {"ok": False, "detail": "unauthorized"})
            handler = _ROUTES.get(self.path)
            if handler is None:
                return self._send(404, {"ok": False, "detail": f"no POST route {self.path}"})
            try:
                length = int(self.headers.get("Content-Length", 0))
                raw = self.rfile.read(length) if length else b"{}"
                body = json.loads(raw) if raw else {}
                result = handler(ops(), body)
                self._send(200, result if isinstance(result, dict) else {"ok": True, "detail": str(result)})
            except Exception as exc:  # never crash the server
                log.exception("route %s failed", self.path)
                self._send(500, {"ok": False, "detail": str(exc)})

    return Handler


def run_agent(cfg=None, port: int | None = None, bind: str | None = None,
              token: str | None = None, block: bool = True):
    """Start the remote-agent HTTP server. Returns the server when block=False."""
    from ..config import get_config
    cfg = cfg or get_config()
    port = port or int(cfg.get("agent", "port", 8123))
    bind = bind or cfg.get("agent", "bind", "0.0.0.0")
    token = token if token is not None else cfg.get("remotes", "controller", {}).get("token", "")

    def ops_factory() -> ServiceOps:
        # Fresh config per request so env.yaml changes apply without an agent restart.
        from ..config import get_config as _gc
        return ServiceOps(_gc())

    httpd = ThreadingHTTPServer((bind, port), _make_handler(ops_factory, token))
    log.info("remote-agent listening on %s:%s (token %s)", bind, port,
             "set" if token else "DISABLED - dev mode")
    if block:
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            httpd.shutdown()
    return httpd

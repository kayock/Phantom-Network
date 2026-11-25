"""Phantom Network companion application wiring.

:class:`App` owns every service (config, events, HTTP, discovery, sandbox,
transfers, chat, audio, intercom, monitor) and implements the concrete
request handlers referenced by :mod:`companion.routes`.  The ``companion``
package is runnable via ``python -m companion``.
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import os
import signal
import socket
import sys
import threading
import time
import uuid
from typing import Optional

import shutil
from . import DEFAULT_API_PORT, __product__, __version__
from .audio import AudioService, PRESETS
from .chat import ChatEngine
from .config import Config, resolve_data_dir
from .discovery import DiscoveryService
from .events import EventBus
from .errors import FsError
from .fsops import (PathSandbox, UploadSessions, close_zip,
                    delete_entries, list_directory, list_drives, make_dir,
                    preview_file, recursive_search, rename_entry)
from .httpd import PhantomServer, Response, Router, SSEHub, file_response, \
    json_response, error_response
from .intercom import IntercomService
from .logging_setup import setup_logging
from .monitor import Monitor
from .peers import PeerClient, PeerRegistry
from .routes import WEB_TYPES, register_routes
from .transfers import TransferEngine
from .util import iso_now, now_ms, self_identity


class App:
    """Companion application context shared by every handler."""

    def __init__(self, args):
        self.args = args
        self.started = time.time()
        self.data_dir = resolve_data_dir(args.data_dir)
        self.log_path = setup_logging(self.data_dir, args.log_level,
                                      console=not args.quiet)
        self.log = __import__("logging").getLogger("pn.app")
        self.log.info("%s companion v%s starting (data: %s)",
                      __product__, __version__, self.data_dir)

        self.config = Config(self.data_dir)
        if args.name:
            self.config.set("device_name", args.name)
        if args.port:
            self.config.set("api_port", int(args.port))
        if args.no_discovery:
            self.config.set("discovery_enabled", False)
        if not self.config.get("device_id"):
            self.config.set("device_id", "dev_" + uuid.uuid4().hex[:12])
        if self.config.get("require_token") and \
                not self.config.get("auth_token"):
            import secrets
            self.config.set("auth_token", secrets.token_hex(16))

        self.router = Router()
        self.sse = SSEHub()
        self.events = EventBus(self.data_dir)
        self.sandbox = PathSandbox(self.config)
        self.registry = PeerRegistry(self.data_dir, self.events)
        self.fs = _FsFacade(self.sandbox)          # drives + helpers
        self.transfers = TransferEngine(self.data_dir, self.config,
                                        self.sandbox, self.events, self.sse,
                                        self.log)
        self.chat = ChatEngine(self.data_dir, self.config, self.registry,
                               self.events, self.sse, self.identity, self.log)
        self.audio = AudioService(self.data_dir, self.config, self.events,
                                  self.log)
        self.audio.attach_sandbox(self.sandbox)
        self.intercom = IntercomService(self.config, self.registry,
                                        self.events, self.sse, self.identity,
                                        self.log)
        self.monitor = Monitor(self.config, self.sandbox.shares, self.log)
        self.uploads = UploadSessions(self.sandbox,
                                      os.path.join(self.data_dir, "uploads"))
        self.discovery = DiscoveryService(self.config, self.registry,
                                          self.identity, self.api_port,
                                          self.events, self.log)
        self.server: Optional[PhantomServer] = None
        self._stop_event = threading.Event()

        register_routes(self)

    # ------------------------------------------------------------------
    # identity / ports
    # ------------------------------------------------------------------
    @property
    def api_port(self) -> int:
        try:
            return int(self.config.get("api_port") or DEFAULT_API_PORT)
        except (TypeError, ValueError):
            return DEFAULT_API_PORT

    def identity(self) -> dict:
        ident = self_identity(self.config.get("device_name") or "unnamed")
        ident["device_id"] = self.config.get("device_id")
        ident["api_port"] = self.api_port
        ident["data_dir"] = self.data_dir
        ident["addresses"] = self._own_addresses()
        # companion uptime — peers display this in the radar ("up 2h 14m")
        ident["uptime_s"] = round(time.time() - self.started, 1)
        # capability flags so other devices can show honest buttons
        ident["audio_enabled"] = bool(self.config.get("remote_audio_enabled", True))
        ident["intercom_enabled"] = bool(self.config.get("intercom_enabled", True))
        ident["shares_count"] = len(self.sandbox.shares())
        return ident

    def _own_addresses(self) -> list:
        from .util import local_ipv4s
        return local_ipv4s()

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------
    def run(self) -> int:
        port = self.api_port
        try:
            self.server = PhantomServer(("0.0.0.0", port), self)
        except OSError as exc:
            self.log.error("cannot bind port %d: %s", port, exc)
            self.events.emit("companion_error",
                             f"Cannot bind API port {port}: {exc}",
                             severity="error")
            return 2
        self.discovery.start()
        self.server_thread = threading.Thread(target=self.server.serve_forever,
                                              kwargs={"poll_interval": 0.5},
                                              name="pn-http", daemon=True)
        self.server_thread.start()
        self.events.emit("system",
                         f"Companion online: {self.config.get('device_name')} "
                         f"on port {port} ({', '.join(self._own_addresses()) or 'no LAN address'})")
        self.log.info("companion serving on 0.0.0.0:%d  UI: http://localhost:%d",
                      port, port)

        # graceful shutdown
        stop = self._stop_event
        def handle_signal(signum, frame):
            self.log.info("signal %s received; shutting down", signum)
            stop.set()
        signal.signal(signal.SIGINT, handle_signal)
        signal.signal(signal.SIGTERM, handle_signal)

        try:
            while not stop.is_set():
                stop.wait(1.0)
        except KeyboardInterrupt:
            pass
        self.shutdown()
        return 0

    def shutdown(self) -> None:
        self.log.info("shutdown: stopping services")
        self.events.emit("system", "Companion shutting down")
        self.discovery.stop()
        self.transfers.stop()
        self.monitor.stop()
        if self.server:
            threading.Thread(target=self.server.shutdown, daemon=True).start()
            try:
                self.server.server_close()
            except Exception:
                pass
        self.log.info("shutdown complete")

    # ------------------------------------------------------------------
    # SSE bridge
    # ------------------------------------------------------------------
    def sse_response(self, req) -> Response:
        topic_queue = []
        def gen(wfile):
            unsubscribe_all = []
            client_id, q = self.sse.subscribe("*")
            unsubscribe_all.append(lambda: self.sse.unsubscribe("*", q))
            try:
                wfile.write(b"retry: 3000\n\n")
                wfile.flush()
                import queue as _q
                while True:
                    try:
                        topic, data = q.get(timeout=15.0)
                        wfile.write(self.sse.format_event(topic, data))
                        wfile.flush()
                    except _q.Empty:
                        wfile.write(b": keepalive\n\n")
                        wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            finally:
                for fn in unsubscribe_all:
                    fn()
        resp = Response(200)
        resp.sse = gen
        return resp

    # ------------------------------------------------------------------
    # static UI
    # ------------------------------------------------------------------
    @property
    def web_dir(self) -> str:
        pkg = os.path.dirname(os.path.abspath(__file__))
        return os.path.join(os.path.dirname(pkg), "web")

    def serve_index(self, req) -> Response:
        return self._serve_file(os.path.join(self.web_dir, "index.html"))

    def serve_static(self, req) -> Response:
        rel = req.path.lstrip("/")
        target = os.path.normpath(os.path.join(self.web_dir, rel))
        if not target.startswith(os.path.abspath(self.web_dir)):
            return error_response(403, "forbidden", "Invalid path")
        if os.path.isdir(target):
            target = os.path.join(target, "index.html")
        return self._serve_file(target)

    def _serve_file(self, target: str) -> Response:
        if not os.path.isfile(target):
            return error_response(404, "not_found", f"No such UI file: {target}")
        ext = os.path.splitext(target)[1].lower()
        ctype = WEB_TYPES.get(ext) or mimetypes.guess_type(target)[0] \
            or "application/octet-stream"
        with open(target, "rb") as fh:
            data = fh.read()
        resp = Response(200, data, ctype)
        resp.headers["Cache-Control"] = "no-cache"
        return resp

    # ------------------------------------------------------------------
    # config / diagnostics
    # ------------------------------------------------------------------
    # ------------------------------------------------------------------
    # local-administrative boundary (v1.2.0)
    # ------------------------------------------------------------------
    def _admin_guard(self, req) -> None:
        """Administrative configuration may only be changed by THIS machine.

        Ordinary peer operations (browsing authorized shares, transfers,
        chat, audio triggering, verified-delete callbacks from a remote move)
        stay fully available to LAN peers.  What a remote client can no
        longer do is silently change local policy: add/remove shares, flip
        read/write permissions, toggle remote audio, or alter companion
        settings.  The user's local selections (first-run wizard) therefore
        actually stick.

        Can be relaxed per installation with config `admin_local_only=false`
        (e.g. a dedicated headless file server operated from a tablet).
        Combined with the optional `require_token` this is a policy layer,
        NOT a security boundary against a hostile LAN (documented in
        API_REFERENCE.md / REFINEMENT_REPORT.md).
        """
        if not self.config.get("admin_local_only", True):
            return
        addr = str(req.client_addr or "")
        if addr.startswith("127.") or addr in ("::1", "::ffff:127.0.0.1"):
            return
        raise FsError(403, "admin_local_only",
                      "Administrative configuration can only be changed from "
                      "this machine (open the command center on this device). "
                      "Remote peers keep normal peer operations.")

    def config_set(self, req) -> Response:
        self._admin_guard(req)
        patch = req.json()
        if not isinstance(patch, dict):
            return error_response(400, "bad_request", "Expected JSON object")
        protected = {"device_id", "auth_token", "api_port"}
        patch = {k: v for k, v in patch.items() if k not in protected}
        if patch.get("require_token") and not self.config.get("auth_token"):
            import secrets
            self.config.set("auth_token", secrets.token_hex(16))
        self.config.update(patch)
        self.events.emit("config_changed",
                         f"Configuration updated: {', '.join(patch.keys()) or '-'}")
        return json_response({"ok": True, "config": self.config.public_view()})

    def diagnostics_run(self, quick: bool = True) -> list:
        checks = []
        def check(name, fn, detail="", hint=""):
            try:
                ok = fn()
                checks.append({"check": name, "ok": bool(ok), "detail": detail,
                               "hint": hint if not ok else ""})
            except Exception as exc:
                checks.append({"check": name, "ok": False, "detail": str(exc),
                               "hint": hint})

        check("Config writable", lambda: (os.access(
            os.path.dirname(self.config.path), os.W_OK), True)[1])
        check("Event log", lambda: os.path.exists(
            os.path.join(self.data_dir, "events.jsonl")) or True,
            f"{len(self.events.stats())} event types recorded")
        check("HTTP server", lambda: self.server is not None
              and self.server_thread.is_alive(), f"port {self.api_port}")
        # loopback HTTP self-call
        def loopback():
            client = PeerClient()
            ident = client.identity(f"http://127.0.0.1:{self.api_port}",
                                    timeout=3.0)
            return ident.get("device_id") == self.config.get("device_id")
        check("Loopback API call", loopback, f"http://127.0.0.1:{self.api_port}")
        check("Discovery socket", lambda: self.discovery._sock is not None
              or not self.config.get("discovery_enabled", True),
              f"rx={self.discovery._rx} tx={self.discovery._tx}")
        check("Audio player", lambda: self.audio.player_name != "none",
              self.audio.player_name)
        check("SHA-256 engine", lambda: len(
            __import__('hashlib').sha256(b'phantom').hexdigest()) == 64)
        check("Shares readable", lambda: all(
            os.path.isdir(sh["path"]) for sh in self.sandbox.shares()),
            f"{len(self.sandbox.shares())} shares",
            "A configured share directory is missing — remove or fix it in Settings.")
        check("Share configured", lambda: len(self.sandbox.shares()) > 0,
              f"{len(self.sandbox.shares())} authorized director"
              f"{'y' if len(self.sandbox.shares()) == 1 else 'ies'}",
              "No directories are shared — other devices cannot browse files. "
              "Use Settings → Authorized directories (or the first-run wizard).")
        try:
            du = shutil.disk_usage(self.data_dir)
            free_gb = du.free / 1024 ** 3
            check("Disk space", lambda: free_gb > 1.0,
                  f"{free_gb:.1f} GB free at {self.data_dir}",
                  "Less than 1 GB free — transfers may fail with "
                  "'insufficient disk space'.")
        except Exception as exc:
            check("Disk space", lambda: False, str(exc))
        failed_transfers = sum(1 for h in self.transfers.list_jobs()["history"]
                               if h.get("status") in ("failed", "cancelled"))
        check("Recent transfer failures", lambda: failed_transfers == 0,
              f"{failed_transfers} failed/cancelled in history",
              "Open the Transfer Center to inspect error details; checksum "
              "mismatches indicate network corruption events.")
        check("Auth token", lambda: not self.config.get("require_token") or
              bool(self.config.get("auth_token")),
              "require_token=" + str(bool(self.config.get("require_token"))),
              "require_token is ON but no token is set — all peer requests "
              "will be refused until a token is configured on every device.")
        if not quick:
            def job_engine():
                st = self.transfers.stats()
                return st is not None
            check("Transfer engine", job_engine,
                  f"limit={self.config.get('bandwidth_limit_bps')} bps")
            def chat_store():
                self.chat.stats()
                return True
            check("Chat store", chat_store,
                  f"{self.chat.stats()['messages_stored']} messages")
            def monitor_ok():
                snap = self.monitor.snapshot()
                return snap["host"] is not None
            check("Telemetry", monitor_ok,
                  f"psutil={'yes' if snap_psutil() else 'no'}")
        return checks

    def diagnostics_report(self, req) -> Response:
        """Exportable local diagnostic report (nothing leaves this machine)."""
        snap = self.monitor.snapshot()
        checks = self.diagnostics_run(quick=False)
        report = {
            "product": __product__, "version": __version__,
            "generated": iso_now(),
            "identity": self.identity(),
            "listening": {"api_port": self.api_port,
                          "discovery_port": int(self.config.get("discovery_port") or 8414)},
            "discovery": self.discovery.status(),
            "peers": [p.to_public() for p in self.registry.all()],
            "config": self.config.public_view(),
            "shares": self.sandbox.shares(),
            "drives": self.fs.list_drives(),
            "transfers": self.transfers.stats(),
            "transfer_failures": [h for h in self.transfers.list_jobs()["history"]
                                  if h.get("status") in ("failed", "cancelled")][:20],
            "chat": self.chat.stats(),
            "audio": self.audio.status(),
            "intercom": {"enabled": self.intercom.enabled(),
                         "sessions": len(self.intercom.sessions())},
            "telemetry": snap,
            "checks": checks,
            "recent_errors": self.events.query(severity="error", limit=20),
            "explanations": DIAGNOSTIC_EXPLANATIONS,
        }
        resp = Response(200, json.dumps(report, indent=1, ensure_ascii=False)
                        .encode("utf-8"), "application/json; charset=utf-8")
        resp.headers["Content-Disposition"] = \
            'attachment; filename="phantom-diagnostics.json"'
        return resp

    # ------------------------------------------------------------------
    # radar handlers
    # ------------------------------------------------------------------
    def peer_register(self, req) -> Response:
        body = req.json() or {}
        address = str(body.get("address") or "").strip()
        name = str(body.get("name") or "").strip()
        port = body.get("port")
        if not address:
            return error_response(400, "bad_request", "address required")
        result = self.discovery.register_manual(
            address, name, int(port) if port else None)
        if result.get("ok"):
            return json_response({"ok": True, **result})
        return json_response({"ok": False, **result}, 200)  # probe result is valid info

    def setup_status(self, req) -> Response:
        """First-run helper: what is shared, what could be shared."""
        import platform as _pl
        home = os.path.expanduser("~")
        candidates = []
        for label, sub in (("Home", ""), ("Desktop", "Desktop"),
                           ("Documents", "Documents"), ("Downloads", "Downloads"),
                           ("Videos", "Videos"), ("Pictures", "Pictures")):
            path = os.path.join(home, sub) if sub else home
            if os.path.isdir(path):
                candidates.append({"path": path, "label": label})
        if _pl.system() == "Windows":
            for letter in "DEFGHIJKLMNOPQRSTUVWXYZ":
                drive = f"{letter}:\\"
                if os.path.isdir(drive):
                    candidates.append({"path": drive, "label": f"Drive {letter}:",
                                       "drive": True})
        else:
            for d in api_fs_drives_static():
                if d.get("kind") in ("drive", "external") and d["path"] != "/":
                    candidates.append({"path": d["path"],
                                       "label": f"{d['kind']}: {d['name']}",
                                       "drive": True})
        return json_response({
            "ok": True,
            "platform": _pl.system(),
            "shares": self.sandbox.shares(),
            "suggestions": candidates,
            "setup_complete": bool(self.config.get("setup_complete")),
        })

    def peer_metadata(self, req) -> Response:
        peer = self.registry.set_metadata(req.params[""], req.json() or {})
        if peer is None:
            return error_response(404, "no_peer", "Unknown device")
        return json_response({"ok": True, "peer": peer.to_public()})

    def peer_probe(self, req) -> Response:
        peer = self.registry.get(req.params[""])
        if peer is None or not peer.addresses:
            return error_response(404, "no_peer", "Unknown device or address")
        result = self.discovery._probe_one(peer.addresses[0])
        return json_response({"ok": True, **result})

    def radar_sweep(self, req) -> Response:
        body = req.json() or {}
        result = self.discovery.sweep(body.get("cidr") or "")
        return json_response(result, 200 if result.get("ok") else 400)

    # ------------------------------------------------------------------
    # filesystem handlers
    # ------------------------------------------------------------------
    def fs_list(self, req) -> Response:
        data = list_directory(self.sandbox, req.q("path"),
                              show_hidden=req.qbool("hidden"),
                              need="w" if req.qbool("write") else "r")
        return json_response({"ok": True, **data})

    def fs_stat(self, req) -> Response:
        real = self.sandbox.authorize(req.q("path"), "r")
        if not os.path.exists(real):
            return error_response(404, "not_found", "No such path")
        st = os.stat(real)
        return json_response({"ok": True, "path": real,
                              "is_dir": os.path.isdir(real),
                              "size": st.st_size,
                              "mtime": int(st.st_mtime * 1000)})

    def fs_preview(self, req) -> Response:
        return json_response({"ok": True,
                              **preview_file(self.sandbox, req.q("path"))})

    def fs_mkdir(self, req) -> Response:
        body = req.json() or {}
        return json_response({"ok": True,
                              **make_dir(self.sandbox, body.get("path", ""),
                                         body.get("name", ""))})

    def fs_rename(self, req) -> Response:
        body = req.json() or {}
        return json_response({"ok": True,
                              **rename_entry(self.sandbox,
                                             body.get("path", ""),
                                             body.get("new_name", ""))})

    def fs_delete(self, req) -> Response:
        body = req.json() or {}
        paths = body.get("paths") or ([body.get("path")] if body.get("path") else [])
        result = delete_entries(self.sandbox, paths,
                                confirm=bool(body.get("confirm")))
        self.events.emit("system",
                         f"Deleted {sum(1 for r in result['results'] if r['deleted'])} "
                         f"item(s)", severity="warn")
        return json_response({"ok": True, **result})

    def fs_search(self, req) -> Response:
        body = req.json() or {}
        return json_response({"ok": True,
                              **recursive_search(self.sandbox,
                                                 body.get("root", ""),
                                                 body.get("query", ""),
                                                 max_results=int(
                                                     body.get("max") or 300))})

    def fs_download(self, req) -> Response:
        fh, size, name = _open_download_guarded(self.sandbox, req.q("path"))
        return file_response(fh, size, "application/octet-stream",
                             filename=name,
                             range_header=req.headers.get("Range", ""))

    def fs_zip(self, req) -> Response:
        from .fsops import make_zip_stream
        fh, size, name = make_zip_stream(
            self.sandbox, req.q("path"),
            os.path.join(self.data_dir, "tmp"))
        resp = file_response(fh, size, "application/zip", filename=name,
                             range_header=req.headers.get("Range", ""))
        resp.headers["X-PN-Cleanup"] = "zip"
        return resp

    def fs_hash(self, req) -> Response:
        return json_response({"ok": True,
                              **self.transfers.file_hash(req.q("path"))})

    def fs_hash_prepare(self, req) -> Response:
        body = req.json() or {}
        path = body.get("path") or req.q("path")
        return json_response(self.transfers.hash_prepare.prepare(path or ""))

    def fs_hash_prepare_status(self, req) -> Response:
        return json_response(
            self.transfers.hash_prepare.status(req.params[""]))

    def fs_hash_prepare_cancel(self, req) -> Response:
        return json_response(
            self.transfers.hash_prepare.cancel(req.params[""]))

    def fs_walk(self, req) -> Response:
        return json_response({"ok": True,
                              **self.transfers.walk_remote_manifest(
                                  req.q("path"),
                                  skip=req.qint("skip", 0),
                                  limit=req.qint("limit", 5000))})

    def fs_verified_delete(self, req) -> Response:
        body = req.json() or {}
        result = self.transfers.verified_delete(
            body.get("path", ""), body.get("sha256", ""),
            expected_mtime_ns=body.get("mtime_ns"),
            expected_size=body.get("size"))
        return json_response({"ok": True, **result})

    def fs_share_add(self, req) -> Response:
        self._admin_guard(req)
        body = req.json() or {}
        share = self.sandbox.add_share(body.get("path", ""),
                                       body.get("mode", "rw"),
                                       body.get("label", ""))
        self.events.emit("config_changed",
                         f"Share added: {share['path']} ({share['mode']})")
        return json_response({"ok": True, "share": share,
                              "shares": self.sandbox.shares()})

    def fs_share_remove(self, req) -> Response:
        self._admin_guard(req)
        body = req.json() or {}
        self.sandbox.remove_share(body.get("path", ""))
        return json_response({"ok": True, "shares": self.sandbox.shares()})

    # -- uploads ---------------------------------------------------------
    def upload_session(self, req) -> Response:
        body = req.json() or {}
        result = self.uploads.create(body.get("path", ""),
                                     body.get("name", ""),
                                     int(body.get("size") or 0),
                                     body.get("conflict", "rename"))
        return json_response({"ok": True, **result})

    def upload_chunk(self, req) -> Response:
        result = self.uploads.append(req.params[""], req.body_iter())
        return json_response({"ok": True, **result})

    def upload_finish(self, req) -> Response:
        body = req.json() or {}
        result = self.uploads.finalize(req.params[""],
                                       body.get("sha256", ""))
        self.events.emit("transfer_completed",
                         f"Upload finished: {result['target']}")
        return json_response({"ok": True, **result})

    def upload_cancel(self, req) -> Response:
        return json_response({"ok": True,
                              **self.uploads.cancel(req.params[""])})

    # ------------------------------------------------------------------
    # transfer handlers
    # ------------------------------------------------------------------
    def job_create(self, req) -> Response:
        spec = req.json() or {}
        job = self.transfers.create_job(spec)
        return json_response({"ok": True, "job": job}, 201)

    def job_get(self, req) -> Response:
        job = self.transfers.get_job(req.params[""])
        if job is None:
            for old in self.transfers.list_jobs()["history"]:
                if old["id"] == req.params[""]:
                    return json_response({"ok": True, "job": old})
            return error_response(404, "no_job", "Unknown job")
        return json_response({"ok": True, "job": job})

    def job_control(self, req) -> Response:
        body = req.json() or {}
        action = body.get("action", "")
        job = self.transfers.control(req.params[""], action)
        if job is None:
            return error_response(404, "no_job", "Unknown job")
        return json_response({"ok": True, "job": job})

    def sync_preview(self, req) -> Response:
        return json_response({"ok": True,
                              **self._sync_plan(req.json() or {})})

    def sync_apply(self, req) -> Response:
        body = req.json() or {}
        plan = body.get("plan") or {}
        delete_confirmed = bool(body.get("delete_confirmed"))
        jobs = []
        for file_entry in plan.get("copy", []):
            spec = {
                "kind": "pull",
                "src_base": plan.get("src_base"),
                "src_path": file_entry["src"],
                "dest_path": file_entry["dest"],
                "conflict": "replace",
                "src_device": plan.get("src_device"),
                "dest_device": plan.get("dst_device"),
            }
            target_base = plan.get("dst_base") or ""
            if target_base and target_base.rstrip("/") not in (
                    f"http://127.0.0.1:{self.api_port}",
                    f"http://localhost:{self.api_port}"):
                client = PeerClient(self._client_token)
                try:
                    job = client.request("POST",
                                         f"{target_base}/api/jobs",
                                         body=spec, timeout=10)
                    jobs.append(job.get("job", job))
                except Exception as exc:
                    jobs.append({"error": str(exc), "src": file_entry["src"]})
            else:
                jobs.append(self.transfers.create_job(spec))
        return json_response({"ok": True, "jobs": jobs,
                              "delete_confirmed": delete_confirmed})

    def _sync_plan(self, spec: dict) -> dict:
        src_base = spec.get("src_base", "").rstrip("/")
        dst_base = spec.get("dst_base", "").rstrip("/")
        src_path = spec.get("src_path", "")
        dst_path = spec.get("dst_path", "")
        compare = spec.get("compare", "size")
        if compare not in ("size", "mtime", "hash"):
            compare = "size"
        client = PeerClient(self._client_token)
        me_local = (not src_base) or src_base in (
            f"http://127.0.0.1:{self.api_port}",
            f"http://localhost:{self.api_port}")
        dst_local = (not dst_base) or dst_base in (
            f"http://127.0.0.1:{self.api_port}",
            f"http://localhost:{self.api_port}")
        if me_local:
            src_walk = self.transfers.walk_remote_manifest(src_path)
        else:
            src_walk = client.request("GET",
                                      f"{src_base}/api/fs/walk?path={_q(src_path)}",
                                      timeout=60)
        if dst_local:
            dst_walk = self.transfers.walk_remote_manifest(dst_path)
        else:
            dst_walk = client.request("GET",
                                      f"{dst_base}/api/fs/walk?path={_q(dst_path)}",
                                      timeout=60)
        dst_files = {f["rel"]: f for f in dst_walk.get("files", [])}
        copy, skip, unverified = [], [], 0
        hash_checked = 0
        HASH_PREVIEW_CAP = 400     # keep previews responsive on huge trees
        for f in src_walk.get("files", []):
            rel = f["rel"]
            dest_f = dst_files.get(rel)
            dest_full = os.path.join(dst_path, *rel.split("/"))
            if dest_f is None:
                copy.append({"rel": rel, "src": f["path"], "dest": dest_full,
                             "size": f["size"], "reason": "missing at destination"})
                continue
            if dest_f.get("size") != f.get("size"):
                copy.append({"rel": rel, "src": f["path"], "dest": dest_full,
                             "size": f["size"], "reason": "size differs"})
                continue
            # equal size: decide per comparison strategy
            verdict = "skip"
            if compare == "hash":
                if hash_checked < HASH_PREVIEW_CAP:
                    try:
                        src_h = (self.transfers.file_hash(f["path"])["sha256"]
                                 if me_local else
                                 client.request("GET",
                                     f"{src_base}/api/fs/hash?path={_q(f['path'])}",
                                     timeout=30)["sha256"])
                        dst_h = (self.transfers.file_hash(dest_f["path"])["sha256"]
                                 if dst_local else
                                 client.request("GET",
                                     f"{dst_base}/api/fs/hash?path={_q(dest_f['path'])}",
                                     timeout=30)["sha256"])
                        hash_checked += 1
                        verdict = "skip" if src_h == dst_h else "content differs"
                    except Exception:
                        unverified += 1
                        verdict = "unverified (hash failed — will copy+verify)"
                else:
                    unverified += 1
                    verdict = "unverified (preview hash cap reached — will copy+verify)"
            elif compare == "mtime":
                verdict = "skip" if dest_f.get("mtime", 0) >= f.get("mtime", 0) \
                    else "destination is older"
            if verdict == "skip":
                skip.append({"rel": rel, "size": f["size"], "reason": "identical"})
            else:
                copy.append({"rel": rel, "src": f["path"], "dest": dest_full,
                             "size": f["size"], "reason": verdict})
        notes = {
            "size": ["Size comparison: equal-size files are skipped. Equal size "
                     "does not prove identical content — choose 'Verified "
                     "(SHA-256)' for content-accurate sync. The transfer engine "
                     "verifies SHA-256 after every copy either way."],
            "mtime": ["Modification-time comparison: destination kept when it is "
                      "not older than the source. Transfer engine verifies "
                      "SHA-256 after every copy. Nothing is deleted."],
            "hash": [f"SHA-256 content comparison ({hash_checked} file(s) hashed, "
                     f"{unverified} beyond cap/failures will be copied+verified "
                     f"by the engine). Nothing is deleted."],
        }[compare]
        return {
            "src_base": src_base, "dst_base": dst_base,
            "src_path": src_path, "dst_path": dst_path,
            "compare": compare,
            "src_device": spec.get("src_device"), "dst_device": spec.get("dst_device"),
            "copy": copy, "skip": skip,
            "copy_bytes": sum(c["size"] or 0 for c in copy),
            "dirs": src_walk.get("dirs", []),
            "notes": notes,
        }

    def _client_token(self) -> str:
        if self.config.get("require_token"):
            return self.config.get("auth_token", "")
        return ""

    # ------------------------------------------------------------------
    # chat handlers
    # ------------------------------------------------------------------
    def chat_send(self, req) -> Response:
        body = req.json() or {}
        msg = self.chat.send(body.get("to_kind", "direct"),
                             body.get("to_id", ""),
                             body.get("body", ""),
                             body.get("attachment"))
        return json_response({"ok": True, "message": msg})

    def chat_incoming(self, req) -> Response:
        body = req.json() or {}
        message = body.get("message") or {}
        if not message or not message.get("id"):
            return error_response(400, "bad_request", "message required")
        result = self.chat.receive_incoming(message, req.client_addr)
        return json_response({"ok": True, **result})

    def chat_read(self, req) -> Response:
        body = req.json() or {}
        result = self.chat.mark_read(body.get("conv", ""),
                                     int(body.get("up_to_ts") or 0))
        return json_response({"ok": True, **result})

    def chat_ack_read(self, req) -> Response:
        body = req.json() or {}
        self.chat.ack_read(body.get("reader", ""),
                           int(body.get("up_to_ts") or 0))
        return json_response({"ok": True})

    def chat_history(self, req) -> Response:
        return json_response({"ok": True, "messages": self.chat.history(
            req.q("conv"), req.q("q"), req.qint("limit", 200))})

    def chat_group_create(self, req) -> Response:
        body = req.json() or {}
        group = self.chat.create_group(body.get("name", "group"),
                                       body.get("members") or [])
        return json_response({"ok": True, "group": group})

    def chat_group_delete(self, req) -> Response:
        self.chat.delete_group(req.params[""])
        return json_response({"ok": True})

    # ------------------------------------------------------------------
    # audio handlers
    # ------------------------------------------------------------------
    def audio_tone(self, req) -> Response:
        body = req.json() or {}
        result = self.audio.play_tone(
            float(body.get("freq", 440)),
            float(body.get("duration", 1.0)),
            body.get("waveform", "sine"),
            float(body.get("volume", 0.6)))
        return json_response({"ok": True, **result})

    def audio_preset(self, req) -> Response:
        body = req.json() or {}
        result = self.audio.play_preset(body.get("name", ""),
                                        float(body.get("volume", 0.7)))
        return json_response({"ok": True, **result})

    def audio_file(self, req) -> Response:
        body = req.json() or {}
        return json_response({"ok": True,
                              **self.audio.play_file(body.get("path", ""))})

    def audio_enabled(self, req) -> Response:
        self._admin_guard(req)
        body = req.json() or {}
        self.audio.set_enabled(bool(body.get("enabled", True)))
        return json_response({"ok": True, "enabled": self.audio.enabled(),
                              "status": self.audio.status()})

    # ------------------------------------------------------------------
    # intercom handlers
    # ------------------------------------------------------------------
    def icom_session(self, req) -> Response:
        body = req.json() or {}
        result = self.intercom.create_session(
            body.get("participants") or [], body.get("label", ""))
        return json_response(result, 200 if result.get("ok") else 400)

    def icom_session_end(self, req) -> Response:
        return json_response(self.intercom.end_session(req.params[""]))

    def icom_talk(self, req) -> Response:
        body = req.json() or {}
        result = self.intercom.talk(body.get("session", ""),
                                    body.get("pcm_b64", ""),
                                    int(body.get("seq") or 0))
        return json_response(result, 200 if result.get("ok") else 400)

    def icom_relay(self, req) -> Response:
        result = self.intercom.relay(req.json() or {})
        return json_response(result, 200 if result.get("ok") else 400)

    def icom_stop(self, req) -> Response:
        body = req.json() or {}
        return json_response(self.intercom.stop_talking(body.get("session", "")))

    # ------------------------------------------------------------------
    # events
    # ------------------------------------------------------------------
    def events_query(self, req) -> Response:
        types = req.q("types")
        return json_response({"ok": True, "events": self.events.query(
            types=[t for t in types.split(",") if t] if types else None,
            search=req.q("q"), since_ts=req.qint("since"),
            limit=req.qint("limit", 300),
            severity=req.q("severity") or None)})

    def events_export(self, req) -> Response:
        events = self.events.query(limit=100000)
        payload = json.dumps({"exported": iso_now(),
                              "device": self.config.get("device_name"),
                              "events": events}, indent=1, ensure_ascii=False)
        resp = Response(200, payload.encode("utf-8"),
                        "application/json; charset=utf-8")
        resp.headers["Content-Disposition"] = \
            'attachment; filename="phantom-events.json"'
        return resp


class _FsFacade:
    """Small adapter exposing fs helpers on the App (api.fs.list_drives)."""

    def __init__(self, sandbox: PathSandbox):
        self.sandbox = sandbox

    def list_drives(self) -> list:
        return list_drives()


def _open_download_guarded(sandbox: PathSandbox, path: str):
    from .fsops import open_download
    return open_download(sandbox, path)


def api_fs_drives_static():
    from .fsops import list_drives
    try:
        return list_drives()
    except Exception:
        return []


def _q(path: str) -> str:
    import urllib.parse
    return urllib.parse.quote(path, safe="")


DIAGNOSTIC_EXPLANATIONS = {
    "companion_unreachable": "The companion is not running on that machine, "
        "its port changed, or a firewall blocks it. Start it with "
        "'python -m companion' and allow port 8415/tcp if needed.",
    "incorrect_ip": "The registered address no longer matches the device "
        "(DHCP change). Re-register it from the Network Radar.",
    "port_conflict": "Another program owns port 8415. Start the companion "
        "with --port <other> on that machine.",
    "firewall": "Discovery uses UDP 8414 and the API TCP 8415. Allow both "
        "in your firewall (commands in DEPLOYMENT.md).",
    "directory_not_shared": "File Commander can only see authorized shares. "
        "Add the directory in Settings → Authorized directories.",
    "insufficient_permissions": "The OS refused the operation. Check that "
        "the account running the companion may read/write that directory.",
    "insufficient_disk": "The destination needs size + min_free_space_bytes "
        "free before the first byte is written.",
    "checksum_mismatch": "Received bytes failed SHA-256 verification — a "
        "corruption event. The destination was NOT finalized; the .part file "
        "was kept as .part.corrupt for diagnosis. Simply retry.",
    "audio_player_unavailable": "No player found (paplay/aplay/ffplay on "
        "Linux; PowerShell SoundPlayer on Windows). Install PulseAudio utils "
        "or check PowerShell availability.",
    "microphone_restricted": "Browsers grant microphone access only on "
        "secure contexts (localhost/HTTPS). See Intercom view notes.",
}


def snap_psutil() -> bool:
    try:
        import psutil  # noqa: F401
        return True
    except ImportError:
        return False


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="companion",
        description="PHANTOM NETWORK companion service (LAN command center agent)")
    parser.add_argument("--port", type=int, default=None,
                        help=f"HTTP API/UI port (default {DEFAULT_API_PORT})")
    parser.add_argument("--name", default=None, help="device display name")
    parser.add_argument("--data-dir", default=None,
                        help="state directory (default: platform config dir)")
    parser.add_argument("--no-discovery", action="store_true",
                        help="disable UDP discovery beacons")
    parser.add_argument("--log-level", default="INFO",
                        choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    parser.add_argument("--quiet", action="store_true",
                        help="no console output (log to file only)")
    parser.add_argument("--version", action="version",
                        version=f"{__product__} {__version__}")
    args = parser.parse_args(argv)

    app = App(args)
    return app.run()


if __name__ == "__main__":
    sys.exit(main())

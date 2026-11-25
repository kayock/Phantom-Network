"""REST API route registration.

Every endpoint the Phantom Network UI and peer companions call is defined
here as ``(method, regex, handler)`` tuples on the shared Router.  Handlers
are thin: validate → delegate to a service module → wrap the result.

Full request/response documentation lives in API_REFERENCE.md.
"""

from __future__ import annotations

import io
import json
import os
import platform
import socket
import time
import uuid
from typing import Optional

from . import PROTOCOL_VERSION, __version__, __product__
from .httpd import Request, Response, error_response, file_response, json_response
from .util import now_ms
from .errors import FsError
from .voice import VoiceError
from .broadcast import BroadcastError
from .media import MediaError
from .clipboard import ClipboardError


def _mesh_result(fn):
    try:
        return json_response(fn())
    except Exception as exc:
        return json_response({"ok": False, "error": str(exc)}, 403)

WEB_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".webmanifest": "application/manifest+json",
    ".txt": "text/plain; charset=utf-8",
    ".webm": "audio/webm",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".mjs": "application/javascript; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
    ".json": "application/json",
    ".wav": "audio/wav",
    ".woff2": "font/woff2",
}


def fs_error_response(exc: FsError) -> Response:
    return error_response(exc.status, exc.code, exc.message)


def register_routes(api) -> None:
    """Attach every route to the shared router.  ``api`` is the App."""
    r = api.router
    s = api.sandbox

    # -- system ---------------------------------------------------------
    r.add("GET", r"/api/health", lambda req: json_response({
        "ok": True, "product": __product__, "version": __version__,
        "protocol": PROTOCOL_VERSION, "uptime_s": round(time.time() - api.started, 1),
        "ts": now_ms(),
    }), auth=False)

    r.add("GET", r"/api/identity", lambda req: json_response(
        dict(api.identity())), auth=False)

    r.add("GET", r"/api/stream", lambda req: api.sse_response(req))

    r.add("GET", r"/api/stats", lambda req: json_response({
        "peers": len(api.registry.all()),
        "peers_online": sum(1 for p in api.registry.all()
                            if p.status == "online"),
        "transfers": api.transfers.stats(),
        "chat": api.chat.stats(),
        "events": api.events.stats(),
        "discovery": api.discovery.status(),
        "process": api.monitor._process(),
    }))

    # -- config ---------------------------------------------------------
    r.add("GET", r"/api/config", lambda req: json_response(
        {"ok": True, "config": api.config.public_view()}))
    r.add("POST", r"/api/config", lambda req: api.config_set(req))

    # -- radar / peers ----------------------------------------------------
    r.add("GET", r"/api/peers", lambda req: json_response({
        "ok": True, "self": api.identity(),
        "peers": api.registry.public_list(),
        "discovery": api.discovery.status(),
    }))
    r.add("POST", r"/api/peers/register", lambda req: api.peer_register(req))
    r.add("POST", r"/api/peers/([^/]+)/metadata", lambda req: api.peer_metadata(req))
    r.add("POST", r"/api/peers/([^/]+)/probe", lambda req: api.peer_probe(req))
    r.add("POST", r"/api/radar/sweep", lambda req: api.radar_sweep(req))
    r.add("POST", r"/api/peers/exchange", lambda req: json_response({
        "ok": True, **api.discovery.receive_exchange(
            (req.json() or {}).get("peers") or [], req.client_addr)}))
    r.add("GET", r"/api/discovery/diagnostics",
          lambda req: api.discovery_diagnostics(req))
    r.add("GET", r"/api/setup/status", lambda req: api.setup_status(req))

    # -- filesystem -------------------------------------------------------
    r.add("GET", r"/api/fs/roots", lambda req: json_response({
        "ok": True,
        "shares": s.shares(),
        "drives": api.fs.list_drives(),
        "platform": platform.system(),
    }))
    r.add("GET", r"/api/fs/list", lambda req: api.fs_list(req))
    r.add("GET", r"/api/fs/stat", lambda req: api.fs_stat(req))
    r.add("GET", r"/api/fs/preview", lambda req: api.fs_preview(req))
    r.add("POST", r"/api/fs/mkdir", lambda req: api.fs_mkdir(req))
    r.add("POST", r"/api/fs/rename", lambda req: api.fs_rename(req))
    r.add("POST", r"/api/fs/delete", lambda req: api.fs_delete(req))
    r.add("POST", r"/api/fs/search", lambda req: api.fs_search(req))
    r.add("GET", r"/api/fs/download", lambda req: api.fs_download(req))
    r.add("GET", r"/api/fs/zip", lambda req: api.fs_zip(req))
    r.add("GET", r"/api/fs/hash", lambda req: api.fs_hash(req))
    r.add("POST", r"/api/fs/hash/prepare", lambda req: api.fs_hash_prepare(req))
    r.add("GET", r"/api/fs/hash/prepare/([^/]+)",
          lambda req: api.fs_hash_prepare_status(req))
    r.add("POST", r"/api/fs/hash/prepare/([^/]+)/cancel",
          lambda req: api.fs_hash_prepare_cancel(req))
    r.add("GET", r"/api/fs/walk", lambda req: api.fs_walk(req))
    r.add("POST", r"/api/fs/verified-delete", lambda req: api.fs_verified_delete(req))
    r.add("POST", r"/api/fs/share", lambda req: api.fs_share_add(req))
    r.add("POST", r"/api/fs/unshare", lambda req: api.fs_share_remove(req))
    # uploads
    r.add("POST", r"/api/fs/upload/session", lambda req: api.upload_session(req))
    r.add("PUT", r"/api/fs/upload/([^/]+)", lambda req: api.upload_chunk(req))
    r.add("POST", r"/api/fs/upload/([^/]+)/finish", lambda req: api.upload_finish(req))
    r.add("POST", r"/api/fs/upload/([^/]+)/cancel", lambda req: api.upload_cancel(req))

    # -- transfers --------------------------------------------------------
    r.add("POST", r"/api/jobs", lambda req: api.job_create(req))
    r.add("GET", r"/api/jobs", lambda req: json_response(
        {"ok": True, **api.transfers.list_jobs(),
         "stats": api.transfers.stats()}))
    r.add("GET", r"/api/jobs/([^/]+)", lambda req: api.job_get(req))
    r.add("POST", r"/api/jobs/([^/]+)/control", lambda req: api.job_control(req))
    r.add("POST", r"/api/jobs/history/clear", lambda req: (
        api.transfers.clear_history(), json_response({"ok": True}))[1])
    # sync
    r.add("POST", r"/api/sync/preview", lambda req: api.sync_preview(req))
    r.add("POST", r"/api/sync/apply", lambda req: api.sync_apply(req))

    # -- mobile clients (v1.3.0) --------------------------------------------
    r.add("POST", r"/api/mobile/register", lambda req: api.mobile_register(req), auth=False)
    r.add("POST", r"/api/mobile/recover", lambda req: api.mobile_recover(req), auth=False)
    r.add("POST", r"/api/mobile/heartbeat", lambda req: api.mobile_heartbeat(req), auth=False)
    r.add("POST", r"/api/mobile/prefs", lambda req: api.mobile_prefs(req), auth=False)
    r.add("POST", r"/api/mobile/chat/send", lambda req: api.mobile_chat_send(req), auth=False)
    r.add("POST", r"/api/mobile/chat/ack", lambda req: api.mobile_chat_ack(req), auth=False)
    r.add("POST", r"/api/mobile/chat/mutate", lambda req: api.mobile_chat_mutate(req), auth=False)
    r.add("POST", r"/api/mobile/inbox", lambda req: api.mobile_inbox(req), auth=False)
    r.add("POST", r"/api/mobile/unread", lambda req: api.mobile_unread(req), auth=False)
    r.add("GET", r"/api/mobile/stream", lambda req: api.mobile_stream(req), auth=False)
    r.add("GET", r"/api/mobile/clients", lambda req: api.mobile_clients(req), auth=False)
    r.add("POST", r"/api/mobile/clients/([^/]+)/rename",
          lambda req: api.mobile_rename(req))
    r.add("POST", r"/api/mobile/clients/([^/]+)/remove",
          lambda req: api.mobile_remove(req))

    # -- chat -------------------------------------------------------------
    r.add("POST", r"/api/chat/send", lambda req: api.chat_send(req))
    r.add("POST", r"/api/chat/incoming", lambda req: api.chat_incoming(req))
    r.add("POST", r"/api/chat/read", lambda req: api.chat_read(req))
    r.add("POST", r"/api/chat/ack-read", lambda req: api.chat_ack_read(req))
    r.add("GET", r"/api/chat/conversations", lambda req: json_response(
        {"ok": True, "conversations": api.chat.conversations()}))
    r.add("GET", r"/api/chat/history", lambda req: api.chat_history(req))
    r.add("POST", r"/api/chat/groups", lambda req: api.chat_group_create(req))
    r.add("DELETE", r"/api/chat/groups/([^/]+)", lambda req: api.chat_group_delete(req))
    # v1.3.0: mutations, receipts, attachments
    r.add("POST", r"/api/chat/mutate", lambda req: api.chat_mutate(req))
    r.add("POST", r"/api/chat/react", lambda req: api.chat_react(req))
    r.add("POST", r"/api/chat/edit", lambda req: api.chat_edit(req))
    r.add("POST", r"/api/chat/delete", lambda req: api.chat_delete_msg(req))
    r.add("POST", r"/api/chat/ack-delivery", lambda req: api.chat_ack_delivery(req))
    r.add("POST", r"/api/chat/file/session", lambda req: api.chat_file_session(req))
    r.add("PUT", r"/api/chat/file/([^/]+)", lambda req: api.chat_file_chunk(req))
    r.add("POST", r"/api/chat/file/([^/]+)/finish",
          lambda req: api.chat_file_finish(req))
    r.add("POST", r"/api/chat/file/([^/]+)/cancel",
          lambda req: api.chat_file_cancel(req))
    r.add("GET", r"/api/chat/file/([^/]+)", lambda req: api.chat_file_download(req), auth=False)

    # -- audio --------------------------------------------------------------
    r.add("GET", r"/api/audio/status", lambda req: json_response(
        {"ok": True, **api.audio.status()}))
    r.add("POST", r"/api/audio/tone", lambda req: api.audio_tone(req))
    r.add("POST", r"/api/audio/preset", lambda req: api.audio_preset(req))
    r.add("POST", r"/api/audio/file", lambda req: api.audio_file(req))
    r.add("POST", r"/api/audio/stop", lambda req: json_response(
        {"ok": True, **api.audio.stop()}))
    r.add("POST", r"/api/audio/enabled", lambda req: api.audio_enabled(req))

    # -- voice (v1.4.0 — Phantom Voice) ------------------------------------
    def _v_err(fn):
        def wrap(req):
            try:
                return json_response(fn(req))
            except VoiceError as exc:
                return fs_error_response(FsError(exc.status, exc.code,
                                                 exc.message))
        return wrap

    r.add("POST", r"/api/voice/call", _v_err(
        lambda req: api.voice_call(req)))
    r.add("POST", r"/api/voice/answer", _v_err(
        lambda req: api.voice_answer(req)))
    r.add("POST", r"/api/voice/signal", _v_err(
        lambda req: api.voice_signal(req)))
    r.add("POST", r"/api/voice/end", _v_err(
        lambda req: api.voice_end(req)))
    r.add("POST", r"/api/voice/room/join", _v_err(
        lambda req: api.voice_room_join(req)))
    r.add("POST", r"/api/voice/room/leave", _v_err(
        lambda req: api.voice_room_leave(req)))
    r.add("POST", r"/api/voice/relay", lambda req: json_response(
        api.voice_relay(req)), auth=False)
    r.add("GET", r"/api/voice/state", _v_err(
        lambda req: api.voice.state()))
    r.add("GET", r"/api/voice/history", _v_err(
        lambda req: api.voice.history_list(
            int(req.q("limit", "100") or 100))))

    # -- broadcast (v1.4.0) -------------------------------------------------
    def _b_err(fn):
        def wrap(req):
            try:
                return json_response(fn(req))
            except BroadcastError as exc:
                return fs_error_response(FsError(exc.status, exc.code,
                                                 exc.message))
        return wrap

    r.add("GET", r"/api/broadcast/profiles", _b_err(
        lambda req: api.broadcast.profiles()))
    r.add("POST", r"/api/broadcast/profile", _b_err(
        lambda req: api.broadcast.profile_add(req.json() or {})))
    r.add("GET", r"/api/broadcast/groups", _b_err(
        lambda req: api.broadcast.groups()))
    r.add("POST", r"/api/broadcast/group", _b_err(
        lambda req: api.broadcast.group_save(req.json() or {})))
    r.add("DELETE", r"/api/broadcast/group/([^/]+)", _b_err(
        lambda req: api.broadcast.group_delete(req.params[""])))
    r.add("GET", r"/api/broadcast/history", _b_err(
        lambda req: api.broadcast.history(int(req.q("limit", "100") or 100))))
    r.add("POST", r"/api/broadcast/announce", _b_err(
        lambda req: api.broadcast.announce(req.json() or {})))
    r.add("POST", r"/api/broadcast/incoming", _b_err(
        lambda req: api.broadcast.incoming((req.json() or {})
                                           .get("announcement") or {})),
        auth=False)
    r.add("POST", r"/api/broadcast/relay", _b_err(
        lambda req: api.broadcast.relay(req.json() or {})), auth=False)
    r.add("POST", r"/api/broadcast/ack", _b_err(
        lambda req: api.broadcast.acknowledge(
            str((req.json() or {}).get("id") or ""),
            (req.json() or {}).get("who") or {})))
    r.add("GET", r"/api/broadcast/schedules", _b_err(
        lambda req: api.broadcast.schedules()))
    r.add("POST", r"/api/broadcast/schedule", _b_err(
        lambda req: api.broadcast.schedule_save(req.json() or {})))
    r.add("DELETE", r"/api/broadcast/schedule/([^/]+)", _b_err(
        lambda req: api.broadcast.schedule_delete(req.params[""])))

    # -- media (v1.4.0) ------------------------------------------------------
    def _m_err(fn):
        def wrap(req):
            try:
                return json_response(fn(req))
            except MediaError as exc:
                return fs_error_response(FsError(exc.status, exc.code,
                                                 exc.message))
        return wrap

    r.add("GET", r"/api/media/rooms", _m_err(lambda req: api.media.rooms()))
    r.add("POST", r"/api/media/room", _m_err(
        lambda req: api.media.room_create(req.json() or {})))
    r.add("DELETE", r"/api/media/room/([^/]+)", _m_err(
        lambda req: api.media.room_delete(req.params[""])))
    r.add("POST", r"/api/media/room/join", _m_err(
        lambda req: api.media.room_join(req.json() or {})))
    r.add("POST", r"/api/media/room/leave", _m_err(
        lambda req: api.media.room_leave(req.json() or {})))
    r.add("POST", r"/api/media/enqueue", _m_err(
        lambda req: api.media.enqueue(req.json() or {})))
    r.add("POST", r"/api/media/dequeue", _m_err(
        lambda req: api.media.dequeue(req.json() or {})))
    r.add("POST", r"/api/media/reorder", _m_err(
        lambda req: api.media.reorder(req.json() or {})))
    r.add("POST", r"/api/media/control", _m_err(
        lambda req: api.media.control(req.json() or {})))
    r.add("GET", r"/api/media/state", _m_err(
        lambda req: api.media.state(req.q("room_id", ""))))
    r.add("GET", r"/api/media/history", _m_err(
        lambda req: api.media.history(req.q("room_id", ""),
                                      int(req.q("limit", "100") or 100))))
    r.add("GET", r"/api/media/stream", _m_err(
        lambda req: api.media_stream(req)))

    # -- tls status / cert export (v1.4.0 steer: HTTPS usability) -----------
    r.add("GET", r"/api/tls/status", lambda req: json_response(
        api.tls_status()))
    r.add("GET", r"/api/tls/cert", lambda req: api.tls_cert_download(),
          auth=False)

    # -- clipboard (v1.4.0, steer addition) ----------------------------------
    def _c_err(fn):
        def wrap(req):
            try:
                return json_response(fn(req))
            except ClipboardError as exc:
                return fs_error_response(FsError(exc.status, exc.code,
                                                 exc.message))
        return wrap

    r.add("GET", r"/api/clipboard/status", _c_err(
        lambda req: api.clipboard.status()))
    r.add("GET", r"/api/clipboard/items", _c_err(
        lambda req: api.clipboard.items(req.q("q", ""),
                                        int(req.q("limit", "100") or 100))))
    r.add("POST", r"/api/clipboard/send", _c_err(
        lambda req: api.clipboard.send(req.json() or {})))
    r.add("POST", r"/api/clipboard/pin", _c_err(
        lambda req: api.clipboard.pin(
            str((req.json() or {}).get("id") or ""),
            bool((req.json() or {}).get("pinned")))))
    r.add("POST", r"/api/clipboard/clear", _c_err(
        lambda req: api.clipboard.clear(
            bool((req.json() or {}).get("pins_too")))))
    r.add("POST", r"/api/clipboard/incoming", _c_err(
        lambda req: api.clipboard.incoming(
            (req.json() or {}).get("entry") or {})), auth=False)
    r.add("POST", r"/api/clipboard/relay", _c_err(
        lambda req: api.clipboard.relay(req.json() or {})), auth=False)
    r.add("POST", r"/api/clipboard/os-copy", _c_err(
        lambda req: json_response(
            {"ok": api.clipboard.os_copy(str((req.json() or {})
                                             .get("text") or ""))})))
    r.add("POST", r"/api/clipboard/settings", _c_err(
        lambda req: api.clipboard_settings(req.json() or {})))

    # -- mesh (v1.4.0) -------------------------------------------------------
    r.add("GET", r"/api/mesh/status", lambda req: json_response(
        api.mesh.status()))
    r.add("POST", r"/api/mesh/claim", lambda req: _mesh_result(
        lambda: api.mesh.claim(str((req.json() or {}).get("client_id") or ""),
                               str((req.json() or {}).get("client_secret")
                                   or ""),
                               int((req.json() or {}).get("inbox_since")
                                   or 0))))
    r.add("GET", r"/api/mesh/ledger/([^/]+)", lambda req: json_response(
        api.mesh.ledger(req.params[""], int(req.q("since", "0") or 0))),
        auth=False)

    # -- intercom -----------------------------------------------------------
    r.add("GET", r"/api/intercom/sessions", lambda req: json_response(
        {"ok": True, "enabled": api.intercom.enabled(),
         "sessions": api.intercom.sessions()}))
    r.add("POST", r"/api/intercom/session", lambda req: api.icom_session(req))
    r.add("POST", r"/api/intercom/session/([^/]+)/end",
          lambda req: api.icom_session_end(req))
    r.add("POST", r"/api/intercom/talk", lambda req: api.icom_talk(req))
    r.add("POST", r"/api/intercom/relay", lambda req: api.icom_relay(req))
    r.add("POST", r"/api/intercom/stop", lambda req: api.icom_stop(req))

    # -- monitoring alerts (v1.3.0) --------------------------------------------
    r.add("GET", r"/api/alerts", lambda req: api.alerts_list(req))
    r.add("POST", r"/api/alerts", lambda req: api.alerts_set(req))
    r.add("DELETE", r"/api/alerts/([^/]+)", lambda req: api.alerts_delete(req))

    # -- automation: watched folders + schedules (v1.3.0) ----------------------
    r.add("GET", r"/api/automation", lambda req: api.automation_list(req))
    r.add("POST", r"/api/automation", lambda req: api.automation_set(req))
    r.add("POST", r"/api/automation/([^/]+)/control",
          lambda req: api.automation_control(req))
    r.add("DELETE", r"/automation/([^/]+)", lambda req: api.automation_delete(req))
    r.add("DELETE", r"/api/automation/([^/]+)", lambda req: api.automation_delete(req))
    r.add("POST", r"/api/automation/preview", lambda req: api.automation_preview(req))

    # -- storage view + media party (v1.3.0) -----------------------------------
    r.add("GET", r"/api/storage", lambda req: api.storage_view(req))
    r.add("POST", r"/api/media/party", lambda req: api.media_party(req))
    r.add("POST", r"/api/media/party/stop", lambda req: api.party_stop(req))

    # -- monitoring -----------------------------------------------------------
    r.add("GET", r"/api/monitor", lambda req: json_response(
        {"ok": True, **api.monitor.snapshot()}))
    r.add("GET", r"/api/monitor/history", lambda req: json_response(
        {"ok": True, "history": api.monitor.history()}))

    # -- events ---------------------------------------------------------------
    r.add("GET", r"/api/events", lambda req: api.events_query(req))
    r.add("GET", r"/api/events/export", lambda req: api.events_export(req))

    # -- diagnostics ------------------------------------------------------------
    r.add("GET", r"/api/diagnostics", lambda req: json_response(
        {"ok": True, "checks": api.diagnostics_run(quick=True)}))
    r.add("POST", r"/api/diagnostics/selftest", lambda req: json_response(
        {"ok": True, "checks": api.diagnostics_run(quick=False)}))
    r.add("GET", r"/api/diagnostics/report", lambda req: api.diagnostics_report(req))

    # -- static web UI ------------------------------------------------------------
    r.add("GET", r"/", api.serve_index, auth=False)
    r.add("GET", r"/m", api.serve_mobile, auth=False)          # mobile UI
    r.add("GET", r"/m/", api.serve_mobile, auth=False)
    r.add("GET", r"/offline.html", api.serve_static, auth=False)
    r.add("GET", r"/sw.js", api.serve_static, auth=False)
    r.add("GET", r"/manifest.webmanifest", api.serve_static, auth=False)
    r.add("GET", r"/(?!api/).*", api.serve_static, auth=False)

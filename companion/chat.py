"""GhostChat 2.0 backend: LAN messaging between computers AND mobile clients.

Message flow (no cloud, no relay server, no accounts):

Computers (v1.2.2 protocol preserved):

* UI  → local companion ``POST /api/chat/send``
* local companion → recipient companions ``POST /api/chat/incoming`` (HTTP)
* recipient companion → its UI clients over SSE (``event: chat``)
* recipient UI → ``POST /api/chat/read`` → ack back to sender

Mobile clients (v1.3.0):

* phone UI → its host companion ``POST /api/mobile/chat/send``
* host → target companion (the recipient phone's HOST) ``/api/chat/incoming``
* host stores the message in the recipient's **independent inbox**
  (``mobile_inbox_<client_id>.jsonl``) and pushes it over SSE
  (``event: mobile``)
* recipient phone acknowledges → host propagates the receipt back to the
  original sender's companion (``/api/chat/ack-delivery``)

A message to a phone is **delivered only after the phone itself
acknowledges** — host-companion receipt alone never upgrades the status.

Delivery states: ``sent`` → ``queued`` (recipient offline; persisted outbox
or inbox) → ``delivered`` → ``read``; ``failed`` when no route exists.

v1.3.0 additions: replies (``reply_to``), reactions, editing, deletion with
consistent tombstones, duplicate-message prevention (idempotent
``/api/chat/incoming``), offline queuing, reconnection synchronization for
phones (inbox fetch since last-seen), and a small chat-file store for
attachments captured on phones (photos, voice notes) that must not require
an authorized share.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
import uuid
from typing import Dict, List, Optional

from .errors import FsError
from .peers import PeerClient, PeerUnreachable
from .util import atomic_write_json, new_id, now_ms, read_json

STORE_CAP = 20000
OUTBOX_RETRY_S = 20
MUTATION_CAP = 20000
CHAT_FILE_CHUNK = 512 * 1024


def conversation_key(kind: str, peer_id: str) -> str:
    return f"{kind}:{peer_id}"


class ChatFileStore:
    """Chunked attachment blobs for chat (photos, voice notes, documents).

    Deliberately separate from the authorized-share upload sessions: a chat
    attachment must not require the operator to share a directory, and its
    total size is capped (``mobile_max_attachment_mb``).
    """

    def __init__(self, data_dir: str, config):
        self.dir = os.path.join(data_dir, "chat_files")
        os.makedirs(self.dir, exist_ok=True)
        self.config = config
        self.meta_path = os.path.join(data_dir, "chat_files.json")
        self._lock = threading.Lock()
        self._meta: Dict[str, dict] = read_json(self.meta_path, {}) or {}
        self._sessions: Dict[str, dict] = {}

    # -- metadata --------------------------------------------------------
    def _save_meta(self) -> None:
        atomic_write_json(self.meta_path, self._meta)

    def cap_bytes(self) -> int:
        try:
            mb = int(self.config.get("mobile_max_attachment_mb") or 50)
        except (TypeError, ValueError):
            mb = 50
        return max(1, mb) * 1024 * 1024

    # -- chunked upload ---------------------------------------------------
    def create_session(self, name: str, size: int, mime: str = "") -> dict:
        if not name or "/" in name or "\\" in name or name in (".", ".."):
            raise FsError(400, "bad_name", f"Illegal file name: {name!r}")
        cap = self.cap_bytes()
        if size and size > cap:
            raise FsError(413, "too_large",
                          f"Attachment larger than the {cap // (1024 * 1024)} MB limit")
        sid = uuid.uuid4().hex
        with self._lock:
            self._sessions[sid] = {
                "name": name, "size": int(size or 0), "mime": mime or "",
                "part": os.path.join(self.dir, f"cf-{sid}.part"),
                "received": 0, "created": now_ms(),
            }
        return {"sid": sid, "name": name, "max_bytes": cap}

    def append(self, sid: str, data_iter) -> dict:
        with self._lock:
            session = self._sessions.get(sid)
            cap = self.cap_bytes()
        if session is None:
            raise FsError(404, "no_session", "Unknown chat-file session")
        written = 0
        with open(session["part"], "ab") as fh:
            for chunk in data_iter:
                written += len(chunk)
                if session["received"] + written > cap:
                    raise FsError(413, "too_large",
                                  "Attachment exceeds the configured size limit")
                fh.write(chunk)
        with self._lock:
            session["received"] += written
            return {"sid": sid, "received": session["received"]}

    def finalize(self, sid: str, sha256: str = "", sender: str = "") -> dict:
        with self._lock:
            session = self._sessions.pop(sid, None)
        if session is None:
            raise FsError(404, "no_session", "Unknown chat-file session")
        received = os.path.getsize(session["part"]) \
            if os.path.exists(session["part"]) else 0
        if session["size"] and received != session["size"]:
            raise FsError(400, "size_mismatch",
                          f"Received {received} of {session['size']} bytes")
        fid = "cf_" + uuid.uuid4().hex[:16]
        target = os.path.join(self.dir, fid)
        os.replace(session["part"], target)
        digest = sha256
        if not digest:
            try:
                from .util import sha256_file
                digest = sha256_file(target)
            except OSError:
                digest = ""
        meta = {
            "id": fid, "name": session["name"], "size": received,
            "mime": session["mime"] or "application/octet-stream",
            "sha256": digest, "from": sender, "created": now_ms(),
        }
        with self._lock:
            self._meta[fid] = meta
            self._save_meta()
        return meta

    def cancel(self, sid: str) -> dict:
        with self._lock:
            session = self._sessions.pop(sid, None)
        if session:
            try:
                os.unlink(session["part"])
            except OSError:
                pass
        return {"cancelled": bool(session)}

    def get(self, file_id: str) -> Optional[dict]:
        meta = self._meta.get(file_id)
        if not meta:
            return None
        path = os.path.join(self.dir, file_id)
        if not os.path.isfile(path):
            return None
        return {**meta, "path": path}

    def open(self, file_id: str):
        meta = self.get(file_id)
        if not meta:
            raise FsError(404, "no_file", "Unknown chat attachment")
        fh = open(meta["path"], "rb")
        return fh, meta["size"], meta["name"], meta["mime"], meta["sha256"]

    def stats(self) -> dict:
        return {"files": len(self._meta),
                "bytes": sum(m.get("size") or 0 for m in self._meta.values())}


class ChatEngine:
    def __init__(self, data_dir: str, config, registry, events, sse,
                 identity_provider, log=None, mobile_registry=None):
        self.data_dir = data_dir
        self.config = config
        self.registry = registry
        self.events = events
        self.sse = sse
        self.identity = identity_provider
        self.log = log
        self.mobile = mobile_registry        # may be wired after construction
        self.store_path = os.path.join(data_dir, "messages.jsonl")
        self.outbox_path = os.path.join(data_dir, "outbox.json")
        self.mutations_path = os.path.join(data_dir, "mutations.jsonl")
        self.files = ChatFileStore(data_dir, config)
        self._lock = threading.RLock()
        self._messages: List[dict] = []          # oldest first
        self._outbox: List[dict] = []
        self._read_marks: Dict[str, int] = {}    # conv_key -> ts of last read
        self._seen_ids: Dict[str, bool] = {}     # duplicate prevention
        # origin metadata for messages relayed INTO hosted phones: needed to
        # route their delivery/read receipts back to the original senders
        # (the message itself lives only in the client's inbox file)
        self._relay_meta: Dict[str, dict] = {}   # msg_id → {origin, host}
        self._client = PeerClient(self._token_provider)
        self._stop = threading.Event()
        self._wake = threading.Event()      # fleet updates shorten the retry
        self._load()
        threading.Thread(target=self._outbox_loop, name="pn-chat-outbox",
                         daemon=True).start()

    def wire_mobile(self, mobile_registry) -> None:
        self.mobile = mobile_registry

    def on_fleet_update(self) -> None:
        """Discovery saw a peer with hosted clients / new routes — retry
        queued messages promptly instead of waiting the full 20 s."""
        self._wake.set()

    # ------------------------------------------------------------------
    def _token_provider(self) -> str:
        if self.config.get("require_token"):
            return self.config.get("auth_token", "")
        return ""

    def _load(self) -> None:
        import json as _json
        try:
            with open(self.store_path, "r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        msg = _json.loads(line)
                        self._messages.append(msg)
                        if msg.get("id"):
                            self._seen_ids[msg["id"]] = True
                    except ValueError:
                        continue
        except OSError:
            pass
        self._apply_mutations()
        from .util import read_json
        outbox = read_json(self.outbox_path, [])
        if isinstance(outbox, list):
            self._outbox = outbox

    def _apply_mutations(self) -> None:
        """Replay the mutation sidecar (reactions/edits/tombstones)."""
        try:
            with open(self.mutations_path, "r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        self._apply_local_mutation(json.loads(line))
                    except (ValueError, KeyError):
                        continue
        except OSError:
            pass

    def _persist_mutation(self, mut: dict) -> None:
        try:
            with open(self.mutations_path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(mut, ensure_ascii=False) + "\n")
            if os.path.getsize(self.mutations_path) > MUTATION_CAP * 300:
                self._rewrite_mutations()
        except OSError:
            pass

    def _rewrite_mutations(self) -> None:
        latest: Dict[str, dict] = {}
        order: List[str] = []
        try:
            with open(self.mutations_path, "r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        mut = json.loads(line)
                    except ValueError:
                        continue
                    key = (mut.get("msg_id") or "") + "|" + \
                          (mut.get("emoji") or "") if mut.get("type") == "react" \
                          else (mut.get("msg_id") or "") + "|" + mut.get("type", "")
                    if key not in latest:
                        order.append(key)
                    latest[key] = mut
            keep = [latest[k] for k in order[-MUTATION_CAP:]]
            tmp = self.mutations_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                for mut in keep:
                    fh.write(json.dumps(mut, ensure_ascii=False) + "\n")
            os.replace(tmp, self.mutations_path)
        except OSError:
            pass

    def _persist_message(self, msg: dict) -> None:
        import json as _json
        try:
            with open(self.store_path, "a", encoding="utf-8") as fh:
                fh.write(_json.dumps(msg, ensure_ascii=False) + "\n")
            if os.path.getsize(self.store_path) > STORE_CAP * 600:
                self._rewrite()
        except OSError:
            pass

    def _rewrite(self) -> None:
        import json as _json
        keep = self._messages[-STORE_CAP:]
        tmp = self.store_path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                for m in keep:
                    fh.write(_json.dumps(m, ensure_ascii=False) + "\n")
            os.replace(tmp, self.store_path)
        except OSError:
            pass

    def _persist_outbox(self) -> None:
        with self._lock:
            atomic_write_json(self.outbox_path, self._outbox)

    # ------------------------------------------------------------------
    # sending
    # ------------------------------------------------------------------
    def send(self, to_kind: str, to_id: str, body: str,
             attachment: Optional[dict] = None,
             from_ui: bool = True, sender: Optional[dict] = None,
             reply_to: str = "") -> dict:
        """Send a message.

        ``to_kind``: direct|group|broadcast|mobile|system.
        ``sender``: ``{"device_id","name","kind","host_device_id"}`` — the
        author.  Defaults to THIS companion (a desktop user); mobile hosts
        pass their client's logical identity.
        """
        if to_kind not in ("direct", "group", "broadcast", "mobile", "system"):
            raise FsError(400, "bad_kind", f"Unknown message kind {to_kind}")
        if to_kind == "system":
            body = body or ""
            if not body:
                raise FsError(400, "empty", "Message body is empty")
        elif not body and not attachment:
            raise FsError(400, "empty", "Message body is empty")
        me = self.identity()
        if sender:
            frm = dict(sender)
        else:
            frm = {"device_id": me["device_id"], "name": me["name"],
                   "kind": "computer"}
        msg = {
            "id": new_id("msg"), "ts": now_ms(),
            "from": frm,
            "to_kind": to_kind, "to_id": to_id,
            "body": body or "",
            "attachment": attachment,
            "status": "sent",
            "outgoing": True,
            "conv": conversation_key(to_kind, to_id or "all"),
        }
        if reply_to:
            msg["reply_to"] = reply_to
        if to_kind == "system":
            # local-only alert channel: no routing, no outbox
            msg["conv"] = "system:alerts"
            self._store_and_publish(msg)
            return msg
        self._store_and_publish(msg)
        targets = self._resolve_targets(to_kind, to_id)
        delivered_any = False
        for target in targets:
            ok = self._deliver(target, msg)
            delivered_any = delivered_any or ok
        if not targets and to_kind == "mobile":
            # the recipient phone's HOST is not known YET (fleet view still
            # converging) or the phone's host is offline — queue honestly and
            # let the outbox deliver when the route appears (beacons update
            # hosted-client lists every ~3 s)
            with self._lock:
                self._outbox.append({"msg": msg, "targets_left": [to_id],
                                     "queued": now_ms()})
            self._persist_outbox()
            msg["status"] = "queued"
            msg["queued_note"] = "waiting for the recipient device to come online"
            self._publish_update(msg)
        elif not targets:
            msg["status"] = "failed"
            msg["error"] = "No recipients online"
            self._publish_update(msg)
        elif not delivered_any:
            with self._lock:
                self._outbox.append({"msg": msg, "targets_left":
                                     [t["id"] for t in targets],
                                     "queued": now_ms()})
            self._persist_outbox()
            msg["status"] = "queued"
            self._publish_update(msg)
        if self.events:
            self.events.emit("message", self._event_text(msg, "sent"),
                             data={"msg_id": msg["id"], "kind": to_kind,
                                   "to": to_id})
        return msg

    def _event_text(self, msg: dict, direction: str) -> str:
        who = msg["from"]["name"]
        to_label = msg["to_id"] or "everyone"
        head = (msg["body"] or "")[:60]
        if msg.get("attachment"):
            head = f"[file: {msg['attachment'].get('name', '?')}]"
        return f"{direction.capitalize()}: {who} → {to_label}: {head}"

    # ------------------------------------------------------------------
    def _mobile_hosted_locally(self, client_id: str) -> Optional[dict]:
        if self.mobile is None:
            return None
        c = self.mobile.get(client_id)
        if c is None:
            return None
        me = self.identity()
        return {"id": client_id, "local": True,
                "host_device_id": me["device_id"],
                "client": c.to_public(me["device_id"], me["name"])}

    def _mobile_host_for(self, client_id: str) -> Optional[dict]:
        """Locate the companion hosting a mobile client (local or remote)."""
        local = self._mobile_hosted_locally(client_id)
        if local:
            return local
        me = self.identity()["device_id"]
        for peer in self.registry.all():
            if peer.device_id == me or peer.status != "online" \
                    or not peer.addresses:
                continue
            for mc in (getattr(peer, "mobile_clients", None) or []):
                if mc.get("client_id") == client_id:
                    return {"id": client_id, "local": False,
                            "host_device_id": peer.device_id,
                            "address": peer.addresses[0],
                            "port": peer.api_port}
        return None

    def _resolve_targets(self, to_kind: str, to_id: str) -> List[dict]:
        """[{id, kind, local?, address?, port?}] for message routing."""
        targets: List[dict] = []
        me = self.identity()["device_id"]
        if to_kind == "direct":
            peer = self.registry.get(to_id)
            if peer and peer.status == "online" and peer.addresses:
                targets.append({"id": peer.device_id, "kind": "computer",
                                "address": peer.addresses[0],
                                "port": peer.api_port})
        elif to_kind == "mobile":
            host = self._mobile_host_for(to_id)
            if host:
                targets.append({"id": to_id, "kind": "mobile", **host})
        elif to_kind == "group":
            group = self._find_group(to_id)
            if group:
                for member in group.get("members", []):
                    if member == me:
                        continue
                    if member.startswith("mob_"):
                        host = self._mobile_host_for(member)
                        if host:
                            targets.append({"id": member, "kind": "mobile",
                                            **host})
                        continue
                    peer = self.registry.get(member)
                    if peer and peer.status == "online" and peer.addresses:
                        targets.append({"id": member, "kind": "computer",
                                        "address": peer.addresses[0],
                                        "port": peer.api_port})
        else:  # broadcast → every participating computer + every mobile client
            for peer in self.registry.all():
                if peer.device_id != me and peer.status == "online" \
                        and peer.addresses and peer.participating:
                    targets.append({"id": peer.device_id, "kind": "computer",
                                    "address": peer.addresses[0],
                                    "port": peer.api_port})
            if self.mobile:
                for c in self.mobile.all():
                    host = self._mobile_host_for(c.client_id)
                    if host:
                        targets.append({"id": c.client_id, "kind": "mobile",
                                        **host})
        return targets

    def _find_group(self, group_id: str) -> Optional[dict]:
        for g in self.config.get("groups") or []:
            if g.get("id") == group_id:
                return g
        return None

    # ------------------------------------------------------------------
    def _deliver(self, target: dict, msg: dict) -> bool:
        """Deliver to one computer companion or one mobile client's host."""
        if target["kind"] == "mobile":
            if target.get("local"):
                # store into the hosted client's independent inbox — the
                # client receives a RECIPIENT copy: conversation keyed from
                # its view, outgoing=False (v1.3.1: the SSE push used to
                # carry the sender-side copy, so phones saw their own host's
                # messages as outgoing under the wrong conversation)
                recipient = dict(msg, conv=self._recipient_conv(msg),
                                 outgoing=False)
                self.mobile.inbox_append(target["id"], recipient)
                self._push_to_mobile(target["id"], "message", recipient)
                return True     # host-side accepted; client ack upgrades status
            try:
                self._client.request(
                    "POST",
                    f"http://{target['address']}:{target['port']}"
                    f"/api/chat/incoming",
                    body={"message": msg}, timeout=6)
                return True
            except (PeerUnreachable, Exception):
                return False
        try:
            self._client.request(
                "POST", f"http://{target['address']}:{target['port']}"
                        f"/api/chat/incoming",
                body={"message": msg}, timeout=6)
            if msg["to_kind"] == "direct":
                msg["status"] = "delivered"
            else:
                msg.setdefault("delivered_to", []).append(target["id"])
                msg["status"] = "delivered"
            self._publish_update(msg)
            return True
        except (PeerUnreachable, Exception):
            return False

    def _push_to_mobile(self, client_id: str, mtype: str, payload) -> None:
        if self.sse:
            self.sse.publish("mobile", {"type": mtype, "for": client_id,
                                        "data": payload})

    # ------------------------------------------------------------------
    # receiving
    # ------------------------------------------------------------------
    def receive_incoming(self, message: dict, sender_address: str) -> dict:
        """Store + fan out an incoming message; returns an ack payload."""
        msg_id = message.get("id") or ""
        if not message or not msg_id:
            raise FsError(400, "bad_request", "message required")
        with self._lock:
            duplicate = self._seen_ids.get(msg_id)
            if not duplicate:
                self._seen_ids[msg_id] = True
        if duplicate:
            return {"acked": True, "id": msg_id, "duplicate": True}
        message["outgoing"] = False
        kind = message.get("to_kind", "broadcast")
        if kind == "mobile":
            # addressed to a hosted phone: independent inbox + SSE push.
            # The inbox conversation is keyed from the RECIPIENT's view:
            # a direct line with the author (computer or phone), a group,
            # or the broadcast channel.
            client_id = message.get("to_id") or ""
            client = self.mobile.get(client_id) if self.mobile else None
            if client is None:
                return {"acked": False, "error": "no_such_mobile_client",
                        "id": msg_id}
            message["conv"] = self._recipient_conv(message)
            frm = (message.get("from") or {})
            self._relay_meta[msg_id] = {
                "origin": frm.get("device_id") or "",
                "host": frm.get("host_device_id") or "",
                "ts": now_ms(),
            }
            if len(self._relay_meta) > 5000:
                for k in list(self._relay_meta)[:1000]:
                    self._relay_meta.pop(k, None)
            self.mobile.inbox_append(client_id, message)
            self._push_to_mobile(client_id, "message", message)
            self._mirror_relay(message)
            return {"acked": True, "id": msg_id, "ts": now_ms(),
                    "client_notified": bool(client.online)}
        message["status"] = "delivered"
        # each device keys a direct conversation by ITS peer, so the local
        # history/`conversations` views line up on both sides
        if kind == "direct":
            key_peer = message["from"]["device_id"]
        elif kind == "group":
            key_peer = message.get("to_id") or ""
        else:
            key_peer = "all"
        message["conv"] = conversation_key(kind, key_peer)
        self._store_and_publish(message)
        self._mirror_relay(message)
        if self.events:
            self.events.emit("message", self._event_text(message, "received"),
                             data={"msg_id": message["id"],
                                   "from": message["from"]["device_id"]})
        return {"acked": True, "id": msg_id, "ts": now_ms()}

    def _recipient_conv(self, message: dict) -> str:
        """Conversation key as seen by a RECIPIENT (not the sender)."""
        kind = message.get("to_kind")
        frm = (message.get("from") or {}).get("device_id") or ""
        if kind == "group":
            return conversation_key("group", message.get("to_id") or "")
        if kind == "broadcast":
            return "broadcast:all"
        return conversation_key("direct", frm or "unknown")

    def _mirror_relay(self, message: dict) -> None:
        """Forward a copy to OTHER mobile clients that share this conversation
        (groups / broadcasts involving several phones hosted here)."""
        if self.mobile is None:
            return
        kind = message.get("to_kind")
        me = self.identity()["device_id"]
        author = message.get("from", {}).get("device_id")
        rekeyed = self._recipient_conv(message)
        recipient = dict(message, conv=rekeyed, outgoing=False)
        if kind == "group":
            group = self._find_group(message.get("to_id") or "")
            for member in (group or {}).get("members", []):
                if member in (author, me):
                    continue
                if member.startswith("mob_"):
                    host = self._mobile_host_for(member)
                    if host and host.get("local"):
                        self.mobile.inbox_append(member, recipient)
                        self._push_to_mobile(member, "message", recipient)
        elif kind == "broadcast":
            for c in self.mobile.all():
                if c.client_id in (author, me):
                    continue
                self.mobile.inbox_append(c.client_id, recipient)
                self._push_to_mobile(c.client_id, "message", recipient)

    # ------------------------------------------------------------------
    # receipts from hosted phones → back to the original senders
    # ------------------------------------------------------------------
    def mobile_ack(self, client_id: str, ids: List[str], state: str) -> dict:
        """Hosted phone acknowledged delivery/read of messages."""
        if state not in ("delivered", "read"):
            raise FsError(400, "bad_state", "state must be delivered|read")
        me = self.identity()
        if self.mobile:
            self.mobile.inbox_mark(client_id, ids, state)
        updated = []
        with self._lock:
            wanted = set(ids)
            for msg in self._messages:
                if msg.get("id") in wanted and msg.get("outgoing"):
                    if msg.get("status") != state:
                        msg["status"] = state
                        updated.append(msg)
        for msg in updated:
            self._publish_update(msg)
        # route receipts to remote originators: prefer the relay-meta map
        # (messages delivered into hosted phones never enter the local
        # store), then fall back to stored messages
        by_origin: Dict[str, List[str]] = {}
        for mid in ids:
            meta = self._relay_meta.get(mid)
            if meta:
                origin = meta["origin"]
                route_to = meta["host"] or (origin if not
                                            origin.startswith("mob_") else "")
                if route_to and route_to != me["device_id"]:
                    by_origin.setdefault(route_to, []).append(mid)
                continue
            for msg in self._messages:
                if msg.get("id") == mid and not msg.get("outgoing"):
                    origin = (msg.get("from") or {}).get("device_id") or ""
                    origin_host = (msg.get("from") or {}).get("host_device_id")
                    route_to = origin if not origin.startswith("mob_") \
                        else (origin_host or "")
                    if route_to and route_to != me["device_id"]:
                        by_origin.setdefault(route_to, []).append(mid)
                    break
        for device_id, msg_ids in by_origin.items():
            peer = self.registry.get(device_id)
            # attempt delivery whenever we hold addresses — the HTTP call
            # itself is the truth (the cached online flag can be stale on
            # quiet networks; a failed attempt is caught below)
            if peer and peer.addresses:
                try:
                    self._client.request(
                        "POST",
                        f"http://{peer.addresses[0]}:{peer.api_port}"
                        f"/api/chat/ack-delivery",
                        body={"reader": client_id, "reader_kind": "mobile",
                              "ids": msg_ids, "state": state}, timeout=6)
                except Exception:
                    pass
        # notify the local mobile sender (if hosted here) that its message
        # reached the recipient
        self._push_to_mobile(client_id, "receipt",
                             {"ids": ids, "state": state})
        return {"ok": True, "updated": len(updated)}

    def ack_delivery(self, reader: str, ids: List[str], state: str,
                     reader_kind: str = "computer") -> dict:
        """A remote recipient (computer UI or hosted phone's host) acks."""
        if state not in ("delivered", "read"):
            raise FsError(400, "bad_state", "state must be delivered|read")
        updated = []
        with self._lock:
            wanted = set(ids)
            for msg in self._messages:
                if msg.get("id") in wanted and msg.get("outgoing"):
                    rank = {"sent": 0, "queued": 0, "delivered": 1,
                            "read": 2}
                    if rank.get(state, 0) >= rank.get(msg.get("status"), 0):
                        msg["status"] = state
                        updated.append(msg)
        for msg in updated:
            self._publish_update(msg)
        return {"ok": True, "updated": len(updated)}

    def mark_read(self, conv_key: str, up_to_ts: int, notify_peer: bool = True) -> dict:
        """Mark a conversation read locally + inform the peer (read receipt)."""
        with self._lock:
            self._read_marks[conv_key] = max(self._read_marks.get(conv_key, 0),
                                             up_to_ts)
        if notify_peer and conv_key.startswith("mobile:"):
            # desktop user read a phone conversation → receipt to that client
            client_id = conv_key.split(":", 1)[1]
            ids = [m["id"] for m in self._messages
                   if m.get("conv") == conv_key and not m.get("outgoing")
                   and m.get("ts", 0) <= up_to_ts]
            if ids:
                if self.mobile:
                    self.mobile.inbox_mark(client_id, ids, "read")
                self._push_to_mobile(client_id, "receipt",
                                     {"ids": ids, "state": "read"})
            return {"conv": conv_key, "read_ts": up_to_ts}
        if notify_peer:
            # find the newest incoming direct message and ack its sender
            peer_id = conv_key.split(":", 1)[1] if ":" in conv_key else ""
            for msg in reversed(self._messages):
                if msg.get("conv") == conv_key and not msg.get("outgoing"):
                    sender = msg["from"]["device_id"]
                    sender_host = (msg.get("from") or {}).get("host_device_id")
                    if sender.startswith("mob_"):
                        sender = sender_host or ""
                    peer = self.registry.get(sender)
                    if peer and peer.addresses:
                        try:
                            self._client.request(
                                "POST",
                                f"http://{peer.addresses[0]}:{peer.api_port}"
                                f"/api/chat/ack-read",
                                body={"reader": self.identity()["device_id"],
                                      "peer_id": peer_id,
                                      "up_to_ts": up_to_ts}, timeout=6)
                        except Exception:
                            pass
                    break
        return {"conv": conv_key, "read_ts": up_to_ts}

    def ack_read(self, reader_id: str, up_to_ts: int) -> None:
        """Peer informs us our outgoing message was read."""
        changed = []
        with self._lock:
            for msg in reversed(self._messages):
                if msg.get("outgoing") and msg["ts"] <= up_to_ts \
                        and msg["to_kind"] in ("direct", "mobile"):
                    if msg["status"] != "read":
                        msg["status"] = "read"
                        changed.append(msg)
                elif msg["ts"] < up_to_ts - 6 * 3600 * 1000:
                    break
        for msg in changed:
            self._publish_update(msg)

    # ------------------------------------------------------------------
    # message mutations (v1.3.0): reactions / edits / tombstones
    # ------------------------------------------------------------------
    def _apply_local_mutation(self, mut: dict) -> bool:
        mtype = mut.get("type")
        msg_id = mut.get("msg_id") or ""
        changed = None
        with self._lock:
            for msg in self._messages:
                if msg.get("id") != msg_id:
                    continue
                if mtype == "react":
                    reactions = msg.setdefault("reactions", {})
                    emoji = str(mut.get("emoji") or "")[:8]
                    if not emoji:
                        return False
                    who = str(mut.get("who") or "")[:80]
                    lst = reactions.setdefault(emoji, [])
                    if mut.get("remove"):
                        if who in lst:
                            lst.remove(who)
                            changed = msg
                    else:
                        if who not in lst:
                            lst.append(who)
                            changed = msg
                elif mtype == "edit":
                    if msg.get("outgoing") or mut.get("allow_remote"):
                        msg["body"] = str(mut.get("body") or "")[:8000]
                        msg["edited"] = True
                        msg["edited_ts"] = mut.get("ts") or now_ms()
                        changed = msg
                elif mtype == "delete":
                    msg["deleted"] = True
                    msg["body"] = ""
                    msg["attachment"] = None
                    msg["edited"] = msg.get("edited", False)
                    changed = msg
                break
        if changed:
            self._persist_mutation(mut)
            self._publish_update(changed)
            # mirror into hosted mobile inboxes that hold this message
            if self.mobile:
                for c in self.mobile.all():
                    self.mobile.inbox_update_message(c.client_id, msg_id,
                                                     _inbox_patch(mut))
        return changed is not None

    def react(self, msg_id: str, emoji: str, who: Optional[dict] = None,
              propagate: bool = True) -> dict:
        me = self.identity()
        actor = (who or {}).get("device_id") or me["device_id"]
        mut = {"type": "react", "msg_id": msg_id, "emoji": emoji[:8],
               "who": actor, "ts": now_ms()}
        ok = self._apply_local_mutation(mut)
        if ok and propagate:
            self._propagate_mutation(mut, msg_id)
        return {"ok": ok, "msg_id": msg_id}

    def toggle_reaction(self, msg_id: str, emoji: str,
                        who: Optional[dict] = None) -> dict:
        """Add the actor's reaction, or remove it when already present."""
        me = self.identity()
        actor = (who or {}).get("device_id") or me["device_id"]
        with self._lock:
            target = next((m for m in self._messages
                           if m.get("id") == msg_id), None)
            present = False
            if target:
                reactions = target.get("reactions") or {}
                present = actor in reactions.get(emoji, [])
        mut = {"type": "react", "msg_id": msg_id, "emoji": emoji[:8],
               "who": actor, "remove": present, "ts": now_ms()}
        ok = self._apply_local_mutation(mut)
        if ok:
            self._propagate_mutation(mut, msg_id)
        return {"ok": ok, "removed": present}

    def edit_message(self, msg_id: str, body: str,
                     who: Optional[dict] = None) -> dict:
        me = self.identity()
        actor = (who or {}).get("device_id") or me["device_id"]
        with self._lock:
            target = next((m for m in self._messages
                           if m.get("id") == msg_id), None)
            if target is None:
                raise FsError(404, "no_message", "Unknown message")
            author = (target.get("from") or {}).get("device_id")
            author_host = (target.get("from") or {}).get("host_device_id")
            if author != actor and not (author and author.startswith("mob_")
                                        and author_host == me["device_id"]):
                raise FsError(403, "not_author",
                              "Only the author can edit a message")
        mut = {"type": "edit", "msg_id": msg_id, "body": body[:8000],
               "who": actor, "ts": now_ms(), "allow_remote": True}
        ok = self._apply_local_mutation(mut)
        if ok:
            self._propagate_mutation(mut, msg_id)
        return {"ok": ok}

    def delete_message(self, msg_id: str, who: Optional[dict] = None) -> dict:
        me = self.identity()
        actor = (who or {}).get("device_id") or me["device_id"]
        with self._lock:
            target = next((m for m in self._messages
                           if m.get("id") == msg_id), None)
            if target is None:
                raise FsError(404, "no_message", "Unknown message")
            author = (target.get("from") or {}).get("device_id")
            author_host = (target.get("from") or {}).get("host_device_id")
            if author != actor and not (author and author.startswith("mob_")
                                        and author_host == me["device_id"]):
                raise FsError(403, "not_author",
                              "Only the author can delete a message")
        mut = {"type": "delete", "msg_id": msg_id, "who": actor,
               "ts": now_ms()}
        ok = self._apply_local_mutation(mut)
        if ok:
            self._propagate_mutation(mut, msg_id)
        return {"ok": ok}

    def apply_remote_mutation(self, mut: dict) -> dict:
        """Another companion relays a mutation for a message we hold."""
        mut = dict(mut or {})
        mut["allow_remote"] = True
        ok = self._apply_local_mutation(mut)
        return {"ok": ok}

    def _propagate_mutation(self, mut: dict, msg_id: str) -> None:
        """Send a mutation to every companion that may hold a copy."""
        me = self.identity()["device_id"]
        with self._lock:
            target = next((m for m in self._messages
                           if m.get("id") == msg_id), None)
        involved: Dict[str, tuple] = {}
        if target:
            frm = (target.get("from") or {}).get("device_id")
            if frm and frm != me:
                peer = self.registry.get(frm)
                if peer and peer.status == "online" and peer.addresses:
                    involved[frm] = (peer.addresses[0], peer.api_port)
            if target.get("to_kind") == "direct":
                to = target.get("to_id")
                if to and to != me and to not in involved:
                    peer = self.registry.get(to)
                    if peer and peer.status == "online" and peer.addresses:
                        involved[to] = (peer.addresses[0], peer.api_port)
            elif target.get("to_kind") == "group":
                group = self._find_group(target.get("to_id") or "")
                for member in (group or {}).get("members", []):
                    if member in (me, frm) or member.startswith("mob_"):
                        continue
                    if member not in involved:
                        peer = self.registry.get(member)
                        if peer and peer.status == "online" and peer.addresses:
                            involved[member] = (peer.addresses[0],
                                                peer.api_port)
            elif target.get("to_kind") == "broadcast":
                for peer in self.registry.all():
                    if peer.device_id not in (me, frm) and peer.status == \
                            "online" and peer.addresses and peer.participating:
                        involved[peer.device_id] = (peer.addresses[0],
                                                    peer.api_port)
        for device_id, (address, port) in involved.items():
            try:
                self._client.request(
                    "POST", f"http://{address}:{port}/api/chat/mutate",
                    body={"mutation": mut}, timeout=5)
            except Exception:
                pass

    # ------------------------------------------------------------------
    # mobile inbox queries (host-side API for its phones)
    # ------------------------------------------------------------------
    def mobile_inbox(self, client_id: str, since_ts: int = 0,
                     limit: int = 200) -> List[dict]:
        if self.mobile is None:
            return []
        msgs = self.mobile.inbox_since(client_id, since_ts, limit)
        # mark delivered rows once actually fetched by the client UI
        ids = [m["id"] for m in msgs
               if m.get("status") in ("delivered_to_host", "queued")]
        if ids:
            self.mobile.inbox_mark(client_id, ids, "delivered")
            self.mobile_ack(client_id, ids, "delivered")
        return msgs

    def mobile_unread(self, client_id: str, read_marks: dict) -> dict:
        if self.mobile is None:
            return {}
        return self.mobile.unread_counts(client_id, read_marks or {})

    # ------------------------------------------------------------------
    def _store_and_publish(self, msg: dict) -> None:
        with self._lock:
            self._messages.append(msg)
            if msg.get("id"):
                self._seen_ids[msg["id"]] = True
        self._persist_message(msg)
        if self.sse:
            self.sse.publish("chat", msg)

    def _publish_update(self, msg: dict) -> None:
        if self.sse:
            self.sse.publish("chat", msg)

    # ------------------------------------------------------------------
    # queries
    # ------------------------------------------------------------------
    def conversations(self) -> List[dict]:
        """Conversation list with last message + unread counts."""
        by_conv: Dict[str, dict] = {}
        with self._lock:
            messages = list(self._messages)
        for msg in messages:
            if msg.get("deleted") and not msg.get("body") \
                    and not (msg.get("attachment") if not msg.get("deleted")
                             else None):
                pass
            conv = msg["conv"]
            entry = by_conv.setdefault(conv, {
                "conv": conv, "kind": msg["to_kind"],
                "peer_id": msg["to_id"] if msg["to_kind"] in
                ("direct", "mobile") else
                (msg["from"]["device_id"] if not msg.get("outgoing")
                 and msg["to_kind"] == "broadcast" else ""),
                "title": "", "last_ts": 0, "last_body": "",
                "unread": 0, "outgoing": True,
            })
            if msg["ts"] >= entry["last_ts"]:
                entry["last_ts"] = msg["ts"]
                if msg.get("deleted"):
                    entry["last_body"] = "message deleted"
                else:
                    entry["last_body"] = msg.get("body") or \
                        (f"[file: {msg['attachment']['name']}]"
                         if msg.get("attachment") else "")
            if not msg.get("outgoing"):
                entry["outgoing"] = False
                if msg["to_kind"] in ("direct", "mobile"):
                    entry["peer_id"] = msg["from"]["device_id"] \
                        if not msg["from"]["device_id"].startswith("mob_") \
                        else (msg["from"].get("host_device_id") or "")
                if msg["ts"] > self._read_marks.get(conv, 0):
                    entry["unread"] += 1
        # decorate titles from registry / groups / hosted mobile clients
        for conv, entry in by_conv.items():
            kind, key = conv.split(":", 1)
            if kind == "direct":
                peer = self.registry.get(key)
                entry["title"] = peer.display_name() if peer else key
                peer_online = peer.status == "online" if peer else False
                entry["online"] = peer_online
            elif kind == "mobile":
                client = self.mobile.get(key) if self.mobile else None
                entry["title"] = client.name or key
                entry["online"] = bool(client.online) if client else False
                entry["kind_of_peer"] = "mobile"
            elif kind == "group":
                group = self._find_group(key)
                entry["title"] = group["name"] if group else key
            elif kind == "system":
                entry["title"] = "System Alerts"
            else:
                entry["title"] = "Broadcast"
        return sorted(by_conv.values(), key=lambda e: -e["last_ts"])

    def history(self, conv: str = "", q: str = "", limit: int = 200,
                before_ts: int = 0) -> List[dict]:
        with self._lock:
            messages = list(self._messages)
        needle = q.lower().strip()
        out = []
        for msg in reversed(messages):
            if conv and msg["conv"] != conv:
                continue
            if before_ts and msg["ts"] >= before_ts:
                continue
            if needle:
                hay = (msg.get("body", "") + " " +
                       (msg.get("attachment") or {}).get("name", "")).lower()
                if needle not in hay and needle not in msg["from"]["name"].lower():
                    continue
            out.append(msg)
            if len(out) >= limit:
                break
        return out

    # ------------------------------------------------------------------
    # groups management
    # ------------------------------------------------------------------
    def create_group(self, name: str, members: List[str]) -> dict:
        group = {"id": f"grp_{uuid.uuid4().hex[:10]}", "name": name,
                 "members": members}
        groups = self.config.get("groups") or []
        groups.append(group)
        self.config.set("groups", groups)
        return group

    def delete_group(self, group_id: str) -> None:
        groups = [g for g in (self.config.get("groups") or [])
                  if g.get("id") != group_id]
        self.config.set("groups", groups)

    def groups(self) -> List[dict]:
        return self.config.get("groups") or []

    # ------------------------------------------------------------------
    # outbox retry
    # ------------------------------------------------------------------
    def _outbox_loop(self) -> None:
        while not self._stop.is_set():
            self._wake.wait(OUTBOX_RETRY_S)
            self._wake.clear()
            if self._stop.is_set():
                return
            if not self._outbox:
                continue
            remaining = []
            changed = False
            for item in self._outbox:
                msg = item["msg"]
                targets_left = []
                any_delivered = False
                for device_id in item.get("targets_left", []):
                    if device_id.startswith("mob_"):
                        host = self._mobile_host_for(device_id)
                        if host:
                            ok = self._deliver(host, msg)
                            any_delivered = any_delivered or ok
                            if ok:
                                changed = True
                                continue
                        targets_left.append(device_id)
                        continue
                    peer = self.registry.get(device_id)
                    if peer and peer.status == "online" and peer.addresses:
                        target = {"id": device_id, "kind": "computer",
                                  "address": peer.addresses[0],
                                  "port": peer.api_port}
                        if self._deliver(target, msg):
                            any_delivered = True
                            changed = True
                            continue
                    targets_left.append(device_id)
                if targets_left:
                    item["targets_left"] = targets_left
                    remaining.append(item)
                else:
                    if msg["to_kind"] != "direct" and any_delivered:
                        msg["status"] = "delivered"
                        self._publish_update(msg)
                    changed = True
            if changed:
                with self._lock:
                    self._outbox = remaining
                self._persist_outbox()

    def stats(self) -> dict:
        with self._lock:
            total = len(self._messages)
            outbox = len(self._outbox)
        return {"messages_stored": total, "outbox_pending": outbox,
                "groups": len(self.groups()), **self.files.stats()}


def _inbox_patch(mut: dict) -> dict:
    """Convert a mutation into a patch for stored inbox rows."""
    if mut.get("type") == "react":
        return {}        # reactions live inside the message object; rows are
                         # patched wholesale for edits/deletes only
    if mut.get("type") == "edit":
        return {"body": mut.get("body", ""), "edited": True,
                "edited_ts": mut.get("ts") or now_ms()}
    if mut.get("type") == "delete":
        return {"deleted": True, "body": "", "attachment": None}
    return {}

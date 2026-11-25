"""GhostChat backend: LAN-only messaging between participating companions.

Message flow (no cloud, no relay server, no accounts):

* UI  → local companion ``POST /api/chat/send``
* local companion → recipient companions ``POST /api/chat/incoming`` (HTTP)
* recipient companion → its UI clients over SSE (``event: chat``)
* recipient UI → ``POST /api/chat/read`` → ack back to sender

Delivery states: ``sent`` → ``delivered`` (recipient companion acked) →
``read`` (recipient UI opened the conversation).  If a recipient is offline
the message waits in an on-disk outbox and is re-delivered automatically
when the peer comes back (discovery notices the beacon).

Groups are simple member lists defined in config.  Broadcasts go to every
online participating companion.  Attachments reference a pull job created
by the sender's companion through the standard transfer engine.
"""

from __future__ import annotations

import os
import threading
import time
from typing import Dict, List, Optional

from .errors import FsError
from .peers import PeerClient, PeerUnreachable
from .util import atomic_write_json, new_id, now_ms

STORE_CAP = 20000
OUTBOX_RETRY_S = 20


def conversation_key(kind: str, peer_id: str) -> str:
    return f"{kind}:{peer_id}"


class ChatEngine:
    def __init__(self, data_dir: str, config, registry, events, sse,
                 identity_provider, log=None):
        self.data_dir = data_dir
        self.config = config
        self.registry = registry
        self.events = events
        self.sse = sse
        self.identity = identity_provider
        self.log = log
        self.store_path = os.path.join(data_dir, "messages.jsonl")
        self.outbox_path = os.path.join(data_dir, "outbox.json")
        self._lock = threading.RLock()
        self._messages: List[dict] = []          # oldest first
        self._outbox: List[dict] = []
        self._read_marks: Dict[str, int] = {}    # conv_key -> ts of last read
        self._client = PeerClient(self._token_provider)
        self._stop = threading.Event()
        self._load()
        threading.Thread(target=self._outbox_loop, name="pn-chat-outbox",
                         daemon=True).start()

    # ------------------------------------------------------------------
    def _token_provider(self) -> str:
        if self.config.get("require_token"):
            return self.config.get("auth_token", "")
        return ""

    def _load(self) -> None:
        try:
            with open(self.store_path, "r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        self._messages.append(
                            __import__("json").loads(line))
                    except ValueError:
                        continue
        except OSError:
            pass
        from .util import read_json
        outbox = read_json(self.outbox_path, [])
        if isinstance(outbox, list):
            self._outbox = outbox

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
             from_ui: bool = True) -> dict:
        """Send a message.  ``to_kind``: direct|group|broadcast."""
        if to_kind not in ("direct", "group", "broadcast"):
            raise FsError(400, "bad_kind", f"Unknown message kind {to_kind}")
        if not body and not attachment:
            raise FsError(400, "empty", "Message body is empty")
        me = self.identity()
        msg = {
            "id": new_id("msg"), "ts": now_ms(),
            "from": {"device_id": me["device_id"], "name": me["name"]},
            "to_kind": to_kind, "to_id": to_id,
            "body": body or "",
            "attachment": attachment,
            "status": "sent",
            "outgoing": True,
            "conv": conversation_key(to_kind, to_id or "all"),
        }
        self._store_and_publish(msg)
        targets = self._resolve_targets(to_kind, to_id)
        delivered_any = False
        for device_id, address, port in targets:
            ok = self._deliver(device_id, address, port, msg)
            delivered_any = delivered_any or ok
        if not targets:
            msg["status"] = "failed"
            msg["error"] = "No recipients online"
            self._publish_update(msg)
        elif not delivered_any:
            with self._lock:
                self._outbox.append({"msg": msg, "targets_left":
                                     [t[0] for t in targets], "queued": now_ms()})
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

    def _resolve_targets(self, to_kind: str, to_id: str) -> List[tuple]:
        """[(device_id, address, api_port)] for message routing."""
        targets = []
        me = self.identity()["device_id"]
        if to_kind == "direct":
            peer = self.registry.get(to_id)
            if peer and peer.status == "online" and peer.addresses:
                targets.append((peer.device_id, peer.addresses[0], peer.api_port))
        elif to_kind == "group":
            group = self._find_group(to_id)
            if group:
                for member in group.get("members", []):
                    if member == me:
                        continue
                    peer = self.registry.get(member)
                    if peer and peer.status == "online" and peer.addresses:
                        targets.append((peer.device_id, peer.addresses[0],
                                        peer.api_port))
        else:  # broadcast
            for peer in self.registry.all():
                if peer.device_id != me and peer.status == "online" \
                        and peer.addresses and peer.participating:
                    targets.append((peer.device_id, peer.addresses[0],
                                    peer.api_port))
        return targets

    def _find_group(self, group_id: str) -> Optional[dict]:
        for g in self.config.get("groups") or []:
            if g.get("id") == group_id:
                return g
        return None

    def _deliver(self, device_id: str, address: str, port: int,
                 msg: dict) -> bool:
        try:
            self._client.request(
                "POST", f"http://{address}:{port}/api/chat/incoming",
                body={"message": msg}, timeout=6)
            if msg["to_kind"] == "direct":
                msg["status"] = "delivered"
            else:
                msg.setdefault("delivered_to", []).append(device_id)
                msg["status"] = "delivered"
            self._publish_update(msg)
            return True
        except (PeerUnreachable, Exception):
            return False

    # ------------------------------------------------------------------
    # receiving
    # ------------------------------------------------------------------
    def receive_incoming(self, message: dict, sender_address: str) -> dict:
        """Store + fan out an incoming message; returns an ack payload."""
        message["outgoing"] = False
        message["status"] = "delivered"
        kind = message.get("to_kind", "broadcast")
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
        if self.events:
            self.events.emit("message", self._event_text(message, "received"),
                             data={"msg_id": message["id"],
                                   "from": message["from"]["device_id"]})
        return {"acked": True, "id": message["id"], "ts": now_ms()}

    def mark_read(self, conv_key: str, up_to_ts: int, notify_peer: bool = True) -> dict:
        """Mark a conversation read locally + inform the peer (read receipt)."""
        with self._lock:
            self._read_marks[conv_key] = max(self._read_marks.get(conv_key, 0),
                                             up_to_ts)
        if notify_peer:
            # find the newest incoming direct message and ack its sender
            peer_id = conv_key.split(":", 1)[1] if ":" in conv_key else ""
            for msg in reversed(self._messages):
                if msg.get("conv") == conv_key and not msg.get("outgoing"):
                    sender = msg["from"]["device_id"]
                    peer = self.registry.get(sender)
                    if peer and peer.status == "online" and peer.addresses:
                        try:
                            self._client.request(
                                "POST",
                                f"http://{peer.addresses[0]}:{peer.api_port}/api/chat/ack-read",
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
                        and msg["to_kind"] == "direct":
                    if msg["status"] != "read":
                        msg["status"] = "read"
                        changed.append(msg)
                elif msg["ts"] < up_to_ts - 6 * 3600 * 1000:
                    break
        for msg in changed:
            self._publish_update(msg)

    # ------------------------------------------------------------------
    def _store_and_publish(self, msg: dict) -> None:
        with self._lock:
            self._messages.append(msg)
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
            conv = msg["conv"]
            entry = by_conv.setdefault(conv, {
                "conv": conv, "kind": msg["to_kind"],
                "peer_id": msg["to_id"] if msg["to_kind"] == "direct"
                else (msg["from"]["device_id"] if not msg.get("outgoing")
                      and msg["to_kind"] == "broadcast" else ""),
                "title": "", "last_ts": 0, "last_body": "",
                "unread": 0, "outgoing": True,
            })
            if msg["ts"] >= entry["last_ts"]:
                entry["last_ts"] = msg["ts"]
                entry["last_body"] = msg.get("body") or \
                    (f"[file: {msg['attachment']['name']}]"
                     if msg.get("attachment") else "")
            if not msg.get("outgoing"):
                entry["outgoing"] = False
                if msg["to_kind"] == "direct":
                    entry["peer_id"] = msg["from"]["device_id"]
                if msg["ts"] > self._read_marks.get(conv, 0):
                    entry["unread"] += 1
        # decorate titles from registry / groups
        for conv, entry in by_conv.items():
            kind, key = conv.split(":", 1)
            if kind == "direct":
                peer = self.registry.get(key)
                entry["title"] = peer.display_name() if peer else key
                peer_online = peer.status == "online" if peer else False
                entry["online"] = peer_online
            elif kind == "group":
                group = self._find_group(key)
                entry["title"] = group["name"] if group else key
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
        import uuid as _uuid
        group = {"id": f"grp_{_uuid.uuid4().hex[:10]}", "name": name,
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
            self._stop.wait(OUTBOX_RETRY_S)
            if self._stop.is_set():
                return
            if not self._outbox:
                continue
            remaining = []
            changed = False
            for item in self._outbox:
                msg = item["msg"]
                targets = []
                for device_id in item.get("targets_left", []):
                    peer = self.registry.get(device_id)
                    if peer and peer.status == "online" and peer.addresses:
                        if self._deliver(device_id, peer.addresses[0],
                                         peer.api_port, msg):
                            changed = True
                            continue
                    targets.append(device_id)
                if targets:
                    item["targets_left"] = targets
                    remaining.append(item)
                else:
                    msg["status"] = "delivered" if msg["to_kind"] != "direct" \
                        else msg["status"]
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
                "groups": len(self.groups())}

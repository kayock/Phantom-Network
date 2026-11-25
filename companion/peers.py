"""Peer registry + lightweight HTTP client for companion-to-companion calls.

The :class:`PeerRegistry` persists known devices in ``known_peers.json``
(name, addresses, custom label/icon/group, first/last seen) so the UI can
distinguish *known* companions from *newly discovered* ones across restarts.

The :class:`PeerClient` is the single place that knows how to talk HTTP to
another companion (auth header, timeouts, URL building).  All higher-level
modules (transfers, chat, sync, radar) go through it.
"""

from __future__ import annotations

import ipaddress
import json
import socket
import threading
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

from .util import JsonStore, now_ms

ONLINE_TTL_MS = 12_000          # beacon interval 3 s × 4 missed
REPROBE_MS = 30_000             # manual peers re-probe interval


class Peer:
    __slots__ = ("device_id", "name", "custom_name", "icon", "group",
                 "addresses", "api_port", "version", "os", "os_full", "host",
                 "first_seen", "last_seen", "manual", "participating",
                 "status", "latency_ms", "uptime_s",
                 "audio_enabled", "intercom_enabled", "shares_count")

    def __init__(self, device_id: str):
        self.device_id = device_id
        self.name = ""
        self.custom_name = ""
        self.icon = ""
        self.group = ""
        self.addresses: List[str] = []
        self.api_port = 8415
        self.version = ""
        self.os = ""
        self.os_full = ""
        self.host = ""
        self.first_seen = now_ms()
        self.last_seen = 0
        self.manual = False
        self.participating = True     # True = runs a Phantom companion
        self.status = "offline"
        self.latency_ms = None
        self.uptime_s = None
        self.audio_enabled = None       # None = unknown (v1.0.0 peer)
        self.intercom_enabled = None
        self.shares_count = None

    def display_name(self) -> str:
        return self.custom_name or self.name or self.host or self.device_id[:12]

    def to_dict(self) -> dict:
        return {
            "device_id": self.device_id, "name": self.name,
            "custom_name": self.custom_name, "icon": self.icon,
            "group": self.group, "addresses": list(self.addresses),
            "api_port": self.api_port, "version": self.version,
            "os": self.os, "os_full": self.os_full, "host": self.host,
            "first_seen": self.first_seen, "last_seen": self.last_seen,
            "manual": self.manual, "participating": self.participating,
            "status": self.status, "latency_ms": self.latency_ms,
            "uptime_s": self.uptime_s, "display_name": self.display_name(),
            "audio_enabled": self.audio_enabled,
            "intercom_enabled": self.intercom_enabled,
            "shares_count": self.shares_count,
        }

    def to_public(self) -> dict:
        """Public view for the UI (excludes internal fields)."""
        return self.to_dict()


class PeerRegistry:
    """Thread-safe registry of every device Phantom Network knows about."""

    def __init__(self, data_dir: str, events=None):
        self._store = JsonStore(data_dir + "/known_peers.json", {"peers": {}})
        self._lock = threading.RLock()
        self._peers: Dict[str, Peer] = {}
        self._events = events
        self._load()

    # ------------------------------------------------------------------
    def _load(self) -> None:
        data = self._store.get() or {"peers": {}}
        for device_id, d in data.get("peers", {}).items():
            p = Peer(device_id)
            for slot in p.__slots__:
                if slot in d:
                    setattr(p, slot, d[slot])
            p.status = "offline"          # always re-probe after restart
            p.latency_ms = None
            self._peers[device_id] = p

    def _save(self) -> None:
        with self._lock:
            data = {"peers": {pid: p.to_dict() for pid, p in self._peers.items()}}
        self._store.set(data)

    # ------------------------------------------------------------------
    def get(self, device_id: str) -> Optional[Peer]:
        with self._lock:
            return self._peers.get(device_id)

    def get_or_create(self, device_id: str) -> Peer:
        with self._lock:
            if device_id not in self._peers:
                self._peers[device_id] = Peer(device_id)
            return self._peers[device_id]

    def all(self) -> List[Peer]:
        with self._lock:
            return list(self._peers.values())

    def find_by_address(self, address: str) -> Optional[Peer]:
        with self._lock:
            for p in self._peers.values():
                if address in p.addresses:
                    return p
        return None

    # ------------------------------------------------------------------
    def observe_beacon(self, identity: dict, address: str,
                       participating: bool = True) -> Tuple[Peer, str]:
        """Update registry from a beacon/identity payload.

        Returns ``(peer, transition)`` where transition is one of
        ``"new"``, ``"reconnected"``, ``"refresh"``.
        """
        device_id = identity.get("device_id") or f"anon-{address}"
        with self._lock:
            existing = self._peers.get(device_id)
            was_online = existing is not None and existing.status == "online"
            existed = existing is not None
            p = self.get_or_create(device_id)
            p.name = identity.get("name") or p.name
            p.host = identity.get("host") or p.host
            p.version = identity.get("version") or p.version
            p.os = identity.get("os") or p.os
            p.os_full = identity.get("os_full") or p.os_full
            p.api_port = int(identity.get("api_port") or p.api_port or 8415)
            p.participating = participating
            # capability flags (v1.1.0 identity payload; None for older peers)
            p.audio_enabled = identity.get("audio_enabled", p.audio_enabled)
            p.intercom_enabled = identity.get("intercom_enabled", p.intercom_enabled)
            p.shares_count = identity.get("shares_count", p.shares_count)
            if address not in p.addresses:
                p.addresses = ([address] + p.addresses)[:8]
            p.last_seen = now_ms()
            p.status = "online"
            if participating and not was_online:
                transition = "reconnected" if existed else "new"
            else:
                transition = "refresh"
        self._save()
        return p, transition

    # ------------------------------------------------------------------
    def mark_offline(self, device_id: str) -> Optional[Peer]:
        with self._lock:
            p = self._peers.get(device_id)
            if p and p.status != "offline":
                p.status = "offline"
                p.latency_ms = None
                self._save()
                return p
        return None

    def sweep_offline(self, ttl_ms: int = ONLINE_TTL_MS) -> List[Peer]:
        """Return peers whose last beacon is older than the TTL; mark offline."""
        cutoff = now_ms() - ttl_ms
        lost = []
        with self._lock:
            for p in self._peers.values():
                if p.status == "online" and p.last_seen < cutoff:
                    p.status = "offline"
                    p.latency_ms = None
                    lost.append(p)
        if lost:
            self._save()
        return lost

    # ------------------------------------------------------------------
    def set_metadata(self, device_id: str, patch: dict) -> Optional[Peer]:
        with self._lock:
            p = self._peers.get(device_id)
            if not p:
                return None
            for key in ("custom_name", "icon", "group"):
                if key in patch:
                    setattr(p, key, patch[key])
        self._save()
        return p

    def set_latency(self, device_id: str, latency_ms: Optional[float],
                    uptime_s: Optional[float] = None) -> None:
        with self._lock:
            p = self._peers.get(device_id)
            if p:
                p.latency_ms = latency_ms
                if uptime_s is not None:
                    p.uptime_s = uptime_s

    def public_list(self) -> List[dict]:
        with self._lock:
            peers = [p.to_public() for p in self._peers.values()]
        peers.sort(key=lambda d: ((0 if d["participating"] else 1),
                                  -(d["last_seen"] or 0)))
        return peers


class PeerClient:
    """Minimal HTTP client for talking to companion instances on the LAN."""

    def __init__(self, token_provider=lambda: ""):
        self._token_provider = token_provider

    # ------------------------------------------------------------------
    def request(self, method: str, url: str, body: Any = None,
                timeout: float = 10.0, raw: bool = False,
                headers: Optional[Dict[str, str]] = None) -> Any:
        """Perform a request; returns parsed JSON or raw bytes when ``raw``.

        Raises :class:`PeerUnreachable` on connection errors and
        :class:`PeerHttpError` for non-2xx replies.
        """
        token = self._token_provider() or ""
        req_headers = {"Accept": "application/json"}
        if token:
            req_headers["X-PN-Token"] = token
        if headers:
            req_headers.update(headers)
        data = None
        if body is not None:
            if isinstance(body, (dict, list)):
                data = json.dumps(body).encode("utf-8")
                req_headers.setdefault("Content-Type", "application/json")
            else:
                data = body
        req = urllib.request.Request(url, data=data, headers=req_headers,
                                     method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                payload = resp.read()
                if raw:
                    return payload
                ctype = resp.headers.get("Content-Type", "")
                if "json" in ctype:
                    return json.loads(payload.decode("utf-8"))
                return payload
        except urllib.error.HTTPError as exc:
            try:
                detail = json.loads(exc.read().decode("utf-8"))
                message = detail.get("message", str(exc))
            except Exception:
                message = str(exc)
            raise PeerHttpError(exc.code, message) from None
        except (urllib.error.URLError, socket.timeout, OSError) as exc:
            raise PeerUnreachable(str(exc)) from None

    # convenience wrappers ------------------------------------------------
    def identity(self, base_url: str, timeout: float = 3.0) -> dict:
        return self.request("GET", base_url + "/api/identity", timeout=timeout)

    def health(self, base_url: str, timeout: float = 3.0) -> dict:
        return self.request("GET", base_url + "/api/health", timeout=timeout)


class PeerUnreachable(Exception):
    pass


class PeerHttpError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(f"HTTP {status}: {message}")
        self.status, self.message = status, message


# ----------------------------------------------------------------------
# subnet helpers (used by the radar's active sweep)
# ----------------------------------------------------------------------

def local_networks() -> List[dict]:
    """Enumerate local IPv4 networks as CIDR dicts for subnet sweeps."""
    nets = []
    try:
        hostnames = socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)
        for info in hostnames:
            addr = info[4][0]
            if addr.startswith("127."):
                continue
            try:
                iface = ipaddress.ip_interface(f"{addr}/24")
                nets.append({"addr": addr, "network": str(iface.network),
                             "broadcast": str(iface.network.broadcast_address)})
            except ValueError:
                continue
    except OSError:
        pass
    if not nets:
        for addr in ("10.255.255.255",):
            pass
    # dedupe by network
    uniq = {}
    for n in nets:
        uniq[n["network"]] = n
    return list(uniq.values())

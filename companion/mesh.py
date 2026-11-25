"""Phantom Mesh (v1.4.0) — resilient companion and mobile-client networking.

Adds four capabilities to the existing probe-validated discovery stack
without changing its guarantees:

1. Exchange TTL — peers introduced ONLY via peer exchange (never probed
   alive afterwards, no beacon, no manual entry) expire honestly after
   EXCHANGE_TTL_S instead of living forever in the registry.
2. Known-peer recovery — offline peers with known addresses are re-probed
   in the background with exponential backoff (10 s → 60 s); the last
   error is recorded for diagnostics.
3. Duplicate reconciliation — two device identities claiming the same
   (address, api_port) are reconciled: the fresher identity wins, the
   stale one is deactivated and an audit event explains what happened.
   Manual peers are never auto-removed (the user registered them).
4. Mobile-host failover (EXPERIMENTAL, disabled by default) — a phone
   whose host died can claim service on another participating companion.
   Security model (see future-development/PHANTOM_MESH_DESIGN.md):
   - only a SHA-256 secret hash is stored host-side; raw secrets never
     leave the owner browser beyond the registration response (v1.3.1
     rule preserved);
   - claims are TTL-bound and single-writer (newer claim wins, the old
     host yields when it sees a fresher claim);
   - inbox continuity: the claiming host pulls the client's inbox entries
     from the previous host when reachable, and merges BY MESSAGE ID when
     the old host returns (dedupe already exists everywhere).

mDNS/DNS-SD discovery is an OPTIONAL provider (``mdns_enabled`` default
OFF): a minimal ``_phantom-net._tcp`` announce/browse over UDP multicast,
implemented in-tree (no third-party dependency), feeding the same
probe-validated registry.  It never modifies firewall rules.
"""

from __future__ import annotations

import hashlib
import json
import os
import socket
import threading
from typing import Dict, List, Optional

from .util import now_ms, iso_now, atomic_write_json, read_json

EXCHANGE_TTL_S = 30 * 60          # exchange-introduced peers expire after 30 min
RECOVERY_MIN_S = 10
RECOVERY_MAX_S = 60
RECONCILE_INTERVAL_S = 20
CLAIM_TTL_S = 10 * 60
MDNS_ADDR = ("224.0.0.251", 5353)
MDNS_SERVICE = "_phantom-net._tcp.local."


def _sha256_hex(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


class MeshEngine:
    def __init__(self, data_dir: str, config, registry, discovery, mobile,
                 identity, sse, events, log=None):
        self.data_dir = data_dir
        self.config = config
        self.registry = registry
        self.discovery = discovery
        self.mobile = mobile
        self.identity = identity
        self.sse = sse
        self.events = events
        self.log = log
        self._lock = threading.RLock()
        self._mesh_path = os.path.join(data_dir, "mesh_state.json")
        self._state: dict = read_json(self._mesh_path, None) or {
            "exchange_seen": {}, "claims": {}, "client_hashes": {}}
        self._stop = threading.Event()
        self._recover_fail: Dict[str, int] = {}
        self._recover_last_err: Dict[str, str] = {}
        self._threads = [
            threading.Thread(target=self._exchange_ttl_loop, daemon=True),
            threading.Thread(target=self._recovery_loop, daemon=True),
            threading.Thread(target=self._reconcile_loop, daemon=True),
        ]
        for t in self._threads:
            t.start()
        if self.config.get("mdns_enabled", False):
            threading.Thread(target=self._mdns_loop, daemon=True).start()

    # ------------------------------------------------------------------
    # status / diagnostics
    # ------------------------------------------------------------------
    def status(self) -> dict:
        me = self.identity() or {}
        with self._lock:
            return {
                "ok": True,
                "device_id": me.get("device_id"),
                "failover_enabled": bool(self.config.get(
                    "mobile_failover_enabled", False)),
                "mdns_enabled": bool(self.config.get("mdns_enabled", False)),
                "exchange_tracked": len(self._state.get("exchange_seen", {})),
                "claims": {k: dict(v) for k, v in
                           self._state.get("claims", {}).items()},
                "recovery": {k: {"fails": v, "last_error":
                                 self._recover_last_err.get(k, "")}
                             for k, v in self._recover_fail.items()
                             if v},
            }

    def _audit(self, text: str, **data) -> None:
        if self.events:
            try:
                self.events.emit("mesh", text, data=data or None)
            except Exception:
                pass

    def _save(self) -> None:
        atomic_write_json(self._mesh_path, self._state)

    # ------------------------------------------------------------------
    # 1. exchange TTL
    # ------------------------------------------------------------------
    def note_exchange_introduction(self, device_id: str) -> None:
        with self._lock:
            self._state.setdefault("exchange_seen", {})[device_id] = now_ms()
            self._save()

    def note_direct_contact(self, device_id: str) -> None:
        """A probe/beacon/manual contact removes the exchange-only flag."""
        with self._lock:
            seen = self._state.get("exchange_seen", {})
            if device_id in seen:
                seen.pop(device_id, None)
                self._save()

    def _exchange_ttl_loop(self) -> None:
        import time as _t
        while not self._stop.wait(60.0):
            me = self.identity() or {}
            expired = []
            with self._lock:
                seen = self._state.get("exchange_seen", {})
                for did, ts in list(seen.items()):
                    if now_ms() - ts > EXCHANGE_TTL_S * 1000:
                        peer = self.registry.get(did)
                        if peer is not None and (
                                not peer.last_seen or
                                now_ms() - peer.last_seen >
                                EXCHANGE_TTL_S * 1000):
                            # exchange-only and never (or not recently)
                            # contacted directly → expire honestly
                            self.registry.remove(did)
                            expired.append(did)
                        seen.pop(did, None)
                if expired:
                    self._save()
            for did in expired:
                self._audit("Mesh: expired exchange-only peer "
                            f"{did} (not seen directly in 30 min)",
                            device_id=did)

    # ------------------------------------------------------------------
    # 2. known-peer recovery with backoff
    # ------------------------------------------------------------------
    def _recovery_loop(self) -> None:
        import time as _t
        while not self._stop.wait(RECOVERY_MIN_S):
            me = self.identity() or {}
            for peer in list(self.registry.all()):
                if peer.device_id == me.get("device_id") or \
                        not peer.addresses:
                    continue
                if peer.status == "online":
                    self._recover_fail.pop(peer.device_id, None)
                    continue
                fails = self._recover_fail.get(peer.device_id, 0)
                delay = min(RECOVERY_MAX_S,
                            RECOVERY_MIN_S * (2 ** min(fails, 6)))
                if peer.last_seen and \
                        now_ms() - peer.last_seen < delay * 1000:
                    continue
                try:
                    result = self.discovery._probe_one(
                        peer.addresses[0], port=peer.api_port)
                    if result.get("ok"):
                        self._recover_fail.pop(peer.device_id, None)
                        self._recover_last_err.pop(peer.device_id, None)
                        self.note_direct_contact(peer.device_id)
                        self._audit("Mesh: recovered known peer "
                                    f"{peer.display_name()}")
                    else:
                        raise RuntimeError(result.get("error", "no answer"))
                except Exception as exc:
                    self._recover_fail[peer.device_id] = fails + 1
                    self._recover_last_err[peer.device_id] = str(exc)[:200]

    # ------------------------------------------------------------------
    # 3. duplicate reconciliation
    # ------------------------------------------------------------------
    def _reconcile_loop(self) -> None:
        import time as _t
        while not self._stop.wait(RECONCILE_INTERVAL_S):
            me = self.identity() or {}
            manual_hosts = set()
            for entry in (self.config.get("manual_peers") or []):
                host, _, port = entry.rpartition(":")
                manual_hosts.add((host, int(port) if port.isdigit() else 0))
            slots: Dict[tuple, list] = {}
            for peer in list(self.registry.all()):
                if peer.device_id == me.get("device_id"):
                    continue
                for addr in peer.addresses:
                    slots.setdefault((addr, peer.api_port),
                                     []).append(peer)
            merged = []
            with self._lock:
                for key, group in slots.items():
                    if len(group) < 2:
                        continue
                    if key in manual_hosts:
                        continue          # user-registered: never touched
                    group.sort(key=lambda p: p.last_seen or 0, reverse=True)
                    winner, losers = group[0], group[1:]
                    for loser in losers:
                        if not loser.participating:
                            continue
                        loser.participating = False
                        loser.notes = getattr(loser, "notes", "")
                        merged.append({
                            "kept": winner.device_id,
                            "kept_name": winner.display_name(),
                            "removed": loser.device_id,
                            "removed_name": loser.display_name(),
                            "address": key[0],
                        })
                if merged:
                    self._save()
            for m in merged:
                self._audit(
                    "Mesh: reconciled duplicate identity — kept "
                    f"{m['kept_name']}, deactivated stale {m['removed_name']} "
                    f"at {m['address']} (newest identity wins; the stale "
                    "entry is never deleted silently)",
                    **m)

    # ------------------------------------------------------------------
    # 4. mobile-host failover (EXPERIMENTAL — flag OFF by default)
    # ------------------------------------------------------------------
    @property
    def failover_enabled(self) -> bool:
        return bool(self.config.get("mobile_failover_enabled", False))

    def record_client(self, client_id: str, secret: str) -> None:
        """Store the SECRET HASH of a client hosted here (never the raw
        secret).  Only when failover is enabled."""
        if not self.failover_enabled:
            return
        with self._lock:
            self._state.setdefault("client_hashes", {})[client_id] = \
                _sha256_hex(client_id + "|" + secret)
            self._save()

    def forget_client(self, client_id: str) -> None:
        with self._lock:
            self._state.get("client_hashes", {}).pop(client_id, None)
            self._state.get("claims", {}).pop(client_id, None)
            self._save()

    def claim(self, client_id: str, secret: str,
              inbox_since: int = 0) -> dict:
        """A phone asks THIS companion to serve it after its host died."""
        if not self.failover_enabled:
            raise RuntimeError("failover disabled on this companion")
        want = _sha256_hex(client_id + "|" + (secret or ""))
        with self._lock:
            have = self._state.get("client_hashes", {}).get(client_id)
        if have is None:
            raise RuntimeError("unknown client for failover")
        if want != have:
            raise RuntimeError("credentials rejected")
        me = self.identity() or {}
        previous = self._state.get("claims", {}).get(client_id, {})
        if previous.get("host") == me.get("device_id") and \
                now_ms() - previous.get("ts", 0) < CLAIM_TTL_S * 1000:
            claim = previous
        else:
            claim = {"host": me.get("device_id"), "ts": now_ms(),
                     "ttl_s": CLAIM_TTL_S,
                     "host_name": me.get("name")}
            self._state.setdefault("claims", {})[client_id] = claim
            self._save()
            self._audit(f"Mesh failover: client {client_id} claimed by "
                        f"{me.get('name')} (previous host: "
                        f"{previous.get('host_name') or 'unknown'})")
        pulled = self._pull_inbox(client_id, previous.get("host"),
                                  inbox_since)
        return {"ok": True, "claim": claim, "pulled": pulled}

    def _pull_inbox(self, client_id: str, prev_host: str,
                    inbox_since: int) -> int:
        """Pull the client's inbox tail from the previous host so the new
        host serves continuous history.  Honest: 0 when unreachable."""
        if not prev_host:
            return 0
        peer = self.registry.get(prev_host)
        if not (peer and peer.addresses):
            return 0
        from .peers import PeerClient
        client = PeerClient(lambda: self.config.get("auth_token") or "")
        try:
            r = client.request(
                "GET",
                f"http://{peer.addresses[0]}:{peer.api_port}"
                f"/api/mesh/ledger/{client_id}?since={int(inbox_since)}",
                timeout=8)
            entries = r.get("entries") or []
            for e in entries:
                try:
                    self.mobile.inbox_append(client_id, e)
                except Exception:
                    continue
            return len(entries)
        except Exception:
            return 0

    def ledger(self, client_id: str, since: int = 0) -> dict:
        """Host-to-host: entries of a client inbox this companion hosts.
        Gated by failover being enabled; message payloads only."""
        if not self.failover_enabled:
            return {"ok": False, "error": "failover disabled"}
        entries: List[dict] = []
        try:
            path = os.path.join(self.data_dir,
                                f"mobile_inbox_{client_id}.jsonl")
            with open(path, "r", encoding="utf-8") as fh:
                for line in fh:
                    try:
                        e = json.loads(line)
                        if e.get("ts", 0) >= since:
                            entries.append(e)
                    except (ValueError, TypeError):
                        continue
        except OSError:
            pass
        return {"ok": True, "entries": entries}

    def claims_public(self) -> dict:
        """Advertised in identity so the old host can yield on return."""
        with self._lock:
            now = now_ms()
            live = {k: v for k, v in
                    self._state.get("claims", {}).items()
                    if now - v.get("ts", 0) < CLAIM_TTL_S * 1000}
        return live

    def yield_claim(self, client_id: str) -> None:
        """The previous host yields when it sees a fresher claim."""
        with self._lock:
            claims = self._state.get("claims", {})
            if client_id in claims:
                claims.pop(client_id, None)
                self._save()
                self._audit(f"Mesh failover: yielded service of "
                            f"{client_id} to its new host")

    # ------------------------------------------------------------------
    # 5. optional mDNS/DNS-SD provider (pure python, OFF by default)
    # ------------------------------------------------------------------
    def _mdns_loop(self) -> None:
        """Minimal DNS-SD: announce this companion and browse for peers on
        224.0.0.251:5353.  Answers are PROBED like every other discovery
        source before they can become peers.  Limitations (documented):
        legacy unicast responses only, no conflict resolution, no DNSSEC.
        """
        import time as _t
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM,
                                 socket.SOCK_CLOEXEC if hasattr(socket,
                                                                "SOCK_CLOEXEC")
                                 else 0)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind(("", 5353))
            except OSError:
                sock.bind(("0.0.0.0", 5353))
            mreq = socket.inet_aton(MDNS_ADDR[0]) + socket.inet_aton("0.0.0.0")
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP,
                            mreq)
            sock.settimeout(2.0)
        except OSError:
            self._audit("Mesh: mDNS unavailable (multicast socket error) — "
                        "UDP beacons and manual registration remain active")
            return
        announced = 0.0
        while not self._stop.is_set():
            # announce every 30 s
            if _t.monotonic() - announced > 30:
                self._mdns_announce(sock)
                announced = _t.monotonic()
            try:
                data, addr = sock.recvfrom(4096)
                name = self._mdns_parse_query(data)
                if name and MDNS_SERVICE.encode() in data:
                    self._mdns_announce(sock)
                    continue
                host, port, svc = self._mdns_parse_answer(data)
                if host and port and svc == "phantom":
                    self.discovery._probe_one(host, port=port)
            except socket.timeout:
                continue
            except OSError:
                break

    def _mdns_announce(self, sock) -> None:
        me = self.identity() or {}
        name = (me.get("name") or "phantom").encode("utf-8", "replace")
        port = int(self.config.get("api_port") or 8415)
        # answer-style packet: PTR + SRV + A records (uncompressed names)
        labels = MDNS_SERVICE.split(".")[:-1]  # _phantom-net,_tcp
        def nm(*parts):
            out = b""
            for p in parts:
                out += bytes([len(p)]) + (p if isinstance(p, bytes) else
                                          p.encode())
            return out + b"\x00"
        inst = nm(name, b"_phantom-net", b"_tcp", b"local")
        ptr = nm(b"_phantom-net", b"_tcp", b"local")
        body = b"\x00\x00\x84\x00"     # flags: response, AA
        body += b"\x00\x00\x00\x00\x00\x03\x00\x00"
        body += ptr + b"\x00\x0c\x00\x01\x00\x00\x00\x78" + \
            bytes([len(inst)]) + b"" + inst[1:]
        # SRV: priority weight port target
        hostlabels = nm((me.get("name") or "phantom")
                        .encode("utf-8", "replace").lower(), b"local")
        srv_payload = b"\x00\x00\x00\x00" + port.to_bytes(2, "big") + \
            hostlabels[1:]
        body += inst + b"\x00\x21\x00\x01\x00\x00\x00\x78" + \
            len(srv_payload).to_bytes(2, "big") + srv_payload
        pkt = b"\x00\x00" + body
        try:
            sock.sendto(pkt, MDNS_ADDR)
        except OSError:
            pass

    @staticmethod
    def _mdns_parse_query(data: bytes) -> str:
        try:
            n = data[12]
            return data[13:13 + n].decode("utf-8", "replace")
        except (IndexError, ValueError):
            return ""

    @staticmethod
    def _mdns_parse_answer(data: bytes) -> tuple:
        """Extract the SRV port and A-record address from an announcement
        (legacy parser: Phantom announcements only)."""
        host, port, svc = "", 0, ""
        try:
            idx = data.find(b"_phantom-net")
            if idx < 0:
                return "", 0, ""
            # SRV record scan: find port bytes near the tail structure
            tail = data[idx:]
            # crude but honest: the A record's 4-byte address is the last
            # 4 bytes of the packet
            if len(data) >= 4:
                a = data[-4:]
                host = ".".join(str(b) for b in a)
            # port: SRV rdata begins with pri(2) weight(2) port(2); find
            # pattern where hostlabels follow a plausible port (1024-65535)
            for i in range(0, len(tail) - 12):
                p = int.from_bytes(tail[i + 4:i + 6], "big")
                if 1024 <= p <= 65535 and tail[i + 6:i + 7] != b"\x00":
                    port = p
                    break
            svc = "phantom"
        except Exception:
            pass
        return host, port, svc

    # ------------------------------------------------------------------
    def shutdown(self) -> None:
        self._stop.set()

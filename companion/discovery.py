"""LAN discovery: UDP beacons, subnet sweep, latency probes.

Three complementary mechanisms:

1. **Passive beacons** (default): every companion broadcasts a small JSON
   identity packet on UDP 8414 (broadcast + multicast ``239.255.80.80``)
   every 3 seconds and listens for others.  Zero scanning traffic; this is
   the primary mechanism on a home LAN.
2. **Active sweep** (user-triggered from the Radar view): TCP-connect to the
   API port across the local /24 (or a user-specified CIDR) with a short
   timeout, asking ``/api/identity`` of anything that answers.  Also
   attempts reverse DNS for hostnames.  Config-gated by
   ``active_sweep_enabled``.
3. **Manual registration**: the operator adds an IP/hostname from the UI;
   the registry probes it periodically.

The service never attempts exploitation, auth attacks or deep port scans —
it only asks "are you a Phantom companion?" on the one port companions own.
"""

from __future__ import annotations

import ipaddress
import json
import socket
import struct
import threading
import time
import urllib.error
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Dict, List, Optional

from . import DEFAULT_API_PORT, DEFAULT_DISCOVERY_PORT, DISCOVERY_MULTICAST_GROUP, __version__
from .util import local_ipv4s, now_ms

BEACON_INTERVAL_S = 3.0
SWEEP_TIMEOUT_S = 0.35
SWEEP_WORKERS = 96
PEER_EXCHANGE_INTERVAL_S = 60
DIAG_LOG_CAP = 240


class DiscoveryService:
    def __init__(self, config, registry, identity_provider: Callable[[], dict],
                 api_port: int, events=None, log=None):
        self.config = config
        self.registry = registry
        self.identity_provider = identity_provider
        self.api_port = api_port
        self.events = events
        self.log = log
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._threads: List[threading.Thread] = []
        self._rx = 0
        self._tx = 0
        self._last_beacon = 0.0
        self._sweep_running = threading.Event()
        self._last_sweep_result: Dict = {}
        self._sock: Optional[socket.socket] = None
        # v1.3.0 diagnostics: ring buffer of beacon tx/rx observations so the
        # UI can show exactly what discovery saw, per interface, over time
        self._diag: deque = deque(maxlen=DIAG_LOG_CAP)
        self._last_rx = 0.0
        self._last_rx_addr = ""
        self._unknown_senders: Dict[str, float] = {}
        # optional callback (wired by App): fired when a beacon introduces
        # newly hosted mobile clients so queued messages retry promptly
        self.on_clients_changed = None

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------
    def start(self) -> None:
        if self.config.get("discovery_enabled", True):
            self._open_socket()
            t1 = threading.Thread(target=self._listen_loop, name="pn-disc-rx",
                                  daemon=True)
            t2 = threading.Thread(target=self._beacon_loop, name="pn-disc-tx",
                                  daemon=True)
            t3 = threading.Thread(target=self._maintenance_loop,
                                  name="pn-disc-maint", daemon=True)
            self._threads = [t1, t2, t3]
            for t in self._threads:
                t.start()
        # manual peers are probed even without broadcast discovery
        t4 = threading.Thread(target=self._manual_probe_loop, name="pn-disc-manual",
                              daemon=True)
        t4.start()
        self._threads.append(t4)
        # v1.3.0: companions exchange known-peer lists to heal fleets on
        # networks where broadcast discovery is unreliable (e.g. the Windows
        # peer-registry gap seen in the physical deployment)
        t5 = threading.Thread(target=self._peer_exchange_loop,
                              name="pn-disc-exchange", daemon=True)
        t5.start()
        self._threads.append(t5)

    def stop(self) -> None:
        self._stop.set()
        if self._sock:
            try:
                self._sock.close()
            except OSError:
                pass

    def status(self) -> dict:
        return {
            "enabled": self.config.get("discovery_enabled", True),
            "rx_packets": self._rx, "tx_packets": self._tx,
            "last_beacon_ms": int(self._last_beacon * 1000) if self._last_beacon else None,
            "sweep_running": self._sweep_running.is_set(),
            "last_sweep": self._last_sweep_result,
            "api_port": self.api_port,
        }

    def _note(self, direction: str, detail: str, ok: bool = True,
              address: str = "") -> None:
        self._diag.append({"ts": now_ms(), "dir": direction,
                           "detail": detail[:200], "ok": ok,
                           "address": address})

    def diagnostics(self) -> dict:
        """Full discovery diagnostics for the UI / support reports."""
        ifaces = [{"address": a, "broadcast": b} for a, b in
                  ((n["addr"], n.get("broadcast")) for n in self._local_networks())]
        sock_state = "open" if self._sock is not None else \
            ("disabled" if not self.config.get("discovery_enabled", True)
             else "failed to bind")
        return {
            "enabled": self.config.get("discovery_enabled", True),
            "socket": sock_state,
            "interfaces": ifaces,
            "beacon_targets": self._beacon_targets() if self._sock or True else [],
            "rx_packets": self._rx,
            "tx_packets": self._tx,
            "last_rx_ms": int(self._last_rx * 1000) if self._last_rx else None,
            "last_rx_address": self._last_rx_addr,
            "last_beacon_ms": int(self._last_beacon * 1000)
                              if self._last_beacon else None,
            "manual_peers": list(self.config.get("manual_peers") or []),
            "unknown_senders": sorted(
                ({"address": a, "last_ms": int(t * 1000)}
                 for a, t in self._unknown_senders.items()),
                key=lambda d: -d["last_ms"])[:10],
            "peer_exchange_enabled":
                bool(self.config.get("peer_exchange_enabled", True)),
            "log": list(self._diag)[-120:],
            "log_note": "Ring buffer of the last discovery observations "
                        "(beacon transmissions, receptions, errors).",
        }

    # ------------------------------------------------------------------
    # sockets
    # ------------------------------------------------------------------
    def _open_socket(self) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:  # allow multiple companions on one machine (tests!)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        except (AttributeError, OSError):
            pass
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        try:
            sock.bind(("", DEFAULT_DISCOVERY_PORT))
        except OSError:
            try:
                sock.bind((("0.0.0.0"), DEFAULT_DISCOVERY_PORT + 1))
            except OSError:
                self._sock = None
                if self.log:
                    self.log.warning("could not bind discovery port %d",
                                     DEFAULT_DISCOVERY_PORT)
                return
        # join the multicast group on each local interface
        try:
            mreq = struct.pack("4sL", socket.inet_aton(DISCOVERY_MULTICAST_GROUP),
                               socket.INADDR_ANY)
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
        except OSError:
            pass
        sock.settimeout(1.0)
        self._sock = sock

    # ------------------------------------------------------------------
    def _listen_loop(self) -> None:
        while not self._stop.is_set():
            if self._sock is None:
                time.sleep(1)
                continue
            try:
                data, addr = self._sock.recvfrom(8192)
            except socket.timeout:
                continue
            except OSError:
                break
            self._rx += 1
            self._last_rx = time.monotonic()
            self._last_rx_addr = addr[0]
            try:
                packet = json.loads(data.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                self._note("rx", f"unparseable packet from {addr[0]}",
                           ok=False, address=addr[0])
                continue
            if packet.get("pn") != 1 or packet.get("kind") != "beacon":
                self._unknown_senders[addr[0]] = time.monotonic()
                self._note("rx", f"non-beacon payload from {addr[0]}",
                           ok=False, address=addr[0])
                continue
            if packet.get("device_id") == self.identity_provider().get("device_id"):
                continue                      # our own beacon echoed back
            peer, transition = self.registry.observe_beacon(packet, addr[0])
            self._note("rx", f"beacon from {peer.display_name()} "
                             f"({addr[0]}:{peer.api_port})",
                       address=addr[0])
            if packet.get("mobile_clients") and self.on_clients_changed:
                try:
                    self.on_clients_changed()
                except Exception:
                    pass
            if self.events and transition in ("new", "reconnected"):
                verb = "New device discovered" if transition == "new" \
                    else "Device reconnected"
                self.events.emit(
                    "device_discovered" if transition == "new" else "device_connected",
                    f"{verb}: {peer.display_name()} ({addr[0]}:{peer.api_port})",
                    data={"device_id": peer.device_id, "address": addr[0]})

    # ------------------------------------------------------------------
    def _beacon_targets(self) -> List[str]:
        targets = ["<broadcast>", DISCOVERY_MULTICAST_GROUP]
        targets += self.config.get("broadcast_addresses") or []
        # directed broadcast per local subnet (covers some switch/AP setups)
        for net in self._local_networks():
            targets.append(net["broadcast"])
        seen, out = set(), []
        for t in targets:
            if t not in seen:
                seen.add(t)
                out.append(t)
        return out

    @staticmethod
    def _local_networks() -> List[dict]:
        nets = []
        for addr in local_ipv4s():
            try:
                iface = ipaddress.ip_interface(f"{addr}/24")
                nets.append({"addr": addr,
                             "broadcast": str(iface.network.broadcast_address)})
            except ValueError:
                continue
        return nets

    def _beacon_loop(self) -> None:
        while not self._stop.is_set():
            if self._sock is not None:
                packet = dict(self.identity_provider())
                packet["kind"] = "beacon"
                packet["api_port"] = self.api_port
                try:
                    payload = json.dumps(packet).encode("utf-8")
                except (TypeError, ValueError):
                    payload = None
                if payload:
                    sent = 0
                    for target in self._beacon_targets():
                        try:
                            self._sock.sendto(payload, (target, DEFAULT_DISCOVERY_PORT))
                            self._tx += 1
                            sent += 1
                        except OSError as exc:
                            self._note("tx", f"beacon to {target} failed: {exc}",
                                       ok=False, address=target)
                    self._note("tx", f"beacon sent to {sent} target(s)")
                self._last_beacon = time.monotonic()
            self._stop.wait(BEACON_INTERVAL_S)

    # ------------------------------------------------------------------
    def _maintenance_loop(self) -> None:
        """Offline-TTL sweep every 5 s (cheap in-memory check so the UI and
        reconnect events react promptly); latency/uptime probes every 30 s."""
        from .peers import PeerClient, PeerUnreachable
        client = PeerClient(self._token_provider)
        tick = 0
        while not self._stop.is_set():
            if self._stop.wait(5):
                return
            tick += 1
            lost = self.registry.sweep_offline()
            for peer in lost:
                if self.events:
                    self.events.emit(
                        "device_lost", f"Device went offline: {peer.display_name()}",
                        severity="warn", data={"device_id": peer.device_id})
            if tick % 6 == 0 and self.config.get("discovery_enabled", True):
                self._probe_latencies(client)

    def _token_provider(self) -> str:
        if self.config.get("require_token"):
            return self.config.get("auth_token", "")
        return ""

    def _probe_latencies(self, client) -> None:
        for peer in self.registry.all():
            if peer.status != "online" or not peer.addresses:
                continue
            url = f"http://{peer.addresses[0]}:{peer.api_port}"
            start = time.monotonic()
            try:
                ident = client.identity(url, timeout=2.5)
                latency = (time.monotonic() - start) * 1000.0
                self.registry.set_latency(peer.device_id, round(latency, 1),
                                          ident.get("uptime_s"))
            except Exception:
                self.registry.set_latency(peer.device_id, None)

    # ------------------------------------------------------------------
    # manual registration
    # ------------------------------------------------------------------
    def register_manual(self, address: str, name: str = "",
                        port: int = None) -> dict:
        """Manually register an IP/hostname; probes it immediately."""
        address = address.strip()
        try:
            ipaddress.ip_address(address)
        except ValueError:
            try:
                address = socket.gethostbyname(address)
            except OSError as exc:
                return {"ok": False, "error": f"Cannot resolve hostname: {exc}"}
        manual = self.config.get("manual_peers") or []
        entry = f"{address}:{port}" if port else address
        if entry not in manual:
            manual.append(entry)
            self.config.set("manual_peers", manual)
        result = self._probe_one(address, custom_name=name, port=port)
        return result

    def remove_manual(self, address: str) -> None:
        manual = [a for a in (self.config.get("manual_peers") or [])
                  if a != address and not a.startswith(address + ":")]
        self.config.set("manual_peers", manual)
        peer = self.registry.find_by_address(address)
        if peer and peer.manual:
            peer.status = "offline"

    def _manual_probe_loop(self) -> None:
        while not self._stop.is_set():
            for entry in list(self.config.get("manual_peers") or []):
                if self._stop.is_set():
                    return
                host, _, port_str = entry.rpartition(":")
                if port_str.isdigit() and host:
                    self._probe_one(host, port=int(port_str))
                else:
                    self._probe_one(entry)
            self._stop.wait(30)

    def _probe_one(self, address: str, custom_name: str = "",
                   port: int = None) -> dict:
        """Probe one address; register it if a Phantom companion answers.

        Never registers THIS device (probing 127.0.0.1 must not create a
        self-peer) and skips identities that do not look like companions.
        """
        from .peers import PeerClient, PeerUnreachable
        client = PeerClient(self._token_provider)
        own_id = self.identity_provider().get("device_id")
        ports = [port] if port else sorted(
            {self.api_port, DEFAULT_API_PORT}, key=lambda p: 0 if p == self.api_port else 1)
        for port_ in ports:
            url = f"http://{address}:{port_}"
            try:
                ident = client.identity(url, timeout=2.5)
            except (PeerUnreachable, Exception):
                continue
            if not isinstance(ident, dict) or "device_id" not in ident:
                continue
            if ident.get("device_id") == own_id:
                return {"ok": False, "self": True,
                        "error": f"{address}:{port_} is THIS device"}
            peer, transition = self.registry.observe_beacon(ident, address)
            peer.manual = True
            if custom_name:
                peer.custom_name = custom_name
            if port:
                peer.api_port = int(port)
            if self.events and transition in ("new", "reconnected"):
                self.events.emit(
                    "device_connected" if transition == "reconnected" else "device_discovered",
                    f"Registered device online: {peer.display_name()} ({address})",
                    data={"device_id": peer.device_id, "address": address})
            return {"ok": True, "device_id": peer.device_id, "identity": ident}
        # host reachable but not a companion → record as non-participating
        peer = self.registry.find_by_address(address)
        return {"ok": False,
                "error": f"No Phantom companion answered at {address} "
                         f"(ports {self.api_port}/{DEFAULT_API_PORT})",
                "device_id": peer.device_id if peer else None}

    # ------------------------------------------------------------------
    # active subnet sweep
    # ------------------------------------------------------------------
    def sweep(self, cidr: str = "") -> dict:
        """User-triggered TCP sweep of a subnet for companions.

        Only attempts connections to the single API port; never scans other
        ports and never sends payloads beyond ``GET /api/identity``.
        """
        if not self.config.get("active_sweep_enabled", True):
            return {"ok": False, "error": "Active sweep disabled in configuration"}
        if self._sweep_running.is_set():
            return {"ok": False, "error": "Sweep already running"}
        network = self._resolve_network(cidr)
        if network is None:
            return {"ok": False, "error": f"Cannot determine subnet for {cidr!r}"}
        hosts = [str(h) for h in network.hosts()]
        if len(hosts) > 1024:
            return {"ok": False, "error": "Subnet too large (>1024 hosts)"}
        self._sweep_running.set()
        threading.Thread(target=self._sweep_worker, args=(hosts,),
                         name="pn-sweep", daemon=True).start()
        return {"ok": True, "network": str(network), "hosts": len(hosts)}

    def _resolve_network(self, cidr: str):
        if cidr:
            try:
                return ipaddress.ip_network(cidr, strict=False)
            except ValueError:
                return None
        nets = self._local_networks()
        if not nets:
            return None
        try:
            return ipaddress.ip_network(nets[0]["addr"] + "/24", strict=False)
        except ValueError:
            return None

    def _sweep_worker(self, hosts: List[str]) -> None:
        found = []
        ports = sorted({self.api_port, DEFAULT_API_PORT})
        try:
            with ThreadPoolExecutor(max_workers=SWEEP_WORKERS) as pool:
                futures = []
                for host in hosts:
                    for port in ports:
                        futures.append(pool.submit(self._sweep_probe, host, port))
                results = [f.result() for f in futures]
            found = [r for r in results if r]
        finally:
            self._last_sweep_result = {
                "finished_ms": now_ms(), "probed": len(hosts), "found": found,
            }
            self._sweep_running.clear()
            if self.events:
                self.events.emit("system",
                                 f"Subnet sweep finished: {len(found)} companion(s) found",
                                 data={"found": found})

    def _sweep_probe(self, host: str, port: int = None) -> Optional[dict]:
        from .peers import PeerClient, PeerUnreachable
        client = PeerClient(self._token_provider)
        url = f"http://{host}:{port or self.api_port}"
        try:
            ident = client.identity(url, timeout=SWEEP_TIMEOUT_S)
            if isinstance(ident, dict) and "device_id" in ident \
                    and ident.get("device_id") != self.identity_provider().get("device_id"):
                peer, transition = self.registry.observe_beacon(ident, host)
                if self.events and transition in ("new", "reconnected"):
                    self.events.emit(
                        "device_discovered" if transition == "new" else "device_connected",
                        f"Sweep found companion: {peer.display_name()} ({host})",
                        data={"device_id": peer.device_id, "address": host})
                return {"address": host, "device_id": peer.device_id,
                        "name": peer.display_name()}
        except (PeerUnreachable, ValueError, OSError):
            pass
        except Exception:
            pass
        return None

    # ------------------------------------------------------------------
    # peer exchange (v1.3.0): fleet visibility without trusting gossip
    # ------------------------------------------------------------------
    def _peer_exchange_loop(self) -> None:
        from .peers import PeerClient, PeerUnreachable
        client = PeerClient(self._token_provider)
        while not self._stop.wait(PEER_EXCHANGE_INTERVAL_S):
            if not self.config.get("peer_exchange_enabled", True):
                continue
            me = self.identity_provider().get("device_id")
            known = []
            for peer in self.registry.all():
                if peer.device_id == me:
                    continue
                known.append({
                    "device_id": peer.device_id, "name": peer.name,
                    "addresses": list(peer.addresses)[:3],
                    "api_port": peer.api_port, "version": peer.version,
                    "os": peer.os, "participating": peer.participating,
                })
            if not known:
                continue
            for peer in self.registry.all():
                if peer.device_id == me or peer.status != "online" \
                        or not peer.addresses:
                    continue
                try:
                    client.request("POST",
                                   f"http://{peer.addresses[0]}:{peer.api_port}"
                                   f"/api/peers/exchange",
                                   body={"peers": known}, timeout=8)
                except (PeerUnreachable, Exception):
                    continue

    def receive_exchange(self, records: list, sender_address: str) -> dict:
        """Handle a peer-exchange payload: probe before trusting.

        Forwarded records are NEVER displayed as peers on their own — each
        one must answer a real /api/identity probe first, so stale or
        fabricated entries cannot poison the registry.
        """
        added, probed = [], 0
        for rec in (records or [])[:20]:
            if not isinstance(rec, dict):
                continue
            device_id = rec.get("device_id") or ""
            if not device_id or device_id == self.identity_provider().get("device_id"):
                continue
            if self.registry.get(device_id) is not None:
                continue                      # already known
            for address in (rec.get("addresses") or [])[:2]:
                probed += 1
                result = self._probe_one(address)
                if result.get("ok") and result.get("device_id") == device_id:
                    peer = self.registry.get(device_id)
                    added.append(device_id)
                    self._note("exchange",
                               f"peer-exchange validated {peer.display_name()} "
                               f"via {sender_address}", address=address)
                    break
        return {"ok": True, "received": len(records or []), "probed": probed,
                "added": added}

    def reverse_dns(self, host: str) -> str:
        try:
            return socket.gethostbyaddr(host)[0]
        except (OSError, IndexError):
            return ""

"""Small shared utilities for the Phantom Network companion.

Everything here is dependency-free and thread-safe.  These helpers are used
across all companion modules; keep them generic and platform-neutral.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import secrets
import socket
import tempfile
import threading
import time
from typing import Any, Dict, Iterator, Optional

from . import PROTOCOL_VERSION, __version__


def now_ms() -> int:
    """Current UNIX time in milliseconds (single clock used everywhere)."""
    return int(time.time() * 1000)


def iso_now() -> str:
    """ISO-8601 timestamp, local time with UTC offset."""
    return time.strftime("%Y-%m-%dT%H:%M:%S") + time.strftime("%z")


def new_id(prefix: str = "id") -> str:
    """Short random identifier, safe in URLs and file names."""
    return f"{prefix}_{secrets.token_hex(8)}"


def human_size(n: Optional[float]) -> str:
    """Human readable byte size ('1.5 GB').  Returns '—' for None."""
    if n is None:
        return "—"
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB", "PB"):
        if n < 1024.0 or unit == "PB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024.0
    return f"{n:.1f} PB"


def sha256_file(path: str, chunk_size: int = 1024 * 1024,
                cancel: Optional[threading.Event] = None,
                progress_cb: Optional[callable] = None) -> str:
    """Stream a file through SHA-256 without loading it into memory.

    ``progress_cb(bytes_processed)`` is invoked after each chunk so callers
    can hash huge files incrementally while transferring.
    """
    h = hashlib.sha256()
    processed = 0
    with open(path, "rb") as fh:
        while True:
            if cancel is not None and cancel.is_set():
                raise KeyboardInterrupt("hash cancelled")
            chunk = fh.read(chunk_size)
            if not chunk:
                break
            h.update(chunk)
            processed += len(chunk)
            if progress_cb is not None:
                progress_cb(processed)
    return h.hexdigest()


def read_json(path: str, default: Any = None) -> Any:
    """Read a JSON document; return ``default`` when missing/corrupt."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


def atomic_write_json(path: str, data: Any) -> None:
    """Atomically persist JSON (write temp file in same dir, then replace)."""
    directory = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", suffix=".json", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, ensure_ascii=False)
            fh.flush()
            os.fsync(fh.fileno())
        # os.replace is atomic on POSIX and Windows (Python 3.3+)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


class JsonStore:
    """A tiny thread-safe JSON document store with atomic persistence."""

    def __init__(self, path: str, default: Any):
        self._path = path
        self._lock = threading.RLock()
        self._data = read_json(path, default)
        if self._data is None:
            self._data = default

    @property
    def path(self) -> str:
        return self._path

    def get(self) -> Any:
        with self._lock:
            return self._data

    def update(self, mutator) -> Any:
        """Apply ``mutator(dict)`` under lock and persist atomically."""
        with self._lock:
            result = mutator(self._data)
            atomic_write_json(self._path, self._data)
            return result

    def set(self, data: Any) -> None:
        with self._lock:
            self._data = data
            atomic_write_json(self._path, data)


def local_ipv4s() -> list:
    """Best-effort list of this host's IPv4 addresses (UDP 'connect' trick)."""
    addrs = []
    try:
        hostname = socket.gethostname()
        for info in socket.getaddrinfo(hostname, None, socket.AF_INET):
            addr = info[4][0]
            if not addr.startswith("127."):
                addrs.append(addr)
    except OSError:
        pass
    # UDP connect trick finds the default-route interface address.
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("10.255.255.255", 1))
            addr = s.getsockname()[0]
            if addr and not addr.startswith("127.") and addr not in addrs:
                addrs.append(addr)
        finally:
            s.close()
    except OSError:
        pass
    return sorted(set(addrs))


def prefer_lan_address(addresses) -> str:
    """Pick the best peer address for LAN companion-to-companion calls.

    Prefers RFC1918 private addresses over CGNAT/Tailscale (100.64/10) and
    other public IPs so multi-homed Windows boxes do not route chat/files
    through the wrong interface.
    """
    addrs = [a for a in (addresses or []) if a]
    if not addrs:
        return ""
    def rank(a: str) -> tuple:
        try:
            import ipaddress
            ip = ipaddress.ip_address(a.split('%')[0])
        except Exception:
            return (3, a)
        if ip.is_private and not str(ip).startswith('100.'):
            return (0, a)
        if str(ip).startswith('100.'):
            return (2, a)
        if ip.is_loopback:
            return (3, a)
        return (1, a)
    return sorted(addrs, key=rank)[0]


def os_family() -> str:
    """'windows' | 'linux' | 'macos' | 'other' — used by discovery payloads."""
    s = platform.system().lower()
    return {"windows": "windows", "linux": "linux", "darwin": "macos"}.get(s, "other")


def self_identity(name: str, role: str = "companion") -> Dict[str, Any]:
    """Identity advertised in discovery beacons and peer handshakes."""
    host = socket.gethostname() or "unknown"
    return {
        "pn": PROTOCOL_VERSION,
        "version": __version__,
        "name": name,
        "host": host,
        "os": os_family(),
        "os_full": f"{platform.system()} {platform.release()}",
        "role": role,
        "ts": now_ms(),
    }

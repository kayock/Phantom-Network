"""Persistent configuration for the Phantom Network companion.

Configuration lives in a single ``config.json`` inside a platform-appropriate
data directory:

* Windows: ``%APPDATA%\\PhantomNetwork`` (``%LOCALAPPDATA%`` fallback)
* Linux/macOS: ``~/.config/phantom-network``
* Portable mode: a ``portable.marker`` file placed next to the package makes
  the companion keep all state in ``./data`` beside it (no writes outside the
  install folder).

Nothing in the configuration is machine-specific by default except what the
operator explicitly adds (device name, shares, bandwidth limits...).
"""

from __future__ import annotations

import json
import os
import threading

from .util import atomic_write_json, read_json

APP_DIR_NAME_LINUX = "phantom-network"
APP_DIR_NAME_WINDOWS = "PhantomNetwork"


def default_device_name() -> str:
    import socket
    return socket.gethostname() or "phantom-device"


def default_config() -> dict:
    return {
        # --- identity -------------------------------------------------
        "device_name": default_device_name(),
        "role": "companion",                # companion | dashboard (label only)
        # --- network --------------------------------------------------
        "api_port": 8415,
        "discovery_port": 8414,
        "discovery_enabled": True,          # respond to + send UDP beacons
        "broadcast_addresses": [],          # extra directed-broadcast targets
        "active_sweep_enabled": True,       # allow user-triggered /24 sweep
        "require_token": False,             # optional shared-secret auth
        "auth_token": "",                   # generated on first run when enabled
        "allowed_networks": [],             # e.g. ["192.168.1.0/24"]; empty = any local peer
        # --- filesystem -----------------------------------------------
        # v1.1.0: NO directory is shared automatically. The first-run wizard
        # (web UI) lets the operator authorize specific directories or whole
        # drives explicitly.  "share_home_rw" is only a convenience flag that
        # the wizard may set after an explicit user choice.
        "shares": [],                       # [{"path","label","mode":"rw"|"ro"}]
        "share_home_rw": False,             # explicit opt-in only (was True in v1.0.0)
        "setup_complete": False,            # first-run share wizard done
        "admin_local_only": True,           # v1.2.0: remote clients cannot
                                            #   change shares/settings
        "follow_symlinks": False,
        # --- transfers --------------------------------------------------
        "max_concurrent_jobs": 2,
        "chunk_size": 1024 * 1024,          # 1 MiB streaming chunks
        "bandwidth_limit_bps": 0,           # 0 = unlimited (per-companion)
        "min_free_space_bytes": 64 * 1024 * 1024,
        "verify_sha256": True,
        "max_dir_files": 200000,            # hard cap; refuse rather than truncate
        # --- audio ------------------------------------------------------
        "remote_audio_enabled": True,
        "audio_max_volume": 0.8,            # hard amplitude ceiling
        "audio_max_duration_s": 30,
        # --- intercom ---------------------------------------------------
        "intercom_enabled": True,
        # --- mobile host (v1.3.0) ---------------------------------------
        "mobile_host_enabled": True,        # host/relay browser clients
        # v1.4.0 — Phantom Signal feature flags (failover stays OFF until
        # the user opts in — documented design decision)
        "voice_enabled": True,
        "screen_enabled": True,
        "broadcast_enabled": True,
        "media_enabled": True,
        "mobile_failover_enabled": False,
        "voice_stun_servers": [],
        # v1.4.0 — Phantom Clipboard (steer addition).  AUTO SYNC IS OFF
        # BY DEFAULT; participation on; honest 256 KiB / 2 MiB caps.
        "clipboard_enabled": True,
        "clipboard_auto_enabled": False,
        "clipboard_paused": False,
        "clipboard_private_mode": False,
        "clipboard_participating": True,
        "clipboard_retention_days": 30,
        "clipboard_max_bytes": 262144,
        "clipboard_max_image_bytes": 2097152,
        "mobile_client_ttl_ms": 45000,      # heartbeat TTL before "offline"
        "mobile_max_attachment_mb": 50,     # per chat-file / voice note
        # --- monitoring alerts (v1.3.0) ---------------------------------
        "alert_rules": [],                  # see companion/alerts.py
        # --- automation (v1.3.0) ----------------------------------------
        "automation_jobs": [],              # watched folders + schedules
        "automation_poll_s": 60,            # watched-folder poll interval
        # --- storage view (v1.3.0) --------------------------------------
        "storage_view": "useful",           # physical | useful | all
        "storage_hidden_prefixes": ["/snap", "/boot", "/run", "/var/lib/docker"],
        # --- notifications (v1.3.0) -------------------------------------
        "notify_sounds_enabled": True,
        # --- monitoring ---------------------------------------------------
        "monitoring_enabled": True,
        "monitor_history_points": 240,
        # --- ui ---------------------------------------------------------
        "ui_state": {},                     # per-device UI prefs mirrored from browser
    }


class Config:
    """Thread-safe wrapper around ``config.json`` with change notification."""

    def __init__(self, data_dir: str):
        self.data_dir = data_dir
        os.makedirs(data_dir, exist_ok=True)
        self.path = os.path.join(data_dir, "config.json")
        self._lock = threading.RLock()
        stored = read_json(self.path, None) or {}
        merged = default_config()
        # keep unknown keys (forward compatibility) but never lose defaults
        for key, value in merged.items():
            if key not in stored:
                stored[key] = value
        self._data = stored
        self._listeners = []
        self._persist()

    # ------------------------------------------------------------------
    def get(self, key: str, default=None):
        with self._lock:
            return self._data.get(key, default)

    def as_dict(self) -> dict:
        with self._lock:
            return dict(self._data)

    def set(self, key: str, value) -> None:
        with self._lock:
            self._data[key] = value
            self._persist()
        self._notify(key, value)

    def update(self, patch: dict) -> None:
        with self._lock:
            for key, value in patch.items():
                self._data[key] = value
            self._persist()
        for key, value in patch.items():
            self._notify(key, value)

    def _persist(self) -> None:
        atomic_write_json(self.path, self._data)

    # ------------------------------------------------------------------
    def on_change(self, callback) -> None:
        """Register callback(key, value) fired after each persisted change."""
        self._listeners.append(callback)

    def _notify(self, key: str, value) -> None:
        for cb in list(self._listeners):
            try:
                cb(key, value)
            except Exception:
                pass

    # ------------------------------------------------------------------
    def public_view(self) -> dict:
        """Config subset safe to expose over the API (no secrets)."""
        with self._lock:
            view = dict(self._data)
        view.pop("auth_token", None)
        return view


def resolve_data_dir(explicit: str = None) -> str:
    """Decide the state directory (CLI flag > portable marker > platform dir)."""
    if explicit:
        return os.path.abspath(explicit)
    package_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if os.path.exists(os.path.join(package_dir, "portable.marker")):
        return os.path.join(package_dir, "data")
    if os.name == "nt":
        base = os.environ.get("APPDATA") or os.environ.get("LOCALAPPDATA") \
            or os.path.expanduser("~")
        return os.path.join(base, APP_DIR_NAME_WINDOWS)
    xdg = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.join(xdg, APP_DIR_NAME_LINUX)

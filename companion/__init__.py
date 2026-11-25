"""PHANTOM NETWORK companion service.

A lightweight, cross-platform (Windows / Linux) LAN companion agent that
provides filesystem access, file transfer, messaging, audio playback,
intercom relay and telemetry to the Phantom Network browser command center.

Pure Python 3.9+ standard library is required; ``psutil`` is an optional
dependency that improves telemetry fidelity when installed.

v1.3.1 (PHANTOM MOBILE — stabilization) adds independent mobile browser clients: phones and
tablets register with their host companion as first-class logical
participants with their own identity, inbox and presence.
"""

__version__ = "1.3.1"
__product__ = "Phantom Network"

# Protocol 2 introduces the mobile-client identity fields (``kind``,
# ``mobile_clients``, ``capabilities``) and GhostChat 2.0 message semantics
# (replies / reactions / edits / tombstones).  Companions still accept
# protocol-1 peers — see BEHAVIOR_COMPATIBILITY in ARCHITECTURE.md.
PROTOCOL_VERSION = 2

# Default network ports (configurable in config.json / CLI).
DEFAULT_API_PORT = 8415      # TCP: HTTP API + web UI + SSE
DEFAULT_DISCOVERY_PORT = 8414  # UDP: broadcast/multicast discovery beacon
DISCOVERY_MULTICAST_GROUP = "239.255.80.80"

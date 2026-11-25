"""PHANTOM NETWORK companion service.

A lightweight, cross-platform (Windows / Linux) LAN companion agent that
provides filesystem access, file transfer, messaging, audio playback,
intercom relay and telemetry to the Phantom Network browser command center.

Pure Python 3.9+ standard library is required; ``psutil`` is an optional
dependency that improves telemetry fidelity when installed.
"""

__version__ = "1.2.1"
__product__ = "Phantom Network"

PROTOCOL_VERSION = 1

# Default network ports (configurable in config.json / CLI).
DEFAULT_API_PORT = 8415      # TCP: HTTP API + web UI + SSE
DEFAULT_DISCOVERY_PORT = 8414  # UDP: broadcast/multicast discovery beacon
DISCOVERY_MULTICAST_GROUP = "239.255.80.80"

#!/usr/bin/env bash
# Generate a self-signed LAN certificate for optional Phantom Network HTTPS.
# Does NOT enable TLS and does NOT modify any trust store.
set -euo pipefail
DATA="${XDG_CONFIG_HOME:-$HOME/.config}/phantom-network/tls"
IP="${1:-}"
if [ -z "$IP" ]; then
  IP=$(hostname -I 2>/dev/null | awk '{print $1}')
fi
[ -n "$IP" ] || { echo "Usage: $0 <lan-ip>"; exit 1; }
mkdir -p "$DATA"
openssl req -x509 -newkey rsa:2048 -sha256 -days 825 -nodes \
  -keyout "$DATA/phantom-key.pem" \
  -out "$DATA/phantom-cert.pem" \
  -subj "/CN=phantom-local" \
  -addext "subjectAltName=DNS:localhost,DNS:phantom-local,IP:127.0.0.1,IP:$IP"
chmod 600 "$DATA/phantom-key.pem"
chmod 644 "$DATA/phantom-cert.pem"
echo "Wrote $DATA/phantom-cert.pem and phantom-key.pem"
echo "Enable in config.json: tls_enabled=true, tls_cert/tls_key paths above."
echo "HTTP remains available until you enable TLS; trust the cert on iPhone manually."

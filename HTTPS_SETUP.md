# Optional HTTPS Setup (v1.3.2)

Some browser features require a **secure context** (HTTPS or localhost):
microphone access (intercom, voice messages), some PWA installation paths
(iOS Add to Home Screen notifications), and service-worker scope rules on
iOS. HTTPS is **optional** — chat, files, discovery, transfers and the
mobile interface all work over plain HTTP on a trusted LAN.

HTTP on port **8415** remains the default. Enabling TLS serves HTTPS on the
**same port**; it does not open a second listener and does not change UDP
discovery port **8414**.

## 1. Generate a certificate (self-signed, LAN SANs)

```bash
# From a Phantom Network source tree:
bash scripts/gen-lan-cert.sh 10.0.0.202
```

Or manually:

```bash
mkdir -p ~/.config/phantom-network/tls && cd ~/.config/phantom-network/tls
openssl req -x509 -newkey rsa:2048 -sha256 -days 825 -nodes \
  -keyout phantom-key.pem -out phantom-cert.pem \
  -subj "/CN=phantom-local" \
  -addext "subjectAltName=DNS:localhost,DNS:phantom-local,IP:127.0.0.1,IP:10.0.0.202"
```

Replace `IP:10.0.0.202` with this machine's LAN IP. One SAN per LAN IP /
hostname you will open in Safari.

## 2. Enable TLS on the companion (opt-in)

In `~/.config/phantom-network/config.json` (Linux) or
`%APPDATA%\PhantomNetwork\config.json` (Windows):

```json
{
  "tls_enabled": true,
  "tls_cert": "/home/YOU/.config/phantom-network/tls/phantom-cert.pem",
  "tls_key":  "/home/YOU/.config/phantom-network/tls/phantom-key.pem"
}
```

Restart the companion. A failed TLS setup is reported honestly and the
companion falls back to HTTP. Phantom never enables TLS by itself.

## 3. iPhone / iPad certificate trust (manual — required for mic)

Phantom Network **never** installs certificates into iOS. You do this once:

1. Copy `phantom-cert.pem` (or `.crt`) to the iPhone (AirDrop, Files, email,
   or open `https://10.0.0.202:8415/` after TLS is on and download the cert
   from a shared folder).
2. On the iPhone: open the profile → **Settings → Profile Downloaded → Install**.
3. Then: **Settings → General → About → Certificate Trust Settings** → enable
   **full trust** for the Phantom certificate.
4. Open Safari to `https://10.0.0.202:8415/m` (note **https**).
5. Grant microphone permission when prompted.

Without step 3, Safari may still show the page after dismissing a warning,
but `getUserMedia` (microphone) often stays blocked for untrusted certs.

## 4. Other platforms

- Ubuntu: copy PEM to `/usr/local/share/ca-certificates/phantom.crt` and run
  `sudo update-ca-certificates` (only if you want system-wide trust).
- Windows: double-click the cert → Install Certificate → Trusted Root
  (Current User). Phantom does not change the store for you.

## 5. What works where

| Feature | HTTP (LAN IP) | HTTPS self-signed | HTTPS trusted |
| --- | --- | --- | --- |
| UI, files, chat, discovery | ✔ | ✔ | ✔ |
| Microphone (intercom/voice) | ✘ on iPhone | ⚠ | ✔ |

## 6. Non-goals

No mandatory HTTPS, no automatic certificate provisioning, no new
authentication gates, no CA automation, no silent trust-store changes.

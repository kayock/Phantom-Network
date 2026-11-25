# Optional HTTPS Setup (v1.4.0 — dual listeners)

v1.4.0 improvement: HTTPS and HTTP can run **simultaneously**. With
`tls_dual: true` (the default), ordinary HTTP keeps serving messaging,
files and the dashboard on the main port, while a second listener adds
HTTPS on `tls_port` (main port + 1 by default) — the secure context
phones need for the microphone, WebRTC voice and PWA install. Nothing
replaces HTTP, nothing touches your trust store, and HTTPS is never
required for ordinary features.

Some browser features require a **secure context** (HTTPS or localhost):
microphone access (intercom, voice messages), some PWA installation paths
(iOS Add to Home Screen notifications), and service-worker scope rules on
iOS. HTTPS is **optional** — chat, files, discovery, transfers and the
mobile interface all work over plain HTTP on a trusted LAN.

## 1. Generate a certificate (self-signed, LAN SANs)

Run on the companion machine (Linux/macOS shell; Windows: use WSL or Git
Bash, or `openssl` from PowerShell if installed):

```bash
mkdir -p ~/.config/phantom-network/tls && cd ~/.config/phantom-network/tls
openssl req -x509 -newkey rsa:2048 -sha256 -days 825 -nodes \
  -keyout phantom-key.pem -out phantom-cert.pem \
  -subj "/CN=phantom-local" \
  -addext "subjectAltName=DNS:localhost,DNS:phantom-local,IP:127.0.0.1,IP:192.168.1.10"
```

Replace `IP:192.168.1.10` with this machine's actual LAN IP (see the
Network Radar). One SAN per LAN IP / hostname you will use. Renew by
re-running with a new `-days`.

## 2. Enable TLS on the companion

In `config.json` (or Settings on the host machine):

```json
{
  "tls_enabled": true,
  "tls_cert": "/home/you/.config/phantom-network/tls/phantom-cert.pem",
  "tls_key":  "/home/you/.config/phantom-network/tls/phantom-key.pem"
}
```

Restart the companion. It now serves HTTPS on the same port; a failed TLS
setup is reported honestly and the companion falls back to HTTP.

## 3. Trusting the certificate vs dismissing the warning

- **Dismissing the browser warning** is enough for a quick look — features
  that need secure contexts usually DO work afterwards, but some browsers
  restrict them for untrusted certificates.
- **Trusting the certificate** (importing it into the OS/browser trust
  store) gives full behavior. Phantom **never** installs certificates,
  changes trust stores or security settings automatically — you do it
  explicitly:
  - Ubuntu: copy the PEM to `/usr/local/share/ca-certificates/phantom.crt`
    and run `sudo update-ca-certificates`.
  - Windows: double-click the .pem → Install Certificate → Trusted Root.
  - iPhone/iPad: download the .pem in Safari → Settings → Profile
    Downloaded → Install, then Settings → General → About → Certificate
    Trust Settings → enable full trust.
  - Android: Settings → Security → Encryption & credentials → Install a
    certificate.

## 4. What works where

| Feature | HTTP (LAN IP) | HTTPS self-signed | HTTPS trusted |
| --- | --- | --- | --- |
| UI, files, chat, discovery | ✔ | ✔ | ✔ |
| PWA install (Android/desktop) | ✘* | ✔* | ✔ |
| PWA install (iOS) | ✘ | ⚠ | ✔ |
| Microphone (intercom/voice) | ✘ | ⚠ | ✔ |

\* depends on browser; ⚠ may require dismissing the warning each time or
may be blocked. **iPhone microphone behavior is documented, not verified —
it must be tested on a real device before you rely on it.**

## 5. Upgrade note (v1.3.1)

Upgrading from v1.2.2/v1.3.0 to v1.3.1 does not touch TLS settings:
`tls_enabled`, `tls_cert`, `tls_key` and existing certificates keep
working. If you enabled HTTPS for microphone/PWA features, the phone only
needs one page reload after the companion restarts.

## 6. Non-goals

No mandatory HTTPS, no automatic certificate provisioning, no new
authentication gates, no CA automation. Everything works over HTTP unless
you explicitly want the secure-context features.

---

# v1.4.0 — dual listeners, cert download, and phone identity

## Quick path (Station host, iPhone)

1. **Generate the certificate** (one command, no trust-store changes):

   ```bash
   scripts/gen-lan-cert.sh 192.168.1.10     # your LAN IP
   ```

   The script writes the key + cert into
   `~/.config/phantom-network/tls/` with SANs for `localhost`,
   `127.0.0.1` and your LAN IP. It prints the two config keys to set.

2. **Enable dual serving** in `config.json`:

   ```json
   {
     "tls_enabled": true,
     "tls_dual": true,
     "tls_cert": "/home/you/.config/phantom-network/tls/phantom-cert.pem",
     "tls_key": "/home/you/.config/phantom-network/tls/phantom-key.pem"
   }
   ```

   Restart the companion. HTTP still answers on the main port; HTTPS
   answers on **main port + 1** (`tls_port` overrides).

3. **Install the certificate on the iPhone** (manual trust — the
   companion never edits your device):

   - On the phone, open the HTTP origin and download
     `http://<station>:<port>/api/tls/cert` (Settings → HTTPS also has a
     download button).
   - iOS downloads a configuration profile. Install it via Settings →
     Profile Downloaded.
   - Then Settings → General → About → **Certificate Trust Settings**
     → enable full trust for `phantom-local`.
   - Open `https://<station>:<port+1>/` — the secure context. Microphone
     permission prompt appears on first voice use.

4. **Keep your identity across the move**: HTTP and HTTPS are different
   origins, so the browser storage starts fresh. In More → **Identity &
   recovery**, copy the identity JSON from the HTTP origin first; on the
   HTTPS origin choose **Restore an existing identity** in onboarding
   and paste it. Same id, same inbox, same history. Without the secret
   the host refuses re-attachment — that is the v1.3.1 anti-takeover
   rule, unchanged.

## Ubuntu / Android notes

- **Ubuntu (Chrome/Chromium/Firefox)**: after trusting, browse
  `https://station:port+1`. Chrome may need the cert imported into
  `NSS` (`certutil -d sql:$HOME/.pki/nssdb -A -t "CT,c,c" -n phantom
  -i phantom-cert.pem`) — manual, documented, never automatic.
- **Android (Chrome)**: install the certificate via Settings →
  Security → Encryption & credentials → Install a certificate → CA
  certificate. Chrome then treats the origin as secure; PWA install and
  microphone work.

## Diagnostics

- `GET /api/tls/status` — enabled/dual flags, ports, certificate expiry,
  subject, SANs, which features unlock, and plain-language notes. The
  desktop Settings → **HTTPS & secure context** card renders it; the
  mobile More → **Secure context** card shows the per-device view
  (secure origin / mic API / WebRTC).
- If HTTPS fails to start (missing file, bad PEM), the companion logs it
  and **keeps serving HTTP** — a TLS mistake can never take messaging
  down.

## What this does NOT do

- No automatic certificate generation, renewal, or trust-store edits.
- No firewall changes.
- No new authentication requirements — the optional `auth_token` model
  is unchanged.
- No forcing HTTPS: plain HTTP remains fully functional for messaging,
  file sharing, transfers, broadcast, clipboard and the dashboard.

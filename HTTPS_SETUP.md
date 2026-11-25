# Optional HTTPS Setup (v1.3.1)

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

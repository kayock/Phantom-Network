# KNOWN_LIMITATIONS.md

Honest limitations of PHANTOM NETWORK v1.0.0. Nothing in this file is
hidden behaviour — each item is also surfaced in the UI or docs where it
matters.

## Platform & environment

1. **Windows code paths are source-verified, not hardware-verified.** The
   v1.1.0 verification ran on Linux (see TEST_REPORT.md §execution matrix).
   Windows-specific logic (drive enumeration via `GetLogicalDriveStringsA`,
   `GetSystemTimes`/`GlobalMemoryStatusEx` telemetry, PowerShell
   `Media.SoundPlayer` playback, `st_file_attributes` hidden flags) is
   implemented and unit-level reviewed but was **not executed on a real
   Windows machine** for this release.
2. **No prebuilt Windows `.exe`** is included: a Linux host cannot
   cross-compile one. The ZIP ships full source plus
   `packaging/companion.spec` for a one-command PyInstaller build on any
   Windows machine with `pip install pyinstaller`.
3. **Same-host discovery is limited**: two companions on the *same machine*
   cannot reliably hear each other's UDP beacons (SO_REUSEPORT delivers
   loopback unicast to one socket; multicast loopback is often blocked in
   containers). On real multi-machine LANs broadcast discovery works.
   Same-host instances should be connected via manual registration — the
   test suite does exactly that.
4. **Audio audibility was not verified** (the dev container has no sound
   device). What IS verified: correct WAV synthesis (byte-checked), the
   player selection chain, process lifecycle (stop), and the API contract.
   Real-speaker testing is a manual step (DEPLOYMENT.md).

## Browsers & intercom

5. **Microphone requires a secure context.** On a plain-HTTP LAN address,
   browsers block `getUserMedia`: the UI detects this and explains the
   three documented workarounds (localhost, companion HTTPS mode with a
   self-signed cert, or Chrome's
   `unsafely-treat-insecure-origin-as-secure` flag on trusted LANs).
   iPhone Safari additionally requires HTTPS — it will not open the mic on
   HTTP LAN origins at all.
6. **Intercom latency** is ~150–400 ms (HTTP relay + 60 ms chunks + jitter
   buffer). It is a walkie-talkie, not a phone call. Echo cancellation
   relies on the browser's own AEC.
7. **No WebRTC mesh** — the PCM relay is deliberately simpler (no STUN/TURN,
   works on isolated LANs). Bandwidth per talker is ~256 kbit/s.
8. **No intercom recording exists in v1.0.0** — audio is relayed live and
   dropped; this is a privacy feature, and no hidden capture path exists.

## Transfers & filesystem

9. **Sync compares by size in the preview** (SHA-256 is verified after each
   copy). Two files with equal size but different content are "skipped" by
   the preview's `skip` list only if... actually they are **copied** only
   when sizes differ; equal-size-different-content files are skipped by
   sync (the compare-conflict mode in manual transfers does compare hashes).
   Sync therefore trades precision for speed — documented in the preview
   dialog itself.
10. **Sync never deletes.** There is no mirror-delete mode in v1.0.0 (the
    `delete_confirmed` flag is accepted but unused by design).
11. **Directory pulls** are paginated and complete fully up to
    `max_dir_files` (config, default 200 000); beyond the cap the job is
    REFUSED (`tree_too_large`) instead of truncated. Each page walk stats
    every file, so very large trees take a little time to enumerate.
12. **Zip downloads cap at 2 GB uncompressed.**
13. **Hash cache granularity**: on filesystems with second-level (or
    coarse) mtime granularity, a same-size rewrite within one timestamp
    tick can serve a stale hash for the `compare` conflict mode.
    `verified-delete` is immune (always re-hashes).
14. **Upload sessions** (browser→companion) do not support byte-offset
    resume; interrupted browser uploads restart. Companion↔companion
    transfers do resume.

## Security model

15. **No per-user authentication.** Anyone on the LAN who can reach port
    8415 can use the API unless you enable the shared-token mode
    (`require_token`). This matches the "trusted home LAN" design goal and
    is stated in Settings; PSK is the maximum in v1.0.0.
16. **Self-signed HTTPS** triggers browser warnings; no CA integration.
17. **Event/message stores are plaintext JSONL** on each device
    (`events.jsonl`, `messages.jsonl`). Disk encryption is the OS's job.

## Signal Laboratory

18. **Acoustic messaging over real air** is experimental: decode success
    depends on room noise, speaker/mic quality and timing. The decoder
    reports confidence and refuses (CRC + threshold) rather than fabricating
    text; the offline simulation provides the deterministic guarantee.
19. **Morse mic decoding** is on/off-keying only — no Farnsworth timing
    tolerance, no noise-adaptive threshold.
20. **The spectrum analyzer** reads the live browser input; there is no
    waterfall/spectrogram view yet.

## Monitoring

21. **Windows telemetry without psutil** is coarser (no per-disk
    breakdown via pure ctypes in the fallback path; install psutil for
    full fidelity — the UI shows which source is active).
22. **GPU info** requires `nvidia-smi`; AMD/Intel GPU stats are not
    collected in v1.0.0.

## Behavioural notes

23. **One audio playback at a time** per device; starting a new one stops
    the previous (this is by design, not a bug).
24. **Companion ports are fixed at startup** — changing `api_port` requires
    a restart; the Settings view says so.
25. **The dashboard is not a hard router**: if the UI-host machine sleeps,
    other browsers still work directly against their own companions.


## v1.2.0 additions

* **Local-administrative boundary is policy, not a security wall.** Remote
  clients cannot change shares/settings, but ordinary peer operations
  (browse authorized shares, receive transfer instructions, trigger audio)
  remain LAN-wide by design. On hostile networks also enable
  `require_token` and a personal firewall.
* **PowerShell validation is static only** (byte scan, quote/brace sanity)
  in this environment — no pwsh; real Windows execution is a manual step
  (CURSOR_DEPLOYMENT_HANDOFF.md Stage C).
* **Same-size mtime-preserving modifications** are caught at deletion by
  full re-hash (v1.2.0), at the cost of one extra read per move.
* **Port conflict**: a second companion on a busy port exits with
  "Address already in use" (verified); the health-poll launcher pattern
  means installers must check process liveness, not just the endpoint.

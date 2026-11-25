# BUILD.md

Building, packaging and verifying PHANTOM NETWORK v1.3.0 from source.

## 1. Prerequisites

| Tool | Needed for |
|---|---|
| Python 3.9+ | companion, tests, sound generation |
| Node.js ≥ 18 | only the Signal-Lab DSP test suite (`tests/js/test_dsp.mjs`) |
| `zip`, `unzip` | packaging + verification |
| `pytest`, `pytest-timeout` | test runner (`pip install pytest pytest-timeout`) |
| `psutil` (optional) | richer telemetry during tests |
| agent-browser (optional) | scripted UI smoke test |

## 2. Regenerate derived assets

```bash
# original sound presets (byte-identical to what the companion synthesizes)
python3 scripts/gen_sounds.py                # writes sounds/*.wav
```

All branding is hand-authored SVG (`web/assets/logo.svg`) — no binary build
step. Screenshots referenced in TEST_REPORT.md are captured with
agent-browser (`scripts/ui_smoke.sh`, optional).

## 3. Run the full verification suite

```bash
python -m pytest                       # unit + JS DSP + real-network integration
node tests/js/test_dsp.mjs             # (also run inside pytest)
```

A green run is the release gate — see TEST_REPORT.md for the recorded
results of the v1.0.0 verification run.

## 4. Build the distributable

```bash
bash packaging/make-dist.sh
```

The script:

1. runs a quick source audit (version strings, no leftover dev files),
2. builds `Phantom-Network-v1.3.0.zip` with reproducible timestamps
   (`SOURCE_DATE_EPOCH` if set),
3. **extracts the ZIP into a clean directory** and re-runs the test suite
   against the shipped source,
4. writes `BUILD_LOG.txt` (the actual commands + outputs).

Output: `dist/Phantom-Network-v1.2.2.zip`.

## 5. Platform-specific distributables

| Artifact | Produced on | How |
|---|---|---|
| Source ZIP (canonical) | any host | `packaging/make-dist.sh` |
| Linux portable `.tar.gz` | any Linux host | `packaging/make-portable-linux.sh` |
| Windows portable folder | any host | the source tree itself + `portable.marker`; run `packaging\run-phantom-windows.bat` |
| Windows one-file `.exe` | **Windows host only** | `pip install pyinstaller && pyinstaller --noconfirm --clean packaging/companion.spec` → `dist/PhantomCompanion.exe` (spec ships with the release; a Linux host cannot cross-compile — documented, not claimed) |
| Ubuntu `.deb` | Debian/Ubuntu host | `bash packaging/make-deb.sh` → `dist/phantom-network_1.2.2_all.deb` |

## 6. Reproducibility notes

* Pure-stdlib companion → no lockfile to drift; optional deps are
  documented (`psutil`). `requirements.txt` lists dev/test tools.
* WAV presets are deterministic (fixed note tables, 44.1 kHz, 16-bit mono) —
  `gen_sounds.py` output is byte-stable across runs/platforms.
* The ZIP stores sorted file lists with fixed timestamps for reproducible
  archive hashes when `SOURCE_DATE_EPOCH` is provided.

## 7. Smoke-checking a build manually

```bash
unzip Phantom-Network-v1.2.0.zip -d /tmp/pn-check && cd /tmp/pn-check/phantom-network
python -m companion --port 18999 --data-dir /tmp/pn-check-data &
curl -s http://127.0.0.1:18999/api/health        # {"ok": true, "version": "1.0.0", ...}
curl -s http://127.0.0.1:18999/ | head -3        # the UI shell
kill %1
```


---

# v1.3.0 build notes

- No new runtime dependencies (pure stdlib backend; vendored QR JS in
  `web/js/lib/qrcode.js`, MIT).
- Generate PWA icons: `python scripts/gen_icons.py` (PIL, dev-only).
- Windows build: `.github/workflows/build-windows.yml` (delivered, awaits a
  real runner; uses packaging/companion.spec with PyInstaller).
- Version consistency checks in `packaging/validate_release.py` now expect
  1.3.0 (see packaging/make-dist.sh).

# DEVELOPER_GUIDE.md

Developing, testing and extending PHANTOM NETWORK v1.2.1.

## 1. Development setup

```bash
git clone <your-fork> phantom-network && cd phantom-network

# system requirements: Python 3.9+ (3.12 used in CI/dev), Node.js ≥ 18 only
# for the DSP test suite, pytest for the test runner
python3 -m venv .venv && source .venv/bin/activate    # optional
pip install pytest pytest-timeout psutil               # psutil optional but recommended

# run a companion from the source tree (uses ~/.config/phantom-network)
python -m companion --log-level DEBUG
```

There is no build step for the frontend: `web/` is plain HTML/CSS/ES-module
JS served statically by the companion. Point a browser at
`http://localhost:8415` and hard-refresh (`Ctrl+Shift+R`) after edits.

### Run two companions on one machine (dev)

```bash
python -m companion --port 8415 --name "Dev A" --data-dir /tmp/pn-a
python -m companion --port 18416 --name "Dev B" --data-dir /tmp/pn-b
# same-host UDP delivery is unreliable by design (SO_REUSEPORT) — connect
# them via Radar → + Register device → 127.0.0.1, port 18416
```

## 2. Code layout & conventions

See ARCHITECTURE.md §2 for the module map. Conventions:

* **Naming**: `snake_case` functions/vars, `PascalCase` classes, views are
  lower-case files matching the nav route id.
* **Errors**: services raise `FsError(status, code, message)`; the HTTP
  layer converts it to JSON. Never leak raw tracebacks (500s log them).
* **Threading**: companion state is guarded by `RLock`s; workers never
  touch the request thread. New background loops follow the
  `threading.Thread(daemon=True)` + `self._stop` pattern used by
  discovery/transfers/chat/monitor.
* **Persistence**: JSON via `atomic_write_json` / `JsonStore` only —
  no SQLite or other runtime deps.
* **Frontend**: no framework, no bundler. `el()` builds DOM; every view
  module exports `async render(container) → cleanup|null`. State flows
  through `state.js`; API calls through `api.js`.
* Versioning: `companion/__init__.py` is the single source of
  `__version__` (`1.2.1`); the sidebar and `/api/health` read it.

## 3. Testing

```bash
# everything (unit + JS DSP + real-network integration)
python -m pytest

# layers separately
python -m pytest tests/unit                      # 27 fast tests, no network
node tests/js/test_dsp.mjs                       # Signal-Lab codec suite
python -m pytest tests/integration               # real companion subprocesses

# single integration test with output
python -m pytest tests/integration/test_network_e2e.py::test_pause_resume_transfer -q --tb=long
```

Test taxonomy (please keep it honest in TEST_REPORT.md):

| Class | Where | What it proves |
|---|---|---|
| Unit | `tests/unit` | logic in isolation (sandbox, rate limiter, stores) |
| JS unit | `tests/js/test_dsp.mjs` | the *shipped* DSP code incl. corrupted-frame refusal |
| Real-network integration | `tests/integration` | actual `python -m companion` subprocesses over HTTP: discovery, browsing, transfers (copy/dir/pause/resume/restart/corruption/conflicts/move/bandwidth), chat delivery + outbox, config, auth, shutdown |
| Browser interface | agent-browser session, see TEST_REPORT.md §UI | every view loads console-clean; a real transfer was driven by clicking the UI |
| Manual / hardware | — | audio audibility, phone microphones (documented, not claimable from CI) |

The integration suite starts real processes with temp data dirs
(`tests/conftest.py::Companion`). Its `wait_job_done` helper polls
`/api/jobs` like the UI does. A corrupting MITM HTTP proxy
(`_CorruptingHandler`) simulates bit-flips without any production test
hooks.

## 4. Debugging tips

* Logs: `<data-dir>/logs/companion.log` (2 MB × 5 rotation). `--log-level DEBUG`.
* Event history: `curl ':8415/api/events?limit=50'` or the Events view.
* Transfer state: `cat <data-dir>/jobs_state.json | python3 -m json.tool`.
* Leftover `.part` / `.part.corrupt` files show interrupted/failed pulls.
* SSE stream live: `curl -N ':8415/api/stream'`.
* UI console: the app logs to the browser console; `agent-browser errors`
  can be scripted in CI.

## 5. Adding a feature — worked example

New endpoint `GET /api/fleet/badges`:

1. Handler in `app.py`:
   ```python
   def fleet_badges(self, req) -> Response:
       return json_response({"ok": True,
                             "badges": [p.display_name() for p in
                                        self.registry.all() if p.status == "online"]})
   ```
2. Route in `routes.py`:
   `r.add("GET", r"/api/fleet/badges", lambda req: api.fleet_badges(req))`
3. Document it in API_REFERENCE.md; add an integration assertion.
4. UI: call via `api.get(deviceId, '/api/fleet/badges')` from a view.

## 6. Packaging quick reference

See BUILD.md for the full flow: `scripts/gen_sounds.py` regenerates the
WAV presets, `packaging/make-dist.sh` builds the release ZIP
(`Phantom-Network-v1.2.0.zip`) and verifies it by extracting to a clean
directory and re-running the test suite against the shipped source.

## 7. PR checklist for collaborators

* `python -m pytest` green (unit + integration).
* `node tests/js/test_dsp.mjs` green.
* New endpoints documented in API_REFERENCE.md.
* Version bumps only in `companion/__init__.py` + CHANGELOG.md.
* No new runtime dependency without a fallback path and a line in
  KNOWN_LIMITATIONS.md.
* Every visible UI control performs its advertised function (audit before PR).

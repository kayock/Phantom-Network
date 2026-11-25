"""Pytest configuration: launch real companion subprocesses on demand.

Integration helpers start genuine ``python -m companion`` processes on
127.0.0.1 with temporary data dirs — the same way a user would run them —
and talk to them over real HTTP.  No mocks for the network layer.
"""

import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request

import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class Companion:
    """A real companion subprocess bound to 127.0.0.1."""

    def __init__(self, name: str, port: int = None, data_dir: str = None,
                 extra_args=(), env_token: str = ""):
        self.port = port or free_port()
        self.data_dir = data_dir or tempfile.mkdtemp(prefix=f"pn-{name}-")
        self.name = name
        self.proc = None
        self.extra_args = list(extra_args)
        self.env_token = env_token
        self.base = f"http://127.0.0.1:{self.port}"

    def start(self, wait: bool = True):
        env = dict(os.environ)
        env["PYTHONUNBUFFERED"] = "1"
        os.makedirs(self.data_dir, exist_ok=True)
        # keep companion output on disk — essential for diagnosing hangs
        self._out = open(os.path.join(self.data_dir, "proc-stdout.log"), "ab")
        self._err = open(os.path.join(self.data_dir, "proc-stderr.log"), "ab")
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "companion", "--port", str(self.port),
             *(["--name", self.name] if self.name else []),
             "--data-dir", self.data_dir,
             "--log-level", "INFO", *self.extra_args],
            cwd=ROOT, env=env,
            stdout=self._out, stderr=self._err,
        )
        if wait:
            self.wait_ready()
        return self

    def wait_ready(self, timeout: float = 15.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError(f"companion {self.name} died on startup")
            try:
                data = self.get("/api/health")
                if data.get("ok"):
                    return
            except Exception:
                time.sleep(0.15)
        raise TimeoutError(f"companion {self.name} not ready in {timeout}s")

    def stop(self):
        if self.proc and self.proc.poll() is None:
            self.proc.send_signal(signal.SIGTERM)
            try:
                self.proc.wait(timeout=8)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=5)

    def kill9(self):
        if self.proc and self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait(timeout=5)

    # ------------------------------------------------------------------
    def request(self, method: str, path: str, body=None, timeout: float = 10,
                raw: bool = False, headers=None):
        h = {"Content-Type": "application/json"}
        if self.env_token:
            h["X-PN-Token"] = self.env_token
        if headers:
            h.update(headers)
        data = None
        if body is not None:
            data = json.dumps(body).encode() if not isinstance(body, bytes) else body
        req = urllib.request.Request(self.base + path, data=data, headers=h,
                                     method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                payload = resp.read()
                if raw:
                    return payload
                return json.loads(payload.decode() or "null")
        except urllib.error.HTTPError as exc:
            # 4xx/5xx responses still carry JSON error bodies — surface them
            body = exc.read().decode()
            try:
                parsed = json.loads(body or "null")
            except ValueError:
                parsed = {"ok": False, "status": exc.code, "raw": body}
            parsed.setdefault("ok", False)
            parsed.setdefault("status", exc.code)
            return parsed

    def get(self, path, **kw):
        return self.request("GET", path, **kw)

    def post(self, path, body=None, **kw):
        return self.request("POST", path, body, **kw)

    def identity(self):
        return self.get("/api/identity")

    @property
    def device_id(self) -> str:
        return self.identity()["device_id"]

    def register(self, other: "Companion", name: str = ""):
        return self.post("/api/peers/register",
                         {"address": "127.0.0.1", "port": other.port,
                          "name": name or other.name})


@pytest.fixture(scope="module")
def pair():
    """Two real companions that know each other (module scope)."""
    a = Companion("AlphaStation").start()
    b = Companion("BetaBox").start()
    a.register(b)
    b.register(a)
    yield a, b
    a.stop()
    b.stop()
    import shutil
    shutil.rmtree(a.data_dir, ignore_errors=True)
    shutil.rmtree(b.data_dir, ignore_errors=True)


IN_PROGRESS_STATUSES = ("running", "queued", "verifying", "paused", "hashing")


def wait_job_done(c: Companion, job_id: str, timeout: float = 60) -> dict:
    """Poll until the job leaves the active set; returns its final record.

    ``hashing`` is the v1.2.2 asynchronous checksum-preparation phase — a
    real machine with a cold page cache can legitimately sit there for a
    while, and on a slow container even small fixtures do.  The helper must
    wait through it (assertions then check genuine completion, never a
    transient state)."""
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        data = c.get("/api/jobs")
        for j in data["jobs"]:
            if j["id"] == job_id:
                last = j
                if j["status"] in IN_PROGRESS_STATUSES:
                    time.sleep(0.3)
                    break
                return j
        else:
            for j in data["history"]:
                if j["id"] == job_id:
                    return j
            if last is not None:
                return last
            time.sleep(0.2)
    raise TimeoutError(f"job {job_id} did not finish in {timeout}s")

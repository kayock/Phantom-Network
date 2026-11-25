"""v1.2.1 regressions: read-only shares and installer user resolution."""

import os
import subprocess
import sys
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from companion.config import Config  # noqa: E402
from companion.errors import FsError  # noqa: E402
from companion.events import EventBus  # noqa: E402
from companion.fsops import PathSandbox, delete_entries  # noqa: E402
from companion.transfers import TransferEngine  # noqa: E402

ROOT = os.path.join(os.path.dirname(__file__), "..", "..")


def _engine(td, shares):
    cfg = Config(td)
    cfg.set("share_home_rw", False)
    cfg.set("shares", shares)
    sb = PathSandbox(cfg)
    eng = TransferEngine(td, cfg, sb, EventBus(td), None)
    return eng, sb


def _wait(eng, job, timeout=15):
    ev = eng._worker_done[job["id"]]
    assert ev.wait(timeout), "transfer did not finish"
    return eng.get_job(job["id"]) or job


def test_readonly_share_copy_delete_and_move(tmp_path):
    root = str(tmp_path)
    rw = os.path.join(root, "rw")
    ro = os.path.join(rw, "archive")
    dest_dir = os.path.join(rw, "inbox")
    os.makedirs(ro)
    os.makedirs(dest_dir)
    src = os.path.join(ro, "notes file.txt")
    payload = "keep me\n"
    with open(src, "w", encoding="utf-8") as fh:
        fh.write(payload)
    shares = [
        {"path": rw, "label": "work", "mode": "rw"},
        {"path": ro, "label": "archive", "mode": "ro"},
    ]
    eng, sb = _engine(root, shares)
    try:
        # 1. Copying from the read-only share works. The ancestor is rw,
        # so only the more specific share can refuse a later delete.
        copied = eng.create_job({
            "kind": "pull", "src_base": "self", "src_path": src,
            "dest_path": os.path.join(dest_dir, "notes file.txt"),
            "conflict": "replace", "delete_source": False,
        })
        done = _wait(eng, copied)
        assert done["status"] == "completed"
        assert done["verify"]["status"] == "ok"
        assert os.path.exists(src)
        assert open(done["dest_path"], encoding="utf-8").read() == payload

        # 2. Direct deletion from the read-only share is refused.
        with pytest.raises(FsError) as denied:
            delete_entries(sb, [src], confirm=True)
        assert denied.value.code == "read_only_share"
        assert os.path.exists(src)

        # 3. Verified deletion from the read-only share is refused.
        digest = eng.file_hash(src)["sha256"]
        with pytest.raises(FsError) as verified:
            eng.verified_delete(src, digest)
        assert verified.value.code == "read_only_share"
        assert os.path.exists(src)

        # 4. A move preserves the source and reports partial completion.
        moved = eng.create_job({
            "kind": "pull", "src_base": "self", "src_path": src,
            "dest_path": os.path.join(dest_dir, "moved.txt"),
            "conflict": "replace", "delete_source": True,
        })
        moved_done = _wait(eng, moved)
        assert moved_done["status"] == "completed_partial"
        assert moved_done["verify"]["status"] == "ok"
        assert os.path.exists(src)
        assert os.path.exists(moved_done["dest_path"])
        assert "source" in (moved_done.get("move_error") or "").lower()

        # 5. A properly authorized verified move still removes the source.
        writable = os.path.join(rw, "editable.txt")
        with open(writable, "w", encoding="utf-8") as fh:
            fh.write("movable\n")
        ok_move = eng.create_job({
            "kind": "pull", "src_base": "self", "src_path": writable,
            "dest_path": os.path.join(dest_dir, "editable.txt"),
            "conflict": "replace", "delete_source": True,
        })
        ok_done = _wait(eng, ok_move)
        assert ok_done["status"] == "completed"
        assert ok_done["verify"]["status"] == "ok"
        assert not os.path.exists(writable)
        assert open(ok_done["dest_path"], encoding="utf-8").read() == "movable\n"
    finally:
        eng.stop()


def _passwd_home(user: str):
    """Home directory from the passwd database (getent), or None."""
    try:
        out = subprocess.check_output(["getent", "passwd", user],
                                      text=True).strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None
    fields = out.split(":")
    return fields[5] if len(fields) >= 6 else None


def test_installer_ignores_root_home_and_root_xdg():
    """Sudo-session simulation: root env vars must not decide ownership.

    The simulated desktop user must exist in THIS machine's passwd DB —
    the installer correctly refuses to guess a home directory for a user
    that does not exist (that is the safety property under test).  On
    container environments without the simulated user the test is skipped
    rather than weakened; test_installer_ownership_uses_existing_desktop_user
    covers the same resolution logic deterministically everywhere.
    """
    if _passwd_home("kayock") is None:
        pytest.skip("simulated desktop user 'kayock' not in this "
                    "environment's passwd DB (sandbox-only condition; "
                    "passes on real Ubuntu deployments)")
    env = os.environ.copy()
    env.update({
        "HOME": "/root",
        "USER": "root",
        "SUDO_USER": "kayock",
        "LOGNAME": "root",
        "XDG_CONFIG_HOME": "/root/.config",
        "XDG_STATE_HOME": "/root/.local/state",
        "PN_RESOLVE_ONLY": "1",
    })
    out = subprocess.check_output(
        ["bash", os.path.join(ROOT, "packaging", "install-ubuntu.sh")],
        env=env, text=True)
    vals = dict(line.split("=", 1) for line in out.strip().splitlines())
    assert vals["user"] == "kayock"
    assert vals["home"] == os.path.expanduser("~kayock")
    assert vals["state"].startswith(vals["home"] + "/")
    assert vals["data"].startswith(vals["home"] + "/")
    assert "/root" not in vals["state"]
    assert "/root" not in vals["data"]


def test_installer_ownership_uses_existing_desktop_user():
    """Deterministic variant: same resolution logic with a desktop user
    that exists in ANY environment (the invoking non-root user).
    Assertions are the same shape — root env must not leak into the
    resolved paths."""
    import pwd as _pwd
    me = os.environ.get("USER") or _pwd.getpwuid(os.getuid()).pw_name
    if not me or me == "root":
        # pick any non-root passwd user with a home directory
        candidates = [p.pw_name for p in _pwd.getpwall()
                      if p.pw_uid != 0 and p.pw_dir and p.pw_dir != "/root"]
        if not candidates:
            pytest.skip("no non-root passwd user available")
        me = candidates[0]
    home = _passwd_home(me)
    if not home:
        pytest.skip(f"passwd entry for {me} unavailable")
    env = os.environ.copy()
    env.update({
        "HOME": "/root",
        "USER": "root",
        "SUDO_USER": me,
        "LOGNAME": "root",
        "XDG_CONFIG_HOME": "/root/.config",
        "XDG_STATE_HOME": "/root/.local/state",
        "PN_RESOLVE_ONLY": "1",
    })
    out = subprocess.check_output(
        ["bash", os.path.join(ROOT, "packaging", "install-ubuntu.sh")],
        env=env, text=True)
    vals = dict(line.split("=", 1) for line in out.strip().splitlines())
    assert vals["user"] == me
    assert vals["home"] == home
    assert vals["state"].startswith(home + "/")
    assert vals["data"].startswith(home + "/")
    assert "/root" not in vals["state"]
    assert "/root" not in vals["data"]

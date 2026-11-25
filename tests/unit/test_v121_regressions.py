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


def test_installer_ignores_root_home_and_root_xdg():
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

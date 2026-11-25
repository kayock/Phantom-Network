"""Unit tests: filesystem sandbox + operations (the security boundary)."""

import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from companion.config import Config  # noqa: E402
from companion.errors import FsError  # noqa: E402
from companion.fsops import (PathSandbox, delete_entries, list_directory,  # noqa: E402
                             make_dir, preview_file, recursive_search,
                             rename_entry)
from companion.fsops import UploadSessions  # noqa: E402


@pytest.fixture()
def sandbox_env():
    with tempfile.TemporaryDirectory() as td:
        home = os.path.join(td, "home")
        share2 = os.path.join(td, "media")
        secret = os.path.join(td, "secret")
        for d in (home, share2, secret):
            os.makedirs(d)
        with open(os.path.join(home, "readme.txt"), "w") as fh:
            fh.write("hello phantom\n" * 40)
        with open(os.path.join(share2, "movie.bin"), "wb") as fh:
            fh.write(os.urandom(5000))
        with open(os.path.join(secret, "keys.txt"), "w") as fh:
            fh.write("SECRET")

        cfg = Config(td)
        cfg.set("share_home_rw", False)
        cfg.set("shares", [{"path": home, "label": "Home", "mode": "rw"},
                           {"path": share2, "label": "Media", "mode": "ro"}])
        yield PathSandbox(cfg), home, share2, secret, td
        cfg.set("shares", [])


def test_authorize_inside_shares(sandbox_env):
    sb, home, share2, secret, td = sandbox_env
    assert sb.authorize(os.path.join(home, "readme.txt"), "r") == \
        os.path.realpath(os.path.join(home, "readme.txt"))
    # read from read-only share: allowed
    sb.authorize(os.path.join(share2, "movie.bin"), "r")


def test_traversal_outside_shares_blocked(sandbox_env):
    sb, home, share2, secret, td = sandbox_env
    with pytest.raises(FsError) as e:
        sb.authorize(secret, "r")
    assert e.value.status == 403
    # traversal via ..
    with pytest.raises(FsError):
        sb.authorize(os.path.join(home, "..", "secret", "keys.txt"), "r")
    # traversal via .. INSIDE the path string (treated as dir name check)
    with pytest.raises(FsError):
        sb.authorize(os.path.join(home, "sub", "..", "..", "secret"), "r")


def test_write_to_readonly_share_blocked(sandbox_env):
    sb, home, share2, secret, td = sandbox_env
    with pytest.raises(FsError) as e:
        sb.authorize(os.path.join(share2, "new.txt"), "w")
    assert e.value.code == "read_only_share"


def test_symlink_escape_blocked(sandbox_env):
    sb, home, share2, secret, td = sandbox_env
    link = os.path.join(home, "sneaky")
    os.symlink(secret, link)
    with pytest.raises(FsError):
        sb.authorize(link, "r")


def test_hidden_symlink_to_share_is_fine(sandbox_env):
    sb, home, share2, secret, td = sandbox_env
    sub = os.path.join(home, "sub")
    os.makedirs(sub, exist_ok=True)
    link = os.path.join(sub, "movies")
    os.symlink(share2, link)
    real = sb.authorize(link, "r")
    assert os.path.realpath(real) == os.path.realpath(share2)


def test_list_directory_hidden_toggle(sandbox_env):
    sb, home, share2, secret, td = sandbox_env
    with open(os.path.join(home, ".hidden"), "w") as fh:
        fh.write("x")
    vis = list_directory(sb, home)
    names = {e["name"] for e in vis["entries"]}
    assert "readme.txt" in names and ".hidden" not in names
    all_ = list_directory(sb, home, show_hidden=True)
    assert ".hidden" in {e["name"] for e in all_["entries"]}


def test_mkdir_rename_delete_flow(sandbox_env):
    sb, home, share2, secret, td = sandbox_env
    made = make_dir(sb, home, "new folder")
    assert os.path.isdir(made["path"])
    renamed = rename_entry(sb, made["path"], "renamed_2")
    assert os.path.isdir(renamed["path"])
    # deletion requires explicit confirm
    with pytest.raises(FsError) as e:
        delete_entries(sb, [renamed["path"]], confirm=False)
    assert e.value.code == "confirm_required"
    res = delete_entries(sb, [renamed["path"]], confirm=True)
    assert res["results"][0]["deleted"] is True
    assert not os.path.exists(renamed["path"])
    # illegal names rejected
    with pytest.raises(FsError):
        make_dir(sb, home, "../escape")
    with pytest.raises(FsError):
        rename_entry(sb, os.path.join(home, "readme.txt"), "..")


def test_readonly_share_write_ops_fail(sandbox_env):
    sb, home, share2, secret, td = sandbox_env
    with pytest.raises(FsError) as e:
        make_dir(sb, share2, "nope")
    assert e.value.code == "read_only_share"


def test_recursive_search(sandbox_env):
    sb, home, share2, secret, td = sandbox_env
    sub = os.path.join(home, "deep", "deeper")
    os.makedirs(sub)
    with open(os.path.join(sub, "needle-file.txt"), "w") as fh:
        fh.write("x")
    res = recursive_search(sb, home, "needle")
    assert any(r["name"] == "needle-file.txt" for r in res["results"])


def test_preview_text_binary_image(sandbox_env):
    sb, home, share2, secret, td = sandbox_env
    pv = preview_file(sb, os.path.join(home, "readme.txt"))
    assert pv["kind"] == "text" and "hello phantom" in pv["data"]
    binpath = os.path.join(share2, "movie.bin")
    pv2 = preview_file(sb, binpath)
    assert pv2["kind"] == "binary"
    img = os.path.join(home, "pic.png")
    with open(img, "wb") as fh:
        fh.write(b"\x89PNG fake")
    pv3 = preview_file(sb, img)
    assert pv3["kind"] == "image"


def test_upload_sessions(sandbox_env):
    sb, home, share2, secret, td = sandbox_env
    ups = UploadSessions(sb, os.path.join(td, "upl"))
    s = ups.create(home, "report final.pdf", 10)
    assert "renam" not in s["name"]
    ups.append(s["sid"], iter([b"0123456789"]))
    fin = ups.finalize(s["sid"])
    assert fin["target"] == os.path.join(home, "report final.pdf")
    # size mismatch
    s2 = ups.create(home, "bad.bin", 100)
    ups.append(s2["sid"], iter([b"short"]))
    import pytest as _pytest
    with _pytest.raises(FsError) as e:
        ups.finalize(s2["sid"])
    assert e.value.code == "size_mismatch"
    # conflict rename
    s3 = ups.create(home, "report final.pdf", 5, conflict="rename")
    assert s3["name"].startswith("report final (")
    ups.append(s3["sid"], iter([b"hello"]))
    ups.finalize(s3["sid"])
    # cancel cleans up
    s4 = ups.create(home, "gone.bin", 4)
    ups.cancel(s4["sid"])
    assert not os.path.exists(s4["part"])

"""v1.1.0 regression tests: safe defaults, pagination, cap, move guards."""

import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from companion.config import Config  # noqa: E402
from companion.errors import FsError  # noqa: E402
from companion.events import EventBus  # noqa: E402
from companion.fsops import PathSandbox  # noqa: E402
from companion.transfers import TransferEngine  # noqa: E402


def test_safe_default_shares_nothing():
    """Defect A: a fresh install shares NOTHING automatically."""
    with tempfile.TemporaryDirectory() as td:
        cfg = Config(td)
        assert cfg.get("shares") == []
        assert cfg.get("share_home_rw") is False
        assert cfg.get("setup_complete") is False
        sb = PathSandbox(cfg)
        assert sb.shares() == []
        with pytest.raises(FsError) as e:
            sb.authorize(os.path.expanduser("~"), "r")
        assert e.value.status == 403


def _mk_engine(td, **overrides):
    cfg = Config(td)
    cfg.set("share_home_rw", False)
    cfg.set("shares", [{"path": td, "label": "test", "mode": "rw"}])
    for k, v in overrides.items():
        cfg.set(k, v)
    sb = PathSandbox(cfg)
    return TransferEngine(td, cfg, sb, EventBus(td), None), cfg, sb


def _make_tree(root: str, n_files: int, depth: int = 3):
    for d in range(depth):
        os.makedirs(os.path.join(root, f"lvl{d}"), exist_ok=True)
    for i in range(n_files):
        sub = f"lvl{i % depth}"
        with open(os.path.join(root, sub, f"f{i:05d}.bin"), "wb") as fh:
            fh.write(b"x" * (64 + i % 64))


def _walk_all(eng, path, page=100):
    collected, offset = [], 0
    while True:
        pg = eng.walk_remote_manifest(path, skip=offset, limit=page)
        collected.extend(pg["files"])
        offset = pg["next_offset"]
        if offset is None:
            return pg, collected


def test_walk_pagination_covers_every_file_exactly_once():
    """Defect G: paging never omits or duplicates entries."""
    with tempfile.TemporaryDirectory() as td:
        eng, cfg, sb = _mk_engine(td)
        tree = os.path.join(td, "tree")
        os.makedirs(tree)
        _make_tree(tree, 250)
        last_page, files = _walk_all(eng, tree, page=100)
        assert len(files) == 250
        assert last_page["total_files"] == 250
        rels = [f["rel"] for f in files]
        assert len(rels) == len(set(rels)), "pagination duplicated entries"


def test_walk_reports_totals_consistently_across_pages():
    with tempfile.TemporaryDirectory() as td:
        eng, cfg, sb = _mk_engine(td)
        tree = os.path.join(td, "tree")
        os.makedirs(tree)
        _make_tree(tree, 77)
        totals = set()
        offset = 0
        while True:
            pg = eng.walk_remote_manifest(tree, skip=offset, limit=20)
            totals.add((pg["total_files"], pg["total_bytes"]))
            offset = pg["next_offset"]
            if offset is None:
                break
        assert len(totals) == 1, "totals must be stable across pages"


def test_tree_cap_refuses_instead_of_truncating():
    """Defect G: beyond the cap → explicit error, not silent truncation."""
    with tempfile.TemporaryDirectory() as td:
        eng, cfg, sb = _mk_engine(td, max_dir_files=100)
        tree = os.path.join(td, "tree")
        os.makedirs(tree)
        _make_tree(tree, 150)
        with pytest.raises(FsError) as e:
            eng.walk_remote_manifest(tree)
        assert e.value.code == "tree_too_large"
        assert "200000" not in e.value.message and "100" in e.value.message


def test_verified_delete_identity_guards():
    """Spec §6: delete refuses when size/mtime identity changed."""
    with tempfile.TemporaryDirectory() as td:
        eng, cfg, sb = _mk_engine(td)
        p = os.path.join(td, "src.bin")
        with open(p, "wb") as fh:
            fh.write(b"payload" * 100)
        info = eng.file_hash(p)
        # size changed since verification
        with open(p, "ab") as fh:
            fh.write(b"extra")
        with pytest.raises(FsError) as e:
            eng.verified_delete(p, info["sha256"],
                                expected_mtime_ns=info["mtime_ns"],
                                expected_size=info["size"])
        assert e.value.code == "source_changed"
        assert os.path.exists(p)
        # matching identity AND the CURRENT content hash → deletes
        cur = eng.file_hash(p)          # new size/mtime → fresh hash
        eng.verified_delete(p, cur["sha256"], expected_size=cur["size"])
        assert not os.path.exists(p)

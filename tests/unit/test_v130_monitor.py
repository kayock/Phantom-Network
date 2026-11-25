"""v1.3.0 unit tests: storage mount classification + GPU/CPU topology.

Runs on the real host filesystem of the test machine (Linux container):
asserts honest behavior — classification never fabricates values and the
"useful" view hides snap/virtual mounts while "all" keeps everything.
"""

import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(__file__, "..", "..", "..")))

from companion.config import Config  # noqa: E402
from companion.monitor import Monitor  # noqa: E402


def make_monitor(tmp_path):
    cfg = Config(str(tmp_path))
    mon = Monitor(cfg, lambda: [], log=None)
    return mon, cfg


def test_mount_meta_honest_fields(tmp_path):
    mon, _ = make_monitor(tmp_path)
    meta = mon._mount_meta("/")
    assert "fstype" in meta and "device" in meta and "read_only" in meta
    # /proc/mounts or psutil must describe the root mount
    assert meta["fstype"] or meta["device"]


def test_mounts_views_consistent(tmp_path):
    mon, _ = make_monitor(tmp_path)
    everything = mon.mounts("all")
    useful = mon.mounts("useful")
    physical = mon.mounts("physical")
    assert isinstance(everything, list)
    # every classification is one of the known kinds
    for m in everything:
        assert m["kind"] in ("physical", "virtual", "snap", "network",
                             "external", "optical")
        assert isinstance(m["read_only"], bool)
    # useful ⊆ all; physical ⊆ all
    all_mounts = {m["mount"] for m in everything}
    assert {m["mount"] for m in useful} <= all_mounts
    assert {m["mount"] for m in physical} <= all_mounts
    # snap loops must not appear in the default useful view
    assert not any(m["mount"].startswith("/snap") or m["kind"] == "snap"
                   for m in useful)


def test_useful_view_hides_configured_prefixes(tmp_path):
    mon, cfg = make_monitor(tmp_path)
    cfg.set("storage_hidden_prefixes", ["/snap"])
    # synthetic: /snap mounts get classified as snap anyway; the config
    # filter additionally hides any prefix-matched mount from useful
    everything = mon.mounts("all")
    for m in everything:
        if m["mount"].startswith("/snap"):
            useful = mon.mounts("useful")
            assert m["mount"] not in {u["mount"] for u in useful}


def test_gpu_info_never_fabricates(tmp_path):
    mon, _ = make_monitor(tmp_path)
    info = mon.gpu_info()
    assert isinstance(info, dict) and "gpus" in info
    for g in info["gpus"]:
        assert g["vendor"] in ("NVIDIA", "Intel", "AMD")
        if g["vendor"] != "NVIDIA":
            # sysfs provides identity only — utilization must be None, not 0
            assert g["util_percent"] is None
            assert g["util_available"] is False


def test_cpu_topology(tmp_path):
    mon, _ = make_monitor(tmp_path)
    topo = mon.cpu_topology()
    assert topo["logical"] == os.cpu_count()
    if topo["physical"] is not None:
        assert topo["physical"] <= topo["logical"]


def test_disks_unchanged_contract(tmp_path):
    mon, _ = make_monitor(tmp_path)
    disks = mon._disks()
    for d in disks:
        assert {"mount", "total", "free", "used", "percent"} <= set(d.keys())

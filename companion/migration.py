"""Versioned data migration for upgrades from older Phantom Network releases.

Run once at companion startup (before any service opens its stores):

1. Backs up every state file it is about to touch into
   ``<data_dir>/migrations/<from-version>/`` (never silently discarded).
2. Applies explicit, idempotent migrations keyed by the recorded
   ``schema_version`` in ``config.json``.
3. Records the new ``schema_version``.

v1.2.2 (schema 1) → v1.3.0 (schema 2) changes:

* ``config.json`` gains the mobile-host, alert, automation, storage-filter
  and notification defaults (added by :mod:`companion.config` itself, but
  the migration records the event and guarantees the backup exists).
* ``known_peers.json`` peers may gain ``kind`` / ``mobile_clients`` fields —
  both are optional and defaulted, so no destructive rewrite is needed.
* ``messages.jsonl`` is forward-compatible: old messages simply lack the
  new optional fields (``reply_to``, ``reactions``, ``edited``, ``deleted``).
"""

from __future__ import annotations

import json
import os
import shutil
import time

from .util import read_json, atomic_write_json

SCHEMA_VERSION = 2

# Files whose content migrations may touch; each is backed up first.
STATE_FILES = ["config.json", "known_peers.json", "messages.jsonl",
               "outbox.json", "mobile_clients.json"]


class MigrationReport:
    def __init__(self):
        self.steps = []
        self.backup_dir = ""
        self.from_version = None
        self.to_version = SCHEMA_VERSION
        self.backed_up = []

    def to_dict(self) -> dict:
        return {
            "from_version": self.from_version,
            "to_version": self.to_version,
            "backup_dir": self.backup_dir,
            "backed_up": self.backed_up,
            "steps": self.steps,
        }


def _detect_from_version(config: dict) -> int:
    try:
        return int(config.get("schema_version") or 1)
    except (TypeError, ValueError):
        return 1


def run_migrations(data_dir: str) -> MigrationReport:
    """Bring ``data_dir`` up to SCHEMA_VERSION.  Safe to run repeatedly."""
    report = MigrationReport()
    config_path = os.path.join(data_dir, "config.json")
    config = read_json(config_path, None) or {}
    report.from_version = _detect_from_version(config)
    if report.from_version >= SCHEMA_VERSION:
        report.steps.append("already current (no-op)")
        # still make sure the version marker exists
        if not config.get("schema_version"):
            config["schema_version"] = SCHEMA_VERSION
            atomic_write_json(config_path, config)
        return report

    # ---- backup -------------------------------------------------------
    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup_dir = os.path.join(data_dir, "migrations",
                              f"v{report.from_version}-{stamp}")
    os.makedirs(backup_dir, exist_ok=True)
    for name in STATE_FILES:
        src = os.path.join(data_dir, name)
        if os.path.exists(src):
            shutil.copy2(src, os.path.join(backup_dir, name))
            report.backed_up.append(name)
    report.backup_dir = os.path.relpath(backup_dir, data_dir)

    # ---- explicit steps -----------------------------------------------
    v = report.from_version
    while v < SCHEMA_VERSION:
        handler = globals().get(f"_migrate_{v}_to_{v + 1}")
        if handler:
            report.steps.append(f"{v} → {v + 1}: " +
                                handler(data_dir, backup_dir))
        else:
            report.steps.append(f"{v} → {v + 1}: no explicit step (defaults)")
        v += 1

    # ---- record the version --------------------------------------------
    config = read_json(config_path, None) or {}
    config["schema_version"] = SCHEMA_VERSION
    atomic_write_json(config_path, config)
    return report


def _migrate_1_to_2(data_dir: str, backup_dir: str) -> str:
    """v1.2.2 → v1.3.0.

    * ensure every stored peer dict keeps its identity (no format rewrite
      needed — new fields are optional and defaulted on load);
    * drop outbox entries that reference message dicts without an ``id``
      (could only appear from an interrupted write);
    * return a human-readable summary for the report.
    """
    dropped = 0
    outbox_path = os.path.join(data_dir, "outbox.json")
    outbox = read_json(outbox_path, None)
    if isinstance(outbox, list):
        cleaned = [e for e in outbox
                   if isinstance(e, dict) and isinstance(e.get("msg"), dict)
                   and e["msg"].get("id")]
        if len(cleaned) != len(outbox):
            dropped = len(outbox) - len(cleaned)
            atomic_write_json(outbox_path, cleaned)
    # verify messages.jsonl stays parseable line-by-line; count bad lines
    bad = 0
    msg_path = os.path.join(data_dir, "messages.jsonl")
    if os.path.exists(msg_path):
        keep = []
        with open(msg_path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                    if isinstance(obj, dict) and isinstance(obj.get("id"), str) \
                            and obj["id"]:
                        keep.append(line)
                    else:
                        bad += 1
                except ValueError:
                    bad += 1
        if bad:
            tmp = msg_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                fh.write("\n".join(keep) + ("\n" if keep else ""))
            os.replace(tmp, msg_path)
    return (f"backed up {len(STATE_FILES)} state file(s); "
            f"dropped {dropped} malformed outbox entr{'y' if dropped == 1 else 'ies'}; "
            f"repaired {bad} unparseable message line(s)")

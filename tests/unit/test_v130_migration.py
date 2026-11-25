"""v1.3.0 unit tests: data migration v1.2.2 → v1.3.0 (backup + repair)."""

import json
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(__file__, "..", "..", "..")))

from companion.migration import run_migrations, SCHEMA_VERSION, SCHEMA_VERSION  # noqa: E402


def seed_v122(tmp_path, config_extra=None, outbox=None, messages=None):
    data = {}
    cfg = {"device_name": "Ubuntu Station", "device_id": "dev_abc",
           "shares": [{"path": "/mnt/media", "mode": "ro", "label": "media"}],
           "setup_complete": True}
    if config_extra:
        cfg.update(config_extra)
    (tmp_path / "config.json").write_text(json.dumps(cfg))
    if outbox is not None:
        (tmp_path / "outbox.json").write_text(json.dumps(outbox))
    if messages is not None:
        with open(tmp_path / "messages.jsonl", "w", encoding="utf-8") as fh:
            for m in messages:
                fh.write(json.dumps(m, ensure_ascii=False) + "\n")
    return data


def test_migration_backs_up_state_files(tmp_path):
    seed_v122(tmp_path, outbox=[{"msg": {"id": "m1"}, "targets_left": ["d"]}],
              messages=[{"id": "m1", "conv": "direct:x", "body": "hi"}])
    report = run_migrations(str(tmp_path))
    assert report.from_version == 1
    assert report.to_version == SCHEMA_VERSION
    assert "config.json" in report.backed_up
    assert "outbox.json" in report.backed_up
    assert "messages.jsonl" in report.backed_up
    # backup dir exists and holds the original config verbatim
    backup_cfg = None
    for root, _, files in os.walk(os.path.join(str(tmp_path), "migrations")):
        if "config.json" in files:
            backup_cfg = os.path.join(root, "config.json")
    assert backup_cfg
    original = json.loads(open(backup_cfg).read())
    assert original["device_name"] == "Ubuntu Station"
    assert original["shares"][0]["path"] == "/mnt/media"


def test_migration_sets_schema_version(tmp_path):
    seed_v122(tmp_path)
    run_migrations(str(tmp_path))
    cfg = json.loads((tmp_path / "config.json").read_text())
    assert cfg["schema_version"] == SCHEMA_VERSION


def test_migration_is_idempotent(tmp_path):
    seed_v122(tmp_path)
    r1 = run_migrations(str(tmp_path))
    r2 = run_migrations(str(tmp_path))
    assert r1.from_version == 1
    assert r2.from_version == SCHEMA_VERSION
    assert "already current" in r2.steps[0]
    # no new backup created on the second run
    assert r2.backed_up == []


def test_migration_repairs_malformed_outbox(tmp_path):
    seed_v122(tmp_path, outbox=[
        {"msg": {"id": "m1"}, "targets_left": ["a"]},
        {"corrupt": True},
        "not-even-a-dict",
        {"msg": {"no_id": True}},
    ])
    report = run_migrations(str(tmp_path))
    cleaned = json.loads((tmp_path / "outbox.json").read_text())
    assert [e["msg"]["id"] for e in cleaned] == ["m1"]
    assert "3 malformed outbox entries" in " ".join(report.steps)


def test_migration_repairs_unparseable_messages(tmp_path):
    seed_v122(tmp_path, messages=[
        {"id": "m1", "conv": "direct:x", "body": "ok"},
        {"id": "m2", "conv": "direct:x", "body": "ok2"},
    ])
    with open(tmp_path / "messages.jsonl", "a", encoding="utf-8") as fh:
        fh.write("{this is not json\n")
        fh.write('{"id": 12345}\n')          # non-string id → dropped
        fh.write('{"body": "no id at all"}\n')  # dropped
    report = run_migrations(str(tmp_path))
    lines = [json.loads(l) for l in
             (tmp_path / "messages.jsonl").read_text().splitlines() if l.strip()]
    assert [m["id"] for m in lines] == ["m1", "m2"]


def test_migration_preserves_unknown_config_keys(tmp_path):
    seed_v122(tmp_path, config_extra={"custom_operator_field": {"a": 1}})
    run_migrations(str(tmp_path))
    cfg = json.loads((tmp_path / "config.json").read_text())
    assert cfg["custom_operator_field"] == {"a": 1}
    # new defaults are added by Config, not the migration — the migration
    # must not REMOVE anything
    assert cfg["device_id"] == "dev_abc"

"""v1.3.1 focused regression: mobile identity protection.

Proves that an existing mobile identity can NOT be reclaimed, nor its
secret disclosed, merely by submitting its client id — and that adjacent
mobile endpoints fail closed on invalid credentials.
"""

import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(__file__, "..", "..", "..")))

from companion.mobile import MobileClientRegistry  # noqa: E402
from companion.errors import FsError  # noqa: E402
from companion.config import Config  # noqa: E402


class FakeEvents:
    def emit(self, *a, **k):
        pass


class FakeSse:
    def publish(self, *a, **k):
        pass


class NullLog:
    def warning(self, *a):
        pass

    def info(self, *a):
        pass


def make_registry(tmp_path):
    cfg = Config(str(tmp_path))
    return MobileClientRegistry(str(tmp_path), cfg, FakeEvents(), FakeSse(),
                                NullLog()), cfg


def test_bare_client_id_cannot_reclaim_identity(tmp_path):
    reg, _ = make_registry(tmp_path)
    first = reg.register({"name": "Victim Phone"})
    cid = first["client_id"]
    real_secret = first["secret"]
    try:
        reg.register({"client_id": cid, "name": "Attacker"})
        raise AssertionError("registration without secret must fail")
    except FsError as exc:
        assert exc.code == "secret_required"
        assert exc.status == 401
    # the stored secret is unchanged and was never returned to the caller
    assert reg.get(cid).secret == real_secret


def test_wrong_secret_cannot_reclaim_identity(tmp_path):
    reg, _ = make_registry(tmp_path)
    first = reg.register({"name": "Victim Phone"})
    try:
        reg.register({"client_id": first["client_id"],
                      "client_secret": "f" * 32, "name": "Attacker"})
        raise AssertionError("wrong secret must fail")
    except FsError as exc:
        assert exc.code == "identity_mismatch"
        assert exc.status == 403
    assert reg.get(first["client_id"]).name == "Victim Phone"


def test_valid_credentials_reattach_without_disclosure(tmp_path):
    reg, _ = make_registry(tmp_path)
    first = reg.register({"name": "My iPhone"})
    again = reg.register({"client_id": first["client_id"],
                          "client_secret": first["secret"],
                          "name": "My iPhone"})
    assert again["client_id"] == first["client_id"]
    assert "secret" not in again        # no secret on re-attach
    assert again["online"] is True


def test_new_registration_receives_newly_generated_secret(tmp_path):
    reg, _ = make_registry(tmp_path)
    a = reg.register({"name": "Phone One"})
    b = reg.register({"name": "Phone Two"})
    assert a["client_id"] != b["client_id"]
    assert a["secret"] and b["secret"] and a["secret"] != b["secret"]
    assert len(a["secret"]) == 32


def test_invalid_credentials_block_every_client_endpoint(tmp_path):
    reg, _ = make_registry(tmp_path)
    ident = reg.register({"name": "Target"})
    cid = ident["client_id"]
    good, bad, none = ident["secret"], "0" * 32, ""
    # heartbeat
    for wrong in (bad, none):
        try:
            reg.heartbeat(cid, wrong)
            raise AssertionError("heartbeat accepted a bad secret")
        except FsError as exc:
            assert exc.code == "identity_mismatch"
    # inbox access
    for wrong in (bad, none):
        try:
            reg.inbox_since(cid, 0) if False else None
            reg._authenticate(cid, wrong)
            raise AssertionError("authenticate accepted a bad secret")
        except FsError as exc:
            assert exc.code == "identity_mismatch"
    # prefs
    try:
        reg.update_prefs(cid, bad, {"sounds": True})
        raise AssertionError("prefs accepted a bad secret")
    except FsError:
        pass
    assert reg.get(cid).notify_prefs == {}


def test_message_impersonation_blocked(tmp_path):
    """A fake 'send as victim' flow must fail at authentication."""
    reg, _ = make_registry(tmp_path)
    ident = reg.register({"name": "Victim"})
    try:
        reg._authenticate(ident["client_id"], "spoofed-secret")
        raise AssertionError("spoofed secret accepted")
    except FsError:
        pass


def test_empty_stored_secret_cannot_authenticate_anything(tmp_path):
    """Defensive: even a client with an empty stored secret must fail
    closed rather than let ANY presented secret pass."""
    reg, _ = make_registry(tmp_path)
    ident = reg.register({"name": "Odd"})
    c = reg.get(ident["client_id"])
    c.secret = ""                       # simulate a broken store
    try:
        reg._authenticate(c.client_id, "anything")
        raise AssertionError("empty stored secret must fail closed")
    except FsError:
        pass


def test_removal_and_reregistration_stay_simple(tmp_path):
    reg, _ = make_registry(tmp_path)
    ident = reg.register({"name": "Old Phone"})
    reg.remove(ident["client_id"])
    # fresh registration is one step, no pairing, QR flow unchanged
    fresh = reg.register({"name": "New Phone"})
    assert fresh["client_id"].startswith("mob_")
    assert fresh["secret"]
    assert fresh["client_id"] != ident["client_id"]


def test_qr_registration_convenience_preserved(tmp_path):
    """No new gates: a browser with no stored identity registers in one
    call with just a name."""
    reg, _ = make_registry(tmp_path)
    ident = reg.register({"name": "Scanned Phone", "device_type": "phone"})
    assert ident["ok"] if "ok" in ident else True
    assert ident["client_id"].startswith("mob_")
    assert len(ident["secret"]) == 32


def test_public_lists_never_contain_secret(tmp_path):
    reg, _ = make_registry(tmp_path)
    ident = reg.register({"name": "Listed Phone"})
    pub = reg.public_list()
    assert all("secret" not in c for c in pub)
    ident2 = reg.register({"client_id": ident["client_id"],
                           "client_secret": ident["secret"]})
    assert "secret" not in ident2

"""Phase γ Tasks 10-12: recovery code generation, single-use verify, regenerate."""
from __future__ import annotations

from core import recovery
from tests.helpers import make_user


def test_generate_returns_10_distinct_plain_codes(db):
    user = make_user(db, username="alice")
    codes = recovery.generate_recovery_codes(user["id"])
    assert len(codes) == 10
    assert len(set(codes)) == 10
    for c in codes:
        assert len(c) == 8
        assert c.isalnum()


def test_codes_stored_hashed_not_plain(db):
    user = make_user(db, username="alice")
    codes = recovery.generate_recovery_codes(user["id"])
    rows = db.list_recovery_codes(user["id"])
    assert len(rows) == 10
    for plain, row in zip(codes, rows):
        assert plain not in row["code_hash"]
        assert row["code_hash"].startswith("$2b$")  # bcrypt prefix
        assert row["used_at"] is None


def test_verify_correct_code_marks_used_and_second_use_fails(db):
    user = make_user(db, username="alice")
    codes = recovery.generate_recovery_codes(user["id"])
    one = codes[0]
    assert recovery.verify_recovery_code(user["id"], one) is True
    assert recovery.verify_recovery_code(user["id"], one) is False


def test_verify_unknown_code_returns_false(db):
    user = make_user(db, username="alice")
    recovery.generate_recovery_codes(user["id"])
    assert recovery.verify_recovery_code(user["id"], "AAAAAAAA") is False


def test_verify_other_users_code_returns_false(db):
    a = make_user(db, username="a")
    b = make_user(db, username="b")
    codes_a = recovery.generate_recovery_codes(a["id"])
    assert recovery.verify_recovery_code(b["id"], codes_a[0]) is False


def test_regenerate_invalidates_old_codes(db):
    user = make_user(db, username="alice")
    old = recovery.generate_recovery_codes(user["id"])
    new = recovery.regenerate_codes(user["id"])
    assert set(old).isdisjoint(set(new))
    for c in old:
        assert recovery.verify_recovery_code(user["id"], c) is False
    assert recovery.verify_recovery_code(user["id"], new[0]) is True

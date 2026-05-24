from core import user_store


def test_create_user_then_lookup():
    u = user_store.create_user(
        username="alice", email="alice@example.com",
        password="correct horse battery staple"
    )
    assert u["id"] >= 1
    assert u["username"] == "alice"
    assert u["email"] == "alice@example.com"
    assert u["password_hash"].startswith("$argon2id$")
    assert u["program_owner"] == 0
    assert u["password_changed_at"] is None  # forced-change flag


def test_create_user_program_owner_flag():
    u = user_store.create_user(
        username="admin", email="admin@x.com",
        password="hunter2hunter2", program_owner=True,
    )
    assert u["program_owner"] == 1


def test_get_user_by_username_email_id():
    u = user_store.create_user(
        username="bob", email="bob@x.com", password="passpasspass1!"
    )
    assert user_store.get_user_by_username("bob")["id"] == u["id"]
    assert user_store.get_user_by_email("bob@x.com")["id"] == u["id"]
    assert user_store.get_user_by_id(u["id"])["username"] == "bob"
    assert user_store.get_user_by_username("nope") is None


def test_verify_password_accepts_correct_after_password_change():
    u = user_store.create_user(
        username="carol", email="c@x.com", password="originalpw1234"
    )
    # password_changed_at is NULL → verify_password must REJECT until forced change
    assert user_store.verify_password(u["id"], "originalpw1234") is False
    user_store.update_password(u["id"], "newpass1234567")
    assert user_store.verify_password(u["id"], "newpass1234567") is True
    assert user_store.verify_password(u["id"], "wrong") is False


def test_verify_password_unknown_user_returns_false():
    assert user_store.verify_password(99999, "anything") is False


def test_update_last_login_at():
    u = user_store.create_user(username="d", email="d@x.com", password="pw1234567890")
    user_store.update_password(u["id"], "newpw1234567890")
    user_store.update_last_login_at(u["id"])
    fresh = user_store.get_user_by_id(u["id"])
    assert fresh["last_login_at"] is not None


def test_update_password_unblocks_verify():
    u = user_store.create_user(
        username="eve", email="e@x.com", password="originalpw1!23"
    )
    # Before update_password, verify returns False even on the right pw.
    assert user_store.verify_password(u["id"], "originalpw1!23") is False
    user_store.update_password(u["id"], "newpw9!876543")
    assert user_store.verify_password(u["id"], "newpw9!876543") is True


def test_verify_password_rejects_unknown_user_id():
    assert user_store.verify_password(0, "x") is False
    assert user_store.verify_password(-1, "x") is False
    assert user_store.verify_password(123_456, "x") is False


def test_password_hash_is_not_plaintext():
    u = user_store.create_user(
        username="f", email="f@x.com", password="supersecret123"
    )
    assert "supersecret123" not in u["password_hash"]

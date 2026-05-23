from core import passwords


def test_pwned_detects_known_passwords():
    assert passwords.is_pwned("password") is True
    assert passwords.is_pwned("123456") is True
    assert passwords.is_pwned("PASSWORD") is True  # case-insensitive
    assert passwords.is_pwned(" password ") is True  # strips


def test_pwned_rejects_unique_strings():
    assert passwords.is_pwned("c0rrect-h0rse-battery-staple-9z") is False


def test_validate_short_password():
    err = passwords.validate_password("short")
    assert err and "12" in err


def test_validate_pwned_password():
    err = passwords.validate_password("password1234")
    assert err is not None and "compromised" in err.lower()


def test_validate_good_password_returns_none():
    err = passwords.validate_password("c0rrect-h0rse-battery-staple-9z")
    assert err is None


def test_validate_non_string():
    err = passwords.validate_password(None)  # type: ignore[arg-type]
    assert err is not None

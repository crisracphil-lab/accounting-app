"""Tests for app/auth.py — password hashing, validation, User dataclass."""
import pytest
from app.auth import (
    User,
    hash_password,
    normalize_role,
    validate_password_strength,
    validate_temporary_password,
    verify_password,
)


# ---------------------------------------------------------------------------
# Password strength validation
# ---------------------------------------------------------------------------

def test_strong_password_passes():
    validate_password_strength("Secure1!")   # should not raise


def test_password_too_short():
    with pytest.raises(ValueError, match="8 characters"):
        validate_password_strength("Ab1!")


def test_password_no_uppercase():
    with pytest.raises(ValueError, match="uppercase"):
        validate_password_strength("secure1!")


def test_password_no_digit():
    with pytest.raises(ValueError, match="number"):
        validate_password_strength("SecurePass!")


def test_password_no_special():
    with pytest.raises(ValueError, match="special character"):
        validate_password_strength("SecurePass1")


# ---------------------------------------------------------------------------
# Temporary password validation (relaxed rules)
# ---------------------------------------------------------------------------

def test_temp_password_min_length():
    validate_temporary_password("abc123")   # exactly 6 chars, should pass


def test_temp_password_too_short():
    with pytest.raises(ValueError):
        validate_temporary_password("ab12")


# ---------------------------------------------------------------------------
# Password hashing + verification
# ---------------------------------------------------------------------------

def test_hash_and_verify_roundtrip():
    pw = "CorrectHorse1!"
    hashed = hash_password(pw)
    assert verify_password(pw, hashed)


def test_wrong_password_fails():
    hashed = hash_password("RightPass1!")
    assert not verify_password("WrongPass1!", hashed)


def test_hash_uses_pbkdf2_scheme():
    hashed = hash_password("Test1234!")
    assert hashed.startswith("pbkdf2_sha256$")


def test_two_hashes_of_same_password_differ():
    """Each hash call uses a fresh random salt."""
    pw = "Unique1!"
    h1 = hash_password(pw)
    h2 = hash_password(pw)
    assert h1 != h2


def test_corrupted_hash_returns_false():
    assert not verify_password("anything", "not-a-valid-hash")


# ---------------------------------------------------------------------------
# normalize_role
# ---------------------------------------------------------------------------

def test_normalize_known_roles():
    assert normalize_role("admin") == "admin"
    assert normalize_role("accountant") == "accountant"
    assert normalize_role("viewer") == "viewer"
    assert normalize_role("department_user") == "department_user"


def test_normalize_unknown_role_defaults_to_accountant():
    assert normalize_role("superuser") == "accountant"
    assert normalize_role("") == "accountant"


def test_normalize_is_case_insensitive():
    assert normalize_role("ADMIN") == "admin"
    assert normalize_role("Accountant") == "accountant"


# ---------------------------------------------------------------------------
# User dataclass
# ---------------------------------------------------------------------------

class _FakeRow:
    """Minimal sqlite3.Row-compatible dict for testing."""
    def __init__(self, data: dict):
        self._data = data

    def __getitem__(self, key):
        return self._data[key]


def _make_fake_row(**overrides):
    defaults = dict(
        id=7,
        username="testuser",
        full_name="Test User",
        email="test@example.com",
        role="accountant",
        is_active=1,
        department_id=None,
        company_id=2,
        is_operations_manager=0,
        notify_operations_approvals=1,
        must_change_password=0,
    )
    defaults.update(overrides)
    return _FakeRow(defaults)


def test_user_from_row_attributes():
    u = User.from_row(_make_fake_row())
    assert u.id == 7
    assert u.username == "testuser"
    assert u.role == "accountant"
    assert u.company_id == 2
    assert not u.is_operations_manager
    assert not u.must_change_password


def test_user_dict_style_access():
    u = User.from_row(_make_fake_row())
    assert u["id"] == 7
    assert u["role"] == "accountant"


def test_user_get_method_existing_key():
    u = User.from_row(_make_fake_row())
    assert u.get("username") == "testuser"


def test_user_get_method_missing_key_returns_default():
    u = User.from_row(_make_fake_row())
    assert u.get("nonexistent_field", "default") == "default"
    assert u.get("nonexistent_field") is None


def test_user_getitem_missing_key_raises_keyerror():
    u = User.from_row(_make_fake_row())
    with pytest.raises(KeyError):
        _ = u["totally_missing"]


def test_user_keys_contains_expected_fields():
    u = User.from_row(_make_fake_row())
    ks = set(u.keys())
    assert {"id", "username", "role", "company_id", "is_active"}.issubset(ks)


def test_user_company_id_defaults_to_1_when_none():
    u = User.from_row(_make_fake_row(company_id=None))
    assert u.company_id == 1


def test_user_role_defaults_to_accountant_when_none():
    u = User.from_row(_make_fake_row(role=None))
    assert u.role == "accountant"


def test_user_is_operations_manager_cast_from_int():
    u_yes = User.from_row(_make_fake_row(is_operations_manager=1))
    u_no  = User.from_row(_make_fake_row(is_operations_manager=0))
    assert u_yes.is_operations_manager is True
    assert u_no.is_operations_manager is False

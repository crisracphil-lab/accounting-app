"""app/services/totp_service.py — TOTP two-factor authentication helpers.

Uses pyotp (RFC 6238 / Google Authenticator compatible).

Public API
----------
generate_totp_secret() -> str
    Generate a fresh base32 TOTP secret.

get_totp_uri(secret, username, issuer) -> str
    Build the otpauth:// URI for QR code generation.

get_qr_data_uri(totp_uri) -> str
    Return a data: URI (PNG) for the QR code so it can be embedded in HTML.

verify_totp(secret, code) -> bool
    Verify a 6-digit code, allowing ±1 window (30-second drift).

generate_backup_codes(n) -> list[str]
    Generate *n* random 10-char alphanumeric backup codes (plaintext).

hash_backup_code(code) -> str
    Hash a single backup code with PBKDF2-HMAC-SHA256.

consume_backup_code(stored_hashes_json, code) -> tuple[bool, str]
    Try to consume a backup code. Returns (success, updated_json).
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import secrets
import string
from typing import Optional

import pyotp

TOTP_ISSUER = "BookPoint"
BACKUP_CODE_COUNT = 8
BACKUP_CODE_LENGTH = 10
_BACKUP_ALPHABET = string.ascii_uppercase + string.digits


# ---------------------------------------------------------------------------
# Secret / URI helpers
# ---------------------------------------------------------------------------

def generate_totp_secret() -> str:
    """Return a fresh base32 TOTP secret (compatible with all authenticator apps)."""
    return pyotp.random_base32()


def get_totp_uri(secret: str, username: str, issuer: str = TOTP_ISSUER) -> str:
    """Build the otpauth:// URI that authenticator apps scan."""
    totp = pyotp.TOTP(secret)
    return totp.provisioning_uri(name=username, issuer_name=issuer)


def get_qr_data_uri(totp_uri: str) -> str:
    """Generate a base64-encoded PNG data URI for the QR code.

    Falls back to a placeholder string if qrcode/Pillow are unavailable.
    """
    try:
        import qrcode  # type: ignore
        from qrcode.image.pure import PyPNGImage  # type: ignore

        qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_L)
        qr.add_data(totp_uri)
        qr.make(fit=True)
        img = qr.make_image(image_factory=PyPNGImage)
        buf = io.BytesIO()
        img.save(buf)
        import base64
        b64 = base64.b64encode(buf.getvalue()).decode()
        return f"data:image/png;base64,{b64}"
    except Exception:
        # If qrcode/Pillow are missing, return empty — the template will show
        # the raw URI as a fallback.
        return ""


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------

def verify_totp(secret: str, code: str) -> bool:
    """Return True if *code* is valid for *secret* (±1 time-step window)."""
    if not secret or not code:
        return False
    totp = pyotp.TOTP(secret)
    # valid_window=1 allows codes from 30 s before / after the current window.
    return totp.verify(code.strip(), valid_window=1)


# ---------------------------------------------------------------------------
# Backup codes
# ---------------------------------------------------------------------------

def generate_backup_codes(n: int = BACKUP_CODE_COUNT) -> list[str]:
    """Generate *n* plaintext backup codes (shown once to the user)."""
    return [
        "".join(secrets.choice(_BACKUP_ALPHABET) for _ in range(BACKUP_CODE_LENGTH))
        for _ in range(n)
    ]


def _hash_code(code: str) -> str:
    """PBKDF2-HMAC-SHA256 hash of a backup code (deterministic salt = code itself)."""
    # We use a fixed salt derived from the code so lookup is possible without
    # storing a per-code salt separately.  Backup codes are high-entropy
    # (10 alphanumeric chars from 36-char alphabet ≈ 51 bits), so this is safe.
    dk = hashlib.pbkdf2_hmac(
        "sha256",
        code.upper().encode(),
        b"bookpoint-backup",
        iterations=100_000,
    )
    return dk.hex()


def hash_backup_codes(codes: list[str]) -> str:
    """Return a JSON string of hashed backup codes suitable for DB storage."""
    return json.dumps([_hash_code(c) for c in codes])


def consume_backup_code(stored_hashes_json: Optional[str], code: str) -> tuple[bool, str]:
    """Attempt to consume *code* from the stored hash list.

    Returns (success, updated_json_string).
    If *success* is True the caller must persist *updated_json_string* to the DB.
    """
    if not stored_hashes_json or not code:
        return False, stored_hashes_json or "[]"
    try:
        hashes: list[str] = json.loads(stored_hashes_json)
    except (json.JSONDecodeError, TypeError):
        return False, "[]"

    candidate = _hash_code(code.strip().upper())
    if candidate in hashes:
        hashes.remove(candidate)
        return True, json.dumps(hashes)
    return False, stored_hashes_json

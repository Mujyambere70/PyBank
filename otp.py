"""
otp.py - two-factor login codes (TOTP, RFC 6238), the same 6-digit codes that
Google Authenticator, Microsoft Authenticator or Authy show.
"""

import base64
import hashlib
import hmac
import secrets
import struct
import time
from urllib.parse import quote, urlencode

PERIOD, DIGITS = 30, 6


def new_secret():
    """160-bit random secret in Base32 (the 'setup key' typed into the app)."""
    return base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")


def _key(secret):
    s = secret.replace(" ", "").upper()
    return base64.b32decode(s + "=" * (-len(s) % 8))


def hotp(secret, counter, digits=DIGITS):
    """RFC 4226: HMAC-SHA1 of the counter -> dynamic truncation -> last 6 digits."""
    mac = hmac.new(_key(secret), struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = mac[-1] & 0x0F
    number = struct.unpack(">I", mac[offset:offset + 4])[0] & 0x7FFFFFFF
    return str(number % 10 ** digits).zfill(digits)


def totp(secret, at=None):
    return hotp(secret, int((time.time() if at is None else at) // PERIOD))


def verify(secret, code, last_step=0, at=None):
    """Return the time step of a valid code (one step of clock drift allowed), or None.
    Steps not newer than `last_step` are refused, so a code can be used only once."""
    code = "".join(ch for ch in str(code or "") if ch.isdigit())
    if not secret or len(code) != DIGITS:
        return None
    step = int((time.time() if at is None else at) // PERIOD)
    for s in (step, step - 1, step + 1):
        if s > (last_step or 0) and hmac.compare_digest(hotp(secret, s), code):
            return s
    return None


def uri(secret, account, issuer="PyBank"):
    """otpauth:// link - shown as a QR code to scan with the authenticator app."""
    label = quote(f"{issuer}:{account}")
    return f"otpauth://totp/{label}?" + urlencode({"secret": secret, "issuer": issuer, "digits": DIGITS, "period": PERIOD})


def grouped(secret):
    return " ".join(secret[i:i + 4] for i in range(0, len(secret), 4))

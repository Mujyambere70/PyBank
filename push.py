"""
push.py - push notifications to phones (mixin class used by Bank).

PyBank can be installed on a phone like an app (a Progressive Web App: static/manifest.webmanifest
and static/sw.js). When the customer allows notifications, the browser gives us a "subscription".
Every alert is then also sent as a push message:

    * the message is encrypted for that phone only (RFC 8291, "aes128gcm" content coding),
    * our server proves who it is with a signed token (VAPID, RFC 8292, ES256 signature).

The keys are made once and kept in the settings table. Library: cryptography.
"""

import base64
import hashlib
import hmac
import json
import os
import struct
import time
import urllib.error
import urllib.request
from urllib.parse import urlparse

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from core import BankError, now

RECORD_SIZE = 4096


def b64u(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def unb64u(text):
    text = str(text)
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _hmac(key, data):
    return hmac.new(key, data, hashlib.sha256).digest()


def new_vapid_keys():
    key = ec.generate_private_key(ec.SECP256R1())
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                            serialization.NoEncryption()).decode()
    public = key.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    return {"private": pem, "public": b64u(public)}


def encrypt(payload, ua_public_b64, auth_b64, salt=None, server_key=None):
    """Encrypt `payload` (bytes) for one browser. Returns the request body (RFC 8291 + RFC 8188)."""
    ua_public = unb64u(ua_public_b64)
    auth = unb64u(auth_b64)
    server_key = server_key or ec.generate_private_key(ec.SECP256R1())
    as_public = server_key.public_key().public_bytes(serialization.Encoding.X962,
                                                     serialization.PublicFormat.UncompressedPoint)
    ua_key = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), ua_public)
    shared = server_key.exchange(ec.ECDH(), ua_key)
    salt = salt or os.urandom(16)
    prk_key = _hmac(auth, shared)
    ikm = _hmac(prk_key, b"WebPush: info\x00" + ua_public + as_public + b"\x01")
    prk = _hmac(salt, ikm)
    cek = _hmac(prk, b"Content-Encoding: aes128gcm\x00\x01")[:16]
    nonce = _hmac(prk, b"Content-Encoding: nonce\x00\x01")[:12]
    if len(payload) > RECORD_SIZE - 17 - 1:
        raise ValueError("push message is too long")
    cipher = AESGCM(cek).encrypt(nonce, payload + b"\x02", None)       # 0x02 = last (and only) record
    header = salt + struct.pack(">I", RECORD_SIZE) + bytes([len(as_public)]) + as_public
    return header + cipher


def vapid_header(endpoint, private_pem, public_b64, subject="mailto:alerts@pybank.example"):
    """Authorization header: a JWT signed with ES256 that says which server sends the message."""
    key = serialization.load_pem_private_key(private_pem.encode(), password=None)
    url = urlparse(endpoint)
    head = b64u(json.dumps({"typ": "JWT", "alg": "ES256"}, separators=(",", ":")).encode())
    claims = b64u(json.dumps({"aud": f"{url.scheme}://{url.netloc}", "exp": int(time.time()) + 12 * 3600,
                              "sub": subject}, separators=(",", ":")).encode())
    der = key.sign(f"{head}.{claims}".encode(), ec.ECDSA(hashes.SHA256()))
    r, s = decode_dss_signature(der)
    signature = b64u(r.to_bytes(32, "big") + s.to_bytes(32, "big"))
    return f"vapid t={head}.{claims}.{signature}, k={public_b64}"


def send(sub, title, body, http=None, url="/netbanking/alerts"):
    """Send one notification. `sub` has endpoint, p256dh, auth and vapid {'private', 'public'}."""
    payload = json.dumps({"title": title, "body": body[:300], "url": url}).encode()
    data = encrypt(payload, sub["p256dh"], sub["auth"])
    req = urllib.request.Request(sub["endpoint"], data=data, method="POST", headers={
        "Content-Encoding": "aes128gcm", "Content-Type": "application/octet-stream", "TTL": "86400",
        "Urgency": "high", "Authorization": vapid_header(sub["endpoint"], sub["vapid"]["private"],
                                                         sub["vapid"]["public"])})
    try:
        with (http or urllib.request.urlopen)(req, timeout=15) as resp:
            if resp.status >= 300:
                raise RuntimeError(f"push service answered {resp.status}")
    except urllib.error.HTTPError as e:
        if e.code in (404, 410):
            raise RuntimeError("gone") from e          # the phone unsubscribed
        raise


class PushMixin:

    def vapid_keys(self):
        s = self.settings()
        if s.get("vapid_private") and s.get("vapid_public"):
            return {"private": s["vapid_private"], "public": s["vapid_public"]}
        keys = new_vapid_keys()
        with self._connect() as conn:                 # the first worker wins; the others read its keys
            conn.execute("INSERT INTO settings (key, value) VALUES ('vapid_private', ?) ON CONFLICT(key) DO NOTHING",
                         (keys["private"],))
            conn.execute("INSERT INTO settings (key, value) VALUES ('vapid_public', ?) ON CONFLICT(key) DO NOTHING",
                         (keys["public"],))
        s = self.settings()
        return {"private": s["vapid_private"], "public": s["vapid_public"]}

    def add_push_subscription(self, cid, sub, device=""):
        try:
            endpoint = str(sub["endpoint"])
            p256dh, auth = str(sub["keys"]["p256dh"]), str(sub["keys"]["auth"])
            if not endpoint.startswith("https://") or len(unb64u(p256dh)) != 65 or len(unb64u(auth)) != 16:
                raise ValueError
        except (KeyError, TypeError, ValueError):
            raise BankError("This browser sent an invalid subscription.")
        with self._connect() as conn:
            conn.execute("DELETE FROM push_subscriptions WHERE endpoint = ?", (endpoint,))
            conn.execute("INSERT INTO push_subscriptions (customer_id, endpoint, p256dh, auth, created_at, device) "
                         "VALUES (?, ?, ?, ?, ?, ?)", (cid, endpoint, p256dh, auth, now(), str(device)[:80]))

    def remove_push_subscription(self, cid, sub_id=None, endpoint=None):
        with self._connect() as conn:
            if sub_id is not None:
                conn.execute("DELETE FROM push_subscriptions WHERE id = ? AND customer_id = ?", (sub_id, cid))
            else:
                conn.execute("DELETE FROM push_subscriptions WHERE endpoint = ? AND customer_id = ?", (endpoint, cid))

    def push_subscriptions(self, cid):
        with self._connect() as conn:
            return [dict(r) for r in conn.execute("SELECT id, device, created_at FROM push_subscriptions "
                                                  "WHERE customer_id = ? ORDER BY id DESC", (cid,))]

    def test_push(self, cid):
        """Send a test notification to every phone of the customer (in the background)."""
        with self._connect() as conn:
            subs = [dict(r) for r in conn.execute("SELECT * FROM push_subscriptions WHERE customer_id = ?", (cid,))]
        if not subs:
            raise BankError("No phone or browser is registered for notifications yet.")
        vapid = self.vapid_keys()
        jobs = [(("push", s["id"]), "push", {**s, "vapid": vapid}, "PyBank", "Test notification: it works!")
                for s in subs]
        self.notifier.deliver(jobs, self._alert_result)
        return len(jobs)

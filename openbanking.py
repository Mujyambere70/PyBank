"""
openbanking.py - open banking with the customer's consent (version 5; "future scope" of version 4).

Another company's app (a budgeting app, an accounting program) may read a customer's account only
when the customer agrees. PyBank does this the way open-banking rules ask, with OAuth 2.0
(RFC 6749) and PKCE (RFC 7636):

  1. Bank staff register the partner app: name, the address it returns to (redirect URI);
     PyBank gives a client ID and a client secret (shown once, stored as SHA-256).
  2. The app sends the customer to  /oauth/authorize?response_type=code&client_id=...&redirect_uri=...
     &scope=accounts transactions&state=...&code_challenge=...&code_challenge_method=S256
  3. The customer signs in to Net Banking, sees what the app wants and agrees (or refuses).
  4. PyBank sends him back with a one-time code (valid 5 minutes).
  5. The app changes the code for an access token at /oauth/token (client secret + code_verifier).
  6. With the token the app reads  /openbanking/v1/accounts  and  /openbanking/v1/accounts/<n>/transactions
     - read only, only what the customer agreed to, for 90 days or until he withdraws the consent
     in Net Banking > Connected apps.
"""

import base64
import hashlib
import secrets
from datetime import datetime, timedelta
from urllib.parse import urlparse

from core import BankError, now

OB_SCHEMA = """
CREATE TABLE IF NOT EXISTS ob_apps (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id    TEXT NOT NULL UNIQUE,
    name         TEXT NOT NULL,
    company      TEXT NOT NULL DEFAULT '',
    redirect_uri TEXT NOT NULL,
    secret_hash  TEXT NOT NULL,
    active       INTEGER NOT NULL DEFAULT 1,
    created_by   TEXT NOT NULL DEFAULT '',
    created_at   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ob_consents (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    customer_id TEXT NOT NULL,
    client_id   TEXT NOT NULL,
    scopes      TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'Active',
    created_at  TEXT NOT NULL,
    expires_at  TEXT NOT NULL,
    revoked_at  TEXT NOT NULL DEFAULT '',
    last_used   TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS ob_codes (
    code_hash      TEXT PRIMARY KEY,
    consent_id     INTEGER NOT NULL,
    redirect_uri   TEXT NOT NULL,
    code_challenge TEXT NOT NULL DEFAULT '',
    expires_at     TEXT NOT NULL,
    used           INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS ob_tokens (
    token_hash TEXT PRIMARY KEY,
    consent_id INTEGER NOT NULL,
    expires_at TEXT NOT NULL
);
"""
SCOPES = {"accounts": "See your accounts and balances",
          "transactions": "See your transactions (the last 12 months)",
          "profile": "See your name and contact details"}
CONSENT_DAYS = 90
TOKEN_MINUTES = 60
CODE_MINUTES = 5


def _hash(value):
    return hashlib.sha256((value or "").encode()).hexdigest()


def _later(**kw):
    return (datetime.now() + timedelta(**kw)).strftime("%Y-%m-%d %H:%M:%S")


def pkce_challenge(verifier):
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")


class OpenBankingMixin:
    """Mixed into Bank."""

    # ===== partner apps (bank staff) =====

    def ob_apps(self):
        with self._connect() as conn:
            return [dict(r) for r in conn.execute(
                "SELECT a.*, (SELECT COUNT(*) FROM ob_consents c WHERE c.client_id = a.client_id AND c.status = 'Active' "
                "AND c.expires_at > ?) AS consents FROM ob_apps a ORDER BY a.active DESC, a.name", (now(),))]

    def ob_app(self, client_id):
        with self._connect() as conn:
            r = conn.execute("SELECT * FROM ob_apps WHERE client_id = ?", (client_id or "",)).fetchone()
        return dict(r) if r else None

    def register_ob_app(self, by, name, company, redirect_uri):
        name = " ".join((name or "").split())[:60]
        company = " ".join((company or "").split())[:80]
        redirect_uri = (redirect_uri or "").strip()
        u = urlparse(redirect_uri)
        if len(name) < 3:
            raise BankError("Please give the app a name.")
        local = u.hostname in ("localhost", "127.0.0.1")
        if not (u.scheme == "https" or (local and u.scheme == "http")) or not u.netloc or u.fragment:
            raise BankError("The redirect address must start with https:// (http:// only for localhost).")
        client_id = "pbk_app_" + secrets.token_hex(8)
        secret = "pbk_secret_" + secrets.token_urlsafe(24)
        with self._connect() as conn:
            conn.execute("INSERT INTO ob_apps (client_id, name, company, redirect_uri, secret_hash, created_by, created_at) "
                         "VALUES (?, ?, ?, ?, ?, ?, ?)",
                         (client_id, name, company, redirect_uri, _hash(secret), by["staff_id"], now()))
            conn.execute("INSERT INTO audit_log (staff_id, action, target, details, at) VALUES (?, ?, ?, ?, ?)",
                         (by["staff_id"], "Open banking app registered", client_id, f"{name} -> {redirect_uri}", now()))
        return client_id, secret

    def set_ob_app_active(self, by, client_id, active):
        with self._connect() as conn:
            if conn.execute("UPDATE ob_apps SET active = ? WHERE client_id = ?", (1 if active else 0, client_id)).rowcount == 0:
                raise BankError("App not found.")
            if not active:
                conn.execute("UPDATE ob_consents SET status = 'Revoked', revoked_at = ? WHERE client_id = ? "
                             "AND status = 'Active'", (now(), client_id))
            conn.execute("INSERT INTO audit_log (staff_id, action, target, details, at) VALUES (?, ?, ?, ?, ?)",
                         (by["staff_id"], "Open banking app " + ("activated" if active else "stopped"), client_id, "", now()))

    # ===== the customer's consent =====

    def check_authorize(self, args):
        """Check the parameters of /oauth/authorize; return (app, scopes)."""
        app = self.ob_app(args.get("client_id"))
        if app is None or not app["active"]:
            raise BankError("This app is not registered with PyBank.")
        if args.get("redirect_uri") != app["redirect_uri"]:
            raise BankError("The return address of the app does not match its registration.")
        if args.get("response_type") != "code":
            raise BankError("Only response_type=code is supported.")
        scopes = [s for s in (args.get("scope") or "").replace(",", " ").split() if s]
        if not scopes or any(s not in SCOPES for s in scopes):
            raise BankError("The app asks for an unknown permission.")
        if args.get("code_challenge") and args.get("code_challenge_method", "S256") != "S256":
            raise BankError("Only the S256 code challenge is supported.")
        return app, scopes

    def give_consent(self, cid, app, scopes, code_challenge=""):
        """The customer agreed: save the consent and return a one-time code."""
        code = secrets.token_urlsafe(24)
        with self._connect() as conn:
            consent_id = conn.execute("INSERT INTO ob_consents (customer_id, client_id, scopes, created_at, expires_at) "
                                      "VALUES (?, ?, ?, ?, ?)",
                                      (cid, app["client_id"], " ".join(scopes), now(), _later(days=CONSENT_DAYS))).lastrowid
            conn.execute("INSERT INTO ob_codes (code_hash, consent_id, redirect_uri, code_challenge, expires_at) "
                         "VALUES (?, ?, ?, ?, ?)", (_hash(code), consent_id, app["redirect_uri"], code_challenge or "",
                                                    _later(minutes=CODE_MINUTES)))
            self._alert(conn, cid, f"You connected {app['name']}",
                        f"You allowed \"{app['name']}\" to read your account ({', '.join(scopes)}) for {CONSENT_DAYS} days. "
                        f"You can withdraw this in Net Banking > Connected apps.", only_in_app=True)
        return code

    def token_for_code(self, form):
        """/oauth/token: code + client secret (+ code_verifier) -> access token."""
        if form.get("grant_type") != "authorization_code":
            raise BankError("unsupported_grant_type")
        app = self.ob_app(form.get("client_id"))
        if app is None or not app["active"] or _hash(form.get("client_secret")) != app["secret_hash"]:
            raise BankError("invalid_client")
        with self._connect() as conn:
            c = conn.execute("SELECT k.*, o.client_id, o.status, o.scopes FROM ob_codes k JOIN ob_consents o "
                             "ON o.id = k.consent_id WHERE k.code_hash = ?", (_hash(form.get("code")),)).fetchone()
            if (c is None or c["used"] or c["expires_at"] < now() or c["client_id"] != app["client_id"]
                    or c["redirect_uri"] != form.get("redirect_uri") or c["status"] != "Active"):
                raise BankError("invalid_grant")
            if c["code_challenge"] and pkce_challenge(form.get("code_verifier") or "") != c["code_challenge"]:
                raise BankError("invalid_grant")
            if conn.execute("UPDATE ob_codes SET used = 1 WHERE code_hash = ? AND used = 0", (c["code_hash"],)).rowcount == 0:
                raise BankError("invalid_grant")
            token = "pbk_ob_" + secrets.token_urlsafe(32)
            conn.execute("INSERT INTO ob_tokens (token_hash, consent_id, expires_at) VALUES (?, ?, ?)",
                         (_hash(token), c["consent_id"], _later(minutes=TOKEN_MINUTES)))
        return {"access_token": token, "token_type": "Bearer", "expires_in": TOKEN_MINUTES * 60, "scope": c["scopes"]}

    def consent_for_token(self, token):
        with self._connect() as conn:
            r = conn.execute("SELECT o.* FROM ob_tokens t JOIN ob_consents o ON o.id = t.consent_id "
                             "JOIN ob_apps a ON a.client_id = o.client_id "
                             "WHERE t.token_hash = ? AND t.expires_at > ? AND o.status = 'Active' AND o.expires_at > ? "
                             "AND a.active = 1", (_hash(token), now(), now())).fetchone()
            if r is None:
                return None
            conn.execute("UPDATE ob_consents SET last_used = ? WHERE id = ?", (now(), r["id"]))
        r = dict(r)
        r["scope_list"] = r["scopes"].split()
        return r

    def consents(self, cid=None):
        sql = ("SELECT o.*, a.name AS app_name, a.company FROM ob_consents o LEFT JOIN ob_apps a "
               "ON a.client_id = o.client_id")
        args = ()
        if cid:
            sql += " WHERE o.customer_id = ?"
            args = (cid,)
        with self._connect() as conn:
            rows = [dict(r) for r in conn.execute(sql + " ORDER BY o.id DESC LIMIT 200", args)]
        for r in rows:
            if r["status"] == "Active" and r["expires_at"] < now():
                r["status"] = "Expired"
        return rows

    def revoke_consent(self, cid, consent_id):
        with self._connect() as conn:
            if conn.execute("UPDATE ob_consents SET status = 'Revoked', revoked_at = ? WHERE id = ? AND customer_id = ? "
                            "AND status = 'Active'", (now(), consent_id, cid)).rowcount == 0:
                raise BankError("Connection not found.")
            conn.execute("DELETE FROM ob_tokens WHERE consent_id = ?", (consent_id,))

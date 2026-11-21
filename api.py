"""
api.py - a JSON API for a mobile app (version 4).

Net Banking is a website; a phone app needs the same data as JSON. Version 4 adds /api/v1:

    POST   /api/v1/token                     customer ID + password (+ 6-digit code when two-factor login is on)
                                             -> a token for the app (valid 30 days)
    DELETE /api/v1/token                     sign the app out (the token stops working)
    GET    /api/v1/me                        the customer
    GET    /api/v1/accounts                  all accounts with balances
    GET    /api/v1/accounts/<n>/transactions the last transactions of one account (?limit=50)
    GET    /api/v1/payees | /alerts | /card
    POST   /api/v1/transfers                 {from, to, amount, pin, remarks} - a transfer inside PyBank
    GET    /api/v1/openapi.json              the description of the API (OpenAPI 3.0)

Every call except /token and /openapi.json needs the header  Authorization: Bearer <token>.
Only a SHA-256 hash of the token is stored, so a stolen database does not give working tokens.
A customer sees his apps in Net Banking -> Security and can sign any of them out. Calls are limited
to 120 a minute per token (and 10 token requests per 10 minutes per IP address).
The API does not use cookies, so it does not need the CSRF token of the web forms.
"""

import hashlib
import secrets
from datetime import datetime, timedelta
from functools import wraps

from flask import Blueprint, jsonify, request

from core import BankError, now

API_SCHEMA = """
CREATE TABLE IF NOT EXISTS api_tokens (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    customer_id TEXT NOT NULL,
    token_hash  TEXT NOT NULL UNIQUE,
    name        TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    expires_at  TEXT NOT NULL,
    last_used   TEXT NOT NULL DEFAULT '',
    revoked     INTEGER NOT NULL DEFAULT 0
);
"""
TOKEN_DAYS = 30


def _hash(token):
    return hashlib.sha256(token.encode()).hexdigest()


class ApiTokensMixin:

    def create_token(self, cid, name):
        token = "pbk_" + secrets.token_urlsafe(32)
        with self._connect() as conn:
            if conn.execute("SELECT COUNT(*) FROM api_tokens WHERE customer_id = ? AND revoked = 0 AND expires_at > ?",
                            (cid, now())).fetchone()[0] >= 5:
                raise BankError("You already have 5 signed-in apps. Sign one out in Net Banking > Security first.")
            conn.execute("INSERT INTO api_tokens (customer_id, token_hash, name, created_at, expires_at) "
                         "VALUES (?, ?, ?, ?, ?)",
                         (cid, _hash(token), " ".join(str(name or "Mobile app").split())[:40] or "Mobile app", now(),
                          (datetime.now() + timedelta(days=TOKEN_DAYS)).strftime("%Y-%m-%d %H:%M:%S")))
            self._alert(conn, cid, "An app signed in to your account",
                        f"\"{name or 'Mobile app'}\" signed in to your PyBank account on {datetime.now():%d-%m-%Y %H:%M}. "
                        f"If this was not you, sign it out in Net Banking > Security.", only_in_app=True)
        return token

    def token_owner(self, token):
        with self._connect() as conn:
            r = conn.execute("SELECT * FROM api_tokens WHERE token_hash = ?", (_hash(token or ""),)).fetchone()
            if r is None or r["revoked"] or r["expires_at"] < now():
                return None
            if r["last_used"] < (datetime.now() - timedelta(minutes=1)).strftime("%Y-%m-%d %H:%M:%S"):
                conn.execute("UPDATE api_tokens SET last_used = ? WHERE id = ?", (now(), r["id"]))
        return dict(r)

    def api_tokens(self, cid):
        with self._connect() as conn:
            return [dict(r) for r in conn.execute("SELECT id, name, created_at, expires_at, last_used FROM api_tokens "
                                                  "WHERE customer_id = ? AND revoked = 0 AND expires_at > ? "
                                                  "ORDER BY id DESC", (cid, now()))]

    def revoke_token(self, cid, token_id=None, token=None):
        with self._connect() as conn:
            if token is not None:
                return conn.execute("UPDATE api_tokens SET revoked = 1 WHERE token_hash = ? AND customer_id = ?",
                                    (_hash(token), cid)).rowcount
            return conn.execute("UPDATE api_tokens SET revoked = 1 WHERE id = ? AND customer_id = ?",
                                (token_id, cid)).rowcount


def openapi():
    def op(summary, auth=True, body=None, params=None):
        o = {"summary": summary, "responses": {"200": {"description": "OK"}, "401": {"description": "No valid token"},
                                               "429": {"description": "Too many requests"}}}
        if auth:
            o["security"] = [{"bearer": []}]
        if body:
            o["requestBody"] = {"required": True, "content": {"application/json": {"schema": {
                "type": "object", "required": [k for k, (_, req) in body.items() if req],
                "properties": {k: {"type": t} for k, (t, _) in body.items()}}}}}
        if params:
            o["parameters"] = params
        return o
    acct = [{"name": "number", "in": "path", "required": True, "schema": {"type": "string"}},
            {"name": "limit", "in": "query", "required": False, "schema": {"type": "integer", "maximum": 200}}]
    return {
        "openapi": "3.0.3",
        "info": {"title": "PyBank API", "version": "1.0",
                 "description": "API of PyBank, a fictional bank for an academic project. Amounts are in the smallest "
                                "unit of the currency (paise, kopeks, cents) and also as text."},
        "servers": [{"url": "/api/v1"}],
        "components": {"securitySchemes": {"bearer": {"type": "http", "scheme": "bearer"}}},
        "paths": {
            "/token": {"post": op("Sign in an app and get a token", False,
                                  {"customer_id": ("string", True), "password": ("string", True),
                                   "code": ("string", False), "device": ("string", False)}),
                       "delete": op("Sign the app out")},
            "/me": {"get": op("The signed-in customer")},
            "/accounts": {"get": op("All accounts with balances")},
            "/accounts/{number}/transactions": {"get": op("Transactions of one account, newest first", params=acct)},
            "/payees": {"get": op("Saved payees")},
            "/alerts": {"get": op("The last alerts")},
            "/card": {"get": op("The debit card and its controls")},
            "/transfers": {"post": op("Transfer money to a PyBank account", body={
                "from": ("string", False), "to": ("string", True), "amount": ("string", True),
                "pin": ("string", True), "remarks": ("string", False)})},
        },
    }


def register(app, bank):
    from bank import parse_amount
    from world import format_money

    api = Blueprint("api", __name__, url_prefix="/api/v1")

    def err(message, status):
        return jsonify({"error": message}), status

    def ip():
        return request.headers.get("Fly-Client-IP") or request.remote_addr or "?"

    def token_required(view):
        @wraps(view)
        def wrapper(*a, **kw):
            auth = request.headers.get("Authorization", "")
            token = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
            owner = bank.token_owner(token) if token else None
            if not owner:
                return err("Please sign in: send a valid token in the Authorization header.", 401)
            if not bank.rate_ok("api", owner["id"]):
                return err("Too many requests. Please wait a minute.", 429)
            try:
                c = bank.get_customer(owner["customer_id"])
            except BankError:
                return err("Customer not found.", 401)
            if c.status != "Active":
                return err("This account is not active.", 403)
            request.api_customer, request.api_token = c, token
            return view(*a, **kw)
        return wrapper

    def account_json(acc):
        return {"number": acc.number, "type": acc.type, "currency": acc.currency, "balance": acc.balance,
                "balance_text": format_money(acc.balance, acc.currency, "en"), "status": acc.status,
                "ifsc": acc.ifsc, "branch": acc.branch}

    @api.post("/token")
    def token():
        if not bank.rate_ok("api_token", ip()):
            return err("Too many sign-in tries. Please wait 10 minutes.", 429)
        data = request.get_json(silent=True) or {}
        try:
            c = bank.authenticate(str(data.get("customer_id", "")), str(data.get("password", "")), ip())
            if c.totp_enabled:
                if not data.get("code"):
                    return err("Two-factor login is on: send the 6-digit code from your authenticator app as \"code\".", 401)
                bank.verify_2fa(c.customer_id, str(data.get("code")))
            t = bank.create_token(c.customer_id, str(data.get("device") or request.headers.get("User-Agent", ""))[:40])
        except BankError as e:
            return err(str(e), 401)
        return jsonify({"token": t, "token_type": "Bearer", "expires_in_days": 30, "customer_id": c.customer_id}), 201

    @api.delete("/token")
    @token_required
    def token_delete():
        bank.revoke_token(request.api_customer.customer_id, token=request.api_token)
        return jsonify({"signed_out": True})

    @api.get("/me")
    @token_required
    def me():
        c = request.api_customer
        return jsonify({"customer_id": c.customer_id, "name": c.full_name, "email": c.email, "mobile": c.mobile,
                        "country": c.country, "language": c.lang, "two_factor": bool(c.totp_enabled),
                        "last_login": c.last_login})

    @api.get("/accounts")
    @token_required
    def accounts():
        return jsonify({"accounts": [account_json(a) for a in bank.accounts_of(request.api_customer.customer_id)
                                     if a.status != "Closed"]})

    @api.get("/accounts/<number>/transactions")
    @token_required
    def transactions(number):
        try:
            acc = bank.account_of(request.api_customer.customer_id, number)
        except BankError as e:
            return err(str(e), 404)
        try:
            limit = max(1, min(200, int(request.args.get("limit", 50))))
        except ValueError:
            limit = 50
        rows = bank.transactions(acc.number, limit=limit)
        return jsonify({"account": acc.number, "currency": acc.currency, "transactions": [
            {"ref": t.ref, "date": t.created_at, "type": t.type, "category": t.category, "description": t.description,
             "amount": t.amount, "amount_text": format_money(t.amount, acc.currency, "en"),
             "balance_after": t.balance_after} for t in rows]})

    @api.get("/payees")
    @token_required
    def payees():
        return jsonify({"payees": [{"id": p["id"], "nickname": p["nickname"], "account": p["account_number"]}
                                   for p in bank.beneficiaries(request.api_customer.customer_id)]})

    @api.get("/alerts")
    @token_required
    def alerts():
        return jsonify({"alerts": [{"subject": a["subject"], "text": a["body"], "at": a["created_at"]}
                                   for a in bank.alerts(request.api_customer.customer_id, 30) if a["channel"] == "in-app"]})

    @api.get("/card")
    @token_required
    def card():
        c = bank.card(request.api_customer.customer_id)
        if not c:
            return err("No debit card.", 404)
        return jsonify({k: c[k] for k in ("last4", "status", "online", "abroad", "contactless", "atm_limit", "pos_limit",
                                          "expires", "currency")})

    @api.post("/transfers")
    @token_required
    def transfer():
        cid = request.api_customer.customer_id
        data = request.get_json(silent=True) or {}
        try:
            src = bank.account_of(cid, data["from"]) if data.get("from") else bank.get_account(cid)
            amount = parse_amount(str(data.get("amount", "")), src.currency)
            t = bank.transfer(cid, str(data.get("to", "")), amount, str(data.get("pin", "")),
                              str(data.get("remarks", "")), from_number=src.number)
        except (BankError, KeyError) as e:
            return err(str(e) if isinstance(e, BankError) else "Please send the field 'from' correctly.", 400)
        return jsonify({"ref": t.ref, "amount": t.amount, "currency": src.currency,
                        "balance_after": t.balance_after, "description": t.description}), 201

    @api.get("/openapi.json")
    def spec():
        return jsonify(openapi())

    @api.errorhandler(404)
    def not_found(e):
        return err("Not found.", 404)

    @api.errorhandler(405)
    def wrong_method(e):
        return err("This method is not allowed here.", 405)

    app.register_blueprint(api)

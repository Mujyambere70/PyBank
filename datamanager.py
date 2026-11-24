"""
datamanager.py - the data manager of the back office (version 5).

The administrator wanted to manage every piece of data of the site from the browser. The data
manager lists the records of the bank's tables, finds them with a search box and lets a person
with the ability "data.manage" change them, add records and delete the ones that may be deleted.

What it never does - on purpose, like a real core-banking system:
  * money: balances, amounts and transactions are read-only (money only moves through an entry
    with a reference, a reversal or a counter operation, so the books always add up);
  * secrets: password and PIN hashes, two-factor secrets and token hashes are never shown;
  * the identity of a record: the customer ID, account number, staff ID or key stays the same.
Every change is written in the audit log with the old and the new value.
"""

import re

from core import BankError

# table -> key column, editable columns, may add / may delete, a short explanation
DATA_TABLES_EDIT = {
    "customers": {"key": "customer_id", "label": "Customers", "add": False, "delete": False,
                  "edit": ["full_name", "dob", "gender", "email", "mobile", "address", "city", "state", "pincode",
                           "occupation", "country", "lang", "alert_email", "alert_sms", "estatement", "pep"],
                  "note": "Personal data. Balances are read-only; to close a customer use the customer page."},
    "accounts": {"key": "number", "label": "Accounts", "add": False, "delete": False,
                 "edit": ["type", "branch", "ifsc", "status"],
                 "note": "The balance is read-only: money moves only with entries that have a reference."},
    "staff": {"key": "staff_id", "label": "Staff", "add": False, "delete": False,
              "edit": ["full_name", "email", "phone", "designation", "address", "dob", "emergency_contact", "branch", "lang"],
              "note": "Access, role and abilities are changed in Staff & roles."},
    "settings": {"key": "key", "label": "Bank settings (raw)", "add": True, "delete": False, "edit": ["value"],
                 "note": "Every setting of the bank as key = value. Wrong values can stop features - use Bank settings when possible."},
    "notices": {"key": "id", "label": "Notices", "add": True, "delete": True,
                "edit": ["title", "body", "level", "active"]},
    "faqs": {"key": "id", "label": "FAQ", "add": True, "delete": True, "edit": ["question", "answer", "category", "position", "active"]},
    "branches": {"key": "id", "label": "Branches & ATMs", "add": True, "delete": True,
                 "edit": ["name", "kind", "address", "city", "country", "ifsc", "phone", "hours", "lat", "lng", "position", "active"]},
    "charges": {"key": "id", "label": "Service charges", "add": True, "delete": True,
                "edit": ["service", "savings_fee", "current_fee", "position", "active"]},
    "billers": {"key": "id", "label": "Billers", "add": True, "delete": False,
                "edit": ["name", "category", "country", "currency", "number_label", "number_regex", "active"]},
    "beneficiaries": {"key": "id", "label": "Customers' payees", "add": False, "delete": True, "edit": ["nickname"]},
    "service_requests": {"key": "id", "label": "Service requests", "add": False, "delete": False,
                         "edit": ["type", "details", "status", "reply"]},
    "goals": {"key": "id", "label": "Savings goals", "add": False, "delete": False, "edit": ["name", "target_date"],
              "note": "The money saved in a goal is read-only."},
    "sanctions_list": {"key": "id", "label": "Sanctions list", "add": True, "delete": True,
                       "edit": ["name", "aliases", "country", "dob", "program", "active"]},
    "ob_apps": {"key": "client_id", "label": "Open banking apps", "add": False, "delete": False,
                "edit": ["name", "company", "redirect_uri", "active"]},
}
HIDDEN = {"password_hash", "pin_hash", "totp_secret", "totp_last_step", "secret_hash", "token_hash", "photo", "data",
          "p256dh", "auth", "endpoint", "payload"}
INT_HINT = re.compile(r"^(active|alert_email|alert_sms|estatement|pep|position|in_footer|published)$")


class DataManager:
    def __init__(self, bank, office):
        self.bank, self.office = bank, office

    def tables(self):
        out = []
        with self.bank._connect() as conn:
            for name, info in DATA_TABLES_EDIT.items():
                out.append({"name": name, "label": info["label"], "note": info.get("note", ""),
                            "count": conn.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0],
                            "add": info["add"], "delete": info["delete"]})
        return out

    def columns(self, table):
        with self.bank._connect() as conn:
            return [c for c in conn.columns(table) if c not in HIDDEN]

    def rows(self, table, q="", limit=200):
        info = DATA_TABLES_EDIT[table]
        cols = self.columns(table)
        sql, args = f"SELECT * FROM {table}", []
        if q:
            text_cols = [c for c in cols if c not in ("lat", "lng")]
            sql += " WHERE " + " OR ".join(f"CAST({c} AS TEXT) LIKE ?" for c in text_cols)
            args = [f"%{q}%"] * len(text_cols)
        with self.bank._connect() as conn:
            rows = conn.execute(sql + f" ORDER BY {info['key']} LIMIT {int(limit)}", args).fetchall()
        return [{c: r[c] for c in cols} for r in rows]

    def row(self, table, key):
        info = DATA_TABLES_EDIT[table]
        with self.bank._connect() as conn:
            r = conn.execute(f"SELECT * FROM {table} WHERE {info['key']} = ?", (self._key(table, key),)).fetchone()
        if r is None:
            raise BankError("Record not found.")
        return {c: r[c] for c in self.columns(table)}

    @staticmethod
    def _key(table, key):
        return int(key) if DATA_TABLES_EDIT[table]["key"] == "id" and str(key).isdigit() else key

    def _value(self, col, old, raw):
        raw = (raw or "").strip()
        if isinstance(old, bool) or isinstance(old, int) or INT_HINT.match(col):
            if raw in ("", "on"):
                raw = "1" if raw == "on" else "0"
            if not re.fullmatch(r"-?\d+", raw):
                raise BankError(f"“{col}” must be a whole number.")
            return int(raw)
        if isinstance(old, float) or col in ("lat", "lng"):
            if raw == "":
                return None
            try:
                return float(raw)
            except ValueError:
                raise BankError(f"“{col}” must be a number.")
        if len(raw) > 2000:
            raise BankError(f"“{col}” is too long.")
        return raw

    def _check(self, table, values):
        if table == "customers":
            if "email" in values and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[a-zA-Z]{2,}", values["email"]):
                raise BankError("Please enter a valid email.")
            if "dob" in values and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", values["dob"]):
                raise BankError("The date of birth must be YYYY-MM-DD.")
            if "full_name" in values and len(values["full_name"]) < 3:
                raise BankError("The name is too short.")
        if table == "accounts" and values.get("status") not in (None, "Active", "Frozen", "Dormant", "Closed", "Pending"):
            raise BankError("Status must be Active, Frozen, Dormant, Closed or Pending.")
        if table == "accounts" and values.get("type") not in (None, "Savings", "Current"):
            raise BankError("Type must be Savings or Current.")
        if table == "ob_apps" and "redirect_uri" in values and not values["redirect_uri"].startswith(("https://", "http://localhost", "http://127.0.0.1")):
            raise BankError("The redirect address must start with https://.")

    def _audit(self, by, action, target, details):
        with self.bank._connect() as conn:
            self.office.audit(conn, by, action, target, details)

    def save(self, by, table, key, form):
        info = DATA_TABLES_EDIT[table]
        old = self.row(table, key)
        values = {c: self._value(c, old.get(c), form.get(c)) for c in info["edit"] if c in old and c in form}
        self._check(table, values)
        changed = {c: v for c, v in values.items() if v != old.get(c)}
        if not changed:
            return
        sets = ", ".join(f"{c} = ?" for c in changed)
        try:
            with self.bank._connect() as conn:
                conn.execute(f"UPDATE {table} SET {sets} WHERE {info['key']} = ?", (*changed.values(), self._key(table, key)))
        except BankError:
            raise
        except Exception as e:
            raise BankError("The database refused the change (" + str(e).split("\n")[0][:80] + ").")
        self._audit(by, "Data manager: record changed", f"{table}/{key}",
                    "; ".join(f"{c}: {str(old.get(c))[:40]} -> {str(v)[:40]}" for c, v in changed.items()))

    def add(self, by, table, form):
        info = DATA_TABLES_EDIT[table]
        if not info["add"]:
            raise BankError("Records of this table are added elsewhere.")
        cols = [c for c in info["edit"] if (form.get(c) or "").strip() != ""]
        if table == "settings":
            key = " ".join((form.get("key") or "").split())
            if not re.fullmatch(r"[a-z0-9_]{2,60}", key):
                raise BankError("A setting name has 2-60 small letters, digits or _.")
            cols, values = ["key", "value"], [key, (form.get("value") or "").strip()[:2000]]
        else:
            if not cols:
                raise BankError("Please fill in the record.")
            values = [self._value(c, None, form.get(c)) for c in cols]
            if "updated_at" in self.columns(table) or table in ("faqs", "branches", "charges", "billers"):
                from core import now
                cols.append("updated_at")
                values.append(now())
            if table == "notices":
                from core import now
                cols += ["created_by", "created_at"]
                values += [by["staff_id"], now()]
            if table == "sanctions_list":
                from core import now
                cols += ["added_by", "added_at"]
                values += [by["staff_id"], now()]
        try:
            with self.bank._connect() as conn:
                cur = conn.execute(f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})", values)
                key = values[0] if info["key"] != "id" else cur.lastrowid
        except Exception as e:
            raise BankError("The database refused the record (" + str(e).split("\n")[0][:80] + ").")
        self._audit(by, "Data manager: record added", f"{table}/{key}", ", ".join(f"{c}={str(v)[:30]}" for c, v in zip(cols, values)))
        return key

    def delete(self, by, table, key):
        info = DATA_TABLES_EDIT[table]
        if not info["delete"]:
            raise BankError("Records of this table cannot be deleted here.")
        old = self.row(table, key)
        with self.bank._connect() as conn:
            conn.execute(f"DELETE FROM {table} WHERE {info['key']} = ?", (self._key(table, key),))
        self._audit(by, "Data manager: record deleted", f"{table}/{key}", str(old)[:250])

"""
admin.py - the bank staff side of PyBank (branch back office).

Class BankAdmin works on the same database as Bank. Two staff roles:
    admin  - everything: customers, money corrections, staff, settings, notices, database, audit log
    staff  - customers, transactions and service requests (no staff management, settings or database)

Financial records are never deleted: a wrong transaction is REVERSED with an opposite entry,
like in a real bank. Every staff action is written to the audit log.
"""

import random
from datetime import date, datetime

from werkzeug.security import check_password_hash, generate_password_hash

from bank import (ACCOUNT_TYPES, DEFAULT_SETTINGS, EMAIL_RE, FD_TENURES, MOBILE_RE, PINCODE_RE,
                  REQUEST_TYPES, BankError, check_password_strength, check_pin_format, format_money, now)

TABLES = ["customers", "accounts", "transactions", "beneficiaries", "fixed_deposits", "tx_notes",
          "service_requests", "login_history", "staff", "notices", "settings", "audit_log"]
SECRET_COLUMNS = {"password_hash", "pin_hash"}
REQUEST_STATUSES = ["Open", "In progress", "Completed", "Rejected"]
NOTICE_LEVELS = ["info", "warning", "offer"]


class BankAdmin:
    def __init__(self, bank):
        self.bank = bank
        self._connect = bank._connect

    def audit(self, conn, staff, action, target="", details=""):
        conn.execute("INSERT INTO audit_log (staff_id, action, target, details, at) VALUES (?, ?, ?, ?, ?)",
                     (staff["staff_id"], action, target, details[:300], now()))

    # ================================================================ staff accounts

    def has_staff(self):
        with self._connect() as conn:
            return conn.execute("SELECT COUNT(*) FROM staff").fetchone()[0] > 0

    def _check_staff_fields(self, name, email, phone):
        name, email, phone = " ".join((name or "").split()), (email or "").strip().lower(), (phone or "").strip()
        if len(name.split()) < 2:
            raise BankError("Please enter the full name (first and last name).")
        if not EMAIL_RE.match(email):
            raise BankError("Please enter a valid email address.")
        if phone and not (phone.lstrip("+").replace(" ", "").isdigit() and 10 <= len(phone.replace(" ", "")) <= 13):
            raise BankError("Please enter a valid phone number.")
        return name, email, phone

    def create_staff(self, by, name, email, phone, designation, role, password, staff_id=None, when=None):
        name, email, phone = self._check_staff_fields(name, email, phone)
        if role not in ("admin", "staff"):
            raise BankError("Unknown role.")
        check_password_strength(password)
        with self._connect() as conn:
            if conn.execute("SELECT 1 FROM staff WHERE email = ?", (email,)).fetchone():
                raise BankError("A staff member with this email already exists.")
            n = conn.execute("SELECT COUNT(*) FROM staff").fetchone()[0] + 1
            sid = staff_id or f"EMP{n:03d}"
            while conn.execute("SELECT 1 FROM staff WHERE staff_id = ?", (sid,)).fetchone():
                n += 1
                sid = f"EMP{n:03d}"
            conn.execute("INSERT INTO staff (staff_id, full_name, email, phone, designation, role, password_hash, "
                         "created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                         (sid, name, email, phone, (designation or "").strip(), role,
                          generate_password_hash(password), when or now()))
            if by:
                self.audit(conn, by, "Staff added", sid, f"{name} as {role}")
        return sid

    def authenticate_staff(self, login, password):
        login = (login or "").strip()
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM staff WHERE staff_id = ? OR email = ?",
                               (login.upper(), login.lower())).fetchone()
            if row is None or not check_password_hash(row["password_hash"], password or ""):
                raise BankError("Invalid staff ID or password.")
            if not row["active"]:
                raise BankError("This staff account is disabled.")
            conn.execute("UPDATE staff SET last_login = ? WHERE id = ?", (now(), row["id"]))
            self.audit(conn, dict(row), "Signed in")
        return dict(row)

    def get_staff(self, sid):
        with self._connect() as conn:
            r = conn.execute("SELECT * FROM staff WHERE staff_id = ?", (sid,)).fetchone()
        return dict(r) if r else None

    def staff_list(self):
        with self._connect() as conn:
            return [dict(r) for r in conn.execute("SELECT * FROM staff ORDER BY role, full_name")]

    def update_staff(self, by, sid, name, email, phone, designation, role=None):
        name, email, phone = self._check_staff_fields(name, email, phone)
        if role is not None and role not in ("admin", "staff"):
            raise BankError("Unknown role.")
        if role and sid == by["staff_id"] and role != by["role"]:
            raise BankError("You cannot change your own role.")
        with self._connect() as conn:
            if conn.execute("SELECT 1 FROM staff WHERE email = ? AND staff_id != ?", (email, sid)).fetchone():
                raise BankError("This email is used by another staff member.")
            conn.execute("UPDATE staff SET full_name = ?, email = ?, phone = ?, designation = ?, "
                         "role = COALESCE(?, role) WHERE staff_id = ?",
                         (name, email, phone, (designation or "").strip(), role, sid))
            self.audit(conn, by, "Staff details changed" if sid != by["staff_id"] else "Own profile changed", sid, name)

    def change_own_password(self, me, old, new):
        if not check_password_hash(self.get_staff(me["staff_id"])["password_hash"], old or ""):
            raise BankError("Current password is wrong.")
        check_password_strength(new)
        with self._connect() as conn:
            conn.execute("UPDATE staff SET password_hash = ? WHERE staff_id = ?",
                         (generate_password_hash(new), me["staff_id"]))
            self.audit(conn, me, "Own password changed")

    def reset_staff_password(self, by, sid, new):
        check_password_strength(new)
        with self._connect() as conn:
            conn.execute("UPDATE staff SET password_hash = ? WHERE staff_id = ?", (generate_password_hash(new), sid))
            self.audit(conn, by, "Staff password reset", sid)

    def toggle_staff(self, by, sid):
        if sid == by["staff_id"]:
            raise BankError("You cannot disable your own account.")
        with self._connect() as conn:
            conn.execute("UPDATE staff SET active = 1 - active WHERE staff_id = ?", (sid,))
            self.audit(conn, by, "Staff enabled/disabled", sid)

    def delete_staff(self, by, sid):
        if sid == by["staff_id"]:
            raise BankError("You cannot delete your own account.")
        with self._connect() as conn:
            if conn.execute("DELETE FROM staff WHERE staff_id = ?", (sid,)).rowcount == 0:
                raise BankError("Staff member not found.")
            self.audit(conn, by, "Staff deleted", sid)

    # ================================================================ overview

    def overview(self):
        today = date.today().isoformat()
        with self._connect() as conn:
            q = lambda sql, *a: conn.execute(sql, a).fetchone()[0]
            return {
                "customers": q("SELECT COUNT(*) FROM customers"),
                "deposits": q("SELECT COALESCE(SUM(balance), 0) FROM accounts"),
                "fd_total": q("SELECT COALESCE(SUM(principal), 0) FROM fixed_deposits WHERE status = 'Active'"),
                "fd_count": q("SELECT COUNT(*) FROM fixed_deposits WHERE status = 'Active'"),
                "tx_today": q("SELECT COUNT(*) FROM transactions WHERE created_at >= ?", today),
                "in_today": q("SELECT COALESCE(SUM(amount), 0) FROM transactions WHERE type = 'CR' AND created_at >= ?", today),
                "out_today": q("SELECT COALESCE(SUM(amount), 0) FROM transactions WHERE type = 'DR' AND created_at >= ?", today),
                "frozen": q("SELECT COUNT(*) FROM accounts WHERE status != 'Active'"),
                "locked": q("SELECT COUNT(*) FROM customers WHERE locked_until > ?", now()),
                "open_requests": q("SELECT COUNT(*) FROM service_requests WHERE status IN ('Open', 'In progress')"),
                "failed_logins": q("SELECT COUNT(*) FROM login_history WHERE success = 0 AND at >= ?", today),
            }

    def recent_transactions(self, q="", kind="", category="", start="", end="", limit=100):
        sql = ("SELECT t.*, c.full_name, c.customer_id FROM transactions t JOIN accounts a ON a.number = t.account_number "
               "JOIN customers c ON c.customer_id = a.customer_id WHERE 1 = 1")
        args = []
        if q:
            sql += " AND (t.ref LIKE ? OR t.description LIKE ? OR c.full_name LIKE ? OR t.account_number LIKE ?)"
            args += [f"%{q}%"] * 4
        if kind in ("CR", "DR"):
            sql += " AND t.type = ?"
            args.append(kind)
        if category:
            sql += " AND t.category = ?"
            args.append(category)
        if start:
            sql += " AND t.created_at >= ?"
            args.append(start + " 00:00:00")
        if end:
            sql += " AND t.created_at <= ?"
            args.append(end + " 23:59:59")
        sql += " ORDER BY t.created_at DESC, t.id DESC LIMIT ?"
        args.append(limit)
        with self._connect() as conn:
            return [dict(r) for r in conn.execute(sql, args)]

    def categories(self):
        with self._connect() as conn:
            return [r[0] for r in conn.execute("SELECT DISTINCT category FROM transactions ORDER BY category")]

    # ================================================================ customers

    def customers(self, q="", status=""):
        sql = ("SELECT c.*, a.number, a.type AS account_type, a.balance, a.status AS account_status, "
               "(SELECT COUNT(*) FROM service_requests r WHERE r.customer_id = c.customer_id AND r.status = 'Open') "
               "AS open_requests FROM customers c JOIN accounts a ON a.customer_id = c.customer_id WHERE 1 = 1")
        args = []
        if q:
            sql += " AND (c.full_name LIKE ? OR c.customer_id LIKE ? OR c.email LIKE ? OR c.mobile LIKE ? OR a.number LIKE ?)"
            args += [f"%{q}%"] * 5
        if status == "frozen":
            sql += " AND a.status != 'Active'"
        elif status == "locked":
            sql += " AND c.locked_until > ?"
            args.append(now())
        sql += " ORDER BY c.full_name"
        with self._connect() as conn:
            return [dict(r) for r in conn.execute(sql, args)]

    def update_customer(self, by, cid, f):
        name = " ".join((f.get("full_name") or "").split())
        if len(name.split()) < 2:
            raise BankError("Please enter the full name (first and last name).")
        try:
            born = datetime.strptime(f.get("dob", ""), "%Y-%m-%d").date()
        except ValueError:
            raise BankError("Please enter a valid date of birth.")
        if (date.today() - born).days < 18 * 365:
            raise BankError("Customer must be at least 18 years old.")
        if f.get("gender") not in ("Male", "Female", "Other"):
            raise BankError("Please select the gender.")
        email, mobile = (f.get("email") or "").strip().lower(), (f.get("mobile") or "").strip()
        if not EMAIL_RE.match(email):
            raise BankError("Please enter a valid email address.")
        if not MOBILE_RE.match(mobile):
            raise BankError("Please enter a valid 10-digit mobile number.")
        if not PINCODE_RE.match((f.get("pincode") or "").strip()):
            raise BankError("Please enter a valid 6-digit PIN code.")
        for k in ("address", "city", "state", "occupation"):
            if not (f.get(k) or "").strip():
                raise BankError("Please fill in the full address and occupation.")
        with self._connect() as conn:
            if conn.execute("SELECT 1 FROM customers WHERE (email = ? OR mobile = ?) AND customer_id != ?",
                            (email, mobile, cid)).fetchone():
                raise BankError("This email or mobile number is used by another customer.")
            conn.execute("UPDATE customers SET full_name = ?, dob = ?, gender = ?, email = ?, mobile = ?, address = ?, "
                         "city = ?, state = ?, pincode = ?, occupation = ? WHERE customer_id = ?",
                         (name.title(), born.isoformat(), f["gender"], email, mobile, " ".join(f["address"].split()),
                          f["city"].strip().title(), f["state"].strip(), f["pincode"].strip(), f["occupation"].strip(), cid))
            self.audit(conn, by, "Customer details changed", cid, name)

    def unlock_customer(self, by, cid):
        with self._connect() as conn:
            conn.execute("UPDATE customers SET failed_logins = 0, locked_until = '' WHERE customer_id = ?", (cid,))
            self.audit(conn, by, "Login unlocked", cid)

    def reset_customer_password(self, by, cid, new):
        check_password_strength(new)
        with self._connect() as conn:
            conn.execute("UPDATE customers SET password_hash = ?, failed_logins = 0, locked_until = '' "
                         "WHERE customer_id = ?", (generate_password_hash(new), cid))
            self.audit(conn, by, "Customer password reset", cid)

    def reset_customer_pin(self, by, cid, new_pin):
        check_pin_format(new_pin)
        with self._connect() as conn:
            conn.execute("UPDATE customers SET pin_hash = ? WHERE customer_id = ?", (generate_password_hash(new_pin), cid))
            self.audit(conn, by, "Transaction PIN reset", cid)

    def set_account_status(self, by, cid, status, reason=""):
        if status not in ("Active", "Frozen"):
            raise BankError("Unknown status.")
        with self._connect() as conn:
            conn.execute("UPDATE accounts SET status = ? WHERE customer_id = ?", (status, cid))
            self.audit(conn, by, "Account frozen" if status == "Frozen" else "Account activated", cid, reason)

    def change_account_type(self, by, cid, new_type):
        if new_type not in ACCOUNT_TYPES:
            raise BankError("Unknown account type.")
        with self._connect() as conn:
            conn.execute("UPDATE accounts SET type = ? WHERE customer_id = ?", (new_type, cid))
            self.audit(conn, by, "Account type changed", cid, new_type)

    def delete_customer(self, by, cid):
        """Close and remove a customer. Allowed only when the balance is zero and no FD is active."""
        acc = self.bank.get_account(cid)
        if acc.balance != 0:
            raise BankError(f"Balance is {format_money(acc.balance)}. Pay it out first (manual debit), then delete.")
        with self._connect() as conn:
            if conn.execute("SELECT 1 FROM fixed_deposits WHERE customer_id = ? AND status = 'Active'", (cid,)).fetchone():
                raise BankError("The customer still has an active fixed deposit.")
            name = conn.execute("SELECT full_name FROM customers WHERE customer_id = ?", (cid,)).fetchone()[0]
            for sql in ("DELETE FROM beneficiaries WHERE customer_id = ?",
                        "DELETE FROM beneficiaries WHERE account_number = ?",
                        "DELETE FROM tx_notes WHERE customer_id = ?",
                        "DELETE FROM service_requests WHERE customer_id = ?",
                        "DELETE FROM fixed_deposits WHERE customer_id = ?",
                        "DELETE FROM login_history WHERE customer_id = ?",
                        "DELETE FROM transactions WHERE account_number = ?",
                        "DELETE FROM accounts WHERE customer_id = ?",
                        "DELETE FROM customers WHERE customer_id = ?"):
                conn.execute(sql, (acc.number if "account_number" in sql else cid,))
            self.audit(conn, by, "Customer deleted", cid, name)

    # ================================================================ money corrections

    def manual_entry(self, by, cid, kind, amount, description):
        description = " ".join((description or "").split())
        if kind not in ("CR", "DR"):
            raise BankError("Choose credit or debit.")
        if len(description) < 5:
            raise BankError("Please write a clear reason (at least 5 characters).")
        acc = self.bank.get_account(cid)
        t = self.bank.post(acc.number, kind, "Branch", f"BRANCH/{description.upper()}/BY {by['staff_id']}", amount, now())
        with self._connect() as conn:
            self.audit(conn, by, "Manual credit" if kind == "CR" else "Manual debit", cid,
                       f"{format_money(amount)} - {description} (ref {t.ref})")
        return t

    def reverse_transaction(self, by, tx_id, reason):
        reason = " ".join((reason or "").split())
        if len(reason) < 5:
            raise BankError("Please write the reason for the reversal.")
        with self._connect() as conn:
            t = conn.execute("SELECT * FROM transactions WHERE id = ?", (tx_id,)).fetchone()
            if t is None:
                raise BankError("Transaction not found.")
            if t["reversed"] or t["category"] == "Reversal":
                raise BankError("This transaction was already reversed (or is itself a reversal).")
            if t["category"] in ("Transfer", "Fixed deposit"):
                raise BankError("Transfers and fixed deposits involve two records - correct them with a manual entry.")
        kind = "DR" if t["type"] == "CR" else "CR"
        rev = self.bank.post(t["account_number"], kind, "Reversal", f"REVERSAL OF {t['ref']}/{reason.upper()}",
                             t["amount"], now())
        with self._connect() as conn:
            conn.execute("UPDATE transactions SET reversed = 1 WHERE id = ?", (tx_id,))
            self.audit(conn, by, "Transaction reversed", t["ref"], f"{format_money(t['amount'])} - {reason}")
        return rev

    # ================================================================ service requests

    def requests(self, status=""):
        sql = ("SELECT r.*, c.full_name FROM service_requests r JOIN customers c USING (customer_id)")
        args = []
        if status:
            sql += " WHERE r.status = ?"
            args.append(status)
        sql += " ORDER BY CASE r.status WHEN 'Open' THEN 0 WHEN 'In progress' THEN 1 ELSE 2 END, r.id DESC"
        with self._connect() as conn:
            return [dict(r) for r in conn.execute(sql, args)]

    def answer_request(self, by, rid, status, reply):
        if status not in REQUEST_STATUSES:
            raise BankError("Unknown status.")
        reply = (reply or "").strip()[:500]
        with self._connect() as conn:
            if conn.execute("UPDATE service_requests SET status = ?, reply = ?, replied_by = ?, updated_at = ? "
                            "WHERE id = ?", (status, reply, by["full_name"], now(), rid)).rowcount == 0:
                raise BankError("Request not found.")
            self.audit(conn, by, "Service request answered", f"#{rid}", f"{status}: {reply[:80]}")

    def delete_request(self, by, rid):
        with self._connect() as conn:
            conn.execute("DELETE FROM service_requests WHERE id = ?", (rid,))
            self.audit(conn, by, "Service request deleted", f"#{rid}")

    # ================================================================ notices

    def notices(self):
        with self._connect() as conn:
            return [dict(r) for r in conn.execute("SELECT * FROM notices ORDER BY id DESC")]

    def save_notice(self, by, title, body, level, active, notice_id=None):
        title, body = " ".join((title or "").split()), (body or "").strip()
        if not 3 <= len(title) <= 80 or not 5 <= len(body) <= 400:
            raise BankError("Notice needs a title (3-80) and a text (5-400 characters).")
        level = level if level in NOTICE_LEVELS else "info"
        with self._connect() as conn:
            if notice_id:
                conn.execute("UPDATE notices SET title = ?, body = ?, level = ?, active = ? WHERE id = ?",
                             (title, body, level, 1 if active else 0, notice_id))
                self.audit(conn, by, "Notice changed", f"#{notice_id}", title)
            else:
                conn.execute("INSERT INTO notices (title, body, level, active, created_by, created_at) "
                             "VALUES (?, ?, ?, ?, ?, ?)", (title, body, level, 1 if active else 0, by["full_name"], now()))
                self.audit(conn, by, "Notice published", "", title)

    def delete_notice(self, by, notice_id):
        with self._connect() as conn:
            conn.execute("DELETE FROM notices WHERE id = ?", (notice_id,))
            self.audit(conn, by, "Notice deleted", f"#{notice_id}")

    # ================================================================ settings

    def save_settings(self, by, form):
        values = {}
        try:
            for key in ("daily_withdraw_limit", "daily_transfer_limit"):
                rupees = int(str(form.get(key, "")).replace(",", ""))
                if not 1000 <= rupees <= 10_000_000:
                    raise ValueError
                values[key] = str(rupees * 100)
            for m in FD_TENURES:
                rate = float(form.get(f"fd_rate_{m}", ""))
                if not 0 < rate <= 15:
                    raise ValueError
                values[f"fd_rate_{m}"] = f"{rate:.2f}"
        except ValueError:
            raise BankError("Limits must be ₹1,000 - ₹1,00,00,000 and FD rates between 0 and 15 %.")
        values["online_account_opening"] = "1" if form.get("online_account_opening") else "0"
        with self._connect() as conn:
            for k, v in values.items():
                conn.execute("INSERT INTO settings (key, value) VALUES (?, ?) "
                             "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (k, v))
            self.audit(conn, by, "Bank settings changed", "",
                       ", ".join(f"{k}={v}" for k, v in values.items() if v != DEFAULT_SETTINGS.get(k)))

    # ================================================================ database & audit

    def table(self, name, limit=300):
        if name not in TABLES:
            raise BankError("Unknown table.")
        with self._connect() as conn:
            cur = conn.execute(f"SELECT * FROM {name} ORDER BY rowid DESC LIMIT ?", (limit,))
            cols = [c[0] for c in cur.description if c[0] not in SECRET_COLUMNS]
            rows = [[r[c] for c in cols] for r in cur.fetchall()]
            total = conn.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]
        return cols, rows, total

    def table_counts(self):
        with self._connect() as conn:
            return {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in TABLES}

    def audit_log(self, limit=200):
        with self._connect() as conn:
            return [dict(r) for r in conn.execute(
                "SELECT a.*, s.full_name FROM audit_log a LEFT JOIN staff s USING (staff_id) "
                "ORDER BY a.id DESC LIMIT ?", (limit,))]


def random_password():
    return f"Pb@{random.randint(100000, 999999)}"

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

import json

import otp
import world
from bank import (ACCOUNT_TYPES, DEFAULT_SETTINGS, EMAIL_RE, FD_TENURES, LOAN_PRODUCTS, REQUEST_TYPES, BankError,
                  check_password_strength, check_pin_format, format_money, now)

TABLES = ["customers", "accounts", "transactions", "cash_requests", "loans", "loan_payments", "beneficiaries",
          "fixed_deposits", "tx_notes", "service_requests", "alerts", "login_history", "interest_runs", "staff",
          "notices", "settings", "audit_log", "backups", "documents", "profile_changes", "external_transfers",
          "fx_rates", "statements", "push_subscriptions"]
SECRET_COLUMNS = {"password_hash", "pin_hash", "totp_secret", "data", "p256dh", "auth", "endpoint"}
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
            by_cur = lambda sql: [(r[0], r[1]) for r in conn.execute(sql) if r[1]]       # noqa: E731
            return {
                "customers": q("SELECT COUNT(*) FROM customers WHERE status = 'Active'"),
                "deposits": by_cur("SELECT currency, SUM(balance) FROM accounts GROUP BY currency ORDER BY currency"),
                "countries": q("SELECT COUNT(DISTINCT country) FROM customers WHERE status = 'Active'"),
                "pending_customers": q("SELECT COUNT(*) FROM customers WHERE status = 'Pending'"),
                "pending_profile": q("SELECT COUNT(*) FROM profile_changes WHERE status = 'Pending'"),
                "external_processing": q("SELECT COUNT(*) FROM external_transfers WHERE status = 'Processing'"),
                "fd_total": by_cur("SELECT a.currency, SUM(f.principal) FROM fixed_deposits f JOIN accounts a "
                                   "USING (customer_id) WHERE f.status = 'Active' GROUP BY a.currency ORDER BY a.currency"),
                "fd_count": q("SELECT COUNT(*) FROM fixed_deposits WHERE status = 'Active'"),
                "tx_today": q("SELECT COUNT(*) FROM transactions WHERE created_at >= ?", today),
                "in_today": q("SELECT COALESCE(SUM(amount), 0) FROM transactions WHERE type = 'CR' AND created_at >= ?", today),
                "out_today": q("SELECT COALESCE(SUM(amount), 0) FROM transactions WHERE type = 'DR' AND created_at >= ?", today),
                "frozen": q("SELECT COUNT(*) FROM accounts WHERE status = 'Frozen'"),
                "locked": q("SELECT COUNT(*) FROM customers WHERE locked_until > ?", now()),
                "open_requests": q("SELECT COUNT(*) FROM service_requests WHERE status IN ('Open', 'In progress')"),
                "failed_logins": q("SELECT COUNT(*) FROM login_history WHERE success = 0 AND at >= ?", today),
                "pending_cash": q("SELECT COUNT(*) FROM cash_requests WHERE status = 'Pending'"),
                "pending_loans": q("SELECT COUNT(*) FROM loans WHERE status = 'Pending'"),
                "active_loans": q("SELECT COUNT(*) FROM loans WHERE status = 'Active'"),
                "loan_book": by_cur("SELECT a.currency, SUM(l.outstanding) FROM loans l JOIN accounts a "
                                    "ON a.number = l.account_number WHERE l.status = 'Active' GROUP BY a.currency"),
            }

    def recent_transactions(self, q="", kind="", category="", start="", end="", limit=100):
        sql = ("SELECT t.*, c.full_name, c.customer_id, a.currency FROM transactions t "
               "JOIN accounts a ON a.number = t.account_number "
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
        sql = ("SELECT c.*, a.number, a.type AS account_type, a.balance, a.status AS account_status, a.currency, "
               "(SELECT COUNT(*) FROM service_requests r WHERE r.customer_id = c.customer_id AND r.status = 'Open') "
               "AS open_requests FROM customers c JOIN accounts a ON a.customer_id = c.customer_id WHERE 1 = 1")
        args = []
        if q:
            sql += " AND (c.full_name LIKE ? OR c.customer_id LIKE ? OR c.email LIKE ? OR c.mobile LIKE ? OR a.number LIKE ?)"
            args += [f"%{q}%"] * 5
        if status == "frozen":
            sql += " AND a.status = 'Frozen'"
        elif status in ("Pending", "Rejected"):
            sql += " AND c.status = ?"
            args.append(status)
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
        if not (f.get("occupation") or "").strip():
            raise BankError("Please fill in the full address and occupation.")
        email, mobile, address, city, state, pincode, country = self.bank.check_contact(
            cid, f.get("email"), f.get("mobile"), f.get("address"), f.get("city"), f.get("state"), f.get("pincode"),
            f.get("country"))
        with self._connect() as conn:
            conn.execute("UPDATE customers SET full_name = ?, dob = ?, gender = ?, email = ?, mobile = ?, address = ?, "
                         "city = ?, state = ?, pincode = ?, occupation = ?, country = ? WHERE customer_id = ?",
                         (name.title(), born.isoformat(), f["gender"], email, mobile, address, city, state, pincode,
                          f["occupation"].strip(), country, cid))
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
            raise BankError(f"Balance is {format_money(acc.balance, acc.currency)}. Pay it out first (manual debit), "
                            f"then delete.")
        with self._connect() as conn:
            if conn.execute("SELECT 1 FROM fixed_deposits WHERE customer_id = ? AND status = 'Active'", (cid,)).fetchone():
                raise BankError("The customer still has an active fixed deposit.")
            if conn.execute("SELECT 1 FROM loans WHERE customer_id = ? AND status = 'Active'", (cid,)).fetchone():
                raise BankError("The customer still has an active loan.")
            name = conn.execute("SELECT full_name FROM customers WHERE customer_id = ?", (cid,)).fetchone()[0]
            for sql in ("DELETE FROM loan_payments WHERE loan_id IN (SELECT id FROM loans WHERE customer_id = ?)",
                        "DELETE FROM loans WHERE customer_id = ?",
                        "DELETE FROM cash_requests WHERE customer_id = ?",
                        "DELETE FROM documents WHERE customer_id = ?",
                        "DELETE FROM profile_changes WHERE customer_id = ?",
                        "DELETE FROM external_transfers WHERE customer_id = ?",
                        "DELETE FROM statements WHERE customer_id = ?",
                        "DELETE FROM push_subscriptions WHERE customer_id = ?",
                        "DELETE FROM alerts WHERE customer_id = ?",
                        "DELETE FROM beneficiaries WHERE customer_id = ?",
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
                       f"{format_money(amount, acc.currency)} - {description} (ref {t.ref})")
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
            cur = conn.execute("SELECT currency FROM accounts WHERE number = ?", (t["account_number"],)).fetchone()[0]
            self.audit(conn, by, "Transaction reversed", t["ref"], f"{format_money(t['amount'], cur)} - {reason}")
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
            for key in ["savings_rate", "late_fee_percent"] + [f"loan_rate_{p}" for p in LOAN_PRODUCTS]:
                rate = float(form.get(key, DEFAULT_SETTINGS[key]))
                if not 0 <= rate <= 30:
                    raise ValueError
                values[key] = f"{rate:.2f}"
        except ValueError:
            raise BankError("Limits must be ₹1,000 - ₹1,00,00,000, FD rates between 0 and 15 % "
                            "and savings / loan rates between 0 and 30 %.")
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
            cols_all = conn.columns(name)
            order = "id" if "id" in cols_all else cols_all[0]
            cur = conn.execute(f"SELECT * FROM {name} ORDER BY {order} DESC LIMIT ?", (limit,))
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


class BackOffice(BankAdmin):
    """New back-office work in version 2: approvals, loans, two-factor login, interest, backups."""

    # ================================================================ deposit / withdrawal approvals

    def cash_requests(self, status="Pending", limit=200):
        sql = ("SELECT r.*, c.full_name, a.balance, a.currency FROM cash_requests r "
               "JOIN customers c ON c.customer_id = r.customer_id "
               "JOIN accounts a ON a.number = r.account_number")
        args = []
        if status:
            sql += " WHERE r.status = ?"
            args.append(status)
        sql += " ORDER BY r.id DESC LIMIT ?"
        args.append(limit)
        with self._connect() as conn:
            return [dict(r) for r in conn.execute(sql, args)]

    def decide_cash(self, by, rid, approve, reason=""):
        r, t = self.bank.decide_cash_request(rid, approve, by["full_name"], reason)
        with self._connect() as conn:
            self.audit(conn, by, f"{r['kind']} approved" if approve else f"{r['kind']} rejected", r["customer_id"],
                       f"{r['ref']} {r['amount'] / 100:.2f}" + (f" (tx {t.ref})" if t else f" - {reason}"))
        return r, t

    # ================================================================ loans

    def loans(self, status=""):
        sql = ("SELECT l.*, c.full_name, a.currency FROM loans l JOIN customers c ON c.customer_id = l.customer_id "
               "JOIN accounts a ON a.number = l.account_number")
        args = []
        if status:
            sql += " WHERE l.status = ?"
            args.append(status)
        sql += " ORDER BY CASE l.status WHEN 'Pending' THEN 0 WHEN 'Active' THEN 1 ELSE 2 END, l.id DESC"
        with self._connect() as conn:
            rows = [dict(r) for r in conn.execute(sql, args)]
        today = date.today().isoformat()
        for ln in rows:
            ln["overdue"] = ln["status"] == "Active" and ln["next_due"] < today
            ln["report"] = json.loads(ln["credit_report"]) if ln["credit_report"] else None
            ln["documents"] = self.bank.documents(ln["customer_id"], f"loan:{ln['id']}")
        return rows

    def decide_loan(self, by, loan_id, approve, reason=""):
        reason = " ".join((reason or "").split())[:200]
        if not approve and len(reason) < 3:
            raise BankError("Please write the reason for rejecting the loan.")
        with self._connect() as conn:
            ln = conn.execute("SELECT * FROM loans WHERE id = ?", (loan_id,)).fetchone()
            if ln is None:
                raise BankError("Loan not found.")
            ln = dict(ln)
            cur = conn.execute("SELECT currency FROM accounts WHERE number = ?", (ln["account_number"],)).fetchone()[0]
            if conn.execute("UPDATE loans SET status = ?, decided_at = ?, decided_by = ?, reason = ? "
                            "WHERE id = ? AND status = 'Pending'",
                            ("Approved" if approve else "Rejected", now(), by["full_name"], reason, loan_id)).rowcount == 0:
                raise BankError(f"This loan application was already handled ({ln['status']}).")
            t = None
            if approve:
                t = self.bank.disburse_loan(conn, ln, now())
            else:
                self.bank._alert(conn, ln["customer_id"], f"Loan application {ln['loan_number']} not approved",
                                 f"Your loan application {ln['loan_number']} for {format_money(ln['principal'], cur, 'en')} "
                                 f"was not approved. Reason: {reason}")
            self.audit(conn, by, "Loan approved and paid out" if approve else "Loan rejected", ln["customer_id"],
                       f"{ln['loan_number']} {format_money(ln['principal'], cur, 'en')}" + (f" (tx {t.ref})" if t else f" - {reason}"))
        return t

    # ================================================================ two-factor login

    def reset_customer_2fa(self, by, cid):
        with self._connect() as conn:
            conn.execute("UPDATE customers SET totp_secret = '', totp_enabled = 0, totp_last_step = 0 "
                         "WHERE customer_id = ?", (cid,))
            self.audit(conn, by, "Two-factor login reset", cid, "customer lost the phone / app")

    def enable_staff_2fa(self, me, secret, code):
        step = otp.verify(secret, code)
        if step is None:
            raise BankError("The code is not correct. Check the time on your phone and type the newest code.")
        with self._connect() as conn:
            conn.execute("UPDATE staff SET totp_secret = ?, totp_enabled = 1, totp_last_step = ? WHERE staff_id = ?",
                         (secret, step, me["staff_id"]))
            self.audit(conn, me, "Two-factor login turned on")

    def verify_staff_2fa(self, sid, code):
        s = self.get_staff(sid)
        step = otp.verify(s["totp_secret"], code, s["totp_last_step"]) if s else None
        if step is None:
            raise BankError("Wrong or expired code. Type the 6-digit code shown in your authenticator app.")
        with self._connect() as conn:
            conn.execute("UPDATE staff SET totp_last_step = ? WHERE staff_id = ?", (step, sid))

    def disable_staff_2fa(self, me, password, code):
        if not check_password_hash(self.get_staff(me["staff_id"])["password_hash"], password or ""):
            raise BankError("Password is wrong.")
        self.verify_staff_2fa(me["staff_id"], code)
        with self._connect() as conn:
            conn.execute("UPDATE staff SET totp_secret = '', totp_enabled = 0, totp_last_step = 0 WHERE staff_id = ?",
                         (me["staff_id"],))
            self.audit(conn, me, "Two-factor login turned off")

    def reset_staff_2fa(self, by, sid):
        with self._connect() as conn:
            conn.execute("UPDATE staff SET totp_secret = '', totp_enabled = 0, totp_last_step = 0 WHERE staff_id = ?", (sid,))
            self.audit(conn, by, "Staff two-factor login reset", sid)

    # ================================================================ savings interest

    def run_interest(self, by):
        results = self.bank.post_quarterly_interest()
        with self._connect() as conn:
            self.audit(conn, by, "Savings interest run", "", "; ".join(
                f"{r['quarter']}: {r['accounts']} accounts" for r in results) or "nothing due")
        return results

    # ================================================================ backups

    def create_backup(self, by):
        bid, blob = self.bank.save_backup("manual", by["full_name"])
        with self._connect() as conn:
            self.audit(conn, by, "Backup created", f"#{bid}", f"{len(blob):,} bytes")
        return bid

    def restore(self, by, blob, password):
        if not check_password_hash(self.get_staff(by["staff_id"])["password_hash"], password or ""):
            raise BankError("Your password is wrong - nothing was restored.")
        data = self.bank.unpack(blob)
        self.bank.save_backup("before restore", by["full_name"])     # safety copy of the data we replace
        counts = self.bank.import_data(data)
        with self._connect() as conn:
            self.audit(conn, by, "Database restored from backup", "", f"backup of {data.get('created_at', '?')}: "
                       f"{counts.get('customers', 0)} customers, {counts.get('transactions', 0)} transactions")
        return counts


class InternationalOffice(BackOffice):
    """Version 3: account applications, changes of personal data, payments to other banks, exchange rates."""

    def decide_application(self, by, cid, approve, reason=""):
        row = self.bank.decide_customer(cid, approve, by["full_name"], reason)
        with self._connect() as conn:
            self.audit(conn, by, "Account application approved" if approve else "Account application rejected",
                       cid, row["full_name"] + ("" if approve else f" - {reason}"))
        return row

    def decide_profile(self, by, pid, approve, reason=""):
        r = self.bank.decide_profile_change(pid, approve, by["full_name"], reason)
        with self._connect() as conn:
            self.audit(conn, by, "Personal data change approved" if approve else "Personal data change rejected",
                       r["customer_id"], ", ".join(r["changes"]) + ("" if approve else f" - {reason}"))
        return r

    def save_fx(self, by, form):
        rates = {}
        for cur in world.CURRENCIES:
            value = (form.get(f"fx_{cur}") or "").strip().replace(",", ".")
            if value:
                try:
                    rate = float(value)
                except ValueError:
                    raise BankError(f"Exchange rate for {cur} must be a number.")
                if not 0 < rate < 1_000_000:
                    raise BankError(f"Exchange rate for {cur} must be greater than 0.")
                rates[cur] = rate
        if not rates:
            raise BankError("Type at least one exchange rate.")
        n = self.bank.save_fx_rates(rates, f"typed by {by['staff_id']}")
        with self._connect() as conn:
            self.audit(conn, by, "Exchange rates changed", "", ", ".join(f"{k}={v}" for k, v in rates.items()))
        return n

    def refresh_fx(self, by):
        n = self.bank.refresh_fx_rates()
        with self._connect() as conn:
            self.audit(conn, by, "Exchange rates downloaded", "", f"{n} currencies")
        return n

    def save_currency_limits(self, by, form):
        values = {}
        for cur in world.CURRENCIES:
            if cur == "INR":
                continue
            for kind in ("withdraw", "transfer"):
                raw = (form.get(f"limit_{kind}_{cur}") or "").replace(",", "").strip()
                if not raw:
                    continue
                try:
                    units = int(raw)
                except ValueError:
                    raise BankError("Daily limits must be whole numbers.")
                if not 1 <= units <= 10 ** 10:
                    raise BankError("Daily limits must be greater than 0.")
                values[f"limit_{kind}_{cur}"] = str(units * 100)
        with self._connect() as conn:
            for k, v in values.items():
                self.bank.save_setting(conn, k, v)
            if values:
                self.audit(conn, by, "Currency limits changed", "", ", ".join(f"{k}={int(v) // 100}" for k, v in values.items()))


def random_password():
    return f"Pb@{random.randint(100000, 999999)}"

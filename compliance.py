"""
compliance.py - suspicious-activity flags, dormant accounts and the four-eyes rule (version 4).

1. Suspicious-activity flags (anti-money-laundering, AML). After every transaction a few simple rules
   look at the account; a rule that fires opens a flag for the compliance officer:
       LARGE_CASH     one cash deposit or withdrawal at or above the reporting amount of the currency
       STRUCTURING    several cash deposits in 7 days, each below the reporting amount, together above it
       PASS_THROUGH   money that arrives and leaves again within 24 hours (at least 90 % of it)
       NEW_PAYEES     5 or more transfers to different accounts within one hour
   The officer clears a flag (with a note) or reports it; a report makes a short text file for the
   financial intelligence unit (sandbox - nothing is sent). One open flag per rule, account and day.
   The rules are examples for a study project, not a legal standard.

2. Dormant accounts. An account without any transaction made by the customer for DORMANT_MONTHS
   (interest and charges do not count) becomes "Dormant": money cannot move until a bank officer
   checks the customer again and activates it. This protects forgotten accounts from fraud.

3. Four-eyes rule (maker-checker). A manual credit or debit, or a reversal, at or above the checker
   amount is not posted at once: it waits until a SECOND staff member with the "checker" ability
   approves it. Nobody can approve his own entry.
"""

import json
from datetime import date, datetime, timedelta

import world
from core import BankError, now
from world import format_money

COMPLIANCE_SCHEMA = """
CREATE TABLE IF NOT EXISTS aml_flags (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    customer_id    TEXT NOT NULL,
    account_number TEXT NOT NULL,
    rule           TEXT NOT NULL,
    details        TEXT NOT NULL,
    amount         INTEGER NOT NULL DEFAULT 0,
    tx_ref         TEXT NOT NULL DEFAULT '',
    day            TEXT NOT NULL,
    status         TEXT NOT NULL DEFAULT 'Open',
    created_at     TEXT NOT NULL,
    reviewed_by    TEXT NOT NULL DEFAULT '',
    reviewed_at    TEXT NOT NULL DEFAULT '',
    note           TEXT NOT NULL DEFAULT '',
    report         TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_aml_open ON aml_flags(status, id);
CREATE TABLE IF NOT EXISTS pending_entries (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    kind        TEXT NOT NULL,
    customer_id TEXT NOT NULL DEFAULT '',
    tx_id       INTEGER,
    entry_type  TEXT NOT NULL DEFAULT '',
    amount      INTEGER NOT NULL,
    currency    TEXT NOT NULL,
    description TEXT NOT NULL,
    made_by     TEXT NOT NULL,
    made_at     TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'Waiting',
    checked_by  TEXT NOT NULL DEFAULT '',
    checked_at  TEXT NOT NULL DEFAULT '',
    reason      TEXT NOT NULL DEFAULT '',
    tx_ref      TEXT NOT NULL DEFAULT ''
);
"""
RULES = {
    "LARGE_CASH": "Large cash transaction",
    "STRUCTURING": "Cash split into smaller amounts",
    "PASS_THROUGH": "Money in and out again within 24 hours",
    "NEW_PAYEES": "Many transfers to different accounts in one hour",
}
FLAG_STATUSES = ["Open", "Cleared", "Reported"]
CUSTOMER_CATEGORIES = ("Deposit", "Withdrawal", "Transfer", "Bill payment", "Card payment", "Exchange",
                       "Fixed deposit", "Loan EMI")
DORMANT_MONTHS = 24


def aml_amount(currency):
    return world.policy(currency, "aml")


def checker_amount(currency):
    return world.policy(currency, "checker")


class ComplianceMixin:
    """Methods of Bank."""

    # ===== AML rules (run after every saved transaction, inside the same database transaction) =====

    def _flag(self, conn, acc_row, rule, details, amount, tx_ref, day):
        if conn.execute("SELECT 1 FROM aml_flags WHERE account_number = ? AND rule = ? AND day = ? AND status = 'Open'",
                        (acc_row["number"], rule, day)).fetchone():
            return False
        conn.execute("INSERT INTO aml_flags (customer_id, account_number, rule, details, amount, tx_ref, day, created_at) "
                     "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                     (acc_row["customer_id"], acc_row["number"], rule, details[:300], amount, tx_ref, day, now()))
        return True

    def _check_aml(self, conn, t):
        if not self.alerts_on or t.category in ("Interest", "Reversal", "Refund", "Charges"):
            return
        acc = conn.execute("SELECT number, customer_id, currency FROM accounts WHERE number = ?",
                           (t.account_number,)).fetchone()
        if acc is None:
            return
        cur, day = acc["currency"], t.created_at[:10]
        limit = aml_amount(cur)
        cash = t.category in ("Deposit", "Withdrawal") and "CASH" in t.description.upper()
        if cash and t.amount >= limit:
            self._flag(conn, acc, "LARGE_CASH", f"Cash {('deposit' if t.type == 'CR' else 'withdrawal')} of "
                       f"{format_money(t.amount, cur, 'en')} (reporting amount {format_money(limit, cur, 'en')}).",
                       t.amount, t.ref, day)
        if cash and t.type == "CR" and t.amount < limit:
            since = (datetime.strptime(t.created_at, "%Y-%m-%d %H:%M:%S") - timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S")
            rows = conn.execute("SELECT amount FROM transactions WHERE account_number = ? AND type = 'CR' AND category = "
                                "'Deposit' AND description LIKE '%CASH%' AND created_at >= ?", (acc["number"], since)).fetchall()
            total = sum(r[0] for r in rows)
            if len(rows) >= 3 and total >= limit and all(r[0] < limit for r in rows):
                self._flag(conn, acc, "STRUCTURING", f"{len(rows)} cash deposits in 7 days, each below "
                           f"{format_money(limit, cur, 'en')}, together {format_money(total, cur, 'en')}.",
                           total, t.ref, day)
        if t.type == "DR" and t.category in ("Transfer", "Withdrawal"):
            since = (datetime.strptime(t.created_at, "%Y-%m-%d %H:%M:%S") - timedelta(hours=24)).strftime("%Y-%m-%d %H:%M:%S")
            ins = conn.execute("SELECT COALESCE(SUM(amount), 0) FROM transactions WHERE account_number = ? AND type = 'CR' "
                               "AND category NOT IN ('Interest', 'Refund', 'Reversal', 'Exchange') AND created_at >= ?",
                               (acc["number"], since)).fetchone()[0]
            outs = conn.execute("SELECT COALESCE(SUM(amount), 0) FROM transactions WHERE account_number = ? AND type = 'DR' "
                                "AND category IN ('Transfer', 'Withdrawal') AND created_at >= ?",
                                (acc["number"], since)).fetchone()[0]
            if ins >= limit // 2 and outs * 10 >= ins * 9:
                self._flag(conn, acc, "PASS_THROUGH", f"{format_money(ins, cur, 'en')} came in and "
                           f"{format_money(outs, cur, 'en')} went out within 24 hours.", outs, t.ref, day)
        if t.type == "DR" and t.category == "Transfer":
            since = (datetime.strptime(t.created_at, "%Y-%m-%d %H:%M:%S") - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
            descr = [r[0] for r in conn.execute("SELECT description FROM transactions WHERE account_number = ? AND type = 'DR' "
                                                "AND category = 'Transfer' AND created_at >= ?", (acc["number"], since))]
            targets = {d.split("/A/c ")[1][:6] if "/A/c " in d else d.split("/")[1][:20] for d in descr if "/" in d}
            if len(targets) >= 5:
                self._flag(conn, acc, "NEW_PAYEES", f"{len(descr)} transfers to {len(targets)} different accounts "
                           f"within one hour.", t.amount, t.ref, day)

    def aml_flags(self, status="Open", limit=200):
        sql = ("SELECT f.*, c.full_name, a.currency FROM aml_flags f JOIN customers c ON c.customer_id = f.customer_id "
               "JOIN accounts a ON a.number = f.account_number")
        args = []
        if status:
            sql += " WHERE f.status = ?"
            args.append(status)
        with self._connect() as conn:
            return [dict(r) for r in conn.execute(sql + " ORDER BY f.id DESC LIMIT ?", (*args, limit))]

    def aml_flag(self, flag_id):
        with self._connect() as conn:
            r = conn.execute("SELECT f.*, c.full_name, a.currency FROM aml_flags f JOIN customers c "
                             "ON c.customer_id = f.customer_id JOIN accounts a ON a.number = f.account_number "
                             "WHERE f.id = ?", (flag_id,)).fetchone()
        if r is None:
            raise BankError("Flag not found.")
        return dict(r)

    def review_flag(self, staff, flag_id, action, note):
        note = " ".join((note or "").split())[:500]
        if len(note) < 5:
            raise BankError("Please write a short note on what you checked.")
        if action not in ("clear", "report"):
            raise BankError("Unknown action.")
        f = self.aml_flag(flag_id)
        report = ""
        if action == "report":
            report = (f"SUSPICIOUS TRANSACTION REPORT (sandbox - not sent)\n"
                      f"Reporting bank: PyBank, {world.BRANCHES['IN']['name']}\n"
                      f"Date: {now()}\nCustomer: {f['full_name']} ({f['customer_id']})\n"
                      f"Account: {f['account_number']} ({f['currency']})\n"
                      f"Rule: {RULES.get(f['rule'], f['rule'])}\nDetails: {f['details']}\n"
                      f"Transaction: {f['tx_ref']}\nOfficer's note: {note}\nOfficer: {staff['full_name']} ({staff['staff_id']})\n")
        with self._connect() as conn:
            if conn.execute("UPDATE aml_flags SET status = ?, reviewed_by = ?, reviewed_at = ?, note = ?, report = ? "
                            "WHERE id = ? AND status = 'Open'",
                            ("Cleared" if action == "clear" else "Reported", staff["staff_id"], now(), note, report,
                             flag_id)).rowcount == 0:
                raise BankError("This flag was already reviewed.")
            conn.execute("INSERT INTO audit_log (staff_id, action, target, details, at) VALUES (?, ?, ?, ?, ?)",
                         (staff["staff_id"], "AML flag " + ("cleared" if action == "clear" else "reported"),
                          str(flag_id), note[:200], now()))
        return report

    # ===== dormant accounts =====

    def dormant_candidates(self, today=None, months=DORMANT_MONTHS):
        """Active accounts with no customer transaction for `months` months (and opened before that)."""
        today = today or date.today()
        y, m = divmod(today.month - 1 - months, 12)
        cut = today.replace(year=today.year + y, month=m + 1, day=min(today.day, 28)).isoformat()
        marks = ", ".join("?" * len(CUSTOMER_CATEGORIES))
        with self._connect() as conn:
            return [dict(r) for r in conn.execute(
                f"SELECT a.number, a.customer_id, a.currency, a.balance, a.opened_at, "
                f"(SELECT MAX(created_at) FROM transactions t WHERE t.account_number = a.number AND t.category IN ({marks})) last "
                f"FROM accounts a WHERE a.status = 'Active' AND a.opened_at < ?",
                (*CUSTOMER_CATEGORIES, cut)) if (r["last"] or "") < cut]

    def mark_dormant(self, today=None):
        n = 0
        for a in self.dormant_candidates(today):
            with self._connect() as conn:
                if conn.execute("UPDATE accounts SET status = 'Dormant' WHERE number = ? AND status = 'Active'",
                                (a["number"],)).rowcount:
                    self._alert(conn, a["customer_id"], f"A/c XX{a['number'][-4:]} is now dormant",
                                f"Your account XX{a['number'][-4:]} was not used for {DORMANT_MONTHS} months, so it is "
                                f"now dormant for your safety. Your money is safe and still earns interest. Visit a "
                                f"branch with your ID, or ask for activation in Net Banking > Service Requests.")
                    n += 1
        return n

    def dormant_accounts(self):
        """Dormant accounts with the date of the last transaction made by the customer (interest does not count)."""
        marks = ", ".join("?" * len(CUSTOMER_CATEGORIES))
        with self._connect() as conn:
            return [dict(r) for r in conn.execute(
                f"SELECT a.*, c.full_name, c.email, (SELECT MAX(created_at) FROM transactions t WHERE "
                f"t.account_number = a.number AND t.category IN ({marks})) last FROM accounts a "
                f"JOIN customers c ON c.customer_id = a.customer_id WHERE a.status = 'Dormant' ORDER BY a.number",
                CUSTOMER_CATEGORIES)]

    def activate_dormant(self, staff, number, note):
        note = " ".join((note or "").split())[:300]
        if len(note) < 5:
            raise BankError("Please write how the customer was checked (e.g. passport seen at the branch).")
        with self._connect() as conn:
            r = conn.execute("SELECT customer_id FROM accounts WHERE number = ? AND status = 'Dormant'", (number,)).fetchone()
            if r is None:
                raise BankError("This account is not dormant.")
            conn.execute("UPDATE accounts SET status = 'Active' WHERE number = ?", (number,))
            conn.execute("INSERT INTO audit_log (staff_id, action, target, details, at) VALUES (?, ?, ?, ?, ?)",
                         (staff["staff_id"], "Dormant account activated", number, note, now()))
            self._alert(conn, r["customer_id"], f"A/c XX{number[-4:]} is active again",
                        f"Your account XX{number[-4:]} was activated by the bank. You can use it again.")

    # ===== four-eyes rule =====

    def pending_entries(self, status="Waiting"):
        with self._connect() as conn:
            return [dict(r) for r in conn.execute(
                "SELECT p.*, c.full_name FROM pending_entries p LEFT JOIN customers c ON c.customer_id = p.customer_id "
                "WHERE p.status = ? ORDER BY p.id DESC", (status,))]


class CheckerOffice:
    """Mixed into the back-office class: large manual entries and reversals wait for a second person."""

    def _needs_checker(self, amount, currency):
        return amount >= checker_amount(currency)

    def _pend(self, by, kind, customer_id, tx_id, entry_type, amount, currency, description):
        with self._connect() as conn:
            pid = conn.execute("INSERT INTO pending_entries (kind, customer_id, tx_id, entry_type, amount, currency, "
                               "description, made_by, made_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                               (kind, customer_id, tx_id, entry_type, amount, currency, description, by["staff_id"],
                                now())).lastrowid
            self.audit(conn, by, "Sent for second approval", customer_id or str(tx_id),
                       f"{kind} {format_money(amount, currency)} - {description}")
        return {"pending": pid}

    def manual_entry(self, by, cid, kind, amount, description, checked=False):
        acc = self.bank.get_account(cid)
        if not checked and self._needs_checker(amount, acc.currency):
            description = " ".join((description or "").split())
            if kind not in ("CR", "DR"):
                raise BankError("Choose credit or debit.")
            if len(description) < 5:
                raise BankError("Please write a clear reason (at least 5 characters).")
            return self._pend(by, "Manual entry", cid, None, kind, amount, acc.currency, description)
        return super().manual_entry(by, cid, kind, amount, description)

    def reverse_transaction(self, by, tx_id, reason, checked=False):
        if not checked:
            with self._connect() as conn:
                t = conn.execute("SELECT t.*, a.currency, a.customer_id FROM transactions t JOIN accounts a "
                                 "ON a.number = t.account_number WHERE t.id = ?", (tx_id,)).fetchone()
            if t is not None and self._needs_checker(t["amount"], t["currency"]):
                reason = " ".join((reason or "").split())
                if len(reason) < 5:
                    raise BankError("Please write the reason for the reversal.")
                if t["reversed"] or t["category"] in ("Reversal", "Transfer", "Fixed deposit"):
                    return super().reverse_transaction(by, tx_id, reason)          # gives the usual error
                return self._pend(by, "Reversal", t["customer_id"], tx_id, "", t["amount"], t["currency"],
                                  f"{t['ref']}: {reason}")
        return super().reverse_transaction(by, tx_id, reason)

    def decide_entry(self, by, pid, approve, reason=""):
        with self._connect() as conn:
            p = conn.execute("SELECT * FROM pending_entries WHERE id = ?", (pid,)).fetchone()
        if p is None:
            raise BankError("Entry not found.")
        p = dict(p)
        if p["status"] != "Waiting":
            raise BankError("This entry was already decided.")
        if p["made_by"] == by["staff_id"]:
            raise BankError("The four-eyes rule: another staff member must approve your entry.")
        reason = " ".join((reason or "").split())[:200]
        if not approve and len(reason) < 3:
            raise BankError("Please write why you reject the entry.")
        maker = self.get_staff(p["made_by"]) or {"staff_id": p["made_by"]}
        with self._connect() as conn:            # claim it first, so it can never be posted twice
            if conn.execute("UPDATE pending_entries SET status = ?, checked_by = ?, checked_at = ?, reason = ? "
                            "WHERE id = ? AND status = 'Waiting'",
                            ("Approved" if approve else "Rejected", by["staff_id"], now(), reason, pid)).rowcount == 0:
                raise BankError("This entry was already decided.")
        if not approve:
            with self._connect() as conn:
                self.audit(conn, by, "Second approval refused", p["customer_id"] or str(p["tx_id"]), reason)
            return None
        try:
            if p["kind"].startswith("Counter"):                           # v5: counter work (teller.py)
                op = self.run_counter_pending(maker, by, p)
                with self._connect() as conn:
                    conn.execute("UPDATE pending_entries SET tx_ref = ? WHERE id = ?", (op["tx_ref"], pid))
                    self.audit(conn, by, "Second approval given", p["customer_id"], f"receipt {op['receipt_no']}")
                return op
            if p["kind"] == "Manual entry":
                t = self.manual_entry(maker, p["customer_id"], p["entry_type"], p["amount"], p["description"], checked=True)
            else:
                t = self.reverse_transaction(maker, p["tx_id"], p["description"].split(": ", 1)[-1], checked=True)
        except BankError:
            with self._connect() as conn:
                conn.execute("UPDATE pending_entries SET status = 'Waiting', checked_by = '', checked_at = '' WHERE id = ?",
                             (pid,))
            raise
        with self._connect() as conn:
            conn.execute("UPDATE pending_entries SET tx_ref = ? WHERE id = ?", (t.ref, pid))
            self.audit(conn, by, "Second approval given", p["customer_id"] or str(p["tx_id"]), f"ref {t.ref}")
        return t

    def entries_log(self, limit=50):
        with self._connect() as conn:
            return [dict(r) for r in conn.execute(
                "SELECT p.*, c.full_name FROM pending_entries p LEFT JOIN customers c ON c.customer_id = p.customer_id "
                "WHERE p.status != 'Waiting' ORDER BY p.id DESC LIMIT ?", (limit,))]

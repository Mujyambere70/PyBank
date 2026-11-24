"""
teller.py - the branch counter: bank workers do the customer's banking for him (version 5).

Until version 4 a worker could only approve requests that the customer made in Net Banking, or post
a "manual entry". At a real branch the customer stands at the counter and the teller does the work:

    cash deposit, cash withdrawal (identity document checked), transfer to a PyBank account,
    payment to another bank, exchange between the customer's currency accounts, fixed or recurring
    deposit, loan EMI payment

Every operation needs its own ability (roles.py), is written in the audit log, gets a counter
receipt and sends the customer the usual alert. Cash always goes through the worker's cash drawer
(till): he opens it in the morning with the cash he received, every deposit adds to it and every
withdrawal takes from it, and in the evening he counts the cash. A difference must be accepted by a
supervisor. Large amounts - above the bank's four-eyes amount or the worker's own limit - wait for a
second worker (Second approvals), exactly like large manual entries.
"""

import json
import random
from datetime import date

from compliance import checker_amount
from core import COUNTER, BankError, now
from world import format_money

TELLER_SCHEMA = """
CREATE TABLE IF NOT EXISTS tills (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    staff_id    TEXT NOT NULL,
    day         TEXT NOT NULL,
    currency    TEXT NOT NULL,
    opening     INTEGER NOT NULL DEFAULT 0,
    cash_in     INTEGER NOT NULL DEFAULT 0,
    cash_out    INTEGER NOT NULL DEFAULT 0,
    counted     INTEGER,
    difference  INTEGER NOT NULL DEFAULT 0,
    status      TEXT NOT NULL DEFAULT 'Open',
    opened_at   TEXT NOT NULL,
    closed_at   TEXT NOT NULL DEFAULT '',
    note        TEXT NOT NULL DEFAULT '',
    accepted_by TEXT NOT NULL DEFAULT '',
    UNIQUE (staff_id, day, currency)
);
CREATE TABLE IF NOT EXISTS counter_ops (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    receipt_no     TEXT NOT NULL UNIQUE,
    kind           TEXT NOT NULL,
    customer_id    TEXT NOT NULL,
    account_number TEXT NOT NULL,
    amount         INTEGER NOT NULL,
    currency       TEXT NOT NULL,
    tx_ref         TEXT NOT NULL DEFAULT '',
    details        TEXT NOT NULL DEFAULT '',
    staff_id       TEXT NOT NULL,
    approved_by    TEXT NOT NULL DEFAULT '',
    created_at     TEXT NOT NULL
);
"""
OPS = {
    "deposit": ("Cash deposit", "counter.deposit"),
    "withdraw": ("Cash withdrawal", "counter.withdraw"),
    "transfer": ("Transfer within PyBank", "counter.transfer"),
    "external": ("Payment to another bank", "counter.external"),
    "fx": ("Exchange between own accounts", "counter.fx"),
    "fd": ("Fixed deposit", "counter.deposits"),
    "rd": ("Recurring deposit", "counter.deposits"),
    "emi": ("Loan EMI", "counter.loans"),
}
ID_DOCUMENTS = ["Passport", "National ID card", "Driving licence", "Aadhaar card", "PAN card", "Residence permit"]
TILL_STATUSES = ["Open", "Closed", "Difference", "Accepted"]


class TellerMixin:
    """Mixed into the back-office class (Office); self.bank is the Bank."""

    # ===== the cash drawer =====

    def till(self, staff_id, currency, day=None):
        with self._connect() as conn:
            r = conn.execute("SELECT * FROM tills WHERE staff_id = ? AND day = ? AND currency = ?",
                             (staff_id, day or date.today().isoformat(), currency)).fetchone()
        return self._till_row(r)

    @staticmethod
    def _till_row(r):
        if r is None:
            return None
        t = dict(r)
        t["expected"] = t["opening"] + t["cash_in"] - t["cash_out"]
        return t

    def my_tills(self, me, day=None):
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM tills WHERE staff_id = ? AND day = ? ORDER BY currency",
                                (me["staff_id"], day or date.today().isoformat())).fetchall()
        return [self._till_row(r) for r in rows]

    def all_tills(self, day=None, status=""):
        sql = "SELECT t.*, s.full_name FROM tills t LEFT JOIN staff s ON s.staff_id = t.staff_id WHERE 1 = 1"
        args = []
        if day:
            sql += " AND t.day = ?"
            args.append(day)
        if status:
            sql += " AND t.status = ?"
            args.append(status)
        with self._connect() as conn:
            return [self._till_row(r) for r in conn.execute(sql + " ORDER BY t.day DESC, t.staff_id, t.currency LIMIT 300",
                                                             args).fetchall()]

    def open_till(self, me, currency, opening):
        import world
        if currency not in world.CURRENCIES:
            raise BankError("Please choose a currency.")
        if opening < 0:
            raise BankError("The opening cash cannot be negative.")
        with self._connect() as conn:
            if conn.execute("SELECT 1 FROM tills WHERE staff_id = ? AND day = ? AND currency = ?",
                            (me["staff_id"], date.today().isoformat(), currency)).fetchone():
                raise BankError(f"Your {currency} cash drawer is already open today.")
            conn.execute("INSERT INTO tills (staff_id, day, currency, opening, opened_at) VALUES (?, ?, ?, ?, ?)",
                         (me["staff_id"], date.today().isoformat(), currency, opening, now()))
            self.audit(conn, me, "Cash drawer opened", currency, format_money(opening, currency))

    def close_till(self, me, till_id, counted, note=""):
        note = " ".join((note or "").split())[:200]
        with self._connect() as conn:
            r = conn.execute("SELECT * FROM tills WHERE id = ? AND staff_id = ?", (till_id, me["staff_id"])).fetchone()
            if r is None:
                raise BankError("Cash drawer not found.")
            t = self._till_row(r)
            if t["status"] != "Open":
                raise BankError("This cash drawer is already closed.")
            if counted < 0:
                raise BankError("The counted cash cannot be negative.")
            diff = counted - t["expected"]
            if diff and len(note) < 5:
                raise BankError("The cash does not match. Please write what happened (at least 5 characters).")
            status = "Closed" if diff == 0 else "Difference"
            conn.execute("UPDATE tills SET counted = ?, difference = ?, status = ?, closed_at = ?, note = ? WHERE id = ?",
                         (counted, diff, status, now(), note, till_id))
            self.audit(conn, me, "Cash drawer closed", t["currency"],
                       f"expected {format_money(t['expected'], t['currency'])}, counted "
                       f"{format_money(counted, t['currency'])}, difference {format_money(diff, t['currency'])}")
        return diff

    def accept_till(self, by, till_id, note=""):
        note = " ".join((note or "").split())[:200]
        with self._connect() as conn:
            r = conn.execute("SELECT * FROM tills WHERE id = ?", (till_id,)).fetchone()
            if r is None:
                raise BankError("Cash drawer not found.")
            if r["status"] != "Difference":
                raise BankError("Only a drawer with a difference needs to be accepted.")
            if r["staff_id"] == by["staff_id"]:
                raise BankError("Another person must accept the difference of your own cash drawer.")
            if len(note) < 3:
                raise BankError("Please write what was done about the difference.")
            conn.execute("UPDATE tills SET status = 'Accepted', accepted_by = ?, note = note || ? WHERE id = ?",
                         (by["staff_id"], f" | {by['staff_id']}: {note}", till_id))
            self.audit(conn, by, "Cash difference accepted", r["staff_id"],
                       f"{format_money(r['difference'], r['currency'])} - {note}")

    def _till_move(self, conn, staff_id, currency, amount, cash_in):
        r = conn.execute("SELECT * FROM tills WHERE staff_id = ? AND day = ? AND currency = ?",
                         (staff_id, date.today().isoformat(), currency)).fetchone()
        if r is None or r["status"] != "Open":
            raise BankError(f"Please open your {currency} cash drawer first (Counter > My cash drawer).")
        t = self._till_row(r)
        if not cash_in and amount > t["expected"]:
            raise BankError(f"Not enough cash in your {currency} drawer "
                            f"({format_money(t['expected'], currency)}). Ask the vault for more cash first.")
        col = "cash_in" if cash_in else "cash_out"
        conn.execute(f"UPDATE tills SET {col} = {col} + ? WHERE id = ?", (amount, t["id"]))

    # ===== finding the customer =====

    def counter_customer(self, cid):
        """Everything the counter page needs about one customer."""
        c = self.bank.get_customer(cid)
        accounts = self.bank.accounts_of(cid)
        for a in accounts:
            a.available = self.bank.available_balance(a.number)
        return c, accounts

    def _account(self, cid, number):
        c = self.bank.get_customer(cid)
        if c.status != "Active":
            raise BankError("This customer's account is not active (approve or activate it first).")
        return self.bank.account_of(cid, number)

    # ===== the operations =====

    def counter(self, me, cid, op, form, checked=False, approved_by=""):
        """Do one counter operation. Returns ("done", receipt id) or ("waiting", pending id)."""
        if op not in OPS:
            raise BankError("Unknown counter operation.")
        label = OPS[op][0]
        data = {k: (form.get(k) or "").strip() for k in
                ("account", "to_account", "amount", "note", "depositor", "id_type", "id_number", "id_checked",
                 "signature", "rail", "name", "upi", "ifsc", "iban", "bic", "bank_name", "remarks", "months",
                 "loan_id", "instalment", "goal")}
        from bank import parse_amount
        acc = self._account(cid, data["account"])
        amount = 0
        if op != "emi":
            amount = data["amount"] if isinstance(data["amount"], int) else parse_amount(data["amount"], acc.currency)
        if op == "withdraw" and not checked:
            if data["id_checked"] != "1" or data["id_type"] not in ID_DOCUMENTS or len(data["id_number"]) < 4:
                raise BankError("Check the customer's identity document and write its type and number.")
            if data["signature"] != "1":
                raise BankError("Compare the customer's signature with the one on file and tick the box.")
        if op == "deposit" and not checked and len(" ".join(data["depositor"].split())) < 3:
            raise BankError("Please write the name of the person who brings the cash.")
        if not checked and op != "emi" and (amount >= checker_amount(acc.currency) or self.over_own_limit(me, amount, acc.currency)):
            self._counter_check_now(me, cid, op, acc, amount, data)          # refuse early what cannot work
            data["amount_minor"] = amount
            payload = json.dumps({"op": op, "data": data}, ensure_ascii=False)
            with self._connect() as conn:
                pid = conn.execute(
                    "INSERT INTO pending_entries (kind, customer_id, tx_id, entry_type, amount, currency, description, "
                    "made_by, made_at, payload) VALUES (?, ?, NULL, ?, ?, ?, ?, ?, ?, ?)",
                    (f"Counter: {label}", cid, "CR" if op == "deposit" else "DR", amount, acc.currency,
                     f"{label} at the counter for {cid}" + (f" - {data['note']}" if data["note"] else ""),
                     me["staff_id"], now(), payload)).lastrowid
                self.audit(conn, me, "Counter work sent for second approval", cid,
                           f"{label} {format_money(amount, acc.currency)}")
            return "waiting", pid
        return "done", self._counter_do(me, cid, op, acc, amount, data, approved_by)

    def _counter_check_now(self, me, cid, op, acc, amount, data):
        if op in ("withdraw", "transfer", "external", "fx", "fd", "rd"):
            if amount > self.bank.available_balance(acc.number):
                raise BankError("Insufficient balance. Available balance is "
                                f"{format_money(self.bank.available_balance(acc.number), acc.currency)}.")
        if op in ("withdraw", "deposit"):
            t = self.till(me["staff_id"], acc.currency)
            if t is None or t["status"] != "Open":
                raise BankError(f"Please open your {acc.currency} cash drawer first (Counter > My cash drawer).")

    def _counter_do(self, me, cid, op, acc, amount, data, approved_by=""):
        bank = self.bank
        details, ref = "", ""
        if op in ("deposit", "withdraw"):
            with self._connect() as conn:
                a = bank._load(conn, acc.number)
                if op == "deposit":
                    who = " ".join(data["depositor"].split())[:60]
                    t = a.credit(amount, "Deposit", f"CASH DEPOSIT/BRANCH COUNTER/BY {me['staff_id']}/{who.upper()}")
                    details = f"Deposited by {who}"
                else:
                    bank._check_available(conn, a, amount)
                    t = a.debit(amount, "Withdrawal", f"CASH WITHDRAWAL/BRANCH COUNTER/BY {me['staff_id']}")
                    details = f"{data['id_type']} ...{data['id_number'][-4:]}, signature checked"
                self._till_move(conn, me["staff_id"], acc.currency, amount, op == "deposit")
                bank._save(conn, t)
                ref = t.ref
        elif op == "transfer":
            to = data["to_account"].replace(" ", "")
            if any(x.number == to for x in bank.accounts_of(cid)):
                raise BankError("This is the customer's own account. Use the exchange between own accounts.")
            name = bank.name_enquiry_internal(to)["name"]
            t = bank.transfer(cid, to, amount, COUNTER, f"COUNTER {me['staff_id']}" + (f" {data['remarks']}" if data["remarks"] else ""),
                              from_number=acc.number)
            ref, details = t.ref, f"To {name}, A/c XX{to[-4:]}"
        elif op == "external":
            ref = bank.send_external(cid, data["rail"], data, amount, COUNTER, from_number=acc.number)
            p = bank.external_transfer(cid, ref)
            details = f"{p['rail']} to {p['beneficiary_name']}, {p['bank_name']}"
            ref = p["tx_ref"] or ref
        elif op == "fx":
            t, q = bank.exchange(cid, acc.number, data["to_account"], amount, COUNTER)
            ref, details = t.ref, f"To own {q['dst']} account: {format_money(q['amount'], q['dst'])}"
        elif op == "fd":
            if acc.number != bank.get_account(cid).number:
                raise BankError("Fixed deposits are booked from the main account.")
            fd_no = bank.open_fd(cid, amount, data["months"], COUNTER)
            details = f"Fixed deposit {fd_no}, {data['months']} months"
        elif op == "rd":
            rd_no = bank.open_rd(cid, acc.number, amount, data["months"], COUNTER)
            details = f"Recurring deposit {rd_no}, {data['months']} months"
        elif op == "emi":
            loan_id = int(data["loan_id"]) if data["loan_id"].isdigit() else 0
            ln = bank.get_loan(cid, loan_id)
            t = bank.pay_emi(cid, loan_id, COUNTER)
            amount, ref, details = t.amount, t.ref, f"Loan {ln['loan_number'] if 'loan_number' in ln else loan_id}"
        if not ref:
            with self._connect() as conn:
                r = conn.execute("SELECT ref FROM transactions WHERE account_number = ? ORDER BY id DESC LIMIT 1",
                                 (acc.number,)).fetchone()
                ref = r[0] if r else ""
        if data["note"]:
            details += f" - {data['note'][:80]}"
        with self._connect() as conn:
            receipt = self.bank._unique(conn, "SELECT 1 FROM counter_ops WHERE receipt_no = ?",
                                        lambda: f"CR{date.today():%y%m%d}{random.randint(0, 99_999):05d}")
            op_id = conn.execute(
                "INSERT INTO counter_ops (receipt_no, kind, customer_id, account_number, amount, currency, tx_ref, details, "
                "staff_id, approved_by, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (receipt, OPS[op][0], cid, acc.number, amount, acc.currency, ref, details, me["staff_id"], approved_by,
                 now())).lastrowid
            self.audit(conn, me, f"Counter: {OPS[op][0]}", cid,
                       f"{format_money(amount, acc.currency)} (receipt {receipt}, ref {ref}) {details}")
        return op_id

    def run_counter_pending(self, maker, by, p):
        """Called by the four-eyes approval (compliance.decide_entry) for counter work."""
        pay = json.loads(p.get("payload") or "{}")
        data = pay.get("data") or {}
        data["amount"] = data.get("amount_minor", p["amount"])
        acc = self._account(p["customer_id"], data.get("account", ""))
        op_id = self._counter_do(maker, p["customer_id"], pay.get("op"), acc, int(data["amount"]),
                                 {k: str(v) for k, v in data.items()}, approved_by=by["staff_id"])
        return self.counter_op(op_id)

    # ===== receipts and lists =====

    def counter_op(self, op_id):
        with self._connect() as conn:
            r = conn.execute("SELECT o.*, c.full_name, s.full_name AS staff_name FROM counter_ops o "
                             "LEFT JOIN customers c ON c.customer_id = o.customer_id "
                             "LEFT JOIN staff s ON s.staff_id = o.staff_id WHERE o.id = ?", (op_id,)).fetchone()
        if r is None:
            raise BankError("Receipt not found.")
        return dict(r)

    def counter_ops(self, cid=None, staff_id=None, day=None, limit=50):
        sql = ("SELECT o.*, c.full_name FROM counter_ops o LEFT JOIN customers c ON c.customer_id = o.customer_id "
               "WHERE 1 = 1")
        args = []
        for col, val in (("o.customer_id", cid), ("o.staff_id", staff_id)):
            if val:
                sql += f" AND {col} = ?"
                args.append(val)
        if day:
            sql += " AND o.created_at >= ? AND o.created_at <= ?"
            args += [day + " 00:00:00", day + " 23:59:59"]
        with self._connect() as conn:
            return [dict(r) for r in conn.execute(sql + " ORDER BY o.id DESC LIMIT ?", (*args, limit)).fetchall()]

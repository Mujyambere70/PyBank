"""
autopay.py - bill payments and standing instructions (version 4).

Bill payments (sandbox): the bank keeps a list of billers - electricity, mobile, water, gas, internet,
insurance companies - managed by the website editor. A customer enters his consumer number; the
sandbox "biller network" answers with the amount due (worked out from the number, always the same
for the same number and month) and the customer pays it with his PIN. A consumer number ending in
000 is "unknown" at the biller, so the refusal can be tested.

Standing instructions: "pay this payee / this bill every month (or week) from this date". The customer
gives his PIN once when he creates the instruction; after that the daily job pays on the due date,
sends an alert, and moves the next date. If the money is not there, the payment of that date is skipped
and the customer gets an alert (banks do not keep trying).
"""

import hashlib
import random
import re
from datetime import date, datetime, timedelta

import world
from core import BankError, now
from world import format_money

AUTOPAY_SCHEMA = """
CREATE TABLE IF NOT EXISTS billers (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    name         TEXT NOT NULL,
    category     TEXT NOT NULL,
    country      TEXT NOT NULL DEFAULT 'IN',
    currency     TEXT NOT NULL DEFAULT 'INR',
    number_label TEXT NOT NULL DEFAULT 'Consumer number',
    number_regex TEXT NOT NULL DEFAULT '^[0-9]{6,12}$',
    active       INTEGER NOT NULL DEFAULT 1,
    updated_at   TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS bill_payments (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    ref            TEXT NOT NULL UNIQUE,
    customer_id    TEXT NOT NULL,
    account_number TEXT NOT NULL,
    biller_id      INTEGER NOT NULL,
    consumer_no    TEXT NOT NULL,
    amount         INTEGER NOT NULL,
    status         TEXT NOT NULL,
    created_at     TEXT NOT NULL,
    tx_ref         TEXT NOT NULL DEFAULT '',
    instruction_id INTEGER
);
CREATE TABLE IF NOT EXISTS standing_instructions (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    customer_id    TEXT NOT NULL,
    account_number TEXT NOT NULL,
    name           TEXT NOT NULL,
    kind           TEXT NOT NULL,
    payee_account  TEXT NOT NULL DEFAULT '',
    biller_id      INTEGER,
    consumer_no    TEXT NOT NULL DEFAULT '',
    amount         INTEGER NOT NULL,
    frequency      TEXT NOT NULL,
    next_run       TEXT NOT NULL,
    ends_on        TEXT NOT NULL DEFAULT '',
    status         TEXT NOT NULL DEFAULT 'Active',
    runs           INTEGER NOT NULL DEFAULT 0,
    last_run       TEXT NOT NULL DEFAULT '',
    last_result    TEXT NOT NULL DEFAULT '',
    created_at     TEXT NOT NULL
);
"""
BILLER_CATEGORIES = ["Electricity", "Mobile", "Water", "Gas", "Internet", "Insurance", "Credit card", "Education"]
FREQUENCIES = {"monthly": "Every month", "weekly": "Every week"}
DEFAULT_BILLERS = [
    ("Haryana Power Distribution (demo)", "Electricity", "IN", "INR", "Consumer number", r"^[0-9]{10}$"),
    ("Sunrise Mobile (demo)", "Mobile", "IN", "INR", "Mobile number", r"^[6-9][0-9]{9}$"),
    ("Ambala Water Board (demo)", "Water", "IN", "INR", "Connection ID", r"^[0-9]{6,8}$"),
    ("City Gas (demo)", "Gas", "IN", "INR", "Customer ID", r"^[0-9]{8}$"),
    ("FastNet Broadband (demo)", "Internet", "IN", "INR", "Account ID", r"^[0-9]{6,12}$"),
    ("Siberia Energy (demo)", "Electricity", "RU", "RUB", "Лицевой счёт / account", r"^[0-9]{8,12}$"),
]


def add_period(day, frequency):
    if frequency == "weekly":
        return day + timedelta(days=7)
    y, m = divmod(day.month, 12)
    return day.replace(year=day.year + y, month=m + 1, day=min(day.day, 28))


def bill_due(biller, consumer_no, month=None):
    """The sandbox biller network: the amount due for a consumer number this month (always the same answer)."""
    if consumer_no.endswith("000"):
        raise BankError(f"{biller['name']} does not know this {biller['number_label'].lower()}.")
    month = month or date.today().strftime("%Y-%m")
    h = int(hashlib.sha256(f"{biller['id']}|{consumer_no}|{month}".encode()).hexdigest()[:8], 16)
    low = world.policy(biller["currency"], "savings")               # roughly 1 - 9 times the minimum deposit
    return low + (h % (low * 8) // 100) * 100


class AutopayMixin:

    def ensure_billers(self):
        with self._connect() as conn:
            if conn.execute("SELECT COUNT(*) FROM billers").fetchone()[0]:
                return
            for b in DEFAULT_BILLERS:
                conn.execute("INSERT INTO billers (name, category, country, currency, number_label, number_regex, "
                             "updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)", (*b, now()))

    # ----- billers (managed in the back office) -----

    def billers(self, active_only=True, currency=None):
        sql, args = "SELECT * FROM billers WHERE 1 = 1", []
        if active_only:
            sql += " AND active = 1"
        if currency:
            sql += " AND currency = ?"
            args.append(currency)
        with self._connect() as conn:
            return [dict(r) for r in conn.execute(sql + " ORDER BY category, name", args)]

    def biller(self, biller_id):
        with self._connect() as conn:
            r = conn.execute("SELECT * FROM billers WHERE id = ?", (biller_id,)).fetchone()
        if r is None:
            raise BankError("Biller not found.")
        return dict(r)

    def save_biller(self, staff, form, biller_id=None):
        name = " ".join(str(form.get("name", "")).split())[:80]
        category, country = form.get("category", ""), str(form.get("country", "IN")).upper()
        currency, label = str(form.get("currency", "INR")).upper(), " ".join(str(form.get("number_label", "")).split())[:40]
        regex = str(form.get("number_regex", "")).strip()[:80] or r"^[0-9]{6,12}$"
        if len(name) < 3:
            raise BankError("Please write the name of the biller.")
        if category not in BILLER_CATEGORIES:
            raise BankError("Please choose a category.")
        if country not in world.DIAL_CODES or currency not in world.CURRENCIES:
            raise BankError("Please choose the country and the currency.")
        if not label:
            raise BankError("Please write what the customer's number is called (e.g. Consumer number).")
        try:
            re.compile(regex)
        except re.error:
            raise BankError("The number pattern is not a valid regular expression.")
        active = int(bool(form.get("active")))
        with self._connect() as conn:
            if biller_id:
                conn.execute("UPDATE billers SET name = ?, category = ?, country = ?, currency = ?, number_label = ?, "
                             "number_regex = ?, active = ?, updated_at = ? WHERE id = ?",
                             (name, category, country, currency, label, regex, active, now(), biller_id))
            else:
                biller_id = conn.execute("INSERT INTO billers (name, category, country, currency, number_label, "
                                         "number_regex, active, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                                         (name, category, country, currency, label, regex, active, now())).lastrowid
            conn.execute("INSERT INTO audit_log (staff_id, action, target, details, at) VALUES (?, ?, ?, ?, ?)",
                         (staff["staff_id"], "Biller saved", str(biller_id), name, now()))
        return biller_id

    # ----- paying a bill -----

    def _check_consumer(self, biller, consumer_no):
        consumer_no = re.sub(r"\s", "", str(consumer_no or ""))
        if not re.fullmatch(biller["number_regex"], consumer_no):
            raise BankError(f"Please check the {biller['number_label'].lower()}.")
        return consumer_no

    def fetch_bill(self, biller_id, consumer_no):
        b = self.biller(biller_id)
        if not b["active"]:
            raise BankError("This biller is not available now.")
        consumer_no = self._check_consumer(b, consumer_no)
        return {"biller": b, "consumer_no": consumer_no, "amount": bill_due(b, consumer_no)}

    def _pay_bill(self, conn, cid, acc_number, b, consumer_no, amount, instruction_id=None):
        acc = self._load(conn, acc_number)
        if acc.currency != b["currency"]:
            raise BankError(f"{b['name']} takes payments in {b['currency']}. Please pay from a {b['currency']} account.")
        self._check_available(conn, acc, amount)
        ref = self._unique(conn, "SELECT 1 FROM bill_payments WHERE ref = ?",
                           lambda: f"BP{datetime.now():%y%m%d}{random.randint(0, 999_999):06d}")
        t = acc.debit(amount, "Bill payment", f"BILL/{b['name'][:30].upper()}/{consumer_no}/{ref}")
        self._save(conn, t)
        conn.execute("INSERT INTO bill_payments (ref, customer_id, account_number, biller_id, consumer_no, amount, status, "
                     "created_at, tx_ref, instruction_id) VALUES (?, ?, ?, ?, ?, ?, 'Paid', ?, ?, ?)",
                     (ref, cid, acc.number, b["id"], consumer_no, amount, now(), t.ref, instruction_id))
        return ref, t

    def pay_bill(self, cid, biller_id, consumer_no, amount, pin, from_number=None):
        self._check_pin(cid, pin)
        bill = self.fetch_bill(biller_id, consumer_no)
        acc = self.account_of(cid, from_number) if from_number else self.get_account(cid)
        if amount <= 0:
            raise BankError("Amount must be greater than zero.")
        with self._connect() as conn:
            return self._pay_bill(conn, cid, acc.number, bill["biller"], bill["consumer_no"], amount)

    def bill_payments(self, cid, limit=30):
        with self._connect() as conn:
            return [dict(r) for r in conn.execute(
                "SELECT p.*, b.name biller, b.category FROM bill_payments p JOIN billers b ON b.id = p.biller_id "
                "WHERE p.customer_id = ? ORDER BY p.id DESC LIMIT ?", (cid, limit))]

    # ----- standing instructions -----

    def create_instruction(self, cid, form, pin):
        self._check_pin(cid, pin)
        kind = form.get("kind")
        acc = self.account_of(cid, form.get("account")) if form.get("account") else self.get_account(cid)
        from bank import parse_amount
        amount = parse_amount(form.get("amount"), acc.currency)
        frequency = form.get("frequency")
        if frequency not in FREQUENCIES:
            raise BankError("Please choose how often to pay.")
        try:
            start = date.fromisoformat(str(form.get("start", "")))
        except ValueError:
            raise BankError("Please choose the date of the first payment.")
        if start < date.today() or start > date.today() + timedelta(days=366):
            raise BankError("The first payment must be between today and one year from now.")
        ends = str(form.get("ends", "") or "")
        if ends:
            try:
                if date.fromisoformat(ends) < start:
                    raise BankError("The last date is before the first payment.")
            except ValueError:
                raise BankError("Please check the last date.")
        name = " ".join(str(form.get("name", "")).split())[:40]
        payee, biller_id, consumer = "", None, ""
        if kind == "transfer":
            payee = str(form.get("payee", "")).strip()
            if payee == acc.number:
                raise BankError("You cannot pay into the same account.")
            self.find_beneficiary(payee)
            name = name or f"Transfer to XX{payee[-4:]}"
        elif kind == "bill":
            b = self.biller(int(form.get("biller") or 0))
            consumer = self._check_consumer(b, form.get("consumer_no"))
            if b["currency"] != acc.currency:
                raise BankError(f"{b['name']} takes payments in {b['currency']}. Please choose a {b['currency']} account.")
            biller_id = b["id"]
            name = name or b["name"]
        else:
            raise BankError("Please choose what to pay.")
        with self._connect() as conn:
            if conn.execute("SELECT COUNT(*) FROM standing_instructions WHERE customer_id = ? AND status = 'Active'",
                            (cid,)).fetchone()[0] >= 10:
                raise BankError("You can have at most 10 active standing instructions.")
            iid = conn.execute("INSERT INTO standing_instructions (customer_id, account_number, name, kind, payee_account, "
                               "biller_id, consumer_no, amount, frequency, next_run, ends_on, created_at) "
                               "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                               (cid, acc.number, name, kind, payee, biller_id, consumer, amount, frequency,
                                start.isoformat(), ends, now())).lastrowid
            self._alert(conn, cid, "Standing instruction created",
                        f"{name}: {format_money(amount, acc.currency, 'en')} {FREQUENCIES[frequency].lower()} from "
                        f"{start:%d-%m-%Y} (A/c XX{acc.number[-4:]}).", only_in_app=True)
        return iid

    def instructions(self, cid):
        with self._connect() as conn:
            return [dict(r) for r in conn.execute(
                "SELECT s.*, a.currency FROM standing_instructions s JOIN accounts a ON a.number = s.account_number "
                "WHERE s.customer_id = ? ORDER BY s.status, s.next_run", (cid,))]

    def set_instruction_status(self, cid, iid, action):
        new = {"pause": "Paused", "resume": "Active", "cancel": "Cancelled"}.get(action)
        if not new:
            raise BankError("Unknown action.")
        with self._connect() as conn:
            r = conn.execute("SELECT * FROM standing_instructions WHERE id = ? AND customer_id = ?", (iid, cid)).fetchone()
            if r is None:
                raise BankError("Standing instruction not found.")
            if r["status"] in ("Cancelled", "Ended"):
                raise BankError("This standing instruction has ended.")
            nxt = r["next_run"]
            if new == "Active" and nxt < date.today().isoformat():   # a paused one does not pay the missed dates
                d = date.fromisoformat(nxt)
                while d < date.today():
                    d = add_period(d, r["frequency"])
                nxt = d.isoformat()
            conn.execute("UPDATE standing_instructions SET status = ?, next_run = ? WHERE id = ?", (new, nxt, iid))

    def run_instructions(self, today=None):
        """Pay every active instruction due today or earlier (called once a day). Returns (paid, failed)."""
        today = today or date.today()
        with self._connect() as conn:
            due = [dict(r) for r in conn.execute("SELECT * FROM standing_instructions WHERE status = 'Active' "
                                                 "AND next_run <= ?", (today.isoformat(),))]
        paid = failed = 0
        for s in due:
            run_day = date.fromisoformat(s["next_run"])
            nxt = add_period(run_day, s["frequency"])
            while nxt <= today:
                nxt = add_period(nxt, s["frequency"])
            status = "Ended" if s["ends_on"] and nxt.isoformat() > s["ends_on"] else "Active"
            with self._connect() as conn:
                # claim this date first: a second worker changes nothing
                if conn.execute("UPDATE standing_instructions SET next_run = ?, status = ?, last_run = ? WHERE id = ? "
                                "AND next_run = ? AND status = 'Active'",
                                (nxt.isoformat(), status, today.isoformat(), s["id"], s["next_run"])).rowcount == 0:
                    continue
            try:
                with self._connect() as conn:
                    cur = conn.execute("SELECT currency FROM accounts WHERE number = ?", (s["account_number"],)).fetchone()[0]
                    if s["kind"] == "bill":
                        b = dict(conn.execute("SELECT * FROM billers WHERE id = ?", (s["biller_id"],)).fetchone())
                        ref, t = self._pay_bill(conn, s["customer_id"], s["account_number"], b, s["consumer_no"],
                                                s["amount"], s["id"])
                    else:
                        t = self._internal_transfer(conn, s["account_number"], s["payee_account"], s["amount"],
                                                    f"SI/{s['name'][:20]}")
                    conn.execute("UPDATE standing_instructions SET runs = runs + 1, last_result = ? WHERE id = ?",
                                 (f"Paid {t.ref}", s["id"]))
                    self._alert(conn, s["customer_id"], f"Standing instruction paid: {s['name']}",
                                f"{format_money(s['amount'], cur, 'en')} was paid for \"{s['name']}\" (Ref {t.ref}). "
                                f"Next payment on {nxt:%d-%m-%Y}." if status == "Active" else
                                f"{format_money(s['amount'], cur, 'en')} was paid for \"{s['name']}\" (Ref {t.ref}). "
                                f"This was the last payment.", only_in_app=True)
                paid += 1
            except BankError as e:
                with self._connect() as conn:
                    conn.execute("UPDATE standing_instructions SET last_result = ? WHERE id = ?",
                                 (f"Not paid: {str(e)[:80]}", s["id"]))
                    self._alert(conn, s["customer_id"], f"Standing instruction not paid: {s['name']}",
                                f"The payment of {run_day:%d-%m-%Y} for \"{s['name']}\" could not be made: {e} "
                                f"The next try is on {nxt:%d-%m-%Y}.")
                failed += 1
        return paid, failed

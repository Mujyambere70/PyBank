"""
bank.py - core banking logic of PyBank (OOP part of the project).

Classes
    Transaction  - one credit (money in) or debit (money out) record
    Customer     - a bank customer (personal details, login data)
    BankAccount  - one account: credit(), debit(), balance
    Bank         - manages customers, accounts and transactions in SQLite

Money is stored as an integer number of paise (1 rupee = 100 paise),
so there are no rounding errors like 0.1 + 0.2 = 0.30000000000000004.
"""

import random
import re
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation

from werkzeug.security import check_password_hash, generate_password_hash

# ---------- bank settings ----------

BANK_NAME = "PyBank"
BRANCH = {"name": "Ambala Cantt Main Branch", "ifsc": "PYBK0001024",
          "address": "12 Mall Road, Ambala Cantt, Haryana 133001"}

ACCOUNT_TYPES = {
    #            minimum opening deposit, yearly interest rate
    "Savings": {"min_balance": 500 * 100, "interest": 3.5},
    "Current": {"min_balance": 5000 * 100, "interest": 0.0},
}

MAX_AMOUNT = 10_000_000 * 100          # one transaction: max ₹1 crore
DAILY_WITHDRAW_LIMIT = 50_000 * 100     # cash withdrawal per day
DAILY_TRANSFER_LIMIT = 200_000 * 100    # fund transfer per day
MAX_FAILED_LOGINS = 5
LOCK_MINUTES = 15

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[a-zA-Z]{2,}$")
MOBILE_RE = re.compile(r"^[6-9]\d{9}$")       # Indian mobile number
PINCODE_RE = re.compile(r"^[1-9]\d{5}$")


class BankError(Exception):
    """Error that can be shown to the user (wrong PIN, not enough money, ...)."""


# ---------- helper functions ----------

def now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def parse_amount(text):
    """Convert user text like '1500.50' into paise (150050). Raise BankError if invalid."""
    try:
        value = Decimal(str(text).replace(",", "").strip())
    except (InvalidOperation, ValueError):
        raise BankError("Please enter a valid amount, for example 500 or 250.75.")
    if not value.is_finite() or value <= 0:
        raise BankError("Amount must be greater than zero.")
    if value.as_tuple().exponent < -2:
        raise BankError("Amount can have at most 2 digits after the point.")
    paise = int(value * 100)
    if paise > MAX_AMOUNT:
        raise BankError("Amount is too large for one transaction.")
    return paise


def format_money(paise):
    """150050 -> '₹1,500.50' (Indian style grouping: ₹1,50,000.00)."""
    sign = "-" if paise < 0 else ""
    rupees, p = divmod(abs(int(paise)), 100)
    s = str(rupees)
    if len(s) > 3:
        head, tail = s[:-3], s[-3:]
        groups = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        if head:
            groups.insert(0, head)
        s = ",".join(groups) + "," + tail
    return f"{sign}₹{s}.{p:02d}"


def mask_name(name):
    """'Rahul Verma' -> 'R**** V****' (shown to the sender before a transfer)."""
    return " ".join(w[0] + "*" * (len(w) - 1) for w in name.split())


def check_pin_format(pin):
    if not (pin.isdigit() and len(pin) == 4):
        raise BankError("Transaction PIN must be exactly 4 digits.")
    if len(set(pin)) == 1:
        raise BankError("This PIN is too easy to guess (all digits are the same).")


def check_password_strength(password):
    if len(password) < 8 or not re.search(r"[A-Za-z]", password) or not re.search(r"\d", password):
        raise BankError("Password must be at least 8 characters with letters and numbers.")


# ---------- data classes ----------

@dataclass
class Transaction:
    ref: str             # reference number, e.g. PB2610260012345
    type: str            # "CR" = credit (money in), "DR" = debit (money out)
    category: str        # Deposit, Withdrawal, Transfer, Salary, Bill payment, ...
    description: str
    amount: int          # paise
    balance_after: int   # paise
    created_at: str
    account_number: str = ""

    @property
    def is_credit(self):
        return self.type == "CR"


@dataclass
class Customer:
    customer_id: str
    full_name: str
    dob: str
    gender: str
    email: str
    mobile: str
    address: str
    city: str
    state: str
    pincode: str
    occupation: str
    password_hash: str
    pin_hash: str
    failed_logins: int
    locked_until: str
    created_at: str
    last_login: str

    @property
    def first_name(self):
        return self.full_name.split()[0]

    @property
    def initials(self):
        return "".join(w[0] for w in self.full_name.split()[:2]).upper()


class BankAccount:
    """A single bank account. Changes the balance and returns Transaction records."""

    def __init__(self, number, customer_id, type, balance, ifsc, branch,
                 card_last4, status, opened_at):
        self.number = number
        self.customer_id = customer_id
        self.type = type
        self.balance = balance
        self.ifsc = ifsc
        self.branch = branch
        self.card_last4 = card_last4
        self.status = status
        self.opened_at = opened_at

    @property
    def masked_number(self):
        return "XXXX XXXX " + self.number[-4:]

    @property
    def interest_rate(self):
        return ACCOUNT_TYPES[self.type]["interest"]

    def credit(self, amount, category, description, when=None):
        """Money in."""
        if amount <= 0:
            raise BankError("Amount must be greater than zero.")
        self.balance += amount
        return Transaction(new_ref(when), "CR", category, description, amount,
                           self.balance, when or now(), self.number)

    def debit(self, amount, category, description, when=None):
        """Money out. The balance can never go below zero."""
        if amount <= 0:
            raise BankError("Amount must be greater than zero.")
        if amount > self.balance:
            raise BankError(f"Insufficient balance. Available balance is {format_money(self.balance)}.")
        self.balance -= amount
        return Transaction(new_ref(when), "DR", category, description, amount,
                           self.balance, when or now(), self.number)

    # names used in the project synopsis
    def deposit(self, amount):
        return self.credit(amount, "Deposit", "Cash deposit")

    def withdraw(self, amount):
        return self.debit(amount, "Withdrawal", "Cash withdrawal")


def new_ref(when=None):
    d = datetime.strptime(when, "%Y-%m-%d %H:%M:%S") if when else datetime.now()
    return f"PB{d:%y%m%d}{random.randint(0, 9_999_999):07d}"


# ---------- the Bank ----------

SCHEMA = """
CREATE TABLE IF NOT EXISTS customers (
    customer_id   TEXT PRIMARY KEY,
    full_name     TEXT NOT NULL,
    dob           TEXT NOT NULL,
    gender        TEXT NOT NULL,
    email         TEXT NOT NULL UNIQUE,
    mobile        TEXT NOT NULL UNIQUE,
    address       TEXT NOT NULL,
    city          TEXT NOT NULL,
    state         TEXT NOT NULL,
    pincode       TEXT NOT NULL,
    occupation    TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    pin_hash      TEXT NOT NULL,
    failed_logins INTEGER NOT NULL DEFAULT 0,
    locked_until  TEXT NOT NULL DEFAULT '',
    created_at    TEXT NOT NULL,
    last_login    TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS accounts (
    number      TEXT PRIMARY KEY,
    customer_id TEXT NOT NULL REFERENCES customers(customer_id),
    type        TEXT NOT NULL,
    balance     INTEGER NOT NULL CHECK (balance >= 0),
    ifsc        TEXT NOT NULL,
    branch      TEXT NOT NULL,
    card_last4  TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'Active',
    opened_at   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS transactions (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    ref            TEXT NOT NULL,
    account_number TEXT NOT NULL REFERENCES accounts(number),
    type           TEXT NOT NULL CHECK (type IN ('CR', 'DR')),
    category       TEXT NOT NULL,
    description    TEXT NOT NULL,
    amount         INTEGER NOT NULL,
    balance_after  INTEGER NOT NULL,
    created_at     TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS login_history (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    customer_id TEXT NOT NULL,
    at          TEXT NOT NULL,
    ip          TEXT NOT NULL,
    success     INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tx_account ON transactions(account_number, created_at);

-- things a customer creates and manages himself
CREATE TABLE IF NOT EXISTS beneficiaries (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    customer_id    TEXT NOT NULL REFERENCES customers(customer_id),
    account_number TEXT NOT NULL,
    nickname       TEXT NOT NULL,
    created_at     TEXT NOT NULL,
    UNIQUE (customer_id, account_number)
);
CREATE TABLE IF NOT EXISTS fixed_deposits (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    fd_number   TEXT NOT NULL UNIQUE,
    customer_id TEXT NOT NULL REFERENCES customers(customer_id),
    principal   INTEGER NOT NULL,
    rate        REAL NOT NULL,
    months      INTEGER NOT NULL,
    opened_at   TEXT NOT NULL,
    matures_on  TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'Active',
    closed_at   TEXT NOT NULL DEFAULT '',
    payout      INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS tx_notes (
    customer_id TEXT NOT NULL,
    ref         TEXT NOT NULL,
    note        TEXT NOT NULL,
    PRIMARY KEY (customer_id, ref)
);
CREATE TABLE IF NOT EXISTS service_requests (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    customer_id TEXT NOT NULL REFERENCES customers(customer_id),
    type        TEXT NOT NULL,
    details     TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'Open',
    reply       TEXT NOT NULL DEFAULT '',
    replied_by  TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
-- bank staff side
CREATE TABLE IF NOT EXISTS staff (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    staff_id      TEXT NOT NULL UNIQUE,
    full_name     TEXT NOT NULL,
    email         TEXT NOT NULL UNIQUE,
    phone         TEXT NOT NULL DEFAULT '',
    designation   TEXT NOT NULL DEFAULT '',
    role          TEXT NOT NULL CHECK (role IN ('admin', 'staff')),
    password_hash TEXT NOT NULL,
    active        INTEGER NOT NULL DEFAULT 1,
    created_at    TEXT NOT NULL,
    last_login    TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS notices (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    title      TEXT NOT NULL,
    body       TEXT NOT NULL,
    level      TEXT NOT NULL DEFAULT 'info',
    active     INTEGER NOT NULL DEFAULT 1,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audit_log (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    staff_id TEXT NOT NULL,
    action   TEXT NOT NULL,
    target   TEXT NOT NULL DEFAULT '',
    details  TEXT NOT NULL DEFAULT '',
    at       TEXT NOT NULL
);
"""

# Bank-wide settings the admin can change (stored in the settings table)
DEFAULT_SETTINGS = {
    "daily_withdraw_limit": str(DAILY_WITHDRAW_LIMIT),
    "daily_transfer_limit": str(DAILY_TRANSFER_LIMIT),
    "fd_rate_6": "6.00", "fd_rate_12": "6.80", "fd_rate_24": "7.00", "fd_rate_36": "7.25",
    "online_account_opening": "1",
}
FD_TENURES = (6, 12, 24, 36)                 # months
FD_MIN = 1000 * 100                          # ₹1,000
FD_PREMATURE_PENALTY = 1.0                   # % less interest if closed early
REQUEST_TYPES = ["Cheque book", "Debit card block", "New debit card", "Account statement by post",
                 "Update nominee", "Address proof letter", "Account closure", "Complaint", "Other"]


class Bank:
    """Keeps all customers, accounts and transactions in an SQLite database."""

    def __init__(self, db_path="bank.db"):
        self.db_path = db_path
        with self._connect() as conn:
            conn.executescript(SCHEMA)
            cols = [r[1] for r in conn.execute("PRAGMA table_info(transactions)")]
            if "reversed" not in cols:                    # older databases
                conn.execute("ALTER TABLE transactions ADD COLUMN reversed INTEGER NOT NULL DEFAULT 0")

    # ----- settings -----

    def settings(self):
        with self._connect() as conn:
            saved = dict(conn.execute("SELECT key, value FROM settings").fetchall())
        return {**DEFAULT_SETTINGS, **saved}

    def limit(self, name):
        return int(self.settings()[name])

    def fd_rates(self):
        s = self.settings()
        return {m: float(s[f"fd_rate_{m}"]) for m in FD_TENURES}

    # ----- database helpers -----

    @contextmanager
    def _connect(self):
        """Open the DB, commit if everything worked (rollback on error), always close."""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    @staticmethod
    def _save(conn, t):
        conn.execute(
            "INSERT INTO transactions (ref, account_number, type, category, description, "
            "amount, balance_after, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (t.ref, t.account_number, t.type, t.category, t.description,
             t.amount, t.balance_after, t.created_at))
        conn.execute("UPDATE accounts SET balance = ? WHERE number = ?",
                     (t.balance_after, t.account_number))

    @staticmethod
    def _unique(conn, sql, make):
        while True:
            value = make()
            if not conn.execute(sql, (value,)).fetchone():
                return value

    def is_empty(self):
        with self._connect() as conn:
            return conn.execute("SELECT COUNT(*) FROM customers").fetchone()[0] == 0

    # ----- registration -----

    def register(self, data, when=None, customer_id=None):
        """Open a new customer + account. `data` is a dict from the form.
        Returns (customer_id, account)."""
        d = {k: " ".join(str(v).split()) for k, v in data.items() if k not in ("password", "pin")}
        password, pin = data.get("password", ""), data.get("pin", "")

        if len(d.get("full_name", "")) < 3 or len(d["full_name"].split()) < 2:
            raise BankError("Please enter your full name (first and last name).")
        try:
            born = datetime.strptime(d.get("dob", ""), "%Y-%m-%d").date()
        except ValueError:
            raise BankError("Please enter a valid date of birth.")
        today = date.today()
        age = today.year - born.year - ((today.month, today.day) < (born.month, born.day))
        if age < 18 or age > 110:
            raise BankError("You must be at least 18 years old to open an account online.")
        if d.get("gender") not in ("Male", "Female", "Other"):
            raise BankError("Please select your gender.")
        d["email"] = d.get("email", "").lower()
        if not EMAIL_RE.match(d["email"]):
            raise BankError("Please enter a valid email address.")
        if not MOBILE_RE.match(d.get("mobile", "")):
            raise BankError("Please enter a valid 10-digit mobile number.")
        if len(d.get("address", "")) < 5 or not d.get("city") or not d.get("state"):
            raise BankError("Please enter your full address.")
        if not PINCODE_RE.match(d.get("pincode", "")):
            raise BankError("Please enter a valid 6-digit PIN code.")
        if not d.get("occupation"):
            raise BankError("Please select your occupation.")
        if d.get("account_type") not in ACCOUNT_TYPES:
            raise BankError("Please choose an account type.")
        check_password_strength(password)
        check_pin_format(pin)

        initial = parse_amount(d.get("initial_deposit", "0") or "0") if d.get("initial_deposit") not in ("", "0") else 0
        minimum = ACCOUNT_TYPES[d["account_type"]]["min_balance"]
        if initial < minimum:
            raise BankError(f"Minimum opening deposit for a {d['account_type']} account "
                            f"is {format_money(minimum)}.")

        when = when or now()
        with self._connect() as conn:
            if conn.execute("SELECT 1 FROM customers WHERE email = ?", (d["email"],)).fetchone():
                raise BankError("An account with this email already exists.")
            if conn.execute("SELECT 1 FROM customers WHERE mobile = ?", (d["mobile"],)).fetchone():
                raise BankError("An account with this mobile number already exists.")

            cid = customer_id or self._unique(conn, "SELECT 1 FROM customers WHERE customer_id = ?",
                               lambda: f"PB{random.randint(10_000_000, 99_999_999)}")
            number = self._unique(conn, "SELECT 1 FROM accounts WHERE number = ?",
                                  lambda: f"5021{random.randint(0, 99_999_999):08d}")
            conn.execute(
                "INSERT INTO customers (customer_id, full_name, dob, gender, email, mobile, address, "
                "city, state, pincode, occupation, password_hash, pin_hash, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (cid, d["full_name"].title(), d["dob"], d["gender"], d["email"], d["mobile"],
                 d["address"], d["city"].title(), d["state"], d["pincode"], d["occupation"],
                 generate_password_hash(password), generate_password_hash(pin), when))
            account = BankAccount(number, cid, d["account_type"], 0, BRANCH["ifsc"], BRANCH["name"],
                                  f"{random.randint(0, 9999):04d}", "Active", when)
            conn.execute(
                "INSERT INTO accounts (number, customer_id, type, balance, ifsc, branch, card_last4, "
                "status, opened_at) VALUES (?, ?, ?, 0, ?, ?, ?, 'Active', ?)",
                (number, cid, account.type, account.ifsc, account.branch, account.card_last4, when))
            self._save(conn, account.credit(initial, "Deposit", "Account opening deposit", when))
        return cid, account

    # ----- login -----

    def authenticate(self, login, password, ip="127.0.0.1"):
        """Login with Customer ID or email. Locks the account after 5 wrong passwords."""
        login = login.strip()
        error = None
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM customers WHERE customer_id = ? OR email = ?",
                               (login.upper(), login.lower())).fetchone()
            if row is None:
                raise BankError("Invalid Customer ID or password.")
            c = Customer(**dict(row))
            if c.locked_until and c.locked_until > now():
                raise BankError(f"Too many wrong attempts. Your login is locked until "
                                f"{c.locked_until[11:16]}. Please try again later.")
            if not check_password_hash(c.password_hash, password):
                fails = c.failed_logins + 1
                locked = ""
                if fails >= MAX_FAILED_LOGINS:
                    locked = (datetime.now() + timedelta(minutes=LOCK_MINUTES)).strftime("%Y-%m-%d %H:%M:%S")
                    error = f"Too many wrong attempts. Login locked for {LOCK_MINUTES} minutes."
                    fails = 0
                else:
                    error = f"Invalid Customer ID or password. {MAX_FAILED_LOGINS - fails} attempt(s) left."
                conn.execute("UPDATE customers SET failed_logins = ?, locked_until = ? WHERE customer_id = ?",
                             (fails, locked, c.customer_id))
                conn.execute("INSERT INTO login_history (customer_id, at, ip, success) VALUES (?, ?, ?, 0)",
                             (c.customer_id, now(), ip))
            else:
                conn.execute("UPDATE customers SET failed_logins = 0, locked_until = '', last_login = ? "
                             "WHERE customer_id = ?", (now(), c.customer_id))
                conn.execute("INSERT INTO login_history (customer_id, at, ip, success) VALUES (?, ?, ?, 1)",
                             (c.customer_id, now(), ip))
        # raise AFTER the with-block, so the failed attempt is saved (not rolled back)
        if error:
            raise BankError(error)
        return c

    # ----- reading data -----

    def get_customer(self, cid):
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM customers WHERE customer_id = ?", (cid,)).fetchone()
        if row is None:
            raise BankError("Customer not found.")
        return Customer(**dict(row))

    def get_account(self, cid):
        """The customer's account (one account per customer in this version)."""
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM accounts WHERE customer_id = ? ORDER BY opened_at LIMIT 1",
                               (cid,)).fetchone()
        if row is None:
            raise BankError("Account not found.")
        return BankAccount(**dict(row))

    def find_beneficiary(self, number):
        """Return the masked holder name of another account (for transfer confirmation)."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT c.full_name, a.status FROM accounts a JOIN customers c USING (customer_id) "
                "WHERE a.number = ?", (number.strip(),)).fetchone()
        if row is None or row["status"] != "Active":
            raise BankError("Beneficiary account not found. Please check the account number.")
        return mask_name(row["full_name"])

    def transactions(self, number, start=None, end=None, kind=None, limit=None):
        sql = "SELECT * FROM transactions WHERE account_number = ?"
        args = [number]
        if start:
            sql += " AND created_at >= ?"
            args.append(start + " 00:00:00")
        if end:
            sql += " AND created_at <= ?"
            args.append(end + " 23:59:59")
        if kind in ("CR", "DR"):
            sql += " AND type = ?"
            args.append(kind)
        sql += " ORDER BY created_at DESC, id DESC"
        if limit:
            sql += f" LIMIT {int(limit)}"
        with self._connect() as conn:
            rows = conn.execute(sql, args).fetchall()
        return [Transaction(**{k: r[k] for k in r.keys() if k not in ("id", "reversed")}) for r in rows]

    def get_transaction(self, number, ref):
        with self._connect() as conn:
            r = conn.execute("SELECT * FROM transactions WHERE account_number = ? AND ref = ?",
                             (number, ref)).fetchone()
        if r is None:
            raise BankError("Transaction not found.")
        return Transaction(**{k: r[k] for k in r.keys() if k not in ("id", "reversed")})

    def month_summary(self, number):
        start = date.today().replace(day=1).isoformat() + " 00:00:00"
        with self._connect() as conn:
            rows = conn.execute("SELECT type, SUM(amount) s FROM transactions WHERE account_number = ? "
                                "AND created_at >= ? GROUP BY type", (number, start)).fetchall()
        sums = {r["type"]: r["s"] for r in rows}
        return {"credit": sums.get("CR", 0), "debit": sums.get("DR", 0)}

    def monthly_chart(self, number, months=6):
        """Money in / out for each of the last `months` months (for the dashboard chart)."""
        first = date.today().replace(day=1)
        keys = []
        for i in range(months - 1, -1, -1):
            y, m = first.year, first.month - i
            while m <= 0:
                y, m = y - 1, m + 12
            keys.append(f"{y}-{m:02d}")
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT substr(created_at, 1, 7) ym, type, SUM(amount) s FROM transactions "
                "WHERE account_number = ? AND created_at >= ? GROUP BY ym, type",
                (number, keys[0] + "-01")).fetchall()
        data = {k: {"CR": 0, "DR": 0} for k in keys}
        for r in rows:
            if r["ym"] in data:
                data[r["ym"]][r["type"]] = r["s"]
        return [{"month": datetime.strptime(k, "%Y-%m").strftime("%b"),
                 "credit": v["CR"], "debit": v["DR"]} for k, v in data.items()]

    def login_history(self, cid, limit=5):
        with self._connect() as conn:
            return [dict(r) for r in conn.execute(
                "SELECT at, ip, success FROM login_history WHERE customer_id = ? "
                "ORDER BY id DESC LIMIT ?", (cid, limit)).fetchall()]

    # ----- money operations -----

    def _check_pin(self, cid, pin):
        if not check_password_hash(self.get_customer(cid).pin_hash, pin):
            raise BankError("Incorrect transaction PIN.")

    def _today_total(self, conn, number, category):
        return conn.execute(
            "SELECT COALESCE(SUM(amount), 0) FROM transactions WHERE account_number = ? AND type = 'DR' "
            "AND category = ? AND created_at >= ?",
            (number, category, date.today().isoformat() + " 00:00:00")).fetchone()[0]

    def _load(self, conn, number, must_be_active=True):
        acc = BankAccount(**dict(conn.execute("SELECT * FROM accounts WHERE number = ?", (number,)).fetchone()))
        if must_be_active and acc.status != "Active":
            raise BankError("This account is frozen. Please contact your branch.")
        return acc

    def deposit(self, cid, amount, pin, note=""):
        self._check_pin(cid, pin)
        number = self.get_account(cid).number
        with self._connect() as conn:
            acc = self._load(conn, number)
            t = acc.credit(amount, "Deposit", "Cash deposit" + (f" - {note}" if note else ""))
            self._save(conn, t)
        return t

    def withdraw(self, cid, amount, pin, note=""):
        self._check_pin(cid, pin)
        number = self.get_account(cid).number
        with self._connect() as conn:
            limit = self.limit("daily_withdraw_limit")
            if self._today_total(conn, number, "Withdrawal") + amount > limit:
                raise BankError(f"Daily cash withdrawal limit is {format_money(limit)}.")
            acc = self._load(conn, number)
            t = acc.debit(amount, "Withdrawal", "Cash withdrawal" + (f" - {note}" if note else ""))
            self._save(conn, t)
        return t

    def transfer(self, cid, to_number, amount, pin, remarks=""):
        """Move money to another PyBank account. Both sides are saved in ONE database
        transaction, so money can never disappear or be created."""
        self._check_pin(cid, pin)
        sender = self.get_account(cid)
        to_number = to_number.strip()
        if to_number == sender.number:
            raise BankError("You cannot transfer money to the same account.")
        self.find_beneficiary(to_number)
        remarks = remarks.strip()[:60]
        with self._connect() as conn:
            limit = self.limit("daily_transfer_limit")
            if self._today_total(conn, sender.number, "Transfer") + amount > limit:
                raise BankError(f"Daily transfer limit is {format_money(limit)}.")
            src, dst = self._load(conn, sender.number), self._load(conn, to_number)
            src_name = conn.execute("SELECT full_name FROM customers WHERE customer_id = ?",
                                    (src.customer_id,)).fetchone()[0]
            dst_name = conn.execute("SELECT full_name FROM customers WHERE customer_id = ?",
                                    (dst.customer_id,)).fetchone()[0]
            note = f" - {remarks}" if remarks else ""
            out = src.debit(amount, "Transfer", f"IMPS/To {dst_name}/A/c XX{dst.number[-4:]}{note}")
            inn = dst.credit(amount, "Transfer", f"IMPS/From {src_name}/A/c XX{src.number[-4:]}{note}")
            inn.ref = out.ref
            self._save(conn, out)
            self._save(conn, inn)
        return out

    # ----- profile & security -----

    def update_contact(self, cid, email, mobile, address, city, state, pincode):
        email, mobile = email.strip().lower(), mobile.strip()
        if not EMAIL_RE.match(email):
            raise BankError("Please enter a valid email address.")
        if not MOBILE_RE.match(mobile):
            raise BankError("Please enter a valid 10-digit mobile number.")
        if not PINCODE_RE.match(pincode.strip()):
            raise BankError("Please enter a valid 6-digit PIN code.")
        if len(address.strip()) < 5 or not city.strip() or not state.strip():
            raise BankError("Please enter your full address.")
        with self._connect() as conn:
            taken = conn.execute("SELECT 1 FROM customers WHERE (email = ? OR mobile = ?) AND customer_id != ?",
                                 (email, mobile, cid)).fetchone()
            if taken:
                raise BankError("This email or mobile number is used by another customer.")
            conn.execute("UPDATE customers SET email = ?, mobile = ?, address = ?, city = ?, state = ?, "
                         "pincode = ? WHERE customer_id = ?",
                         (email, mobile, " ".join(address.split()), city.strip().title(),
                          state.strip(), pincode.strip(), cid))

    def change_password(self, cid, old, new):
        if not check_password_hash(self.get_customer(cid).password_hash, old):
            raise BankError("Current password is wrong.")
        check_password_strength(new)
        if old == new:
            raise BankError("New password must be different from the old one.")
        with self._connect() as conn:
            conn.execute("UPDATE customers SET password_hash = ? WHERE customer_id = ?",
                         (generate_password_hash(new), cid))

    def change_pin(self, cid, password, new_pin):
        if not check_password_hash(self.get_customer(cid).password_hash, password):
            raise BankError("Password is wrong.")
        check_pin_format(new_pin)
        with self._connect() as conn:
            conn.execute("UPDATE customers SET pin_hash = ? WHERE customer_id = ?",
                         (generate_password_hash(new_pin), cid))

    # ===== things the customer creates and manages =====

    # ----- saved beneficiaries (payees) -----

    def beneficiaries(self, cid):
        with self._connect() as conn:
            return [dict(r) for r in conn.execute(
                "SELECT b.*, c.full_name FROM beneficiaries b LEFT JOIN accounts a ON a.number = b.account_number "
                "LEFT JOIN customers c ON c.customer_id = a.customer_id WHERE b.customer_id = ? "
                "ORDER BY b.nickname", (cid,)).fetchall()]

    def _clean_nickname(self, nickname):
        nickname = " ".join((nickname or "").split())
        if not 2 <= len(nickname) <= 30:
            raise BankError("Nickname must have 2-30 characters.")
        return nickname

    def add_beneficiary(self, cid, number, nickname):
        number = (number or "").strip()
        nickname = self._clean_nickname(nickname)
        if not (number.isdigit() and len(number) == 12):
            raise BankError("Account number must be 12 digits.")
        if number == self.get_account(cid).number:
            raise BankError("You cannot add your own account as a beneficiary.")
        self.find_beneficiary(number)                       # must exist and be active
        with self._connect() as conn:
            if conn.execute("SELECT 1 FROM beneficiaries WHERE customer_id = ? AND account_number = ?",
                            (cid, number)).fetchone():
                raise BankError("This beneficiary is already saved.")
            if conn.execute("SELECT COUNT(*) FROM beneficiaries WHERE customer_id = ?", (cid,)).fetchone()[0] >= 25:
                raise BankError("You can save up to 25 beneficiaries.")
            conn.execute("INSERT INTO beneficiaries (customer_id, account_number, nickname, created_at) "
                         "VALUES (?, ?, ?, ?)", (cid, number, nickname, now()))

    def rename_beneficiary(self, cid, bid, nickname):
        nickname = self._clean_nickname(nickname)
        with self._connect() as conn:
            if conn.execute("UPDATE beneficiaries SET nickname = ? WHERE id = ? AND customer_id = ?",
                            (nickname, bid, cid)).rowcount == 0:
                raise BankError("Beneficiary not found.")

    def delete_beneficiary(self, cid, bid):
        with self._connect() as conn:
            if conn.execute("DELETE FROM beneficiaries WHERE id = ? AND customer_id = ?", (bid, cid)).rowcount == 0:
                raise BankError("Beneficiary not found.")

    # ----- fixed deposits -----

    def fixed_deposits(self, cid):
        with self._connect() as conn:
            rows = [dict(r) for r in conn.execute(
                "SELECT * FROM fixed_deposits WHERE customer_id = ? ORDER BY status, opened_at DESC", (cid,))]
        for fd in rows:
            fd["maturity_value"] = fd["principal"] + self._fd_interest(fd["principal"], fd["rate"], fd["months"] * 365 // 12)
            if fd["status"] == "Active":
                fd["value_today"] = self._fd_close_value(fd)[0]
        return rows

    @staticmethod
    def _fd_interest(principal, rate, days):
        return int(principal * rate / 100 * days / 365)

    def _fd_close_value(self, fd, today=None):
        today = today or date.today()
        opened = datetime.strptime(fd["opened_at"], "%Y-%m-%d %H:%M:%S").date()
        matures = date.fromisoformat(fd["matures_on"])
        if today >= matures:
            days, rate, early = (matures - opened).days, fd["rate"], False
        else:
            days, rate, early = (today - opened).days, max(fd["rate"] - FD_PREMATURE_PENALTY, 0), True
        return fd["principal"] + self._fd_interest(fd["principal"], rate, days), early

    def open_fd(self, cid, amount, months, pin, when=None):
        self._check_pin(cid, pin)
        months = int(months) if str(months).isdigit() else 0
        if months not in FD_TENURES:
            raise BankError("Please choose a valid deposit period.")
        if amount < FD_MIN:
            raise BankError(f"Minimum fixed deposit is {format_money(FD_MIN)}.")
        rate = self.fd_rates()[months]
        when = when or now()
        start = datetime.strptime(when, "%Y-%m-%d %H:%M:%S").date()
        y, m = divmod(start.month - 1 + months, 12)
        matures = start.replace(year=start.year + y, month=m + 1, day=min(start.day, 28))
        number = self.get_account(cid).number
        with self._connect() as conn:
            fd_no = self._unique(conn, "SELECT 1 FROM fixed_deposits WHERE fd_number = ?",
                                 lambda: f"FD{random.randint(10_000_000, 99_999_999)}")
            acc = self._load(conn, number)
            self._save(conn, acc.debit(amount, "Fixed deposit", f"FD BOOKING/{fd_no}/{months} MONTHS @ {rate:.2f}%", when))
            conn.execute("INSERT INTO fixed_deposits (fd_number, customer_id, principal, rate, months, opened_at, "
                         "matures_on) VALUES (?, ?, ?, ?, ?, ?, ?)",
                         (fd_no, cid, amount, rate, months, when, matures.isoformat()))
        return fd_no

    def close_fd(self, cid, fd_id, pin):
        self._check_pin(cid, pin)
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM fixed_deposits WHERE id = ? AND customer_id = ?", (fd_id, cid)).fetchone()
            if row is None:
                raise BankError("Fixed deposit not found.")
            fd = dict(row)
            if fd["status"] != "Active":
                raise BankError("This fixed deposit is already closed.")
            payout, early = self._fd_close_value(fd)
            acc = self._load(conn, self.get_account(cid).number)
            what = "PREMATURE CLOSURE" if early else "MATURITY"
            t = acc.credit(payout, "Fixed deposit", f"FD {what}/{fd['fd_number']}/PRINCIPAL + INTEREST")
            self._save(conn, t)
            conn.execute("UPDATE fixed_deposits SET status = 'Closed', closed_at = ?, payout = ? WHERE id = ?",
                         (now(), payout, fd_id))
        return t

    # ----- personal notes on transactions -----

    def tx_notes(self, cid):
        with self._connect() as conn:
            return dict(conn.execute("SELECT ref, note FROM tx_notes WHERE customer_id = ?", (cid,)).fetchall())

    def set_tx_note(self, cid, ref, note):
        """Empty note = delete the note."""
        note = " ".join((note or "").split())[:80]
        self.get_transaction(self.get_account(cid).number, ref)      # must be the customer's own transaction
        with self._connect() as conn:
            if note:
                conn.execute("INSERT INTO tx_notes (customer_id, ref, note) VALUES (?, ?, ?) "
                             "ON CONFLICT(customer_id, ref) DO UPDATE SET note = excluded.note", (cid, ref, note))
            else:
                conn.execute("DELETE FROM tx_notes WHERE customer_id = ? AND ref = ?", (cid, ref))

    # ----- service requests -----

    def service_requests(self, cid):
        with self._connect() as conn:
            return [dict(r) for r in conn.execute(
                "SELECT * FROM service_requests WHERE customer_id = ? ORDER BY id DESC", (cid,))]

    def _check_request(self, rtype, details):
        details = (details or "").strip()
        if rtype not in REQUEST_TYPES:
            raise BankError("Please choose a request type.")
        if not 5 <= len(details) <= 500:
            raise BankError("Please describe your request (5-500 characters).")
        return details

    def create_request(self, cid, rtype, details, when=None):
        details = self._check_request(rtype, details)
        when = when or now()
        with self._connect() as conn:
            cur = conn.execute("INSERT INTO service_requests (customer_id, type, details, created_at, updated_at) "
                               "VALUES (?, ?, ?, ?, ?)", (cid, rtype, details, when, when))
            return cur.lastrowid

    def _own_open_request(self, conn, cid, rid):
        r = conn.execute("SELECT * FROM service_requests WHERE id = ? AND customer_id = ?", (rid, cid)).fetchone()
        if r is None:
            raise BankError("Request not found.")
        if r["status"] != "Open":
            raise BankError("Only open requests can be changed. This one is already being handled.")
        return r

    def update_request(self, cid, rid, rtype, details):
        details = self._check_request(rtype, details)
        with self._connect() as conn:
            self._own_open_request(conn, cid, rid)
            conn.execute("UPDATE service_requests SET type = ?, details = ?, updated_at = ? WHERE id = ?",
                         (rtype, details, now(), rid))

    def delete_request(self, cid, rid):
        with self._connect() as conn:
            self._own_open_request(conn, cid, rid)
            conn.execute("DELETE FROM service_requests WHERE id = ?", (rid,))

    # ----- notices from the bank (shown to customers) -----

    def active_notices(self):
        with self._connect() as conn:
            return [dict(r) for r in conn.execute("SELECT * FROM notices WHERE active = 1 ORDER BY id DESC")]

    # ----- used by the demo data script and by bank staff -----

    def post(self, number, type, category, description, amount, when):
        with self._connect() as conn:
            acc = self._load(conn, number, must_be_active=False)
            t = (acc.credit if type == "CR" else acc.debit)(amount, category, description, when)
            self._save(conn, t)
        return t

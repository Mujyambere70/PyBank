"""
bank.py - core banking logic of PyBank (OOP part of the project).

Classes
    Transaction  - one credit (money in) or debit (money out) record
    Customer     - a bank customer (personal details, country, language, login data)
    BankAccount  - one account in one currency: credit(), debit(), balance
    Bank         - manages customers, accounts, transactions, cash requests, loans,
                   savings interest, alerts and two-factor login (SQLite or PostgreSQL).
                   More features come from the mixin classes in onboarding.py, payments.py,
                   statements.py and push.py.

Money is stored as an integer number of the smallest unit (paise, kopecks, cents: 1/100),
so there are no rounding errors like 0.1 + 0.2 = 0.30000000000000004.
"""

import base64
import gzip
import json
import random
import re
import secrets
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from werkzeug.security import check_password_hash, generate_password_hash

import credit
import i18n
import otp
import world
from core import BankError, now
from db import Database
from notify import Notifier
from onboarding import OnboardingMixin
from payments import PaymentsMixin
from push import PushMixin
from statements import StatementsMixin
from world import format_money, policy

# ---------- bank settings ----------

BANK_NAME = "PyBank"
BRANCH = world.BRANCHES["IN"]           # head office

ACCOUNT_TYPES = {
    #            minimum opening deposit (rupee accounts; see world.CURRENCIES), yearly interest rate
    "Savings": {"min_balance": 500 * 100, "interest": 3.5},
    "Current": {"min_balance": 5000 * 100, "interest": 0.0},
}

MAX_AMOUNT = 10_000_000 * 100          # one transaction: max ₹1 crore (other currencies: world.CURRENCIES)
DAILY_WITHDRAW_LIMIT = 50_000 * 100     # cash withdrawal per day (rupee accounts)
DAILY_TRANSFER_LIMIT = 200_000 * 100    # fund transfer per day (rupee accounts)
MAX_FAILED_LOGINS = 5
LOCK_MINUTES = 15

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[a-zA-Z]{2,}$")
MOBILE_RE = re.compile(r"^[6-9]\d{9}$")       # Indian mobile number
PINCODE_RE = re.compile(r"^[1-9]\d{5}$")


# ---------- helper functions ----------

def parse_amount(text, currency=None):
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
    if paise > policy(currency or world.current_currency(), "max_tx"):
        raise BankError("Amount is too large for one transaction.")
    return paise


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
    currency: str = "INR"

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
    totp_secret: str = ""
    totp_enabled: int = 0
    totp_last_step: int = 0
    alert_email: int = 1
    alert_sms: int = 1
    country: str = "IN"
    lang: str = "en"
    status: str = "Active"          # Active, Pending (waiting for staff approval), Rejected
    status_reason: str = ""
    opening_deposit: int = 0
    decided_by: str = ""
    decided_at: str = ""
    estatement: int = 1

    @property
    def first_name(self):
        return self.full_name.split()[0]

    @property
    def initials(self):
        return "".join(w[0] for w in self.full_name.split()[:2]).upper()


class BankAccount:
    """A single bank account. Changes the balance and returns Transaction records."""

    def __init__(self, number, customer_id, type, balance, ifsc, branch,
                 card_last4, status, opened_at, currency="INR"):
        self.number = number
        self.customer_id = customer_id
        self.type = type
        self.balance = balance
        self.ifsc = ifsc
        self.branch = branch
        self.card_last4 = card_last4
        self.status = status
        self.opened_at = opened_at
        self.currency = currency

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
                           self.balance, when or now(), self.number, self.currency)

    def debit(self, amount, category, description, when=None):
        """Money out. The balance can never go below zero."""
        if amount <= 0:
            raise BankError("Amount must be greater than zero.")
        if amount > self.balance:
            raise BankError(f"Insufficient balance. Available balance is {format_money(self.balance, self.currency)}.")
        self.balance -= amount
        return Transaction(new_ref(when), "DR", category, description, amount,
                           self.balance, when or now(), self.number, self.currency)

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
    last_login    TEXT NOT NULL DEFAULT '',
    totp_secret   TEXT NOT NULL DEFAULT '',
    totp_enabled  INTEGER NOT NULL DEFAULT 0,
    totp_last_step INTEGER NOT NULL DEFAULT 0,
    alert_email   INTEGER NOT NULL DEFAULT 1,
    alert_sms     INTEGER NOT NULL DEFAULT 1,
    country       TEXT NOT NULL DEFAULT 'IN',
    lang          TEXT NOT NULL DEFAULT 'en',
    status        TEXT NOT NULL DEFAULT 'Active',
    status_reason TEXT NOT NULL DEFAULT '',
    opening_deposit INTEGER NOT NULL DEFAULT 0,
    decided_by    TEXT NOT NULL DEFAULT '',
    decided_at    TEXT NOT NULL DEFAULT '',
    estatement    INTEGER NOT NULL DEFAULT 1
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
    opened_at   TEXT NOT NULL,
    currency    TEXT NOT NULL DEFAULT 'INR'
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
    created_at     TEXT NOT NULL,
    reversed       INTEGER NOT NULL DEFAULT 0
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
    last_login    TEXT NOT NULL DEFAULT '',
    totp_secret   TEXT NOT NULL DEFAULT '',
    totp_enabled  INTEGER NOT NULL DEFAULT 0,
    totp_last_step INTEGER NOT NULL DEFAULT 0
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
-- deposits and withdrawals wait here until a staff member approves them
CREATE TABLE IF NOT EXISTS cash_requests (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    ref            TEXT NOT NULL UNIQUE,
    customer_id    TEXT NOT NULL REFERENCES customers(customer_id),
    account_number TEXT NOT NULL,
    kind           TEXT NOT NULL CHECK (kind IN ('Deposit', 'Withdrawal')),
    amount         INTEGER NOT NULL,
    note           TEXT NOT NULL DEFAULT '',
    status         TEXT NOT NULL DEFAULT 'Pending',
    created_at     TEXT NOT NULL,
    decided_at     TEXT NOT NULL DEFAULT '',
    decided_by     TEXT NOT NULL DEFAULT '',
    reason         TEXT NOT NULL DEFAULT '',
    tx_ref         TEXT NOT NULL DEFAULT ''
);
-- loans with EMI (equated monthly instalment) schedules
CREATE TABLE IF NOT EXISTS loans (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    loan_number    TEXT NOT NULL UNIQUE,
    customer_id    TEXT NOT NULL REFERENCES customers(customer_id),
    account_number TEXT NOT NULL,
    product        TEXT NOT NULL,
    principal      INTEGER NOT NULL,
    rate           REAL NOT NULL,
    months         INTEGER NOT NULL,
    emi            INTEGER NOT NULL,
    purpose        TEXT NOT NULL,
    status         TEXT NOT NULL DEFAULT 'Pending',
    applied_at     TEXT NOT NULL,
    decided_at     TEXT NOT NULL DEFAULT '',
    decided_by     TEXT NOT NULL DEFAULT '',
    reason         TEXT NOT NULL DEFAULT '',
    disbursed_at   TEXT NOT NULL DEFAULT '',
    outstanding    INTEGER NOT NULL DEFAULT 0,
    emis_paid      INTEGER NOT NULL DEFAULT 0,
    next_due       TEXT NOT NULL DEFAULT '',
    last_paid_on   TEXT NOT NULL DEFAULT '',
    closed_at      TEXT NOT NULL DEFAULT '',
    credit_score   INTEGER NOT NULL DEFAULT 0,
    credit_report  TEXT NOT NULL DEFAULT '',
    penalty_due    INTEGER NOT NULL DEFAULT 0,
    penalty_total  INTEGER NOT NULL DEFAULT 0,
    late_count     INTEGER NOT NULL DEFAULT 0,
    last_penalty_for TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS loan_payments (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    loan_id       INTEGER NOT NULL REFERENCES loans(id),
    number        INTEGER NOT NULL,
    paid_at       TEXT NOT NULL,
    amount        INTEGER NOT NULL,
    interest      INTEGER NOT NULL,
    principal     INTEGER NOT NULL,
    balance_after INTEGER NOT NULL,
    tx_ref        TEXT NOT NULL,
    kind          TEXT NOT NULL DEFAULT 'EMI'
);
-- email / SMS / in-app alerts for every transaction
CREATE TABLE IF NOT EXISTS alerts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    customer_id TEXT NOT NULL,
    channel     TEXT NOT NULL,
    destination TEXT NOT NULL DEFAULT '',
    subject     TEXT NOT NULL,
    body        TEXT NOT NULL,
    status      TEXT NOT NULL,
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_alerts_customer ON alerts(customer_id, id);
-- quarterly savings interest: one row per quarter that was paid
CREATE TABLE IF NOT EXISTS interest_runs (
    quarter  TEXT PRIMARY KEY,
    run_at   TEXT NOT NULL,
    accounts INTEGER NOT NULL,
    total    INTEGER NOT NULL,
    totals   TEXT NOT NULL DEFAULT ''
);
-- copies of the whole database (automatic every day + manual)
CREATE TABLE IF NOT EXISTS backups (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    kind       TEXT NOT NULL,
    created_by TEXT NOT NULL DEFAULT '',
    size       INTEGER NOT NULL,
    data       TEXT NOT NULL
);
-- uploaded files: ID proof for a new account, documents of a loan application or a profile change
CREATE TABLE IF NOT EXISTS documents (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    customer_id TEXT NOT NULL,
    kind        TEXT NOT NULL,
    purpose     TEXT NOT NULL,
    filename    TEXT NOT NULL,
    mime        TEXT NOT NULL,
    size        INTEGER NOT NULL,
    data        TEXT NOT NULL,
    uploaded_at TEXT NOT NULL
);
-- changes of personal data asked by the customer, applied only after staff approval
CREATE TABLE IF NOT EXISTS profile_changes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    customer_id TEXT NOT NULL REFERENCES customers(customer_id),
    changes     TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'Pending',
    created_at  TEXT NOT NULL,
    decided_at  TEXT NOT NULL DEFAULT '',
    decided_by  TEXT NOT NULL DEFAULT '',
    reason      TEXT NOT NULL DEFAULT ''
);
-- payments to other banks: NEFT / IMPS / UPI (India) and SWIFT (abroad), in a sandbox
CREATE TABLE IF NOT EXISTS external_transfers (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    ref                 TEXT NOT NULL UNIQUE,
    customer_id         TEXT NOT NULL REFERENCES customers(customer_id),
    account_number      TEXT NOT NULL,
    rail                TEXT NOT NULL,
    beneficiary_name    TEXT NOT NULL,
    beneficiary_account TEXT NOT NULL,
    bank_code           TEXT NOT NULL DEFAULT '',
    bank_name           TEXT NOT NULL DEFAULT '',
    country             TEXT NOT NULL DEFAULT '',
    currency            TEXT NOT NULL,
    amount              INTEGER NOT NULL,
    remarks             TEXT NOT NULL DEFAULT '',
    status              TEXT NOT NULL DEFAULT 'Processing',
    created_at          TEXT NOT NULL,
    settle_after        TEXT NOT NULL,
    settled_at          TEXT NOT NULL DEFAULT '',
    utr                 TEXT NOT NULL DEFAULT '',
    reason              TEXT NOT NULL DEFAULT '',
    tx_ref              TEXT NOT NULL DEFAULT '',
    return_ref          TEXT NOT NULL DEFAULT ''
);
-- exchange rates: units of a currency for 1 US dollar
CREATE TABLE IF NOT EXISTS fx_rates (
    currency   TEXT PRIMARY KEY,
    per_usd    REAL NOT NULL,
    updated_at TEXT NOT NULL,
    source     TEXT NOT NULL
);
-- monthly e-statements (PDF)
CREATE TABLE IF NOT EXISTS statements (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    customer_id TEXT NOT NULL,
    month       TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    size        INTEGER NOT NULL,
    data        TEXT NOT NULL,
    emailed     TEXT NOT NULL DEFAULT '',
    UNIQUE (customer_id, month)
);
-- phones and browsers that receive push notifications
CREATE TABLE IF NOT EXISTS push_subscriptions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    customer_id TEXT NOT NULL,
    endpoint    TEXT NOT NULL UNIQUE,
    p256dh      TEXT NOT NULL,
    auth        TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    device      TEXT NOT NULL DEFAULT ''
);
"""

# columns added after the first version (older databases get them automatically)
MIGRATIONS = {
    "transactions": [("reversed", "INTEGER NOT NULL DEFAULT 0")],
    "customers": [("totp_secret", "TEXT NOT NULL DEFAULT ''"), ("totp_enabled", "INTEGER NOT NULL DEFAULT 0"),
                  ("totp_last_step", "INTEGER NOT NULL DEFAULT 0"), ("alert_email", "INTEGER NOT NULL DEFAULT 1"),
                  ("alert_sms", "INTEGER NOT NULL DEFAULT 1"), ("country", "TEXT NOT NULL DEFAULT 'IN'"),
                  ("lang", "TEXT NOT NULL DEFAULT 'en'"), ("status", "TEXT NOT NULL DEFAULT 'Active'"),
                  ("status_reason", "TEXT NOT NULL DEFAULT ''"), ("opening_deposit", "INTEGER NOT NULL DEFAULT 0"),
                  ("decided_by", "TEXT NOT NULL DEFAULT ''"), ("decided_at", "TEXT NOT NULL DEFAULT ''"),
                  ("estatement", "INTEGER NOT NULL DEFAULT 1")],
    "accounts": [("currency", "TEXT NOT NULL DEFAULT 'INR'")],
    "interest_runs": [("totals", "TEXT NOT NULL DEFAULT ''")],
    "loans": [("credit_score", "INTEGER NOT NULL DEFAULT 0"), ("credit_report", "TEXT NOT NULL DEFAULT ''"),
              ("penalty_due", "INTEGER NOT NULL DEFAULT 0"), ("penalty_total", "INTEGER NOT NULL DEFAULT 0"),
              ("late_count", "INTEGER NOT NULL DEFAULT 0"), ("last_penalty_for", "TEXT NOT NULL DEFAULT ''")],
    "staff": [("totp_secret", "TEXT NOT NULL DEFAULT ''"), ("totp_enabled", "INTEGER NOT NULL DEFAULT 0"),
              ("totp_last_step", "INTEGER NOT NULL DEFAULT 0")],
}

# tables in the order they can be filled (parents before children) - used by backups
DATA_TABLES = ["customers", "accounts", "transactions", "login_history", "beneficiaries", "fixed_deposits",
               "tx_notes", "service_requests", "cash_requests", "loans", "loan_payments", "alerts", "staff",
               "notices", "settings", "audit_log", "interest_runs", "documents", "profile_changes",
               "external_transfers", "fx_rates", "statements", "push_subscriptions"]

# Bank-wide settings the admin can change (stored in the settings table)
DEFAULT_SETTINGS = {
    "daily_withdraw_limit": str(DAILY_WITHDRAW_LIMIT),
    "daily_transfer_limit": str(DAILY_TRANSFER_LIMIT),
    "fd_rate_6": "6.00", "fd_rate_12": "6.80", "fd_rate_24": "7.00", "fd_rate_36": "7.25",
    "online_account_opening": "1",
    "savings_rate": "3.50",
    "loan_rate_Personal": "10.75", "loan_rate_Vehicle": "8.90", "loan_rate_Education": "9.15",
    "late_fee_percent": "2.00",          # penalty for a late EMI, % of the EMI
}
FD_TENURES = (6, 12, 24, 36)                 # months
FD_MIN = 1000 * 100                          # ₹1,000
FD_PREMATURE_PENALTY = 1.0                   # % less interest if closed early
LOAN_PRODUCTS = ["Personal", "Vehicle", "Education"]
LOAN_TENURES = (6, 12, 24, 36, 48, 60)        # months
LOAN_MIN, LOAN_MAX = 10_000 * 100, 10_00_000 * 100
CASH_STATUSES = ["Pending", "Approved", "Rejected", "Cancelled"]
REQUEST_TYPES = ["Cheque book", "Debit card block", "New debit card", "Account statement by post",
                 "Update nominee", "Address proof letter", "Account closure", "Complaint", "Other"]


class Bank(OnboardingMixin, PaymentsMixin, StatementsMixin, PushMixin):
    """Keeps all customers, accounts and transactions in a database (SQLite file or PostgreSQL)."""

    def __init__(self, db_path="bank.db", notifier=None, driver=None, fx_fetch=None):
        self.db = Database(db_path, driver=driver)
        self.db_path = str(db_path)
        self.notifier = notifier or Notifier()
        self.fx_fetch = fx_fetch                 # function that downloads exchange rates (None = internet)
        self.alerts_on = True
        self.db.init_schema(SCHEMA)
        with self._connect() as conn:
            for table, cols in MIGRATIONS.items():
                have = conn.columns(table)
                for name, ddl in cols:
                    if name not in have:
                        conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")

    # ----- settings -----

    def settings(self):
        with self._connect() as conn:
            saved = dict(conn.execute("SELECT key, value FROM settings").fetchall())
        return {**DEFAULT_SETTINGS, **saved}

    def limit(self, name):
        return int(self.settings()[name])

    def limit_for(self, kind, currency):
        """Daily limit ('withdraw' or 'transfer') of an account in its own currency."""
        s = self.settings()
        if currency == "INR":
            return int(s[f"daily_{kind}_limit"])
        return int(s.get(f"limit_{kind}_{currency}") or policy(currency, kind))

    def fd_rates(self):
        s = self.settings()
        return {m: float(s[f"fd_rate_{m}"]) for m in FD_TENURES}

    # ----- database helpers -----

    def save_setting(self, conn, key, value):
        conn.execute("INSERT INTO settings (key, value) VALUES (?, ?) "
                     "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, str(value)))

    def secret_key(self):
        """A secret for signing login cookies, kept in the database so it survives restarts
        and is the same for every web worker (used when SECRET_KEY is not set)."""
        s = self.settings().get("secret_key")
        if s:
            return s
        with self._connect() as conn:
            conn.execute("INSERT INTO settings (key, value) VALUES ('secret_key', ?) ON CONFLICT(key) DO NOTHING",
                         (secrets.token_hex(32),))
        return self.settings()["secret_key"]

    # ----- database helpers -----

    def _connect(self):
        """Open the DB, commit if everything worked (rollback on error), always close."""
        return self.db.connect()

    @contextmanager
    def alerts_paused(self):
        """Used while loading demo data, so thousands of old transactions do not create alerts."""
        self.alerts_on = False
        try:
            yield
        finally:
            self.alerts_on = True

    def _save(self, conn, t):
        conn.execute(
            "INSERT INTO transactions (ref, account_number, type, category, description, "
            "amount, balance_after, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (t.ref, t.account_number, t.type, t.category, t.description,
             t.amount, t.balance_after, t.created_at))
        conn.execute("UPDATE accounts SET balance = ? WHERE number = ?",
                     (t.balance_after, t.account_number))
        if self.alerts_on:
            self._transaction_alert(conn, t)

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

    def register(self, data, when=None, customer_id=None, online=False, documents=None):
        """Open a new customer + account. `data` is a dict from the form.
        online=True: an application from the website. It waits for staff approval (status Pending);
        the opening deposit is credited when it is approved. An ID document is required.
        documents: [(kind, file name, bytes)]. Returns (customer_id, account)."""
        d = {k: " ".join(str(v).split()) for k, v in data.items() if k not in ("password", "pin")}
        password, pin = data.get("password", ""), data.get("pin", "")
        country = (d.get("country") or "IN").upper()
        lang = d.get("lang") if d.get("lang") in world.LANGUAGES else "en"

        if country not in world.DIAL_CODES:
            raise BankError("Please choose your country.")
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
        mobile = world.clean_mobile(country, d.get("mobile", ""))
        if not mobile:
            raise BankError("Please enter a valid 10-digit mobile number." if country == "IN"
                            else "Please enter a valid mobile number for your country.")
        if len(d.get("address", "")) < 5 or not d.get("city") or (country == "IN" and not d.get("state")):
            raise BankError("Please enter your full address.")
        postcode = world.clean_postcode(country, d.get("pincode", ""))
        if not postcode:
            raise BankError("Please enter a valid 6-digit PIN code." if country == "IN"
                            else "Please enter a valid postal code.")
        if not d.get("occupation"):
            raise BankError("Please select your occupation.")
        if d.get("account_type") not in ACCOUNT_TYPES:
            raise BankError("Please choose an account type.")
        currency = "RUB" if lang == "ru" else (d.get("currency") or world.default_currency(country, lang))
        if currency not in world.CURRENCIES:
            raise BankError("Please choose the currency of your account.")
        check_password_strength(password)
        check_pin_format(pin)

        initial = (parse_amount(d.get("initial_deposit"), currency)
                   if d.get("initial_deposit") not in (None, "", "0") else 0)
        minimum = policy(currency, "savings" if d["account_type"] == "Savings" else "current")
        if initial < minimum:
            raise BankError(f"Minimum opening deposit for a {d['account_type']} account "
                            f"is {format_money(minimum, currency)}.")
        files = [self.check_document(kind, name, blob) for kind, name, blob in (documents or [])]
        if online and not any(f[0] == "ID proof" for f in files):
            raise BankError("Please upload a photo or scan of your passport or national ID card.")

        when = when or now()
        branch = world.branch_for(country, currency)
        status = "Pending" if online else "Active"
        with self._connect() as conn:
            if conn.execute("SELECT 1 FROM customers WHERE email = ?", (d["email"],)).fetchone():
                raise BankError("An account with this email already exists.")
            if conn.execute("SELECT 1 FROM customers WHERE mobile = ?", (mobile,)).fetchone():
                raise BankError("An account with this mobile number already exists.")

            cid = customer_id or self._unique(conn, "SELECT 1 FROM customers WHERE customer_id = ?",
                               lambda: f"PB{random.randint(10_000_000, 99_999_999)}")
            number = self._unique(conn, "SELECT 1 FROM accounts WHERE number = ?",
                                  lambda: f"5021{random.randint(0, 99_999_999):08d}")
            conn.execute(
                "INSERT INTO customers (customer_id, full_name, dob, gender, email, mobile, address, "
                "city, state, pincode, occupation, password_hash, pin_hash, created_at, country, lang, status, "
                "opening_deposit) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (cid, d["full_name"].title(), d["dob"], d["gender"], d["email"], mobile,
                 d["address"], d["city"].title(), d.get("state", ""), postcode, d["occupation"],
                 generate_password_hash(password), generate_password_hash(pin), when, country, lang, status,
                 initial if online else 0))
            account = BankAccount(number, cid, d["account_type"], 0, branch["ifsc"], branch["name"],
                                  f"{random.randint(0, 9999):04d}", status, when, currency)
            conn.execute(
                "INSERT INTO accounts (number, customer_id, type, balance, ifsc, branch, card_last4, "
                "status, opened_at, currency) VALUES (?, ?, ?, 0, ?, ?, ?, ?, ?, ?)",
                (number, cid, account.type, account.ifsc, account.branch, account.card_last4, status, when, currency))
            for kind, name, mime, blob in files:
                self.save_document(conn, cid, kind, "account", name, mime, blob, when)
            if online:
                self._alert(conn, cid, "Account application received",
                            f"Thank you! Your application {cid} was received. The bank will check your details and "
                            f"documents and tell you when your account is ready.")
            elif initial:
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
            elif c.status != "Active":
                conn.execute("UPDATE customers SET failed_logins = 0 WHERE customer_id = ?", (c.customer_id,))
                error = ("Your account application is being checked by the bank. You will get an email "
                         "when it is approved." if c.status == "Pending" else
                         f"Your account application was not approved. Reason: {c.status_reason}")
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
        lock = " FOR UPDATE" if conn.is_postgres else ""          # two payments at once wait for each other
        acc = BankAccount(**dict(conn.execute("SELECT * FROM accounts WHERE number = ?" + lock, (number,)).fetchone()))
        if must_be_active and acc.status != "Active":
            raise BankError("This account is not approved yet." if acc.status == "Pending"
                            else "This account is frozen. Please contact your branch.")
        return acc

    # ----- deposits and withdrawals: the customer asks, a staff member approves -----

    def _reserved(self, conn, number):
        """Money held for withdrawal requests that wait for approval."""
        return conn.execute("SELECT COALESCE(SUM(amount), 0) FROM cash_requests WHERE account_number = ? "
                            "AND kind = 'Withdrawal' AND status = 'Pending'", (number,)).fetchone()[0]

    def available_balance(self, number):
        with self._connect() as conn:
            balance = conn.execute("SELECT balance FROM accounts WHERE number = ?", (number,)).fetchone()[0]
            return balance - self._reserved(conn, number)

    def _check_available(self, conn, acc, amount):
        reserved = self._reserved(conn, acc.number)
        if amount > acc.balance - reserved:
            extra = (f" ({format_money(reserved, acc.currency)} is held for withdrawal requests waiting for approval)"
                     if reserved else "")
            raise BankError(f"Insufficient balance. Available balance is "
                            f"{format_money(acc.balance - reserved, acc.currency)}{extra}.")

    def request_cash(self, cid, kind, amount, pin, note=""):
        """Create a deposit or withdrawal request. The money moves only when bank staff approve it."""
        if kind not in ("Deposit", "Withdrawal"):
            raise BankError("Unknown request type.")
        self._check_pin(cid, pin)
        number = self.get_account(cid).number
        note = " ".join((note or "").split())[:40]
        when = now()
        with self._connect() as conn:
            acc = self._load(conn, number)
            if kind == "Withdrawal":
                self._check_available(conn, acc, amount)
                limit = self.limit_for("withdraw", acc.currency)
                pending_today = conn.execute(
                    "SELECT COALESCE(SUM(amount), 0) FROM cash_requests WHERE account_number = ? AND kind = 'Withdrawal' "
                    "AND status = 'Pending' AND created_at >= ?", (number, date.today().isoformat())).fetchone()[0]
                if self._today_total(conn, number, "Withdrawal") + pending_today + amount > limit:
                    raise BankError(f"Daily cash withdrawal limit is {format_money(limit, acc.currency)}.")
            if conn.execute("SELECT COUNT(*) FROM cash_requests WHERE customer_id = ? AND status = 'Pending'",
                            (cid,)).fetchone()[0] >= 5:
                raise BankError("You already have 5 requests waiting for approval. Please wait for the branch.")
            ref = self._unique(conn, "SELECT 1 FROM cash_requests WHERE ref = ?",
                               lambda: f"CR{datetime.now():%y%m%d}{random.randint(0, 99_999):05d}")
            conn.execute("INSERT INTO cash_requests (ref, customer_id, account_number, kind, amount, note, created_at) "
                         "VALUES (?, ?, ?, ?, ?, ?, ?)", (ref, cid, number, kind, amount, note, when))
            self._alert(conn, cid, f"{kind} request {ref} received",
                        f"Your {kind.lower()} request {ref} for {format_money(amount, acc.currency)} was sent to the branch "
                        f"and is waiting for approval.", only_in_app=True)
        return ref

    def cash_requests(self, cid, limit=30):
        with self._connect() as conn:
            return [dict(r) for r in conn.execute(
                "SELECT * FROM cash_requests WHERE customer_id = ? ORDER BY id DESC LIMIT ?", (cid, limit))]

    def cancel_cash_request(self, cid, rid):
        with self._connect() as conn:
            if conn.execute("UPDATE cash_requests SET status = 'Cancelled', decided_at = ? WHERE id = ? "
                            "AND customer_id = ? AND status = 'Pending'", (now(), rid, cid)).rowcount == 0:
                raise BankError("Only requests that are still waiting can be cancelled.")

    def decide_cash_request(self, rid, approve, decided_by, reason=""):
        """Bank staff approve (money moves now) or reject a request. Returns (request, transaction or None)."""
        reason = " ".join((reason or "").split())[:200]
        if not approve and len(reason) < 3:
            raise BankError("Please write the reason for rejecting the request.")
        with self._connect() as conn:
            r = conn.execute("SELECT * FROM cash_requests WHERE id = ?", (rid,)).fetchone()
            if r is None:
                raise BankError("Request not found.")
            r = dict(r)
            # claim the request first, so two staff members can never approve it twice
            if conn.execute("UPDATE cash_requests SET status = ?, decided_at = ?, decided_by = ?, reason = ? "
                            "WHERE id = ? AND status = 'Pending'",
                            ("Approved" if approve else "Rejected", now(), decided_by, reason, rid)).rowcount == 0:
                raise BankError(f"This request was already handled ({r['status']}).")
            t = None
            if approve:
                acc = self._load(conn, r["account_number"])
                note = f" - {r['note']}" if r["note"] else ""
                if r["kind"] == "Deposit":
                    t = acc.credit(r["amount"], "Deposit", f"CASH DEPOSIT/{r['ref']}{note}")
                else:
                    t = acc.debit(r["amount"], "Withdrawal", f"CASH WITHDRAWAL/{r['ref']}{note}")
                self._save(conn, t)
                conn.execute("UPDATE cash_requests SET tx_ref = ? WHERE id = ?", (t.ref, rid))
            else:
                cur = conn.execute("SELECT currency FROM accounts WHERE number = ?", (r["account_number"],)).fetchone()[0]
                self._alert(conn, r["customer_id"], f"{r['kind']} request {r['ref']} rejected",
                            f"Your {r['kind'].lower()} request {r['ref']} for {format_money(r['amount'], cur)} was not "
                            f"approved. Reason: {reason}")
        return r, t

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
            limit = self.limit_for("transfer", sender.currency)
            if self._today_total(conn, sender.number, "Transfer") + amount > limit:
                raise BankError(f"Daily transfer limit is {format_money(limit, sender.currency)}.")
            src, dst = self._load(conn, sender.number), self._load(conn, to_number)
            self._check_available(conn, src, amount)
            received, fx = amount, ""
            if dst.currency != src.currency:                 # different currencies: convert at the bank's rate
                received, rate = self.convert(amount, src.currency, dst.currency, conn)
                fx = f" (FX 1 {src.currency} = {rate:.4f} {dst.currency})"
            rail = "IMPS" if src.currency == dst.currency == "INR" else "TRANSFER"
            src_name = conn.execute("SELECT full_name FROM customers WHERE customer_id = ?",
                                    (src.customer_id,)).fetchone()[0]
            dst_name = conn.execute("SELECT full_name FROM customers WHERE customer_id = ?",
                                    (dst.customer_id,)).fetchone()[0]
            note = f" - {remarks}" if remarks else ""
            out = src.debit(amount, "Transfer", f"{rail}/To {dst_name}/A/c XX{dst.number[-4:]}{note}{fx}")
            inn = dst.credit(received, "Transfer", f"{rail}/From {src_name}/A/c XX{src.number[-4:]}{note}{fx}")
            inn.ref = out.ref
            self._save(conn, out)
            self._save(conn, inn)
        return out

    # ----- profile & security -----

    def check_contact(self, cid, email, mobile, address, city, state, pincode, country=None):
        """Check contact details; return them cleaned (email, mobile, address, city, state, postcode, country)."""
        country = (country or self.get_customer(cid).country or "IN").upper()
        email = (email or "").strip().lower()
        if country not in world.DIAL_CODES:
            raise BankError("Please choose your country.")
        if not EMAIL_RE.match(email):
            raise BankError("Please enter a valid email address.")
        clean_mobile = world.clean_mobile(country, mobile)
        if not clean_mobile:
            raise BankError("Please enter a valid 10-digit mobile number." if country == "IN"
                            else "Please enter a valid mobile number for your country.")
        postcode = world.clean_postcode(country, pincode)
        if not postcode:
            raise BankError("Please enter a valid 6-digit PIN code." if country == "IN"
                            else "Please enter a valid postal code.")
        if len((address or "").strip()) < 5 or not (city or "").strip() or (country == "IN" and not (state or "").strip()):
            raise BankError("Please enter your full address.")
        with self._connect() as conn:
            taken = conn.execute("SELECT 1 FROM customers WHERE (email = ? OR mobile = ?) AND customer_id != ?",
                                 (email, clean_mobile, cid)).fetchone()
        if taken:
            raise BankError("This email or mobile number is used by another customer.")
        return (email, clean_mobile, " ".join(address.split()), city.strip().title(), (state or "").strip(),
                postcode, country)

    def update_contact(self, cid, email, mobile, address, city, state, pincode, country=None):
        """Change contact details at once (used by bank staff; customers send a change request)."""
        email, mobile, address, city, state, pincode, country = self.check_contact(
            cid, email, mobile, address, city, state, pincode, country)
        with self._connect() as conn:
            conn.execute("UPDATE customers SET email = ?, mobile = ?, address = ?, city = ?, state = ?, "
                         "pincode = ?, country = ? WHERE customer_id = ?",
                         (email, mobile, address, city, state, pincode, country, cid))

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
        currency = self.get_account(cid).currency
        if amount < policy(currency, "fd_min"):
            raise BankError(f"Minimum fixed deposit is {format_money(policy(currency, 'fd_min'), currency)}.")
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
            self._check_available(conn, acc, amount)
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

    # ===== alerts (email / SMS / in-app) =====

    def _contact(self, conn, cid):
        return conn.execute("SELECT full_name, email, mobile, alert_email, alert_sms, lang FROM customers "
                            "WHERE customer_id = ?", (cid,)).fetchone()

    def _alert(self, conn, cid, subject, text, only_in_app=False):
        """Save an alert for the customer and, after the commit, send it by email/SMS if configured."""
        if not self.alerts_on:
            return
        c = self._contact(conn, cid)
        if c is None:
            return
        when = now()
        conn.execute("INSERT INTO alerts (customer_id, channel, destination, subject, body, status, created_at) "
                     "VALUES (?, 'in-app', '', ?, ?, 'Delivered', ?)", (cid, subject, text, when))
        if only_in_app:
            return
        # sent alerts are written in the customer's language (the in-app copy is translated when it is shown)
        lang = c["lang"] or "en"
        subject_l, text_l = i18n.translate_text(subject, lang), i18n.translate_text(text, lang)
        jobs = [(("push", sub["id"]), "push", dict(sub), subject_l, text_l) for sub in conn.execute(
            "SELECT * FROM push_subscriptions WHERE customer_id = ?", (cid,)).fetchall()]
        if jobs:
            jobs = [(j[0], j[1], {**j[2], "vapid": self.vapid_keys()}, j[3], j[4]) for j in jobs]
        status = self.notifier.status()
        for channel, wanted, to in (("email", c["alert_email"], c["email"]), ("sms", c["alert_sms"], c["mobile"])):
            if is_example_email(c["email"]):        # demo / made-up customer: never email or text anyone
                break
            if wanted and status[channel]:
                cur = conn.execute("INSERT INTO alerts (customer_id, channel, destination, subject, body, status, created_at) "
                                   "VALUES (?, ?, ?, ?, ?, 'Queued', ?)", (cid, channel, to, subject, text, when))
                greeting = i18n.translate_text("Dear {0},", lang).replace("{0}", c["full_name"].split()[0])
                jobs.append((cur.lastrowid, channel, to, subject_l, f"{greeting}\n\n{text_l}\n\n- PyBank"
                             if channel == "email" else text_l))
        if jobs:
            conn.after_commit.append(lambda: self.notifier.deliver(jobs, self._alert_result))

    def _alert_result(self, alert_id, status):
        if isinstance(alert_id, tuple):                  # push notification
            if status.startswith("Failed: gone"):        # the browser unsubscribed: forget it
                with self._connect() as conn:
                    conn.execute("DELETE FROM push_subscriptions WHERE id = ?", (alert_id[1],))
            return
        with self._connect() as conn:
            conn.execute("UPDATE alerts SET status = ? WHERE id = ?", (status[:150], alert_id))

    def _transaction_alert(self, conn, t):
        row = conn.execute("SELECT customer_id FROM accounts WHERE number = ?", (t.account_number,)).fetchone()
        if row is None:
            return
        word = "credited with" if t.type == "CR" else "debited by"
        when = datetime.strptime(t.created_at, "%Y-%m-%d %H:%M:%S").strftime("%d-%m-%y %H:%M")
        cur = t.currency
        text = (f"PyBank: A/c XX{t.account_number[-4:]} {word} {format_money(t.amount, cur, 'en')} on {when} "
                f"({t.category}, Ref {t.ref}). Avl bal {format_money(t.balance_after, cur, 'en')}. "
                f"Not you? Call 1800-202-5050.")
        self._alert(conn, row["customer_id"], f"A/c XX{t.account_number[-4:]} {word} {format_money(t.amount, cur, 'en')}",
                    text)

    def alerts(self, cid, limit=60):
        with self._connect() as conn:
            return [dict(r) for r in conn.execute(
                "SELECT * FROM alerts WHERE customer_id = ? ORDER BY created_at DESC, id DESC LIMIT ?", (cid, limit))]

    def set_alert_prefs(self, cid, email_on, sms_on):
        with self._connect() as conn:
            conn.execute("UPDATE customers SET alert_email = ?, alert_sms = ? WHERE customer_id = ?",
                         (1 if email_on else 0, 1 if sms_on else 0, cid))

    # ===== two-factor login (authenticator app) =====

    def enable_2fa(self, cid, secret, code):
        step = otp.verify(secret, code)
        if step is None:
            raise BankError("The code is not correct. Check the time on your phone and type the newest code.")
        with self._connect() as conn:
            conn.execute("UPDATE customers SET totp_secret = ?, totp_enabled = 1, totp_last_step = ? "
                         "WHERE customer_id = ?", (secret, step, cid))

    def disable_2fa(self, cid, password, code):
        c = self.get_customer(cid)
        if not check_password_hash(c.password_hash, password or ""):
            raise BankError("Password is wrong.")
        self.verify_2fa(cid, code)
        with self._connect() as conn:
            conn.execute("UPDATE customers SET totp_secret = '', totp_enabled = 0, totp_last_step = 0 "
                         "WHERE customer_id = ?", (cid,))

    def verify_2fa(self, cid, code):
        c = self.get_customer(cid)
        step = otp.verify(c.totp_secret, code, c.totp_last_step)
        if step is None:
            raise BankError("Wrong or expired code. Type the 6-digit code shown in your authenticator app.")
        with self._connect() as conn:
            conn.execute("UPDATE customers SET totp_last_step = ? WHERE customer_id = ?", (step, cid))

    # ===== loans with EMI =====

    def loan_rate(self, product):
        return float(self.settings()[f"loan_rate_{product}"])

    @staticmethod
    def emi(principal, rate, months):
        """EMI = P x i / (1 - (1 + i)^-n), i = monthly rate. Result in paise."""
        i = Decimal(str(rate)) / 1200
        if i == 0:
            return int((Decimal(principal) / months).to_integral_value(ROUND_HALF_UP))
        value = Decimal(principal) * i / (1 - (1 + i) ** -months)
        return int(value.to_integral_value(ROUND_HALF_UP))

    @staticmethod
    def _monthly_interest(balance, rate):
        return int((Decimal(balance) * Decimal(str(rate)) / 1200).to_integral_value(ROUND_HALF_UP))

    @classmethod
    def schedule(cls, principal, rate, months, emi, start, first_number=1):
        """Month-by-month plan: number, due date, EMI, interest, principal, balance after."""
        rows, balance, n = [], principal, first_number
        due = start
        while balance > 0 and n <= months + 1:
            interest = cls._monthly_interest(balance, rate)
            part = min(max(emi - interest, 0), balance)
            if n >= months:                       # last instalment pays everything that is left
                part = balance
            balance -= part
            rows.append({"number": n, "due": due.isoformat(), "emi": interest + part, "interest": interest,
                         "principal": part, "balance": balance})
            n += 1
            due = add_months(due, 1)
        return rows

    def credit_report(self, cid, new_emi=0, today=None):
        """PyBank's own credit score of a customer (see credit.py)."""
        today = today or date.today()
        acc = self.get_account(cid)
        start6 = add_months(today.replace(day=1), -6).isoformat()
        start90 = (today - timedelta(days=90)).isoformat()
        with self._connect() as conn:
            credits = {}
            for r in conn.execute("SELECT substr(created_at, 1, 7) ym, category, SUM(amount) s FROM transactions "
                                  "WHERE account_number = ? AND type = 'CR' AND created_at >= ? GROUP BY ym, category",
                                  (acc.number, start6)):
                if r["category"] not in credit.INCOME_SKIP and r["ym"] < today.isoformat()[:7]:
                    credits[r["ym"]] = credits.get(r["ym"], 0) + r["s"]
            balances = [r[0] for r in conn.execute(
                "SELECT balance_after FROM transactions WHERE account_number = ? AND created_at >= ?",
                (acc.number, start90))] or [acc.balance]
            loans = [dict(r) for r in conn.execute("SELECT status, emi, late_count, next_due FROM loans "
                                                   "WHERE customer_id = ?", (cid,))]
        for ln in loans:
            ln["overdue"] = ln["status"] == "Active" and bool(ln["next_due"]) and ln["next_due"] < today.isoformat()
        return credit.score(acc.opened_at, credits, balances, loans, new_emi, today)

    def apply_loan(self, cid, product, amount, months, purpose, pin, documents=None):
        """Loan application. From Net Banking `documents` must contain an ID proof and an income proof
        [(kind, file name, bytes)]. The credit score is saved with the application for the staff."""
        self._check_pin(cid, pin)
        if product not in LOAN_PRODUCTS:
            raise BankError("Please choose a loan type.")
        months = int(months) if str(months).isdigit() else 0
        if months not in LOAN_TENURES:
            raise BankError("Please choose a valid loan period.")
        acc = self.get_account(cid)
        low, high = policy(acc.currency, "loan_min"), policy(acc.currency, "loan_max")
        if not low <= amount <= high:
            raise BankError(f"Loan amount must be between {format_money(low, acc.currency)} and "
                            f"{format_money(high, acc.currency)}.")
        purpose = " ".join((purpose or "").split())
        if not 5 <= len(purpose) <= 200:
            raise BankError("Please describe the purpose of the loan (5-200 characters).")
        if acc.status != "Active":
            raise BankError("This account is frozen. Please contact your branch.")
        files = None
        if documents is not None:
            files = [self.check_document(kind, name, blob) for kind, name, blob in documents]
            kinds = {f[0] for f in files}
            if not {"ID proof", "Income proof"} <= kinds:
                raise BankError("Please upload your ID proof and an income proof (salary slip, bank statement "
                                "or tax return).")
        rate = self.loan_rate(product)
        emi = self.emi(amount, rate, months)
        report = self.credit_report(cid, emi)
        with self._connect() as conn:
            if conn.execute("SELECT COUNT(*) FROM loans WHERE customer_id = ? AND status IN ('Pending', 'Active')",
                            (cid,)).fetchone()[0] >= 2:
                raise BankError("You can have at most 2 loans (active or waiting for approval).")
            number = self._unique(conn, "SELECT 1 FROM loans WHERE loan_number = ?",
                                  lambda: f"LN{random.randint(10_000_000, 99_999_999)}")
            cur = conn.execute("INSERT INTO loans (loan_number, customer_id, account_number, product, principal, rate, "
                               "months, emi, purpose, applied_at, credit_score, credit_report) "
                               "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                               (number, cid, acc.number, product, amount, rate, months, emi, purpose, now(),
                                report["score"], json.dumps(report)))
            for kind, name, mime, blob in files or []:
                self.save_document(conn, cid, kind, f"loan:{cur.lastrowid}", name, mime, blob)
            self._alert(conn, cid, f"Loan application {number} received",
                        f"Your {product.lower()} loan application {number} for {format_money(amount, acc.currency, 'en')} "
                        f"over {months} months was received. The branch will reply soon.", only_in_app=True)
        return number

    def loans(self, cid):
        with self._connect() as conn:
            rows = [dict(r) for r in conn.execute("SELECT * FROM loans WHERE customer_id = ? ORDER BY id DESC", (cid,))]
        for ln in rows:
            ln["overdue"] = ln["status"] == "Active" and ln["next_due"] < date.today().isoformat()
        return rows

    def get_loan(self, cid, loan_id):
        with self._connect() as conn:
            ln = conn.execute("SELECT * FROM loans WHERE id = ? AND customer_id = ?", (loan_id, cid)).fetchone()
            if ln is None:
                raise BankError("Loan not found.")
            ln = dict(ln)
            ln["payments"] = [dict(r) for r in conn.execute(
                "SELECT * FROM loan_payments WHERE loan_id = ? ORDER BY id", (loan_id,))]
        ln["overdue"] = ln["status"] == "Active" and ln["next_due"] < date.today().isoformat()
        if ln["status"] == "Active":
            ln["plan"] = self.schedule(ln["outstanding"], ln["rate"], ln["months"], ln["emi"],
                                       date.fromisoformat(ln["next_due"]), ln["emis_paid"] + 1)
            ln["close_amount"] = ln["outstanding"] + self._accrued(ln) + (ln["penalty_due"] or 0)
        elif ln["status"] == "Pending":
            ln["plan"] = self.schedule(ln["principal"], ln["rate"], ln["months"], ln["emi"], add_months(date.today(), 1))
        else:
            ln["plan"] = []
        return ln

    def _accrued(self, ln, today=None):
        """Interest since the last payment (or the payout), paid when the loan is closed early."""
        today = today or date.today()
        since = date.fromisoformat((ln["last_paid_on"] or ln["disbursed_at"])[:10])
        days = max((today - since).days, 0)
        return int((Decimal(ln["outstanding"]) * Decimal(str(ln["rate"])) / 100 * days / 365).to_integral_value(ROUND_HALF_UP))

    def disburse_loan(self, conn, ln, when):
        """Pay the loan amount into the customer's account (called when staff approve the loan)."""
        acc = self._load(conn, ln["account_number"])
        t = acc.credit(ln["principal"], "Loan", f"LOAN DISBURSAL/{ln['loan_number']}/{ln['product'].upper()} LOAN")
        self._save(conn, t)
        start = datetime.strptime(when, "%Y-%m-%d %H:%M:%S").date()
        conn.execute("UPDATE loans SET status = 'Active', disbursed_at = ?, outstanding = ?, next_due = ? WHERE id = ?",
                     (when, ln["principal"], add_months(start, 1).isoformat(), ln["id"]))
        return t

    def _pay_instalment(self, conn, ln, when=None, kind="EMI"):
        """Pay the next EMI of a loan (plus any late fee) from the customer's account."""
        number = ln["emis_paid"] + 1
        row = self.schedule(ln["outstanding"], ln["rate"], ln["months"], ln["emi"],
                            date.fromisoformat(ln["next_due"]), number)[0]
        acc = self._load(conn, ln["account_number"])
        fee = ln.get("penalty_due", 0) or 0
        self._check_available(conn, acc, row["emi"] + fee)
        label = "AUTO-DEBIT EMI" if kind == "Auto-debit" else "EMI"
        t = acc.debit(row["emi"], "Loan EMI", f"{label} {number}/{ln['months']}/{ln['loan_number']}", when)
        self._save(conn, t)
        if fee:
            self._save(conn, acc.debit(fee, "Charges", f"LATE EMI FEE/{ln['loan_number']}", when))
        paid_on = (when or now())[:10]
        conn.execute("INSERT INTO loan_payments (loan_id, number, paid_at, amount, interest, principal, balance_after, "
                     "tx_ref, kind) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                     (ln["id"], number, when or now(), row["emi"] + fee, row["interest"], row["principal"],
                      row["balance"], t.ref, kind))
        closed = row["balance"] == 0
        conn.execute("UPDATE loans SET outstanding = ?, emis_paid = ?, next_due = ?, last_paid_on = ?, status = ?, "
                     "closed_at = ?, penalty_due = 0 WHERE id = ?",
                     (row["balance"], number, add_months(date.fromisoformat(ln["next_due"]), 1).isoformat(), paid_on,
                      "Closed" if closed else "Active", (when or now()) if closed else "", ln["id"]))
        return t

    def pay_emi(self, cid, loan_id, pin, when=None):
        self._check_pin(cid, pin)
        with self._connect() as conn:
            ln = conn.execute("SELECT * FROM loans WHERE id = ? AND customer_id = ?", (loan_id, cid)).fetchone()
            if ln is None:
                raise BankError("Loan not found.")
            ln = dict(ln)
            if ln["status"] != "Active":
                raise BankError("This loan is not active.")
            return self._pay_instalment(conn, ln, when)

    def collect_due_emis(self, today=None):
        """Auto-debit: on the due date the EMI is taken from the account. If there is not enough money,
        a late fee (late_fee_percent of the EMI) is charged once for that EMI; the debit is tried again every day."""
        today = today or date.today()
        pct = Decimal(self.settings()["late_fee_percent"])
        result = {"auto_debited": 0, "late": 0}
        with self._connect() as conn:
            ids = [r[0] for r in conn.execute("SELECT id FROM loans WHERE status = 'Active' AND next_due <= ?",
                                              (today.isoformat(),))]
        for loan_id in ids:
            for _ in range(12):                                     # several months behind: pay them one by one
                lock = " FOR UPDATE" if self.db.is_postgres else ""
                try:
                    with self._connect() as conn:
                        ln = dict(conn.execute("SELECT * FROM loans WHERE id = ?" + lock, (loan_id,)).fetchone())
                        if ln["status"] != "Active" or ln["next_due"] > today.isoformat():
                            break
                        self._pay_instalment(conn, ln, None, "Auto-debit")
                        result["auto_debited"] += 1
                except BankError:
                    fee = int((Decimal(ln["emi"]) * pct / 100).to_integral_value(ROUND_HALF_UP))
                    with self._connect() as conn:
                        if conn.execute("UPDATE loans SET penalty_due = penalty_due + ?, penalty_total = penalty_total + ?, "
                                        "late_count = late_count + 1, last_penalty_for = ? WHERE id = ? "
                                        "AND last_penalty_for != ?",
                                        (fee, fee, ln["next_due"], loan_id, ln["next_due"])).rowcount:
                            result["late"] += 1
                            cur = conn.execute("SELECT currency FROM accounts WHERE number = ?",
                                               (ln["account_number"],)).fetchone()[0]
                            self._alert(conn, ln["customer_id"], f"EMI of loan {ln['loan_number']} is overdue",
                                        f"The EMI of {format_money(ln['emi'], cur, 'en')} due on {ln['next_due']} could not "
                                        f"be debited: not enough money. A late fee of {format_money(fee, cur, 'en')} was "
                                        f"added. Please add money; we will try again tomorrow.")
                    break
        return result

    def close_loan(self, cid, loan_id, pin):
        """Pay back everything that is left (outstanding principal + interest since the last EMI)."""
        self._check_pin(cid, pin)
        with self._connect() as conn:
            ln = conn.execute("SELECT * FROM loans WHERE id = ? AND customer_id = ?", (loan_id, cid)).fetchone()
            if ln is None:
                raise BankError("Loan not found.")
            ln = dict(ln)
            if ln["status"] != "Active":
                raise BankError("This loan is not active.")
            interest = self._accrued(ln)
            amount = ln["outstanding"] + interest + (ln["penalty_due"] or 0)
            acc = self._load(conn, ln["account_number"])
            self._check_available(conn, acc, amount)
            t = acc.debit(amount, "Loan EMI", f"LOAN CLOSURE/{ln['loan_number']}/PRINCIPAL + INTEREST")
            self._save(conn, t)
            conn.execute("INSERT INTO loan_payments (loan_id, number, paid_at, amount, interest, principal, balance_after, "
                         "tx_ref, kind) VALUES (?, ?, ?, ?, ?, ?, 0, ?, 'Closure')",
                         (loan_id, ln["emis_paid"] + 1, now(), amount, interest, ln["outstanding"], t.ref))
            conn.execute("UPDATE loans SET outstanding = 0, penalty_due = 0, status = 'Closed', closed_at = ?, "
                         "last_paid_on = ? WHERE id = ?",
                         (now(), date.today().isoformat(), loan_id))
        return t

    def cancel_loan_application(self, cid, loan_id):
        with self._connect() as conn:
            if conn.execute("UPDATE loans SET status = 'Cancelled', decided_at = ? WHERE id = ? AND customer_id = ? "
                            "AND status = 'Pending'", (now(), loan_id, cid)).rowcount == 0:
                raise BankError("Only applications that are still waiting can be cancelled.")

    # ===== quarterly interest on savings accounts =====

    @staticmethod
    def quarter_of(day):
        q = (day.month - 1) // 3 + 1
        start = date(day.year, 3 * q - 2, 1)
        return f"{day.year}-Q{q}", start, add_months(start, 3) - timedelta(days=1)

    def due_quarters(self, today=None):
        """Finished quarters (at most the last 4) whose interest was not paid yet."""
        today = today or date.today()
        _, this_start, _ = self.quarter_of(today)
        with self._connect() as conn:
            done = {r[0] for r in conn.execute("SELECT quarter FROM interest_runs")}
            first = conn.execute("SELECT MIN(opened_at) FROM accounts").fetchone()[0]
        if not first:
            return []
        first_day = datetime.strptime(first, "%Y-%m-%d %H:%M:%S").date()
        out, end = [], this_start - timedelta(days=1)
        while len(out) < 4 and end >= first_day:
            name, start, end_q = self.quarter_of(end)
            if name not in done:
                out.append((name, start, end_q))
            end = start - timedelta(days=1)
        return list(reversed(out))

    def _daily_balance_sum(self, conn, number, start, end):
        """Sum of the end-of-day balances over the quarter (daily balance method used by banks)."""
        before = conn.execute("SELECT balance_after FROM transactions WHERE account_number = ? AND created_at < ? "
                              "ORDER BY created_at DESC, id DESC LIMIT 1", (number, start.isoformat())).fetchone()
        balance = before[0] if before else 0
        rows = conn.execute("SELECT created_at, balance_after FROM transactions WHERE account_number = ? "
                            "AND created_at >= ? AND created_at <= ? ORDER BY created_at, id",
                            (number, start.isoformat(), end.isoformat() + " 23:59:59")).fetchall()
        day_end = {}
        for r in rows:
            day_end[r[0][:10]] = r[1]
        total, d = 0, start
        while d <= end:
            balance = day_end.get(d.isoformat(), balance)
            total += balance
            d += timedelta(days=1)
        return total

    def post_quarterly_interest(self, today=None):
        """Credit interest for every finished quarter not paid yet. Safe to call many times."""
        rate = float(self.settings()["savings_rate"])
        results = []
        for name, start, end in self.due_quarters(today):
            with self._connect() as conn:
                # claim the quarter first: a second worker doing the same thing fails here and changes nothing
                if conn.execute("SELECT 1 FROM interest_runs WHERE quarter = ?", (name,)).fetchone():
                    continue
                conn.execute("INSERT INTO interest_runs (quarter, run_at, accounts, total) VALUES (?, ?, 0, 0)",
                             (name, now()))
                accounts = conn.execute("SELECT number, currency FROM accounts WHERE type = 'Savings' "
                                        "AND status != 'Pending' AND opened_at <= ?",
                                        (end.isoformat() + " 23:59:59",)).fetchall()
                paid, total, totals = 0, 0, {}
                for (number, currency) in accounts:
                    product = self._daily_balance_sum(conn, number, start, end)
                    interest = int((Decimal(product) * Decimal(str(rate)) / 100 / 365).to_integral_value(ROUND_HALF_UP))
                    if interest < 100:                     # less than ₹1 is not paid
                        continue
                    acc = self._load(conn, number, must_be_active=False)
                    self._save(conn, acc.credit(interest, "Interest",
                                                f"SAVINGS INTEREST {name} ({start:%d-%m-%Y} TO {end:%d-%m-%Y}) @ {rate:.2f}%"))
                    paid, total = paid + 1, total + interest
                    totals[currency] = totals.get(currency, 0) + interest
                conn.execute("UPDATE interest_runs SET accounts = ?, total = ?, totals = ? WHERE quarter = ?",
                             (paid, total, json.dumps(totals), name))
            results.append({"quarter": name, "accounts": paid, "total": total, "totals": totals})
        return results

    def interest_runs(self):
        with self._connect() as conn:
            rows = [dict(r) for r in conn.execute("SELECT * FROM interest_runs ORDER BY quarter DESC")]
        for r in rows:                                    # older rows (before currencies) were all in rupees
            r["totals"] = json.loads(r["totals"]) if r.get("totals") else ({"INR": r["total"]} if r["total"] else {})
        return rows

    # ===== backups =====

    def export_data(self):
        """Every table as JSON-ready lists of rows (password and PIN hashes included, so a restore can log in)."""
        data = {"app": "PyBank", "version": 2, "created_at": now(), "tables": {}}
        with self._connect() as conn:
            for t in DATA_TABLES:
                cur = conn.execute(f"SELECT * FROM {t}")
                rows = cur.fetchall()
                data["tables"][t] = [dict(zip(r.keys(), list(r))) for r in rows]
        return data

    @staticmethod
    def pack(data):
        return gzip.compress(json.dumps(data, ensure_ascii=False).encode())

    @staticmethod
    def unpack(blob):
        try:
            data = json.loads(gzip.decompress(blob) if blob[:2] == b"\x1f\x8b" else blob)
        except (OSError, ValueError):
            raise BankError("This file is not a PyBank backup.")
        if not isinstance(data, dict) or data.get("app") != "PyBank" or "tables" not in data:
            raise BankError("This file is not a PyBank backup.")
        return data

    def import_data(self, data):
        """Replace ALL data with the backup (one transaction: if anything fails, nothing changes)."""
        tables = data["tables"]
        with self._connect() as conn:
            for t in reversed(DATA_TABLES):
                conn.execute(f"DELETE FROM {t}")
            for t in DATA_TABLES:
                have = set(conn.columns(t))
                for row in tables.get(t, []):
                    cols = [c for c in row if c in have]
                    conn.execute(f"INSERT INTO {t} ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                                 [row[c] for c in cols])
                if "id" in have:
                    conn.reset_sequence(t)
        return {t: len(tables.get(t, [])) for t in DATA_TABLES}

    def save_backup(self, kind, by=""):
        blob = self.pack(self.export_data())
        with self._connect() as conn:
            cur = conn.execute("INSERT INTO backups (created_at, kind, created_by, size, data) VALUES (?, ?, ?, ?, ?)",
                               (now(), kind, by, len(blob), base64.b64encode(blob).decode()))
            if kind == "auto":                      # keep the last 7 automatic copies
                ids = [r[0] for r in conn.execute("SELECT id FROM backups WHERE kind = 'auto' ORDER BY id DESC")]
                for old in ids[7:]:
                    conn.execute("DELETE FROM backups WHERE id = ?", (old,))
            return cur.lastrowid, blob

    def backups(self):
        with self._connect() as conn:
            return [dict(r) for r in conn.execute(
                "SELECT id, created_at, kind, created_by, size FROM backups ORDER BY id DESC")]

    def backup_blob(self, backup_id):
        with self._connect() as conn:
            r = conn.execute("SELECT data FROM backups WHERE id = ?", (backup_id,)).fetchone()
        if r is None:
            raise BankError("Backup not found.")
        return base64.b64decode(r[0])

    # ===== daily housekeeping (called by the web app, at most once a day) =====

    def daily_tasks(self, today=None):
        """Pay savings interest after a quarter ends and make the automatic daily backup."""
        today = today or date.today()
        if self.settings().get("last_daily_tasks") == today.isoformat():
            return None
        with self._connect() as conn:                 # only one worker does it
            row = conn.execute("SELECT value FROM settings WHERE key = 'last_daily_tasks'").fetchone()
            if row is not None and row[0] == today.isoformat():
                return None
            self.save_setting(conn, "last_daily_tasks", today.isoformat())
        done = {"interest": self.post_quarterly_interest(today)}
        for name, job in (("fx", self.refresh_fx_rates), ("external", self.process_external),
                          ("emis", lambda: self.collect_due_emis(today)),
                          ("statements", lambda: self.monthly_estatements(today))):
            try:
                done[name] = job()
            except Exception as e:                       # one failing job must not stop the others
                done[name + "_error"] = str(e)[:120]
        bid, blob = self.save_backup("auto", "system")
        done["backup"] = bid
        to = self.notifier.env.get("BACKUP_EMAIL")
        if to and self.notifier.email_provider():
            try:
                self.notifier.send_email(to, f"PyBank automatic backup {today}",
                                         "The automatic daily backup of the PyBank database is attached.",
                                         attachment=(f"pybank_backup_{today}.json.gz", blob))
                done["emailed"] = to
            except Exception as e:
                done["email_error"] = str(e)[:120]
        return done


def is_example_email(email):
    """Addresses on reserved example domains (RFC 2606) belong to demo customers, not real people."""
    domain = str(email or "").rsplit("@", 1)[-1].lower()
    return domain in ("example.com", "example.org", "example.net") or domain.endswith((".example", ".test", ".invalid"))


def add_months(day, months):
    y, m = divmod(day.month - 1 + months, 12)
    return day.replace(year=day.year + y, month=m + 1, day=min(day.day, 28))

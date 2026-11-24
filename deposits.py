"""
deposits.py - recurring deposits and savings goals (version 5; "future scope" of version 4).

Recurring deposit (RD): the customer saves the same amount every month for 6-36 months. The
first instalment is paid when the RD is opened, the next ones are taken from the account on the
same day of each month (daily task). Every instalment earns interest from the day it is paid to the
maturity date, compounded every quarter like a fixed deposit:

    maturity value = sum over the instalments of  P x (1 + r/400)^(quarters left) x (1 + r x days left / 36500)

Three missed instalments stop the RD (the money paid stays and earns interest until maturity).
Closing it early pays the instalments with 1 % less interest.

Savings goals: a named "pot" (e.g. "Laptop", 60,000 by June). The customer moves money from his
account into the goal and back; money in a goal is not spent by mistake. A goal shows how much is
saved, how much is left and how much a month is needed to reach the target date.
"""

import random
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal

from core import BankError, now
from world import format_money, policy

DEPOSIT_SCHEMA = """
CREATE TABLE IF NOT EXISTS recurring_deposits (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    rd_number      TEXT NOT NULL UNIQUE,
    customer_id    TEXT NOT NULL,
    account_number TEXT NOT NULL,
    currency       TEXT NOT NULL,
    instalment     INTEGER NOT NULL,
    months         INTEGER NOT NULL,
    rate           REAL NOT NULL,
    paid_count     INTEGER NOT NULL DEFAULT 0,
    missed         INTEGER NOT NULL DEFAULT 0,
    next_due       TEXT NOT NULL,
    matures_on     TEXT NOT NULL,
    status         TEXT NOT NULL DEFAULT 'Active',
    opened_at      TEXT NOT NULL,
    closed_at      TEXT NOT NULL DEFAULT '',
    payout         INTEGER NOT NULL DEFAULT 0,
    paid_dates     TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS goals (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    customer_id    TEXT NOT NULL,
    account_number TEXT NOT NULL,
    currency       TEXT NOT NULL,
    name           TEXT NOT NULL,
    target         INTEGER NOT NULL,
    saved          INTEGER NOT NULL DEFAULT 0,
    target_date    TEXT NOT NULL,
    status         TEXT NOT NULL DEFAULT 'Active',
    created_at     TEXT NOT NULL,
    closed_at      TEXT NOT NULL DEFAULT ''
);
"""
RD_TENURES = (6, 12, 24, 36)
RD_MISSED_STOP = 3
RD_EARLY_PENALTY = 1.0
MAX_GOALS = 10


def add_months(day, months):
    y, m = divmod(day.month - 1 + months, 12)
    return day.replace(year=day.year + y, month=m + 1, day=min(day.day, 28))


def rd_value(instalment, rate, paid_dates, until):
    """Value on `until` of the instalments paid on `paid_dates` (quarterly compounding)."""
    total = Decimal(0)
    r = Decimal(str(rate))
    for d in paid_dates:
        days = max((until - d).days, 0)
        quarters = int(days * 4 // 365)
        rest = Decimal(str(days - quarters * 365 / 4))
        total += Decimal(instalment) * (1 + r / 400) ** quarters * (1 + r * rest / 36500)
    return int(total.to_integral_value(ROUND_HALF_UP))


class DepositsMixin:
    """Mixed into Bank."""

    # ===== recurring deposits =====

    def rd_rate(self, cid, months):
        try:
            rate = float(self.settings().get(f"rd_rate_{months}") or self.fd_rates()[months])
        except (ValueError, KeyError):
            rate = 6.5
        if self.is_senior(cid):
            rate += float(self.settings().get("fd_senior_extra", "0.50"))
        return round(rate, 2)

    def rd_min(self, currency):
        return max(policy(currency, "fd_min") // 10, 100)

    def rd_quote(self, cid, instalment, months):
        rate = self.rd_rate(cid, months)
        start = date.today()
        dates = [add_months(start, k) for k in range(months)]
        value = rd_value(instalment, rate, dates, add_months(start, months))
        return {"rate": rate, "paid": instalment * months, "value": value, "interest": value - instalment * months}

    def open_rd(self, cid, account_number, instalment, months, pin):
        self._check_pin(cid, pin)
        months = int(months) if str(months).isdigit() else 0
        if months not in RD_TENURES:
            raise BankError("Please choose how many months (6, 12, 24 or 36).")
        acc = self.account_of(cid, account_number) if account_number else self.get_account(cid)
        if instalment < self.rd_min(acc.currency):
            raise BankError(f"The monthly amount must be at least {format_money(self.rd_min(acc.currency), acc.currency)}.")
        rate = self.rd_rate(cid, months)
        today = date.today()
        with self._connect() as conn:
            rd_no = self._unique(conn, "SELECT 1 FROM recurring_deposits WHERE rd_number = ?",
                                 lambda: f"RD{random.randint(10_000_000, 99_999_999)}")
            a = self._load(conn, acc.number)
            self._check_available(conn, a, instalment)
            self._save(conn, a.debit(instalment, "Recurring deposit", f"RD INSTALMENT 1/{months}/{rd_no} @ {rate:.2f}%"))
            conn.execute("INSERT INTO recurring_deposits (rd_number, customer_id, account_number, currency, instalment, "
                         "months, rate, paid_count, next_due, matures_on, opened_at, paid_dates) "
                         "VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?)",
                         (rd_no, cid, acc.number, acc.currency, instalment, months, rate,
                          add_months(today, 1).isoformat(), add_months(today, months).isoformat(), now(),
                          today.isoformat()))
        return rd_no

    def recurring_deposits(self, cid):
        with self._connect() as conn:
            rows = [dict(r) for r in conn.execute("SELECT * FROM recurring_deposits WHERE customer_id = ? "
                                                  "ORDER BY id DESC", (cid,))]
        for r in rows:
            dates = [date.fromisoformat(d) for d in r["paid_dates"].split(",") if d]
            matures = date.fromisoformat(r["matures_on"])
            r["saved"] = r["instalment"] * r["paid_count"]
            r["value_now"] = rd_value(r["instalment"], r["rate"], dates, min(date.today(), matures))
            r["maturity_value"] = rd_value(r["instalment"], r["rate"],
                                           dates + [add_months(date.fromisoformat(r["next_due"]), k)
                                                    for k in range(r["months"] - r["paid_count"])]
                                           if r["status"] == "Active" else dates, matures)
        return rows

    def _rd(self, conn, cid, rd_id):
        r = conn.execute("SELECT * FROM recurring_deposits WHERE id = ? AND customer_id = ?", (rd_id, cid)).fetchone()
        if r is None:
            raise BankError("Recurring deposit not found.")
        return dict(r)

    def close_rd(self, cid, rd_id, pin, today=None):
        self._check_pin(cid, pin)
        today = today or date.today()
        with self._connect() as conn:
            r = self._rd(conn, cid, rd_id)
            if r["status"] not in ("Active", "Stopped"):
                raise BankError("This recurring deposit is already closed.")
            dates = [date.fromisoformat(d) for d in r["paid_dates"].split(",") if d]
            matures = date.fromisoformat(r["matures_on"])
            early = today < matures
            rate = max(r["rate"] - RD_EARLY_PENALTY, 0) if early else r["rate"]
            payout = rd_value(r["instalment"], rate, dates, min(today, matures))
            acc = self._load(conn, r["account_number"])
            t = acc.credit(payout, "Recurring deposit",
                           f"RD {'PREMATURE CLOSURE' if early else 'MATURITY'}/{r['rd_number']}/INSTALMENTS + INTEREST")
            self._save(conn, t)
            conn.execute("UPDATE recurring_deposits SET status = 'Closed', closed_at = ?, payout = ? WHERE id = ?",
                         (now(), payout, rd_id))
        return t

    def collect_rd_instalments(self, today=None):
        """Daily task: take the instalments that are due; pay out the RDs that matured."""
        today = today or date.today()
        done = {"paid": 0, "missed": 0, "matured": 0}
        with self._connect() as conn:
            due = [dict(r) for r in conn.execute("SELECT * FROM recurring_deposits WHERE status = 'Active' "
                                                 "AND next_due <= ?", (today.isoformat(),))]
        for r in due:
            nxt = date.fromisoformat(r["next_due"])
            if r["paid_count"] >= r["months"]:
                continue
            with self._connect() as conn:
                acc = self._load(conn, r["account_number"], must_be_active=False)
                avail = acc.balance - self._reserved(conn, acc.number)
                if acc.status == "Active" and avail >= r["instalment"]:
                    k = r["paid_count"] + 1
                    self._save(conn, acc.debit(r["instalment"], "Recurring deposit",
                                               f"RD INSTALMENT {k}/{r['months']}/{r['rd_number']}"))
                    conn.execute("UPDATE recurring_deposits SET paid_count = ?, next_due = ?, paid_dates = paid_dates || ? "
                                 "WHERE id = ?", (k, add_months(nxt, 1).isoformat(), "," + today.isoformat(), r["id"]))
                    done["paid"] += 1
                else:
                    missed = r["missed"] + 1
                    status = "Stopped" if missed >= RD_MISSED_STOP else "Active"
                    conn.execute("UPDATE recurring_deposits SET missed = ?, next_due = ?, status = ? WHERE id = ?",
                                 (missed, add_months(nxt, 1).isoformat(), status, r["id"]))
                    self._alert(conn, r["customer_id"], f"RD instalment missed ({r['rd_number']})",
                                f"The instalment of {format_money(r['instalment'], r['currency'])} for your recurring "
                                f"deposit {r['rd_number']} could not be taken (not enough money)."
                                + (" The deposit is stopped after 3 missed instalments." if status == "Stopped" else ""))
                    done["missed"] += 1
        with self._connect() as conn:
            matured = [dict(r) for r in conn.execute("SELECT * FROM recurring_deposits WHERE status IN ('Active', 'Stopped') "
                                                     "AND matures_on <= ?", (today.isoformat(),))]
        for r in matured:
            dates = [date.fromisoformat(d) for d in r["paid_dates"].split(",") if d]
            payout = rd_value(r["instalment"], r["rate"], dates, date.fromisoformat(r["matures_on"]))
            with self._connect() as conn:
                acc = self._load(conn, r["account_number"], must_be_active=False)
                self._save(conn, acc.credit(payout, "Recurring deposit", f"RD MATURITY/{r['rd_number']}/INSTALMENTS + INTEREST"))
                conn.execute("UPDATE recurring_deposits SET status = 'Matured', closed_at = ?, payout = ? WHERE id = ?",
                             (now(), payout, r["id"]))
            done["matured"] += 1
        return done

    # ===== savings goals =====

    def goals(self, cid):
        with self._connect() as conn:
            rows = [dict(r) for r in conn.execute("SELECT * FROM goals WHERE customer_id = ? AND status = 'Active' "
                                                  "ORDER BY target_date, id", (cid,))]
        today = date.today()
        for g in rows:
            left = max(g["target"] - g["saved"], 0)
            end = date.fromisoformat(g["target_date"])
            months = max((end.year - today.year) * 12 + end.month - today.month, 1)
            g["left"], g["percent"] = left, min(100, int(g["saved"] * 100 / g["target"])) if g["target"] else 0
            g["per_month"] = -(-left // months) if left else 0
            g["late"] = end < today and left > 0
        return rows

    def create_goal(self, cid, account_number, name, target, target_date):
        name = " ".join((name or "").split())[:40]
        if len(name) < 2:
            raise BankError("Please give the goal a name.")
        acc = self.account_of(cid, account_number) if account_number else self.get_account(cid)
        if target <= 0:
            raise BankError("Please enter the amount you want to save.")
        try:
            end = date.fromisoformat(target_date)
        except (TypeError, ValueError):
            raise BankError("Please choose the date of your goal.")
        if end <= date.today():
            raise BankError("The date of the goal must be in the future.")
        with self._connect() as conn:
            if conn.execute("SELECT COUNT(*) FROM goals WHERE customer_id = ? AND status = 'Active'", (cid,)).fetchone()[0] >= MAX_GOALS:
                raise BankError(f"You can have at most {MAX_GOALS} goals.")
            return conn.execute("INSERT INTO goals (customer_id, account_number, currency, name, target, target_date, "
                                "created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                                (cid, acc.number, acc.currency, name, target, end.isoformat(), now())).lastrowid

    def move_goal(self, cid, goal_id, amount, into, pin):
        """Move money into the goal (into=True) or back to the account."""
        self._check_pin(cid, pin)
        if amount <= 0:
            raise BankError("Please enter an amount.")
        with self._connect() as conn:
            g = conn.execute("SELECT * FROM goals WHERE id = ? AND customer_id = ? AND status = 'Active'",
                             (goal_id, cid)).fetchone()
            if g is None:
                raise BankError("Goal not found.")
            acc = self._load(conn, g["account_number"])
            if into:
                self._check_available(conn, acc, amount)
                t = acc.debit(amount, "Goal", f"TO GOAL/{g['name'].upper()}")
                new = g["saved"] + amount
            else:
                if amount > g["saved"]:
                    raise BankError(f"The goal has only {format_money(g['saved'], g['currency'])}.")
                t = acc.credit(amount, "Goal", f"FROM GOAL/{g['name'].upper()}")
                new = g["saved"] - amount
            self._save(conn, t)
            conn.execute("UPDATE goals SET saved = ? WHERE id = ?", (new, goal_id))
            if into and g["saved"] < g["target"] <= new:
                self._alert(conn, cid, f"Goal reached: {g['name']}",
                            f"Well done! You saved {format_money(new, g['currency'])} for “{g['name']}”.", only_in_app=True)
        return t

    def close_goal(self, cid, goal_id, pin):
        self._check_pin(cid, pin)
        with self._connect() as conn:
            g = conn.execute("SELECT * FROM goals WHERE id = ? AND customer_id = ? AND status = 'Active'",
                             (goal_id, cid)).fetchone()
            if g is None:
                raise BankError("Goal not found.")
            if g["saved"]:
                acc = self._load(conn, g["account_number"])
                self._save(conn, acc.credit(g["saved"], "Goal", f"GOAL CLOSED/{g['name'].upper()}"))
            conn.execute("UPDATE goals SET status = 'Closed', saved = 0, closed_at = ? WHERE id = ?", (now(), goal_id))

    def money_in_deposits(self, cid):
        """Totals for the dashboard: RDs and goals per currency."""
        out = {}
        for r in self.recurring_deposits(cid):
            if r["status"] in ("Active", "Stopped"):
                out[r["currency"]] = out.get(r["currency"], 0) + r["saved"]
        for g in self.goals(cid):
            out[g["currency"]] = out.get(g["currency"], 0) + g["saved"]
        return out


def parse_day(value):
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None

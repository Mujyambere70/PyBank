"""
creditcard.py - a credit card with a monthly bill and a minimum payment (version 6; future scope of v5).

The debit card spends the customer's own money. The credit card spends the bank's money up to a
credit limit, and the customer pays it back every month:

  1. The customer applies in Net Banking with his monthly income. PyBank suggests a limit from its
     own credit score (credit.py) and the income; a loan officer ("credit.decide") approves it with a
     limit or rejects it.
  2. Card payments (sandbox shops) use the limit; the "outstanding" amount grows.
  3. On the bill day (the 5th) a statement is made: what was spent, the total to pay and the
     MINIMUM to pay (5 % of the total, at least ₹200, or the whole amount if it is smaller), due
     20 days later.
  4. Paying the whole bill by the due date costs nothing. If less is paid, interest (setting
     "cc_rate", 3.5 % a month) is charged on what was not paid at the next bill; if even the
     minimum is not paid, a late fee (setting "cc_late_fee", ₹500) is added once.
"""

import random
from datetime import date, timedelta

from core import BankError, now
from world import format_money

CREDIT_SCHEMA = """
CREATE TABLE IF NOT EXISTS credit_cards (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    customer_id  TEXT NOT NULL,
    last4        TEXT NOT NULL DEFAULT '',
    income       INTEGER NOT NULL DEFAULT 0,
    requested    INTEGER NOT NULL DEFAULT 0,
    limit_amount INTEGER NOT NULL DEFAULT 0,
    outstanding  INTEGER NOT NULL DEFAULT 0,
    status       TEXT NOT NULL DEFAULT 'Applied',
    bill_day     INTEGER NOT NULL DEFAULT 5,
    score        INTEGER NOT NULL DEFAULT 0,
    reason       TEXT NOT NULL DEFAULT '',
    decided_by   TEXT NOT NULL DEFAULT '',
    decided_at   TEXT NOT NULL DEFAULT '',
    expires      TEXT NOT NULL DEFAULT '',
    applied_at   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS cc_transactions (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    card_id    INTEGER NOT NULL,
    kind       TEXT NOT NULL,
    amount     INTEGER NOT NULL,
    merchant   TEXT NOT NULL DEFAULT '',
    statement  INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS cc_statements (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    card_id      INTEGER NOT NULL,
    period_start TEXT NOT NULL,
    period_end   TEXT NOT NULL,
    purchases    INTEGER NOT NULL DEFAULT 0,
    payments     INTEGER NOT NULL DEFAULT 0,
    interest     INTEGER NOT NULL DEFAULT 0,
    fees         INTEGER NOT NULL DEFAULT 0,
    total        INTEGER NOT NULL DEFAULT 0,
    minimum      INTEGER NOT NULL DEFAULT 0,
    due_date     TEXT NOT NULL,
    paid         INTEGER NOT NULL DEFAULT 0,
    status       TEXT NOT NULL DEFAULT 'Open',
    created_at   TEXT NOT NULL
);
"""
CC_STATUSES = ["Applied", "Active", "Blocked", "Rejected", "Closed"]
MIN_PERCENT = 5
MIN_DUE = 200 * 100
DUE_DAYS = 20
LIMIT_MIN, LIMIT_MAX = 10_000 * 100, 10_00_000 * 100


def minimum_due(total):
    if total <= 0:
        return 0
    return min(total, max(MIN_DUE, total * MIN_PERCENT // 100))


class CreditCardMixin:
    """Mixed into Bank."""

    def credit_card(self, cid):
        with self._connect() as conn:
            r = conn.execute("SELECT * FROM credit_cards WHERE customer_id = ? AND status <> 'Rejected' "
                             "ORDER BY id DESC LIMIT 1", (cid,)).fetchone()
        if r is None:
            return None
        c = dict(r)
        c["available"] = max(0, c["limit_amount"] - c["outstanding"])
        return c

    def suggested_limit(self, cid, income):
        """Limit PyBank suggests: 2 to 4 months of income depending on the credit score."""
        rep = self.credit_report(cid)
        months = 4 if rep["score"] >= 750 else 3 if rep["score"] >= 650 else 2 if rep["score"] >= 550 else 0
        limit = min(LIMIT_MAX, income * months)
        return (limit // 100_000) * 100_000, rep["score"]          # round down to ₹1,000

    def apply_credit_card(self, cid, income, requested=0):
        if self.credit_card(cid):
            raise BankError("You already have a credit card or an application.")
        if income < 15_000 * 100:
            raise BankError("A credit card needs a monthly income of at least ₹15,000.")
        acc = self.get_account(cid)
        if acc.currency != "INR":
            raise BankError("Credit cards are issued for rupee accounts.")
        suggested, score = self.suggested_limit(cid, income)
        with self._connect() as conn:
            cid_ = conn.execute("INSERT INTO credit_cards (customer_id, income, requested, limit_amount, score, applied_at) "
                                "VALUES (?, ?, ?, ?, ?, ?)", (cid, income, requested or suggested, suggested, score,
                                                              now())).lastrowid
            self._alert(conn, cid, "Credit card application received",
                        "We received your credit card application. A loan officer will answer within 2 working days.",
                        only_in_app=True)
        return cid_

    def credit_card_applications(self, status="Applied"):
        with self._connect() as conn:
            return [dict(r) for r in conn.execute(
                "SELECT k.*, c.full_name FROM credit_cards k JOIN customers c ON c.customer_id = k.customer_id "
                + ("WHERE k.status = ? " if status else "") + "ORDER BY k.id DESC LIMIT 200", (status,) if status else ())]

    def decide_credit_card(self, by, card_id, approve, limit=0, reason=""):
        with self._connect() as conn:
            k = conn.execute("SELECT * FROM credit_cards WHERE id = ?", (card_id,)).fetchone()
            if k is None or k["status"] != "Applied":
                raise BankError("This application is not waiting.")
            if approve:
                if not LIMIT_MIN <= limit <= LIMIT_MAX:
                    raise BankError("The limit must be between ₹10,000 and ₹10,00,000.")
                last4 = f"{random.randint(0, 9999):04d}"
                conn.execute("UPDATE credit_cards SET status = 'Active', limit_amount = ?, last4 = ?, decided_by = ?, "
                             "decided_at = ?, expires = ? WHERE id = ?",
                             (limit, last4, by["staff_id"], now(), (date.today() + timedelta(days=5 * 365)).isoformat()[:7],
                              card_id))
                self._alert(conn, k["customer_id"], "Credit card approved",
                            f"Your PyBank credit card XX{last4} is ready with a limit of {format_money(limit, 'INR', 'en')}. "
                            f"The bill is made on the 5th of every month.")
            else:
                if len(" ".join((reason or "").split())) < 5:
                    raise BankError("Please write the reason for the customer.")
                conn.execute("UPDATE credit_cards SET status = 'Rejected', reason = ?, decided_by = ?, decided_at = ? "
                             "WHERE id = ?", (reason[:200], by["staff_id"], now(), card_id))
                self._alert(conn, k["customer_id"], "Credit card application",
                            f"We are sorry, your credit card application was not approved: {reason[:200]}")
            conn.execute("INSERT INTO audit_log (staff_id, action, target, details, at) VALUES (?, ?, ?, ?, ?)",
                         (by["staff_id"], "Credit card " + ("approved" if approve else "rejected"), k["customer_id"],
                          format_money(limit, "INR") if approve else reason[:200], now()))

    def set_credit_limit(self, by, card_id, limit):
        if not LIMIT_MIN <= limit <= LIMIT_MAX:
            raise BankError("The limit must be between ₹10,000 and ₹10,00,000.")
        with self._connect() as conn:
            k = conn.execute("SELECT * FROM credit_cards WHERE id = ?", (card_id,)).fetchone()
            if k is None or k["status"] not in ("Active", "Blocked"):
                raise BankError("Card not found.")
            if limit < k["outstanding"]:
                raise BankError("The new limit is lower than what the customer already owes.")
            conn.execute("UPDATE credit_cards SET limit_amount = ? WHERE id = ?", (limit, card_id))
            conn.execute("INSERT INTO audit_log (staff_id, action, target, details, at) VALUES (?, ?, ?, ?, ?)",
                         (by["staff_id"], "Credit limit changed", k["customer_id"], format_money(limit, "INR"), now()))

    def block_credit_card(self, cid, block=True):
        k = self.credit_card(cid)
        if k is None or k["status"] not in ("Active", "Blocked"):
            raise BankError("You have no active credit card.")
        with self._connect() as conn:
            conn.execute("UPDATE credit_cards SET status = ? WHERE id = ?", ("Blocked" if block else "Active", k["id"]))
            self._alert(conn, cid, "Credit card " + ("blocked" if block else "unblocked"),
                        f"Your credit card XX{k['last4']} was {'blocked' if block else 'unblocked'} in Net Banking.")

    def cc_purchase(self, card_id, amount, merchant, when=None):
        """Sandbox card network for the credit card: APPROVED or the reason for DECLINED."""
        with self._connect() as conn:
            k = conn.execute("SELECT * FROM credit_cards WHERE id = ?", (card_id,)).fetchone()
            if k is None:
                return "DECLINED: unknown card"
            if k["status"] != "Active":
                return "DECLINED: card " + k["status"].lower()
            if amount <= 0:
                return "DECLINED: wrong amount"
            if k["outstanding"] + amount > k["limit_amount"]:
                return "DECLINED: credit limit reached"
            conn.execute("UPDATE credit_cards SET outstanding = outstanding + ? WHERE id = ?", (amount, card_id))
            conn.execute("INSERT INTO cc_transactions (card_id, kind, amount, merchant, created_at) VALUES (?, ?, ?, ?, ?)",
                         (card_id, "Purchase", amount, " ".join(str(merchant).split())[:60], when or now()))
            return "APPROVED"

    def cc_transactions(self, card_id, limit=50):
        with self._connect() as conn:
            return [dict(r) for r in conn.execute("SELECT * FROM cc_transactions WHERE card_id = ? ORDER BY id DESC "
                                                  f"LIMIT {int(limit)}", (card_id,))]

    def cc_statements(self, card_id):
        with self._connect() as conn:
            return [dict(r) for r in conn.execute("SELECT * FROM cc_statements WHERE card_id = ? ORDER BY id DESC",
                                                  (card_id,))]

    def pay_credit_card(self, cid, how, amount, pin, from_number=None):
        """how: 'full' (the whole outstanding), 'minimum' (minimum of the last bill not yet paid) or 'other'."""
        self._check_pin(cid, pin)
        k = self.credit_card(cid)
        if k is None or k["status"] not in ("Active", "Blocked"):
            raise BankError("You have no active credit card.")
        sts = self.cc_statements(k["id"])
        if how == "full":
            amount = k["outstanding"]
        elif how == "minimum":
            amount = max(0, sts[0]["minimum"] - sts[0]["paid"]) if sts else 0
        if amount <= 0:
            raise BankError("There is nothing to pay.")
        if amount > k["outstanding"]:
            raise BankError(f"You owe only {format_money(k['outstanding'], 'INR')}.")
        acc = self.account_of(cid, from_number) if from_number else self.get_account(cid)
        if acc.currency != "INR":
            raise BankError("Pay the credit card from a rupee account.")
        with self._connect() as conn:
            a = self._load(conn, acc.number)
            self._check_available(conn, a, amount)
            t = a.debit(amount, "Bill payment", f"CREDIT CARD XX{k['last4']} PAYMENT")
            self._save(conn, t)
            conn.execute("UPDATE credit_cards SET outstanding = outstanding - ? WHERE id = ?", (amount, k["id"]))
            conn.execute("INSERT INTO cc_transactions (card_id, kind, amount, merchant, created_at) VALUES (?, 'Payment', ?, "
                         "?, ?)", (k["id"], amount, f"From A/c XX{acc.number[-4:]}", now()))
            if sts:
                s = sts[0]
                paid = s["paid"] + amount
                status = "Paid" if paid >= s["total"] else "Minimum paid" if paid >= s["minimum"] else s["status"]
                conn.execute("UPDATE cc_statements SET paid = ?, status = ? WHERE id = ?", (paid, status, s["id"]))
        return t

    def make_cc_statements(self, today=None):
        """Daily: on the bill day make the statement; after the due date add interest and late fees."""
        today = today or date.today()
        rate = float(self.settings().get("cc_rate", "3.5"))
        late_fee = int(float(self.settings().get("cc_late_fee", "500")) * 100)
        made = 0
        with self._connect() as conn:
            cards = [dict(r) for r in conn.execute("SELECT * FROM credit_cards WHERE status IN ('Active', 'Blocked')")]
        for k in cards:
            with self._connect() as conn:
                last = conn.execute("SELECT * FROM cc_statements WHERE card_id = ? ORDER BY id DESC LIMIT 1",
                                    (k["id"],)).fetchone()
                # late fee once, the day after the due date, if not even the minimum was paid
                if last is not None and last["status"] == "Open" and today.isoformat() > last["due_date"] \
                        and last["minimum"] > last["paid"] and not last["fees"]:
                    conn.execute("UPDATE credit_cards SET outstanding = outstanding + ? WHERE id = ?", (late_fee, k["id"]))
                    conn.execute("INSERT INTO cc_transactions (card_id, kind, amount, merchant, created_at) VALUES "
                                 "(?, 'Late fee', ?, 'Minimum not paid', ?)", (k["id"], late_fee, now()))
                    conn.execute("UPDATE cc_statements SET fees = ?, status = 'Overdue' WHERE id = ?", (late_fee, last["id"]))
                    self._alert(conn, k["customer_id"], "Credit card payment overdue",
                                f"The minimum payment of your credit card bill was not paid by {last['due_date']}. "
                                f"A late fee of {format_money(late_fee, 'INR', 'en')} was added.")
                if today.day != k["bill_day"] or (last is not None and last["period_end"] >= today.isoformat()):
                    continue
                start = (date.fromisoformat(last["period_end"]) + timedelta(days=1)) if last else \
                    date.fromisoformat(k["decided_at"][:10] or k["applied_at"][:10])
                end = today - timedelta(days=1)
                interest = 0
                if last is not None and last["paid"] < last["total"]:
                    interest = (last["total"] - last["paid"]) * rate / 100
                    interest = int(round(interest))
                    if interest:
                        conn.execute("UPDATE credit_cards SET outstanding = outstanding + ? WHERE id = ?",
                                     (interest, k["id"]))
                        conn.execute("INSERT INTO cc_transactions (card_id, kind, amount, merchant, created_at) VALUES "
                                     "(?, 'Interest', ?, ?, ?)", (k["id"], interest, f"{rate:.1f}% on unpaid bill", now()))
                rows = conn.execute("SELECT kind, COALESCE(SUM(amount), 0) FROM cc_transactions WHERE card_id = ? "
                                    "AND statement = 0 GROUP BY kind", (k["id"],)).fetchall()
                sums = {r[0]: r[1] for r in rows}
                total = conn.execute("SELECT outstanding FROM credit_cards WHERE id = ?", (k["id"],)).fetchone()[0]
                sid = conn.execute("INSERT INTO cc_statements (card_id, period_start, period_end, purchases, payments, "
                                   "interest, fees, total, minimum, due_date, status, created_at) VALUES (?, ?, ?, ?, ?, ?, "
                                   "0, ?, ?, ?, ?, ?)",
                                   (k["id"], start.isoformat(), end.isoformat(), sums.get("Purchase", 0),
                                    sums.get("Payment", 0), interest, total, minimum_due(total),
                                    (today + timedelta(days=DUE_DAYS)).isoformat(), "Open" if total > 0 else "Paid",
                                    now())).lastrowid
                conn.execute("UPDATE cc_transactions SET statement = ? WHERE card_id = ? AND statement = 0", (sid, k["id"]))
                if total > 0:
                    self._alert(conn, k["customer_id"], "Credit card bill",
                                f"Your credit card bill: total {format_money(total, 'INR', 'en')}, minimum "
                                f"{format_money(minimum_due(total), 'INR', 'en')}, pay by "
                                f"{(today + timedelta(days=DUE_DAYS)):%d %b %Y}.")
                made += 1
        return made

"""
wallets.py - accounts in several currencies for one customer, and the bank's buy / sell rates (version 4).

Until version 3 every customer had exactly one account. Now a customer keeps his first account
(the "main" account, with the debit card) and can open one more account in each other currency
of the bank, up to MAX_EXTRA. Money moves between his own accounts at the bank's rate:

    middle rate   = rate of the market (open.er-api.com, once a day) or typed by the admin
    customer rate = middle rate minus the bank's margin (setting "fx_margin", 1.00 % by default)

So 100 USD -> INR at a middle rate of 84.00 and a 1 % margin gives 8,316 rupees, and the screen shows
the margin before the customer confirms. Every day's middle rates are also kept in `fx_history`,
so the Rates page can show the change over the last week.
"""

import random
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal

import world
from core import BankError, now
from world import format_money

WALLET_SCHEMA = """
CREATE TABLE IF NOT EXISTS fx_history (
    day      TEXT NOT NULL,
    currency TEXT NOT NULL,
    per_usd  REAL NOT NULL,
    PRIMARY KEY (day, currency)
);
"""
MAX_EXTRA = 4                      # extra currency accounts per customer


class WalletsMixin:

    # ===== the customer's accounts =====

    def accounts_of(self, cid):
        """All accounts of a customer; the first one (oldest) is the main account."""
        from bank import BankAccount
        with self._connect() as conn:
            # the main account (the one with the debit card) first, then the currency accounts by age
            rows = conn.execute("SELECT * FROM accounts WHERE customer_id = ? "
                                "ORDER BY CASE WHEN card_last4 = '' THEN 1 ELSE 0 END, opened_at, number",
                                (cid,)).fetchall()
        return [BankAccount(**dict(r)) for r in rows]

    def account_of(self, cid, number):
        """One account that belongs to this customer (BankError otherwise - never another person's account)."""
        for acc in self.accounts_of(cid):
            if acc.number == str(number or "").strip():
                return acc
        raise BankError("Account not found.")

    def open_currency_account(self, cid, currency, pin):
        from bank import BankAccount
        self._check_pin(cid, pin)
        currency = (currency or "").upper()
        if currency not in world.CURRENCIES:
            raise BankError("Please choose a currency.")
        c = self.get_customer(cid)
        if c.status != "Active":
            raise BankError("Your account is not approved yet.")
        accounts = self.accounts_of(cid)
        if any(a.currency == currency for a in accounts):
            raise BankError(f"You already have an account in {currency}.")
        if len(accounts) - 1 >= MAX_EXTRA:
            raise BankError(f"You can have at most {MAX_EXTRA} extra currency accounts.")
        main = accounts[0]
        branch = world.branch_for(c.country, currency)
        when = now()
        with self._connect() as conn:
            number = self._unique(conn, "SELECT 1 FROM accounts WHERE number = ?",
                                  lambda: f"5021{random.randint(0, 99_999_999):08d}")
            conn.execute("INSERT INTO accounts (number, customer_id, type, balance, ifsc, branch, card_last4, status, "
                         "opened_at, currency) VALUES (?, ?, 'Savings', 0, ?, ?, '', 'Active', ?, ?)",
                         (number, cid, branch["ifsc"], branch["name"], when, currency))
            self._alert(conn, cid, f"{currency} account opened",
                        f"Your new {currency} account XX{number[-4:]} is ready. Move money into it from "
                        f"your {main.currency} account in Net Banking > My Accounts.", only_in_app=True)
        return BankAccount(number, cid, "Savings", 0, branch["ifsc"], branch["name"], "", "Active", when, currency)

    def close_currency_account(self, cid, number, pin):
        self._check_pin(cid, pin)
        accounts = self.accounts_of(cid)
        acc = self.account_of(cid, number)
        if acc.number == accounts[0].number:
            raise BankError("The main account cannot be closed in Net Banking. Please visit your branch.")
        if acc.balance:
            raise BankError("Move the money out of this account first; only an empty account can be closed.")
        with self._connect() as conn:
            if conn.execute("SELECT COUNT(*) FROM transactions WHERE account_number = ?", (acc.number,)).fetchone()[0]:
                conn.execute("UPDATE accounts SET status = 'Closed' WHERE number = ?", (acc.number,))
            else:
                conn.execute("DELETE FROM accounts WHERE number = ?", (acc.number,))

    # ===== rates =====

    def fx_margin(self):
        try:
            return max(0.0, min(5.0, float(self.settings().get("fx_margin", "1.00"))))
        except ValueError:
            return 1.0

    def middle_rate(self, src, dst, conn=None):
        if src == dst:
            return 1.0
        rows = conn.execute("SELECT currency, per_usd FROM fx_rates").fetchall() if conn else None
        rates = ({r[0]: r[1] for r in rows} if rows is not None
                 else {c: r["per_usd"] for c, r in self.fx_rates().items()})
        if src not in rates or dst not in rates:
            raise BankError(f"Transfers between {src} and {dst} accounts are not possible right now: "
                            f"the bank has no exchange rate. Please try again later.")
        return rates[dst] / rates[src]

    def customer_rate(self, src, dst, conn=None):
        """(customer rate, middle rate): how many `dst` the customer gets for 1 `src`."""
        mid = self.middle_rate(src, dst, conn)
        if src == dst:
            return 1.0, 1.0
        return mid * (1 - self.fx_margin() / 100), mid

    def quote(self, src, dst, amount, conn=None):
        rate, mid = self.customer_rate(src, dst, conn)
        keep = 1 - Decimal(str(self.fx_margin())) / 100 if src != dst else Decimal(1)
        at_mid_exact = Decimal(amount) * Decimal(str(mid))
        out = int((at_mid_exact * keep).to_integral_value(ROUND_HALF_UP))      # 8,800 x 0.0909 x 0.99 = 7,920
        at_mid = int(at_mid_exact.to_integral_value(ROUND_HALF_UP))
        if amount > 0 and out <= 0:
            raise BankError("The amount is too small to convert.")
        return {"amount": out, "rate": rate, "mid": mid, "margin": self.fx_margin(), "cost": max(0, at_mid - out),
                "src": src, "dst": dst}

    def rate_table(self, base="INR"):
        """For the Rates page: what the bank pays (buys) and asks (sells) for each currency, in `base`."""
        rates = {c: r["per_usd"] for c, r in self.fx_rates().items()}
        if base not in rates:
            return []
        m = self.fx_margin() / 100
        week = self.fx_history_days(8)
        old = week[0][1] if week and week[0][0] < date.today().isoformat() else {}   # no older day yet -> no change
        out = []
        for c in world.CURRENCIES:
            if c == base or c not in rates:
                continue
            mid = rates[base] / rates[c]                # base units for 1 unit of c
            change = None
            if c in old and base in old:
                before = old[base] / old[c]
                change = (mid - before) / before * 100 if before else None
            out.append({"currency": c, "mid": mid, "buy": mid * (1 - m), "sell": mid * (1 + m), "change": change})
        return out

    def save_fx_history(self, conn, rates, day=None):
        day = (day or date.today()).isoformat()
        for cur, per_usd in rates.items():
            conn.execute("DELETE FROM fx_history WHERE day = ? AND currency = ?", (day, cur))
            conn.execute("INSERT INTO fx_history (day, currency, per_usd) VALUES (?, ?, ?)", (day, cur, per_usd))

    def fx_history_days(self, days=30):
        since = (date.today() - timedelta(days=days)).isoformat()
        with self._connect() as conn:
            rows = conn.execute("SELECT day, currency, per_usd FROM fx_history WHERE day >= ? ORDER BY day",
                                (since,)).fetchall()
        out = {}
        for r in rows:
            out.setdefault(r["day"], {})[r["currency"]] = r["per_usd"]
        return sorted(out.items())

    # ===== moving money between own accounts =====

    def exchange(self, cid, from_number, to_number, amount, pin):
        """Move money between two accounts of the same customer (converted at the customer rate)."""
        self._check_pin(cid, pin)
        src, dst = self.account_of(cid, from_number), self.account_of(cid, to_number)
        if src.number == dst.number:
            raise BankError("Please choose two different accounts.")
        with self._connect() as conn:
            a, b = self._load(conn, src.number), self._load(conn, dst.number)
            self._check_available(conn, a, amount)
            q = self.quote(a.currency, b.currency, amount, conn)
            fx = (f" @ {q['rate']:.4f} (margin {q['margin']:.2f}%)" if a.currency != b.currency else "")
            out = a.debit(amount, "Exchange", f"OWN A/C XX{b.number[-4:]} {b.currency}{fx}")
            inn = b.credit(q["amount"], "Exchange", f"OWN A/C XX{a.number[-4:]} {a.currency}{fx}")
            inn.ref = out.ref
            self._save(conn, out)
            self._save(conn, inn)
        return out, q

    def total_in(self, cid, currency):
        """All the customer's money shown in one currency (middle rates) - for the dashboard."""
        total = 0
        for acc in self.accounts_of(cid):
            if acc.status == "Closed":
                continue
            try:
                total += int(acc.balance * self.middle_rate(acc.currency, currency))
            except BankError:
                return None
        return total

    def money_text(self, amount, currency):
        return format_money(amount, currency)

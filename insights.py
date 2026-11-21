"""
insights.py - where my money goes, and monthly budgets (version 4).

Every debit already has a category (Transfer, Bill payment, Card payment, Withdrawal, Charges ...).
The Insights page adds up this month's spending by category and compares it with the last 3 months.
A customer can set a monthly budget for a category; when the spending passes 80 % and again 100 %
of the budget, he gets an alert (once per level and month).
"""

from datetime import date

from core import BankError, now
from world import format_money

INSIGHT_SCHEMA = """
CREATE TABLE IF NOT EXISTS budgets (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    customer_id  TEXT NOT NULL,
    account_number TEXT NOT NULL,
    category     TEXT NOT NULL,
    monthly_limit INTEGER NOT NULL,
    alerted      TEXT NOT NULL DEFAULT '',
    created_at   TEXT NOT NULL,
    UNIQUE (account_number, category)
);
"""
SPENDING = ["Transfer", "Bill payment", "Card payment", "Withdrawal", "Charges", "Loan EMI", "Fixed deposit", "Exchange"]
NOT_SPENDING = {"Fixed deposit", "Exchange"}         # money that stays the customer's


def month_start(day=None):
    return (day or date.today()).replace(day=1).isoformat()


class InsightsMixin:

    def spending(self, number, months=4):
        """{month: {category: total}} for this month and the months before."""
        today = date.today()
        y, m = today.year, today.month - (months - 1)
        while m <= 0:
            y, m = y - 1, m + 12
        start = f"{y}-{m:02d}-01"
        with self._connect() as conn:
            rows = conn.execute("SELECT substr(created_at, 1, 7) ym, category, SUM(amount) s FROM transactions "
                                "WHERE account_number = ? AND type = 'DR' AND created_at >= ? AND reversed = 0 "
                                "GROUP BY ym, category", (number, start)).fetchall()
        out = {}
        for r in rows:
            out.setdefault(r["ym"], {})[r["category"]] = r["s"]
        return out

    def insights(self, number):
        data = self.spending(number)
        this = date.today().strftime("%Y-%m")
        earlier = [v for k, v in data.items() if k != this]
        cats = sorted({c for v in data.values() for c in v if c not in NOT_SPENDING})
        rows = []
        for c in cats:
            now_ = data.get(this, {}).get(c, 0)
            avg = sum(v.get(c, 0) for v in earlier) // max(1, len(earlier))
            rows.append({"category": c, "this_month": now_, "average": avg,
                         "change": (now_ - avg) * 100 // avg if avg else None})
        total = sum(r["this_month"] for r in rows)
        for r in rows:
            r["share"] = r["this_month"] * 100 // total if total else 0
        rows.sort(key=lambda r: -r["this_month"])
        return {"rows": rows, "total": total, "months": sorted(data)}

    def budgets(self, cid, number):
        spent = self.spending(number, 1).get(date.today().strftime("%Y-%m"), {})
        with self._connect() as conn:
            rows = [dict(r) for r in conn.execute("SELECT * FROM budgets WHERE customer_id = ? AND account_number = ? "
                                                  "ORDER BY category", (cid, number))]
        for b in rows:
            b["spent"] = spent.get(b["category"], 0)
            b["percent"] = b["spent"] * 100 // b["monthly_limit"] if b["monthly_limit"] else 0
        return rows

    def spending_categories(self, number):
        """The usual categories and every category this account has really spent on (last 4 months)."""
        used = {c for month in self.spending(number).values() for c in month}
        return sorted((set(SPENDING) | used) - NOT_SPENDING)

    def set_budget(self, cid, number, category, amount):
        acc = self.account_of(cid, number)
        if category not in self.spending_categories(acc.number):
            raise BankError("Please choose a spending category.")
        with self._connect() as conn:
            conn.execute("DELETE FROM budgets WHERE account_number = ? AND category = ?", (acc.number, category))
            if amount:
                conn.execute("INSERT INTO budgets (customer_id, account_number, category, monthly_limit, created_at) "
                             "VALUES (?, ?, ?, ?, ?)", (cid, acc.number, category, amount, now()))

    def _check_budget(self, conn, t):
        """Called after every debit: alert at 80 % and 100 % of a budget (once per level and month)."""
        if t.type != "DR" or t.category in NOT_SPENDING:
            return
        b = conn.execute("SELECT * FROM budgets WHERE account_number = ? AND category = ?",
                         (t.account_number, t.category)).fetchone()
        if b is None or b["monthly_limit"] <= 0:
            return
        month = t.created_at[:7]
        spent = conn.execute("SELECT COALESCE(SUM(amount), 0) FROM transactions WHERE account_number = ? AND type = 'DR' "
                             "AND category = ? AND created_at >= ?", (t.account_number, t.category, month + "-01")).fetchone()[0]
        level = 100 if spent >= b["monthly_limit"] else 80 if spent * 100 >= b["monthly_limit"] * 80 else 0
        done = b["alerted"] if b["alerted"].startswith(month) else ""
        if not level or done.endswith(f":{level}") or (level == 80 and done.endswith(":100")):
            return
        conn.execute("UPDATE budgets SET alerted = ? WHERE id = ?", (f"{month}:{level}", b["id"]))
        self._alert(conn, b["customer_id"], f"Budget for {t.category}: {level}% used",
                    f"You have spent {format_money(spent, t.currency, 'en')} on {t.category.lower()} this month - "
                    f"{level}% of your budget of {format_money(b['monthly_limit'], t.currency, 'en')}.", only_in_app=True)

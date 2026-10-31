"""
credit.py - PyBank's own credit score (300-900, like the scores of Indian credit bureaus).

This is a sandbox score made only from what PyBank itself knows about the customer.
It is NOT a report from a credit bureau. Bank staff see it next to every loan application.

    score = 300 + history + income + savings + affordability + repayment - overdue      (max 900)
"""

from datetime import date, datetime

BANDS = [(750, "Excellent"), (650, "Good"), (550, "Fair"), (0, "Poor")]
INCOME_SKIP = {"Loan", "Reversal", "Fixed deposit", "Interest"}       # not real income


def band(score):
    return next(name for low, name in BANDS if score >= low)


def score(opened_at, credits_by_month, balances, loans, new_emi, today=None):
    """
    opened_at        'YYYY-MM-DD ...' when the account was opened
    credits_by_month {'2026-05': amount, ...} money received in each of the last 6 months (no loans, no own FDs)
    balances         end-of-day balances of the last 90 days (any number of values)
    loans            list of dicts: status, emi, late_count, overdue (bool)
    new_emi          EMI of the loan applied for (0 = just a check)
    Returns {'score', 'band', 'parts': [(name, points, max points, explanation)]}.
    """
    today = today or date.today()
    opened = datetime.strptime(opened_at[:10], "%Y-%m-%d").date()
    months_open = max((today - opened).days // 30, 0)
    parts = []

    # 1. how long the customer has banked with PyBank (24 months = full points)
    p = min(months_open, 24) * 5
    parts.append(("Banking history", p, 120, f"{months_open} months with PyBank"))

    # 2. regular income: in how many of the last 6 months money came in
    active_months = sum(1 for v in credits_by_month.values() if v > 0)
    p = active_months * 20
    parts.append(("Regular income", p, 120, f"money received in {active_months} of the last 6 months"))

    # 3. savings: average balance compared with 3 EMIs
    avg_balance = sum(balances) / len(balances) if balances else 0
    need = 3 * (new_emi or 1)
    p = int(min(avg_balance / need, 1) * 150) if new_emi else (150 if avg_balance > 0 else 0)
    parts.append(("Savings", p, 150, "average balance of the last 90 days"))

    # 4. affordability: all EMIs (old + new) compared with average monthly income
    income = sum(credits_by_month.values()) / 6 if credits_by_month else 0
    emis = sum(ln["emi"] for ln in loans if ln["status"] == "Active") + new_emi
    if income <= 0:
        p, text = 0, "no regular income seen"
    else:
        share = emis / income
        p = 200 if share <= 0.3 else 120 if share <= 0.5 else 50 if share <= 0.7 else 0
        text = f"EMIs would take {share:.0%} of monthly income"
    parts.append(("Affordability", p, 200, text))

    # 5. repayment history of earlier PyBank loans
    history = [ln for ln in loans if ln["status"] in ("Active", "Closed")]
    late = sum(ln.get("late_count", 0) for ln in history)
    if not history:
        p, text = 80, "no earlier loan (thin file)"
    else:
        p = max(150 - 40 * late, 0) + (30 if any(ln["status"] == "Closed" for ln in history) else 0)
        text = f"{late} late EMI(s) on earlier loans"
    parts.append(("Repayment history", p, 180, text))

    # 6. an EMI that is overdue right now pulls the score down
    overdue = sum(1 for ln in loans if ln.get("overdue"))
    if overdue:
        parts.append(("Overdue now", -100 * overdue, 0, f"{overdue} loan(s) with an unpaid EMI"))

    total = max(300, min(900, 300 + sum(pt for _, pt, _, _ in parts)))
    return {"score": total, "band": band(total), "parts": parts}

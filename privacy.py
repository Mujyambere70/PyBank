"""
privacy.py - "download my data" and the yearly interest certificate (version 4).

Download my data: a customer can download, in one JSON file, everything PyBank keeps about him -
personal details, accounts, transactions, payees, deposits, loans, requests, alerts, logins, devices,
standing instructions, budgets and the list of his documents. Secrets (password and PIN hashes,
the two-factor key) and the document files themselves are not included.

Interest certificate: for a tax return a customer needs the interest he earned in a financial year
(in India 1 April - 31 March) on savings and fixed deposits, and the interest he paid on loans. The
certificate is a PDF made from the transactions; other countries use the calendar year.
"""

import io
import json
from datetime import date

import i18n
import world
from core import BankError, now
from statements import _fonts, shape
from world import format_money

SECRET = {"password_hash", "pin_hash", "totp_secret", "totp_last_step", "data", "p256dh", "auth", "endpoint",
          "token_hash"}


def financial_year(country, year):
    """(start, end, label) of the tax year that STARTS in `year`."""
    if country == "IN":
        return date(year, 4, 1), date(year + 1, 3, 31), f"FY {year}-{str(year + 1)[2:]}"
    return date(year, 1, 1), date(year, 12, 31), str(year)


class PrivacyMixin:

    def export_my_data(self, cid):
        c = self.get_customer(cid)
        out = {"about": "All data PyBank keeps about you (passwords, PINs and document files are not included).",
               "made_at": now(), "customer": {k: v for k, v in c.__dict__.items() if k not in SECRET}}
        accounts = [a.number for a in self.accounts_of(cid)]
        marks = ", ".join("?" * len(accounts)) or "''"
        queries = {
            "accounts": ("SELECT * FROM accounts WHERE customer_id = ?", (cid,)),
            "transactions": (f"SELECT ref, account_number, type, category, description, amount, balance_after, created_at "
                             f"FROM transactions WHERE account_number IN ({marks}) ORDER BY created_at", tuple(accounts)),
            "beneficiaries": ("SELECT * FROM beneficiaries WHERE customer_id = ?", (cid,)),
            "fixed_deposits": ("SELECT * FROM fixed_deposits WHERE customer_id = ?", (cid,)),
            "loans": ("SELECT * FROM loans WHERE customer_id = ?", (cid,)),
            "cash_requests": ("SELECT * FROM cash_requests WHERE customer_id = ?", (cid,)),
            "service_requests": ("SELECT * FROM service_requests WHERE customer_id = ?", (cid,)),
            "profile_changes": ("SELECT * FROM profile_changes WHERE customer_id = ?", (cid,)),
            "payments_to_other_banks": ("SELECT * FROM external_transfers WHERE customer_id = ?", (cid,)),
            "bill_payments": ("SELECT * FROM bill_payments WHERE customer_id = ?", (cid,)),
            "standing_instructions": ("SELECT * FROM standing_instructions WHERE customer_id = ?", (cid,)),
            "budgets": ("SELECT * FROM budgets WHERE customer_id = ?", (cid,)),
            "cards": ("SELECT id, account_number, last4, status, online, abroad, contactless, atm_limit, pos_limit, "
                      "expires, created_at FROM cards WHERE customer_id = ?", (cid,)),
            "alerts": ("SELECT * FROM alerts WHERE customer_id = ?", (cid,)),
            "login_history": ("SELECT * FROM login_history WHERE customer_id = ?", (cid,)),
            "devices": ("SELECT created_at, last_seen, ip, device, via, ended_at FROM sessions WHERE customer_id = ?", (cid,)),
            "api_tokens": ("SELECT name, created_at, last_used, expires_at, revoked FROM api_tokens WHERE customer_id = ?",
                           (cid,)),
            "documents": ("SELECT id, kind, purpose, filename, mime, size, uploaded_at FROM documents WHERE customer_id = ?",
                          (cid,)),
            "notes": ("SELECT * FROM tx_notes WHERE customer_id = ?", (cid,)),
        }
        with self._connect() as conn:
            for name, (sql, args) in queries.items():
                out[name] = [{k: v for k, v in dict(r).items() if k not in SECRET} for r in conn.execute(sql, args)]
        return json.dumps(out, ensure_ascii=False, indent=1, default=str)

    # ----- interest certificate -----

    def interest_summary(self, cid, year):
        c = self.get_customer(cid)
        start, end, label = financial_year(c.country, year)
        if start > date.today():
            raise BankError("Please choose a year that has started.")
        s, e = start.isoformat(), end.isoformat() + " 23:59:59"
        rows = []
        with self._connect() as conn:
            for acc in self.accounts_of(cid):
                savings = conn.execute("SELECT COALESCE(SUM(amount), 0) FROM transactions WHERE account_number = ? "
                                       "AND category = 'Interest' AND type = 'CR' AND created_at BETWEEN ? AND ?",
                                       (acc.number, s, e)).fetchone()[0]
                fd = conn.execute("SELECT COALESCE(SUM(payout - principal), 0) FROM fixed_deposits WHERE customer_id = ? "
                                  "AND status = 'Closed' AND closed_at BETWEEN ? AND ?", (cid, s, e)).fetchone()[0] \
                    if acc.number == self.get_account(cid).number else 0
                loan = conn.execute("SELECT COALESCE(SUM(p.interest), 0) FROM loan_payments p JOIN loans l ON l.id = p.loan_id "
                                    "WHERE l.account_number = ? AND p.paid_at BETWEEN ? AND ?", (acc.number, s, e)).fetchone()[0]
                if savings or fd or loan or acc.number == self.get_account(cid).number:
                    rows.append({"account": acc.number, "currency": acc.currency, "savings": savings,
                                 "fd": max(0, fd), "loan": loan})
        return {"customer": c, "start": start, "end": end, "label": label, "rows": rows}

    def interest_certificate(self, cid, year, lang=None):
        from reportlab.lib import colors
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.units import mm
        from reportlab.pdfgen import canvas

        data = self.interest_summary(cid, year)
        c = data["customer"]
        lang = lang or c.lang or "en"
        _fonts()
        T = lambda text: shape(i18n.translate_text(text, lang), lang)          # noqa: E731
        F, FB = ("ZenHei", "ZenHei") if lang == "zh" else ("DejaVu", "DejaVu-Bold")
        buf = io.BytesIO()
        pdf = canvas.Canvas(buf, pagesize=A4)
        pdf.setTitle(f"PyBank interest certificate {data['label']}")
        width, height = A4
        navy = colors.HexColor("#0f2a4a")
        pdf.setFillColor(navy)
        pdf.rect(0, height - 28 * mm, width, 28 * mm, fill=1, stroke=0)
        pdf.setFillColor(colors.white)
        pdf.setFont(FB, 18)
        pdf.drawString(18 * mm, height - 15 * mm, "PyBank")
        pdf.setFont(F, 10)
        pdf.drawString(18 * mm, height - 21 * mm, T("Interest certificate") + f"  ·  {data['label']}")
        pdf.setFillColor(colors.black)
        y = height - 42 * mm
        pdf.setFont(FB, 11)
        pdf.drawString(18 * mm, y, c.full_name)
        pdf.setFont(F, 9)
        pdf.drawString(18 * mm, y - 6 * mm, f"{T('Customer ID')}: {c.customer_id}")
        pdf.drawString(18 * mm, y - 11 * mm, f"{T('Period')}: {data['start'].isoformat()} — {data['end'].isoformat()}")
        y -= 24 * mm
        heads = [T("Account"), T("Savings interest earned"), T("Fixed deposit interest earned"), T("Loan interest paid")]
        xs = [18, 46, 92, 142]
        pdf.setFillColor(navy)
        pdf.rect(18 * mm, y - 2 * mm, width - 36 * mm, 7 * mm, fill=1, stroke=0)
        pdf.setFillColor(colors.white)
        pdf.setFont(FB, 7.5)
        for x, h in zip(xs, heads):
            pdf.drawString((x + 1.5) * mm, y, h)
        pdf.setFillColor(colors.black)
        y -= 8 * mm
        for r in data["rows"]:
            M = lambda v: format_money(v, r["currency"], "en" if lang == "ar" else lang)     # noqa: E731
            pdf.setFont("DejaVu", 8.5)
            pdf.drawString(19.5 * mm, y, f"XX{r['account'][-4:]} ({r['currency']})")
            pdf.drawString((xs[1] + 1.5) * mm, y, M(r["savings"]))
            pdf.drawString((xs[2] + 1.5) * mm, y, M(r["fd"]))
            pdf.drawString((xs[3] + 1.5) * mm, y, M(r["loan"]))
            y -= 7 * mm
        y -= 6 * mm
        pdf.setFont(F, 8.5)
        for line in [T("This certificate is made from the transactions of your accounts in the period above."),
                     T("Tax deducted at source: nil (PyBank is a fictional bank; check the rules of your country)."),
                     T("This is a computer-generated certificate and does not need a signature.")]:
            pdf.drawString(18 * mm, y, line)
            y -= 5 * mm
        pdf.setFont(F, 7.5)
        pdf.setFillColor(colors.HexColor("#64748b"))
        pdf.drawString(18 * mm, 10 * mm, T("Fictional bank for an academic project — not a real bank.") + f"  {now()}")
        pdf.save()
        return data["label"], buf.getvalue()

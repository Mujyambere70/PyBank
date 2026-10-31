"""
statements.py - monthly e-statements as PDF files (mixin class used by Bank).

On the first day of every month (the first visit of the day) PyBank makes a PDF statement of the
last month for every customer, keeps it in the database (the customer can download it in Net Banking)
and emails it when an email service is configured. Statements can also be made for any month on demand.

The PDF is drawn with reportlab and the DejaVu Sans font (it has ₹, ₽ and Cyrillic letters).
Labels are in the customer's language. Chinese text uses the WenQuanYi Zen Hei font (only the characters PyBank needs);
for Arabic the labels stay in English, because Arabic letters must be joined and written right to left,
which this simple PDF drawing cannot do.
"""

import base64
import io
import os
from datetime import date

import i18n
import world
from core import BankError, now
from world import format_money

FONT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "fonts")
PDF_LANGS = {"en", "ru", "fr", "es", "zh"}
_fonts_ready = False


def _fonts():
    global _fonts_ready
    if not _fonts_ready:
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.ttfonts import TTFont
        pdfmetrics.registerFont(TTFont("DejaVu", os.path.join(FONT_DIR, "DejaVuSans.ttf")))
        pdfmetrics.registerFont(TTFont("DejaVu-Bold", os.path.join(FONT_DIR, "DejaVuSans-Bold.ttf")))
        # Chinese: WenQuanYi Zen Hei, cut down to the characters PyBank uses (made with fontTools)
        pdfmetrics.registerFont(TTFont("ZenHei", os.path.join(FONT_DIR, "WenQuanYiZenHei-subset.ttf")))
        _fonts_ready = True


def month_bounds(year, month):
    start = date(year, month, 1)
    end = date(year + (month == 12), month % 12 + 1, 1)
    return start, end


def build_pdf(customer, account, rows, opening, closing, year, month, lang="en"):
    """rows: transactions of the month, oldest first. Returns the PDF as bytes."""
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import mm
    from reportlab.pdfgen import canvas

    _fonts()
    lang = lang if lang in PDF_LANGS else "en"
    T = lambda text: i18n.translate_text(text, lang)                     # noqa: E731
    M = lambda amount: format_money(amount, account.currency, lang)      # noqa: E731
    F, FB = ("ZenHei", "ZenHei") if lang == "zh" else ("DejaVu", "DejaVu-Bold")
    buf = io.BytesIO()
    pdf = canvas.Canvas(buf, pagesize=A4)
    pdf.setTitle(f"PyBank statement {year}-{month:02d}")
    pdf.setAuthor("PyBank")
    width, height = A4
    navy, teal = colors.HexColor("#0f2a4a"), colors.HexColor("#0d9488")
    first, after = month_bounds(year, month)
    period = f"{first.isoformat()} — {date.fromordinal(after.toordinal() - 1).isoformat()}"

    def header(page):
        pdf.setFillColor(navy)
        pdf.rect(0, height - 28 * mm, width, 28 * mm, fill=1, stroke=0)
        pdf.setFillColor(colors.white)
        pdf.setFont(FB, 18)
        pdf.drawString(18 * mm, height - 15 * mm, "PyBank")
        pdf.setFont(F, 10)
        pdf.drawString(18 * mm, height - 21 * mm, T("Monthly account statement") + f"  ·  {period}")
        pdf.drawRightString(width - 18 * mm, height - 15 * mm, account.branch)
        pdf.drawRightString(width - 18 * mm, height - 21 * mm, T("Page") + f" {page}")
        pdf.setFillColor(colors.black)

    def footer():
        pdf.setFont(F, 7.5)
        pdf.setFillColor(colors.HexColor("#64748b"))
        pdf.drawString(18 * mm, 10 * mm, T("This is a computer-generated statement and does not need a signature.") +
                       "  " + T("Fictional bank for an academic project — not a real bank."))
        pdf.setFillColor(colors.black)

    page = 1
    header(page)
    y = height - 38 * mm
    pdf.setFont(FB, 11)
    pdf.drawString(18 * mm, y, customer.full_name)
    pdf.setFont(F, 9)
    lines = [customer.address, f"{customer.city} {customer.pincode}".strip(),
             world.country_name(customer.country, lang if lang in world.LANGUAGES else "en")]
    for i, line in enumerate(lines):
        pdf.drawString(18 * mm, y - (5 + 4.5 * i) * mm, line)
    info = [(T("Customer ID"), customer.customer_id), (T("Account number"), account.number),
            (T("Account type"), T(account.type)), (T("Currency"), account.currency), ("IFSC", account.ifsc)]
    for i, (k, v) in enumerate(info):
        pdf.setFont(F, 8.5)
        pdf.drawRightString(width - 60 * mm, y - 4.5 * i * mm, k)
        pdf.setFont(FB, 8.5)
        pdf.drawString(width - 57 * mm, y - 4.5 * i * mm, v)
    y -= 28 * mm

    credits = sum(t.amount for t in rows if t.is_credit)
    debits = sum(t.amount for t in rows if not t.is_credit)
    boxes = [(T("Opening balance"), M(opening)), (T("Money in"), M(credits)), (T("Money out"), M(debits)),
             (T("Closing balance"), M(closing))]
    box_w = (width - 36 * mm - 9 * mm) / 4
    for i, (k, v) in enumerate(boxes):
        x = 18 * mm + i * (box_w + 3 * mm)
        pdf.setFillColor(colors.HexColor("#f1f5f9"))
        pdf.roundRect(x, y - 14 * mm, box_w, 14 * mm, 2 * mm, fill=1, stroke=0)
        pdf.setFillColor(colors.HexColor("#475569"))
        pdf.setFont(F, 7.5)
        pdf.drawString(x + 3 * mm, y - 5 * mm, k)
        pdf.setFillColor(teal if i == 3 else navy)
        pdf.setFont("DejaVu-Bold", 10)                                # amounts: DejaVu has ₹ and ₽
        pdf.drawString(x + 3 * mm, y - 11 * mm, v)
    pdf.setFillColor(colors.black)
    y -= 22 * mm

    cols = [(18, T("Date")), (38, T("Description")), (118, T("Debit")), (146, T("Credit")), (176, T("Balance"))]

    def table_head(y):
        pdf.setFillColor(navy)
        pdf.rect(18 * mm, y - 2 * mm, width - 36 * mm, 7 * mm, fill=1, stroke=0)
        pdf.setFillColor(colors.white)
        pdf.setFont(FB, 8)
        for i, (x, title) in enumerate(cols):
            if i >= 2:
                pdf.drawRightString((x + (26 if i < 4 else 16)) * mm, y, title)
            else:
                pdf.drawString((x + 1.5) * mm, y, title)
        pdf.setFillColor(colors.black)
        return y - 7 * mm

    y = table_head(y)
    if not rows:
        pdf.setFont(F, 9)
        pdf.drawString(20 * mm, y, T("No transactions in this month."))
    for n, t in enumerate(rows):
        if y < 22 * mm:
            footer()
            pdf.showPage()
            page += 1
            header(page)
            y = table_head(height - 38 * mm)
        if n % 2:
            pdf.setFillColor(colors.HexColor("#f8fafc"))
            pdf.rect(18 * mm, y - 2 * mm, width - 36 * mm, 6 * mm, fill=1, stroke=0)
            pdf.setFillColor(colors.black)
        pdf.setFont(F, 7.5)
        pdf.drawString(19.5 * mm, y, t.created_at[:10])
        desc = t.description if len(t.description) <= 52 else t.description[:50] + "…"
        pdf.drawString(39.5 * mm, y, desc)
        pdf.setFont("DejaVu", 7.5)
        pdf.drawRightString(144 * mm, y, "" if t.is_credit else M(t.amount))
        pdf.drawRightString(172 * mm, y, M(t.amount) if t.is_credit else "")
        pdf.drawRightString(192 * mm, y, M(t.balance_after))
        y -= 6 * mm
    footer()
    pdf.save()
    return buf.getvalue()


class StatementsMixin:

    def statement_pdf(self, cid, year, month, lang=None):
        """PDF statement of one month for one customer (made on demand)."""
        if not (1 <= int(month) <= 12) or not (2000 <= int(year) <= date.today().year):
            raise BankError("Please choose a valid month.")
        year, month = int(year), int(month)
        start, end = month_bounds(year, month)
        if start > date.today():
            raise BankError("Please choose a valid month.")
        customer, account = self.get_customer(cid), self.get_account(cid)
        with self._connect() as conn:
            before = conn.execute("SELECT balance_after FROM transactions WHERE account_number = ? AND created_at < ? "
                                  "ORDER BY created_at DESC, id DESC LIMIT 1",
                                  (account.number, start.isoformat())).fetchone()
        opening = before[0] if before else 0
        rows = list(reversed(self.transactions(account.number, start.isoformat(),
                                               date.fromordinal(end.toordinal() - 1).isoformat())))
        closing = rows[-1].balance_after if rows else opening
        return build_pdf(customer, account, rows, opening, closing, year, month, lang or customer.lang)

    def monthly_estatements(self, today=None):
        """Make last month's statement for every active customer once (called by the daily tasks)."""
        today = today or date.today()
        last = date.fromordinal(today.replace(day=1).toordinal() - 1)
        key = f"{last.year}-{last.month:02d}"
        if self.settings().get("last_estatement_month") == key:
            return 0
        with self._connect() as conn:
            self.save_setting(conn, "last_estatement_month", key)
            people = [dict(r) for r in conn.execute(
                "SELECT c.customer_id, c.email, c.full_name, c.lang FROM customers c JOIN accounts a USING (customer_id) "
                "WHERE c.status = 'Active' AND c.estatement = 1 AND a.opened_at < ?", (today.replace(day=1).isoformat(),))]
        made = 0
        for p in people:
            blob = self.statement_pdf(p["customer_id"], last.year, last.month)
            emailed = ""
            from bank import is_example_email
            if self.notifier.email_provider() and not is_example_email(p["email"]):
                try:
                    lang = p["lang"] or "en"
                    self.notifier.send_email(
                        p["email"], i18n.translate_text("Your PyBank statement for {0}", lang).replace("{0}", key),
                        i18n.translate_text("Your monthly account statement is attached as a PDF file.", lang),
                        attachment=(f"PyBank_statement_{key}.pdf", blob))
                    emailed = now()
                except Exception:
                    emailed = ""
            with self._connect() as conn:
                conn.execute("INSERT INTO statements (customer_id, month, created_at, size, data, emailed) "
                             "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT (customer_id, month) DO NOTHING",
                             (p["customer_id"], key, now(), len(blob), base64.b64encode(blob).decode(), emailed))
            made += 1
        return made

    def estatements(self, cid):
        with self._connect() as conn:
            return [dict(r) for r in conn.execute("SELECT id, month, created_at, size, emailed FROM statements "
                                                  "WHERE customer_id = ? ORDER BY month DESC", (cid,))]

    def estatement_blob(self, cid, sid):
        with self._connect() as conn:
            r = conn.execute("SELECT month, data FROM statements WHERE id = ? AND customer_id = ?", (sid, cid)).fetchone()
        if r is None:
            raise BankError("Statement not found.")
        return r[0], base64.b64decode(r[1])

    def set_estatement(self, cid, on):
        with self._connect() as conn:
            conn.execute("UPDATE customers SET estatement = ? WHERE customer_id = ?", (1 if on else 0, cid))

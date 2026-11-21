"""
sitecontent.py - the bank's website is managed from the back office (version 4).

Until version 3 every text of the public website was written inside the HTML templates: the phone
number, the address, the FAQ, the products on the home page, the branches, the service charges.
To change one word a programmer had to edit a file and deploy again. Now the texts live in the
database and a website editor changes them in Back office -> Website:

    texts      contacts and short texts (phone numbers, email, hours, footer, top banner line)
    pages      About us, Terms, Privacy, Security tips ... (simple text with headings, lists, links)
    faqs       questions and answers with a category and an order
    products   the cards on the home page
    branches   branches and ATMs (contact page, with a map link)
    banners    the big box at the top of the home page, each with a start and end date
    charges    the table of service charges on the Rates page
    billers    companies whose bills customers can pay (see autopay.py)
    media      pictures uploaded by the editor (PNG / JPG, max 1 MB)

Every change keeps the old text in `content_history`, so an older version can be put back.
Every item can be translated into the five other languages (`content_tr`); a page shows the
translation when there is one, otherwise the English text.
"""

import base64
import html
import json
import re
from datetime import date

import world
from core import BankError, now

CONTENT_SCHEMA = """
CREATE TABLE IF NOT EXISTS site_texts (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT '',
    updated_by TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS site_pages (
    slug       TEXT PRIMARY KEY,
    title      TEXT NOT NULL,
    body       TEXT NOT NULL,
    in_footer  INTEGER NOT NULL DEFAULT 1,
    published  INTEGER NOT NULL DEFAULT 1,
    updated_at TEXT NOT NULL DEFAULT '',
    updated_by TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS faqs (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    question   TEXT NOT NULL,
    answer     TEXT NOT NULL,
    category   TEXT NOT NULL DEFAULT 'General',
    position   INTEGER NOT NULL DEFAULT 0,
    active     INTEGER NOT NULL DEFAULT 1,
    updated_at TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS products (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    title      TEXT NOT NULL,
    text       TEXT NOT NULL,
    stat       TEXT NOT NULL DEFAULT '',
    image      TEXT NOT NULL DEFAULT '',
    button     TEXT NOT NULL DEFAULT 'Know more',
    link       TEXT NOT NULL DEFAULT '/rates',
    position   INTEGER NOT NULL DEFAULT 0,
    active     INTEGER NOT NULL DEFAULT 1,
    updated_at TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS branches (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT NOT NULL,
    kind       TEXT NOT NULL DEFAULT 'Branch + ATM',
    address    TEXT NOT NULL,
    city       TEXT NOT NULL,
    country    TEXT NOT NULL DEFAULT 'IN',
    ifsc       TEXT NOT NULL DEFAULT '',
    phone      TEXT NOT NULL DEFAULT '',
    hours      TEXT NOT NULL DEFAULT '',
    lat        REAL,
    lng        REAL,
    position   INTEGER NOT NULL DEFAULT 0,
    active     INTEGER NOT NULL DEFAULT 1,
    updated_at TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS banners (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    eyebrow    TEXT NOT NULL DEFAULT '',
    title      TEXT NOT NULL,
    text       TEXT NOT NULL DEFAULT '',
    button     TEXT NOT NULL DEFAULT '',
    link       TEXT NOT NULL DEFAULT '',
    image      TEXT NOT NULL DEFAULT '',
    starts     TEXT NOT NULL DEFAULT '',
    ends       TEXT NOT NULL DEFAULT '',
    position   INTEGER NOT NULL DEFAULT 0,
    active     INTEGER NOT NULL DEFAULT 1,
    updated_at TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS charges (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    service    TEXT NOT NULL,
    savings_fee TEXT NOT NULL DEFAULT '',
    current_fee TEXT NOT NULL DEFAULT '',
    position   INTEGER NOT NULL DEFAULT 0,
    active     INTEGER NOT NULL DEFAULT 1,
    updated_at TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS media (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    filename    TEXT NOT NULL,
    mime        TEXT NOT NULL,
    size        INTEGER NOT NULL,
    data        TEXT NOT NULL,
    alt         TEXT NOT NULL DEFAULT '',
    uploaded_at TEXT NOT NULL,
    uploaded_by TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS content_tr (
    kind   TEXT NOT NULL,
    ref    TEXT NOT NULL,
    lang   TEXT NOT NULL,
    field  TEXT NOT NULL,
    value  TEXT NOT NULL,
    PRIMARY KEY (kind, ref, lang, field)
);
CREATE TABLE IF NOT EXISTS content_history (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    kind     TEXT NOT NULL,
    ref      TEXT NOT NULL,
    data     TEXT NOT NULL,
    saved_at TEXT NOT NULL,
    saved_by TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_history_ref ON content_history(kind, ref, id);
"""

CONTENT_TABLES = ["site_texts", "site_pages", "faqs", "products", "branches", "banners", "charges", "media",
                  "content_tr", "content_history"]

# ---------- what an editor can change, and how each field is checked ----------
#   kind -> (table, key column, {field: (label, max length, required)}, translatable fields)
KINDS = {
    "text": ("site_texts", "key", {"value": ("Text", 300, True)}, ["value"]),
    "page": ("site_pages", "slug", {"title": ("Title", 80, True), "body": ("Text", 20000, True)}, ["title", "body"]),
    "faq": ("faqs", "id", {"question": ("Question", 200, True), "answer": ("Answer", 1500, True),
                           "category": ("Category", 40, True)}, ["question", "answer", "category"]),
    "product": ("products", "id", {"title": ("Title", 60, True), "text": ("Text", 200, True),
                                   "stat": ("Highlight", 40, False), "image": ("Picture", 80, False),
                                   "button": ("Button", 30, True), "link": ("Link", 200, True)},
                ["title", "text", "stat", "button"]),
    "branch": ("branches", "id", {"name": ("Name", 80, True), "kind": ("Type", 20, True),
                                  "address": ("Address", 200, True), "city": ("City", 60, True),
                                  "country": ("Country", 2, True), "ifsc": ("IFSC", 11, False),
                                  "phone": ("Phone", 30, False), "hours": ("Opening hours", 120, False)},
               ["name", "address", "hours"]),
    "banner": ("banners", "id", {"eyebrow": ("Small line above", 60, False), "title": ("Title", 90, True),
                                 "text": ("Text", 300, False), "button": ("Button", 30, False),
                                 "link": ("Link", 200, False), "image": ("Picture", 80, False),
                                 "starts": ("Show from", 10, False), "ends": ("Show until", 10, False)},
               ["eyebrow", "title", "text", "button"]),
    "charge": ("charges", "id", {"service": ("Service", 80, True), "savings_fee": ("Savings account", 40, False),
                                 "current_fee": ("Current account", 40, False)}, ["service", "savings_fee", "current_fee"]),
}
BRANCH_KINDS = ["Branch + ATM", "Branch", "ATM"]
STATIC_PICTURES = ["savings.jpg", "current.jpg", "deposits.jpg", "loans.jpg", "cards.jpg", "app.jpg", "branch.jpg",
                   "secure.jpg", "hero.png"]
PICTURE_TYPES = [(b"\x89PNG\r\n\x1a\n", "image/png", ".png"), (b"\xff\xd8\xff", "image/jpeg", ".jpg"),
                 (b"RIFF", "image/webp", ".webp")]
PICTURE_MAX = 1024 * 1024
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,39}$")
KEY_RE = re.compile(r"^[a-z][a-z0-9_]{1,39}$")

# ---------- the texts of version 3, used to fill an empty database ----------
DEFAULT_TEXTS = {
    "care_phone": ("1800-000-7926", "Customer care phone (top bar, contact page)"),
    "care_hours": ("24x7", "Customer care hours"),
    "fraud_phone": ("1800-000-1930", "Phone to report fraud / block a card"),
    "care_email": ("care@pybank.example", "Customer care email"),
    "email_reply": ("Reply within 24 hours", "Under the email on the contact page"),
    "office_hours": ("Mon-Sat, 9:30 AM - 4:30 PM", "Head office hours (footer)"),
    "branch_hours": ("Monday to Saturday 9:30 AM - 4:30 PM (closed on 2nd & 4th Saturday and public holidays).",
                     "Branch hours (contact page)"),
    "tagline": ("Banking made simple", "Small line under the logo"),
    "footer_about": ("Simple, secure banking for everyone. Savings, current accounts, deposits and loans with easy "
                     "net banking.", "Text in the footer"),
    "contact_intro": ("We are here to help, 24 hours a day.", "Line under the title of the contact page"),
    "rates_effective": ("Effective from 01 October", "Line under the title of the rates page"),
    "security_line": ("PyBank will never ask for your password, transaction PIN or OTP by phone, SMS or email.",
                      "Security warning on the home page"),
}
TEXT_GROUPS = [("Contacts", ["care_phone", "care_hours", "fraud_phone", "care_email", "email_reply", "office_hours",
                             "branch_hours"]),
               ("Website lines", ["tagline", "footer_about", "contact_intro", "rates_effective", "security_line"])]

DEFAULT_PAGES = [
    ("about", "About PyBank", """## Who we are
PyBank provides savings and current accounts, fixed deposits, loans and cards, with a fast and secure net banking platform. Our goal is to make banking easy for everyone - students, families, shop owners and senior citizens.

Everything you need is online: open an account, check your balance, transfer money and download statements, 24 hours a day.

## Our values
- **Security:** encrypted passwords and PINs, session timeouts and account lock protection.
- **Trust:** clear charges and interest rates. No hidden fees.
- **Speed:** quick account opening and real-time fund transfers.
- **For everyone:** simple language and an easy, mobile-friendly website in six languages.""", 0),
    ("terms", "Terms and Conditions", """## About these terms
PyBank is a fictional bank made for an academic project. These terms show what a real bank would publish; they are not a contract.

## Your account
- Keep your password, transaction PIN and one-time codes secret. The bank never asks for them.
- Tell the bank at once if your card or phone is lost. You can block your card yourself in Net Banking.
- The bank may freeze an account when the law requires it or when fraud is suspected.

## Charges and interest
The current rates and charges are on the [Interest rates and charges](/rates) page. The bank tells customers about a change at least 30 days before it starts.""", 1),
    ("privacy", "Privacy Notice", """## What we keep
Your name, date of birth, contact details, address, the documents you upload and the record of your transactions.

## Why
To open and run your account, to protect it against fraud and to follow banking law.

## Your rights
- In Net Banking -> Security you can download all the data the bank keeps about you (a JSON file).
- Passport and ID photos are seen only by bank officers who check your application.
- You can ask the branch to correct wrong data at any time.""", 1),
    ("security-tips", "Safe Banking Tips", """## Protect your account
- PyBank will never ask for your password, transaction PIN or OTP by phone, SMS or email.
- Turn on two-factor login in Net Banking -> Security.
- Check the list of signed-in devices from time to time and sign out the ones you do not know.
- Use the card controls: switch off online or international payments when you do not need them.

## If something looks wrong
Call the fraud line, block your card in Net Banking -> My Card and report the payment with "Report a problem" on the receipt.""", 1),
]

DEFAULT_FAQS = [
    ("Account", "How do I open an account?", 'Click "Open Account", fill in your personal and contact details, choose Savings or Current, make the opening deposit and set your password and 4-digit transaction PIN. You get your Customer ID and account number immediately.'),
    ("Account", "What is my Customer ID?", 'It is your login ID, for example PB10000001. It is shown after opening the account and on the "Account details" page. You can also log in with your registered email.'),
    ("Security", "What is the difference between password and transaction PIN?", "The password is used to log in. The 4-digit transaction PIN is needed for every deposit, withdrawal and transfer, so even if someone sees your password they cannot move your money."),
    ("Payments", "How do I transfer money?", "Go to Net Banking > Fund Transfer, enter the 12-digit PyBank account number twice and the amount. On the next screen check the beneficiary name, then enter your PIN to confirm."),
    ("Payments", "Is there a transaction limit?", "Yes. Every account has a daily limit for cash withdrawals and for transfers. The limits of your currency are on the Interest rates and charges page."),
    ("Security", "My login is locked. What should I do?", "After 5 wrong passwords your login is locked for 15 minutes for your safety. Wait and try again, or call customer care."),
    ("Account", "How do I download my statement?", 'Go to Net Banking > Statement, choose the dates and click "Download CSV" (opens in Excel) or "Print / Save PDF".'),
    ("Security", "Why was I logged out?", "For your security, PyBank logs you out automatically after 10 minutes without activity."),
    ("Account", "Can I have accounts in several currencies?", "Yes. In Net Banking > My Accounts you can open one more account in each of the bank's eight currencies and move money between your accounts at the bank's buy and sell rates."),
    ("Payments", "How do I pay a bill every month without forgetting?", "Use Net Banking > Bills & Autopay. Save the biller once, then create a standing instruction: the bank pays on the day you choose and sends you an alert."),
]

DEFAULT_PRODUCTS = [
    ("Savings Account", "Grow your money safely with quarterly interest and a free debit card.", "3.5% p.a. interest", "savings.jpg", "Open now", "/open-account"),
    ("Current Account", "For shops and businesses: unlimited transactions and bulk payments.", "Min. balance ₹5,000", "current.jpg", "Open now", "/open-account"),
    ("Fixed Deposits", "Lock in high returns for 6 months to 3 years. Extra 0.5% for senior citizens.", "Up to 7.25% p.a.", "deposits.jpg", "Know more", "/rates"),
    ("Home & Car Loans", "Low interest rates, quick approval and flexible EMIs.", "From 8.40% p.a.", "loans.jpg", "Know more", "/rates"),
    ("Debit Cards", "Contactless card with controls in Net Banking: switch off online or abroad payments in one click.", "Zero joining fee", "cards.jpg", "Know more", "/rates"),
    ("Accounts in 8 currencies", "Keep rupees, rubles, dollars, euros and more, and move money between them at clear rates.", "₹ ₽ $ € £ ¥ AED RWF", "app.jpg", "Know more", "/rates"),
]

DEFAULT_BRANCHES = [
    ("Ambala Cantt Main Branch", "Branch + ATM", "12 Mall Road, Ambala Cantt, Haryana 133001, India", "Ambala Cantt", "IN",
     "PYBK0001024", "0171-000-1024", "Mon-Sat 9:30 AM - 4:30 PM; ATM and cash deposit machine 24x7", 30.3336, 76.8382),
    ("Ambala City Branch", "Branch + ATM", "SCO 45, Jagadhri Road, Ambala City, Haryana 134003, India", "Ambala City", "IN",
     "PYBK0001031", "0171-000-1031", "Mon-Sat 9:30 AM - 4:30 PM; ATM 24x7", 30.3782, 76.7767),
    ("Mullana Branch", "Branch + ATM", "Near MMDU Gate 1, Mullana, Ambala, Haryana 133203, India", "Mullana", "IN",
     "PYBK0001057", "0171-000-1057", "Mon-Sat 9:30 AM - 4:30 PM; ATM 8 AM - 10 PM", 30.2783, 77.0486),
    ("Krasnoyarsk International Branch", "Branch + ATM", "79 Svobodny Prospekt, Krasnoyarsk 660041, Russia", "Krasnoyarsk",
     "RU", "PYBK0007001", "+7 391 000-70-01", "Mon-Fri 9:00 - 18:00", 56.0045, 92.7700),
    ("Mall Road ATM", "ATM", "Sadar Bazaar, Ambala Cantt, Haryana 133001, India", "Ambala Cantt", "IN", "", "", "24x7",
     30.3399, 76.8420),
]

DEFAULT_BANNERS = [
    ("Zero-paperwork digital account", "Open a savings account in 5 minutes",
     "Earn up to 3.5% p.a. interest, get a free debit card and bank 24x7 with PyBank Net Banking - transfers, "
     "statements and more.", "Open Account Now", "/open-account", "hero.png"),
]

DEFAULT_CHARGES = [
    ("Minimum opening deposit", "₹500", "₹5,000"),
    ("IMPS / net banking transfer", "Free", "Free"),
    ("UPI payment", "Free", "Free"),
    ("NEFT payment", "Free", "Free"),
    ("SWIFT / SEPA payment abroad", "₹500", "₹500"),
    ("Debit card annual fee", "Free (1st year)", "₹250"),
    ("Replacement debit card", "₹150", "₹150"),
    ("Account statement (online)", "Free", "Free"),
    ("Currency exchange between own accounts", "Margin shown before you pay", "Margin shown before you pay"),
]


# ---------- the text format of pages: safe, simple, no HTML from the editor ----------

_LINK = re.compile(r"\[([^\]\n]{1,80})\]\(((?:https?://|/)[^\s)]{0,200})\)")
_BOLD = re.compile(r"\*\*([^*\n]{1,200})\*\*")


def _inline(text):
    out = html.escape(text, quote=True)
    out = _BOLD.sub(r"<strong>\1</strong>", out)

    def link(m):
        label, url = m.group(1), html.unescape(m.group(2))
        if url.startswith("//"):
            return m.group(0)
        ext = url.startswith("http")
        return (f'<a href="{html.escape(url, quote=True)}"' + (' rel="noopener" target="_blank"' if ext else "")
                + f">{label}</a>")
    return _LINK.sub(link, out)


def render_body(text):
    """Turn the editor's text into HTML: '## ' headings, '- ' lists, **bold**, [link](url), paragraphs.
    Everything is escaped first, so an editor can never put a script into the page."""
    blocks, para, items = [], [], []

    def flush():
        if para:
            blocks.append("<p>" + _inline(" ".join(para)) + "</p>")
            para.clear()
        if items:
            blocks.append("<ul class=\"check-list\">" + "".join(f"<li>{_inline(i)}</li>" for i in items) + "</ul>")
            items.clear()

    for raw in (text or "").replace("\r", "").split("\n"):
        line = raw.strip()
        if not line:
            flush()
        elif line.startswith("## "):
            flush()
            blocks.append(f"<h2>{_inline(line[3:])}</h2>")
        elif line.startswith(("- ", "* ")):
            if para:
                flush()
            items.append(line[2:])
        else:
            if items:
                flush()
            para.append(line)
    flush()
    return "\n".join(blocks)


def check_picture(filename, blob):
    """(mime, extension) of an uploaded picture, judged by its first bytes - not by its name."""
    if not blob:
        raise BankError("Please choose a picture.")
    if len(blob) > PICTURE_MAX:
        raise BankError("The picture is too big (maximum 1 MB).")
    for magic, mime, ext in PICTURE_TYPES:
        if blob.startswith(magic) and (magic != b"RIFF" or blob[8:12] == b"WEBP"):
            return mime, ext
    raise BankError("Only PNG, JPG or WEBP pictures can be uploaded.")


class SiteContentMixin:
    """Methods of Bank for the website texts."""

    def ensure_site_content(self):
        """Fill the content tables once with the texts of version 3 (also for an existing online database)."""
        with self._connect() as conn:
            if conn.execute("SELECT COUNT(*) FROM site_texts").fetchone()[0]:
                return False
            when = now()
            for key, (value, _) in DEFAULT_TEXTS.items():
                conn.execute("INSERT INTO site_texts (key, value, updated_at) VALUES (?, ?, ?)", (key, value, when))
            for slug, title, body, footer in DEFAULT_PAGES:
                conn.execute("INSERT INTO site_pages (slug, title, body, in_footer, updated_at) VALUES (?, ?, ?, ?, ?)",
                             (slug, title, body, footer, when))
            for i, (cat, q, a) in enumerate(DEFAULT_FAQS):
                conn.execute("INSERT INTO faqs (question, answer, category, position, updated_at) VALUES (?, ?, ?, ?, ?)",
                             (q, a, cat, i + 1, when))
            for i, (t, x, s, img, b, l) in enumerate(DEFAULT_PRODUCTS):
                conn.execute("INSERT INTO products (title, text, stat, image, button, link, position, updated_at) "
                             "VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (t, x, s, "static:" + img, b, l, i + 1, when))
            for i, b in enumerate(DEFAULT_BRANCHES):
                conn.execute("INSERT INTO branches (name, kind, address, city, country, ifsc, phone, hours, lat, lng, "
                             "position, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", (*b, i + 1, when))
            for i, (e, t, x, b, l, img) in enumerate(DEFAULT_BANNERS):
                conn.execute("INSERT INTO banners (eyebrow, title, text, button, link, image, position, updated_at) "
                             "VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (e, t, x, b, l, "static:" + img, i + 1, when))
            for i, (s, a, c) in enumerate(DEFAULT_CHARGES):
                conn.execute("INSERT INTO charges (service, savings_fee, current_fee, position, updated_at) VALUES (?, ?, ?, ?, ?)",
                             (s, a, c, i + 1, when))
        return True

    # ===== reading (public pages) =====

    def _translations(self, conn, kind, lang):
        if lang == "en":
            return {}
        out = {}
        for r in conn.execute("SELECT ref, field, value FROM content_tr WHERE kind = ? AND lang = ?", (kind, lang)):
            out.setdefault(str(r["ref"]), {})[r["field"]] = r["value"]
        return out

    def _rows(self, kind, sql, args=(), lang=None):
        lang = lang or world.current_lang()
        table, key = KINDS[kind][0], KINDS[kind][1]
        with self._connect() as conn:
            rows = [dict(r) for r in conn.execute(sql, args)]
            tr = self._translations(conn, kind, lang)
        for r in rows:
            r.update({f: v for f, v in tr.get(str(r[key]), {}).items() if v})
        return rows

    def site_texts(self, lang=None):
        rows = self._rows("text", "SELECT * FROM site_texts", lang=lang)
        texts = {k: v for k, (v, _) in DEFAULT_TEXTS.items()}
        texts.update({r["key"]: r["value"] for r in rows})
        return texts

    def site_page(self, slug, lang=None, include_hidden=False):
        rows = self._rows("page", "SELECT * FROM site_pages WHERE slug = ?", (slug,), lang)
        if not rows or (not rows[0]["published"] and not include_hidden):
            raise BankError("Page not found.")
        page = rows[0]
        page["html"] = render_body(page["body"])
        return page

    def site_pages(self, lang=None, footer_only=False):
        sql = "SELECT * FROM site_pages" + (" WHERE published = 1 AND in_footer = 1" if footer_only else "") + \
              " ORDER BY slug"
        return self._rows("page", sql, lang=lang)

    def faqs(self, lang=None, active_only=True, q=""):
        sql = "SELECT * FROM faqs" + (" WHERE active = 1" if active_only else "") + " ORDER BY position, id"
        rows = self._rows("faq", sql, lang=lang)
        words = [w.lower() for w in (q or "").split() if len(w) > 1]
        if words:
            rows = [r for r in rows if all(w in (r["question"] + " " + r["answer"]).lower() for w in words)]
        return rows

    def products(self, lang=None, active_only=True):
        sql = "SELECT * FROM products" + (" WHERE active = 1" if active_only else "") + " ORDER BY position, id"
        return self._rows("product", sql, lang=lang)

    def branches(self, lang=None, active_only=True, country=""):
        sql, args = "SELECT * FROM branches WHERE 1 = 1", []
        if active_only:
            sql += " AND active = 1"
        if country:
            sql += " AND country = ?"
            args.append(country)
        return self._rows("branch", sql + " ORDER BY position, id", args, lang)

    def banners(self, lang=None, active_only=True, today=None):
        rows = self._rows("banner", "SELECT * FROM banners" + (" WHERE active = 1" if active_only else "") +
                          " ORDER BY position, id", lang=lang)
        if active_only:
            day = (today or date.today()).isoformat()
            rows = [b for b in rows if (not b["starts"] or b["starts"] <= day) and (not b["ends"] or b["ends"] >= day)]
        return rows

    def charges(self, lang=None, active_only=True):
        return self._rows("charge", "SELECT * FROM charges" + (" WHERE active = 1" if active_only else "") +
                          " ORDER BY position, id", lang=lang)

    def media_blob(self, media_id):
        with self._connect() as conn:
            r = conn.execute("SELECT * FROM media WHERE id = ?", (media_id,)).fetchone()
        if r is None:
            raise BankError("Picture not found.")
        return dict(r), base64.b64decode(r["data"])

    def media_list(self):
        with self._connect() as conn:
            return [dict(r) for r in conn.execute("SELECT id, filename, mime, size, alt, uploaded_at, uploaded_by "
                                                  "FROM media ORDER BY id DESC")]

    @staticmethod
    def picture_url(value):
        """'static:hero.png' -> /static/images/hero.png, 'media:3' -> /media/3."""
        value = value or ""
        if value.startswith("media:") and value[6:].isdigit():
            return f"/media/{value[6:]}"
        if value.startswith("static:") and value[7:] in STATIC_PICTURES:
            return f"/static/images/{value[7:]}"
        return ""

    # ===== changing (back office) =====

    def _clean(self, kind, form):
        fields = KINDS[kind][2]
        out = {}
        for name, (label, size, required) in fields.items():
            value = str(form.get(name, "") or "").replace("\r", "")
            value = value.strip() if name == "body" else " ".join(value.split())
            if required and not value:
                raise BankError(f"Please fill in: {label}.")
            if len(value) > size:
                raise BankError(f"{label} is too long (maximum {size} characters).")
            out[name] = value
        if kind in ("product", "banner"):
            if out.get("link") and not (out["link"].startswith("/") and not out["link"].startswith("//")) \
                    and not out["link"].startswith("https://"):
                raise BankError("The link must start with / (a page of this site) or https://.")
            if out.get("image") and not self.picture_url(out["image"]):
                raise BankError("Please choose one of the pictures.")
        if kind == "banner":
            for f in ("starts", "ends"):
                if out[f] and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", out[f]):
                    raise BankError("Dates must look like 2026-12-31.")
            if out["starts"] and out["ends"] and out["ends"] < out["starts"]:
                raise BankError("The end date is before the start date.")
        if kind == "branch":
            out["country"] = out["country"].upper()
            if out["country"] not in world.DIAL_CODES:
                raise BankError("Please choose the country.")
            if out["kind"] not in BRANCH_KINDS:
                raise BankError("Please choose the type (branch, ATM or both).")
            if out["ifsc"] and not re.fullmatch(r"PYBK0[0-9]{6}", out["ifsc"].upper()):
                raise BankError("A PyBank IFSC looks like PYBK0001024.")
            out["ifsc"] = out["ifsc"].upper()
        return out

    def _history(self, conn, kind, ref, row, who):
        if row:
            conn.execute("INSERT INTO content_history (kind, ref, data, saved_at, saved_by) VALUES (?, ?, ?, ?, ?)",
                         (kind, str(ref), json.dumps(dict(row), ensure_ascii=False, default=str), now(), who))

    def _audit_content(self, conn, staff, action, target):
        if staff:
            conn.execute("INSERT INTO audit_log (staff_id, action, target, details, at) VALUES (?, ?, ?, ?, ?)",
                         (staff["staff_id"], action, target, "", now()))

    def save_text(self, staff, key, value):
        if key not in DEFAULT_TEXTS:
            raise BankError("Unknown text.")
        value = self._clean("text", {"value": value})["value"]
        with self._connect() as conn:
            old = conn.execute("SELECT * FROM site_texts WHERE key = ?", (key,)).fetchone()
            if old and old["value"] == value:
                return False
            self._history(conn, "text", key, old, staff["staff_id"])
            if old:
                conn.execute("UPDATE site_texts SET value = ?, updated_at = ?, updated_by = ? WHERE key = ?",
                             (value, now(), staff["staff_id"], key))
            else:
                conn.execute("INSERT INTO site_texts (key, value, updated_at, updated_by) VALUES (?, ?, ?, ?)",
                             (key, value, now(), staff["staff_id"]))
            self._audit_content(conn, staff, "Website text changed", key)
        return True

    def save_page(self, staff, slug, form, new=False):
        slug = (slug or "").strip().lower()
        if not SLUG_RE.match(slug):
            raise BankError("The address of the page may have small letters, numbers and - (e.g. fees-2027).")
        data = self._clean("page", form)
        in_footer, published = int(bool(form.get("in_footer"))), int(bool(form.get("published")))
        with self._connect() as conn:
            old = conn.execute("SELECT * FROM site_pages WHERE slug = ?", (slug,)).fetchone()
            if new and old:
                raise BankError("A page with this address already exists.")
            if not new and not old:
                raise BankError("Page not found.")
            self._history(conn, "page", slug, old, staff["staff_id"])
            if old:
                conn.execute("UPDATE site_pages SET title = ?, body = ?, in_footer = ?, published = ?, updated_at = ?, "
                             "updated_by = ? WHERE slug = ?",
                             (data["title"], data["body"], in_footer, published, now(), staff["staff_id"], slug))
            else:
                conn.execute("INSERT INTO site_pages (slug, title, body, in_footer, published, updated_at, updated_by) "
                             "VALUES (?, ?, ?, ?, ?, ?, ?)",
                             (slug, data["title"], data["body"], in_footer, published, now(), staff["staff_id"]))
            self._audit_content(conn, staff, "Page saved", slug)
        return slug

    def delete_page(self, staff, slug):
        if slug == "about":
            raise BankError("The About page is needed by the website. Hide it instead of deleting it.")
        with self._connect() as conn:
            old = conn.execute("SELECT * FROM site_pages WHERE slug = ?", (slug,)).fetchone()
            if not old:
                raise BankError("Page not found.")
            self._history(conn, "page", slug, old, staff["staff_id"])
            conn.execute("DELETE FROM site_pages WHERE slug = ?", (slug,))
            conn.execute("DELETE FROM content_tr WHERE kind = 'page' AND ref = ?", (slug,))
            self._audit_content(conn, staff, "Page deleted", slug)

    def save_item(self, staff, kind, form, item_id=None):
        """Add (item_id=None) or change a FAQ, product, branch, banner or charge."""
        if kind not in ("faq", "product", "branch", "banner", "charge"):
            raise BankError("Unknown kind of content.")
        table = KINDS[kind][0]
        data = self._clean(kind, form)
        data["active"] = int(bool(form.get("active")))
        try:
            data["position"] = max(0, min(999, int(form.get("position") or 0)))
        except ValueError:
            raise BankError("The order must be a number.")
        if kind == "branch":
            for f in ("lat", "lng"):
                v = str(form.get(f, "") or "").strip()
                try:
                    data[f] = float(v) if v else None
                except ValueError:
                    raise BankError("Latitude and longitude must be numbers, e.g. 30.3336 and 76.8382.")
            if data["lat"] is not None and not (-90 <= data["lat"] <= 90 and -180 <= (data["lng"] or 0) <= 180):
                raise BankError("This place is not on the Earth. Please check latitude and longitude.")
        data["updated_at"] = now()
        cols = list(data)
        with self._connect() as conn:
            if item_id:
                old = conn.execute(f"SELECT * FROM {table} WHERE id = ?", (item_id,)).fetchone()
                if not old:
                    raise BankError("Item not found.")
                self._history(conn, kind, item_id, old, staff["staff_id"])
                conn.execute(f"UPDATE {table} SET " + ", ".join(f"{c} = ?" for c in cols) + " WHERE id = ?",
                             [data[c] for c in cols] + [item_id])
            else:
                if not data["position"]:
                    data["position"] = conn.execute(f"SELECT COALESCE(MAX(position), 0) + 1 FROM {table}").fetchone()[0]
                item_id = conn.execute(f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                                       [data[c] for c in cols]).lastrowid
            self._audit_content(conn, staff, f"Website {kind} saved", str(item_id))
        return item_id

    def delete_item(self, staff, kind, item_id):
        if kind not in ("faq", "product", "branch", "banner", "charge"):
            raise BankError("Unknown kind of content.")
        table = KINDS[kind][0]
        with self._connect() as conn:
            old = conn.execute(f"SELECT * FROM {table} WHERE id = ?", (item_id,)).fetchone()
            if not old:
                raise BankError("Item not found.")
            self._history(conn, kind, item_id, old, staff["staff_id"])
            conn.execute(f"DELETE FROM {table} WHERE id = ?", (item_id,))
            conn.execute("DELETE FROM content_tr WHERE kind = ? AND ref = ?", (kind, str(item_id)))
            self._audit_content(conn, staff, f"Website {kind} deleted", str(item_id))

    def content_item(self, kind, ref):
        table, key = KINDS[kind][0], KINDS[kind][1]
        with self._connect() as conn:
            r = conn.execute(f"SELECT * FROM {table} WHERE {key} = ?", (ref,)).fetchone()
        if r is None:
            raise BankError("Item not found.")
        return dict(r)

    def content_items(self, kind):
        table = KINDS[kind][0]
        order = "key" if kind == "text" else "slug" if kind == "page" else "position, id"
        with self._connect() as conn:
            return [dict(r) for r in conn.execute(f"SELECT * FROM {table} ORDER BY {order}")]

    def history(self, kind, ref, limit=30):
        with self._connect() as conn:
            rows = [dict(r) for r in conn.execute("SELECT * FROM content_history WHERE kind = ? AND ref = ? "
                                                  "ORDER BY id DESC LIMIT ?", (kind, str(ref), limit))]
        for r in rows:
            r["data"] = json.loads(r["data"])
        return rows

    def restore_version(self, staff, history_id):
        """Put an older text of a page or a website text back (the current one goes into the history)."""
        with self._connect() as conn:
            h = conn.execute("SELECT * FROM content_history WHERE id = ?", (history_id,)).fetchone()
        if not h or h["kind"] not in ("page", "text"):
            raise BankError("This version cannot be restored.")
        data = json.loads(h["data"])
        if h["kind"] == "text":
            self.save_text(staff, data["key"], data["value"])
        else:
            exists = True
            try:
                self.content_item("page", data["slug"])
            except BankError:
                exists = False
            self.save_page(staff, data["slug"], {**data, "in_footer": data.get("in_footer"),
                                                 "published": data.get("published")}, new=not exists)
        return h["kind"], h["ref"]

    # ----- translations -----

    def content_translation(self, kind, ref, lang):
        with self._connect() as conn:
            return {r["field"]: r["value"] for r in conn.execute(
                "SELECT field, value FROM content_tr WHERE kind = ? AND ref = ? AND lang = ?", (kind, str(ref), lang))}

    def save_translation(self, staff, kind, ref, lang, form):
        if kind not in KINDS or lang not in world.LANGUAGES or lang == "en":
            raise BankError("Please choose what to translate and a language other than English.")
        self.content_item(kind, ref)                         # must exist
        fields = KINDS[kind][2]
        with self._connect() as conn:
            for f in KINDS[kind][3]:
                value = str(form.get(f, "") or "").replace("\r", "")
                value = value.strip() if f == "body" else " ".join(value.split())
                if len(value) > fields[f][1]:
                    raise BankError(f"{fields[f][0]} is too long (maximum {fields[f][1]} characters).")
                conn.execute("DELETE FROM content_tr WHERE kind = ? AND ref = ? AND lang = ? AND field = ?",
                             (kind, str(ref), lang, f))
                if value:
                    conn.execute("INSERT INTO content_tr (kind, ref, lang, field, value) VALUES (?, ?, ?, ?, ?)",
                                 (kind, str(ref), lang, f, value))
            self._audit_content(conn, staff, f"Website {kind} translated ({lang})", str(ref))

    def translation_status(self):
        """{(kind, ref): set of languages with a translation} - for the ticks in the lists."""
        with self._connect() as conn:
            out = {}
            for r in conn.execute("SELECT DISTINCT kind, ref, lang FROM content_tr"):
                out.setdefault((r["kind"], str(r["ref"])), set()).add(r["lang"])
        return out

    # ----- pictures -----

    def add_media(self, staff, filename, blob, alt=""):
        mime, ext = check_picture(filename, blob)
        name = re.sub(r"[^A-Za-z0-9._-]", "_", (filename or "picture").rsplit("/", 1)[-1])[:60] or "picture" + ext
        with self._connect() as conn:
            mid = conn.execute("INSERT INTO media (filename, mime, size, data, alt, uploaded_at, uploaded_by) "
                               "VALUES (?, ?, ?, ?, ?, ?, ?)",
                               (name, mime, len(blob), base64.b64encode(blob).decode(), " ".join((alt or "").split())[:120],
                                now(), staff["staff_id"])).lastrowid
            self._audit_content(conn, staff, "Picture uploaded", name)
        return mid

    def delete_media(self, staff, media_id):
        ref = f"media:{media_id}"
        with self._connect() as conn:
            used = (conn.execute("SELECT COUNT(*) FROM products WHERE image = ?", (ref,)).fetchone()[0] +
                    conn.execute("SELECT COUNT(*) FROM banners WHERE image = ?", (ref,)).fetchone()[0])
            if used:
                raise BankError("This picture is used by a product or a banner. Choose another picture there first.")
            if conn.execute("DELETE FROM media WHERE id = ?", (media_id,)).rowcount == 0:
                raise BankError("Picture not found.")
            self._audit_content(conn, staff, "Picture deleted", str(media_id))

    def picture_choices(self):
        return ([("static:" + p, p) for p in STATIC_PICTURES] +
                [(f"media:{m['id']}", f"{m['filename']} (uploaded)") for m in self.media_list()])

# PyBank — Python-Based Banking System

A complete **international** online banking web application built with **Python, Flask, HTML and CSS**
(BCA project, Mujyambere Jean Michel, MMDU Mullana).

**Version 3:** customers from any country, accounts in 8 currencies (₹ ₽ $ € £ ¥ AED RWF), the interface in the
6 UN languages (English, Русский, Français, Español, العربية, 中文), staff approval of new accounts and of
changes of personal data, payments to other banks (IMPS / NEFT / UPI / SWIFT sandbox), credit score and loan
documents, EMI auto-debit with late fee, monthly PDF e-statements and push notifications on the phone.

> PyBank is a **fictional bank** made for an academic project. It is not a real bank and no real money is used.
> Designed and developed by **Jean Michel Mujyambere** — BCA (AI & ML), Maharishi Markandeshwar University (2022–2025);
> now Master's student in Applied Mathematics and Informatics at Siberian Federal University.
> In the demo, the developer is the bank's **Branch Manager & System Administrator** (back office admin).
> GitHub: [Mujyambere70](https://github.com/Mujyambere70)

## How to run / Как запустить

```bash
pip install -r requirements.txt
python app.py
```
Open **http://127.0.0.1:5000**. On the first run the database `bank.db` is created and filled
with demo customers and 6 months of transactions.

### Demo logins / Демо-аккаунты
| Customer ID | Name | Account | Password | Transaction PIN |
|---|---|---|---|---|
| PB10000001 | Rahul Verma | Savings | Demo@1234 | 2580 |
| PB10000002 | Priya Nair | Savings | Demo@1234 | 2580 |
| PB10000003 | Arjun Malhotra | Current (business) | Demo@1234 | 2580 |
| PB10000004 | Anna Smirnova (Russia, Russian interface) | Savings, **RUB** | Demo@1234 | 2580 |
| PB10000005 | Amani Uwase (Rwanda, French interface) | Savings, **RWF** | Demo@1234 | 2580 |

Lucia Fernandez (Spain, EUR) has applied online and waits in Back office → *New accounts & data*.

**Back office (bank staff)** — open **http://127.0.0.1:5000/admin** (or the *Staff login* link in the footer):

| Login | Password | Who | Role |
|---|---|---|---|
| `admin@pybank.demo` (EMP001) | `Admin@1234` | **Jean Michel Mujyambere** – Branch Manager & System Administrator | Admin (everything) |
| `neha.sharma@pybank.demo` (EMP002) | `Staff@1234` | Neha Sharma – Customer Service Officer | Staff |

To start again with fresh demo data, delete `bank.db` and run `python app.py` again.

### Tests / Тесты
```bash
python -m unittest discover -s tests -v      # 101 tests, SQLite
# the same tests on PostgreSQL (the database is emptied - use a test database only!)
TEST_DATABASE_URL=postgresql://user:pass@localhost/pybank_test python -m unittest discover -s tests
```

## Put it online without losing data / Онлайн без потери данных

**Why the data was resetting / Почему данные сбрасывались:** on Render's free plan the server's disk is
deleted at every restart, redeploy and after 15 minutes without visitors. The SQLite file `bank.db` was
on that disk, so every change disappeared and the demo data was created again.
На бесплатном Render диск стирается при каждом перезапуске, поэтому файл `bank.db` и все изменения пропадали.

**Fix / Решение:** keep the data in a PostgreSQL database outside Render. The app uses PostgreSQL
automatically when the `DATABASE_URL` environment variable is set (SQLite is still used on your computer).

1. Create a free PostgreSQL database at **neon.com** → copy the *connection string*
   (`postgresql://USER:PASSWORD@HOST/neondb?sslmode=require`).
2. Render → your web service → **Environment** → add `DATABASE_URL` = that string,
   and `SECRET_KEY` = any long random text. Start command: `gunicorn app:app --workers 1 --threads 4`.
3. **Manual Deploy → Deploy latest commit.** On the first start the tables and demo data are created in
   PostgreSQL once; after that nothing is reset. Check it in Back office → Bank settings → *System status*.

`render.yaml` (Render Blueprint) contains the same settings.

**Backups / Резервные копии:** every day the app saves a full copy inside the database (the last 7 are kept)
and can email it to `BACKUP_EMAIL`. In Back office → **Backups** the admin can download a copy (`.json.gz`)
at any time and restore from a file (a copy of the current data is saved first). Neon also keeps its own
restore history.

**Optional alerts / Оповещения (необязательно):**

| Variable | For |
|---|---|
| `BREVO_API_KEY`, `ALERT_EMAIL_FROM` | email alerts through the Brevo API (works on Render free; normal SMTP ports are blocked there) |
| `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`, `ALERT_EMAIL_FROM` | email through any SMTP server (on your own computer) |
| `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_FROM` | SMS alerts through Twilio |
| `BACKUP_EMAIL` | where the daily backup is emailed |

Without them, alerts are still saved and shown on the customer's **Alerts** page.
The demo customers have made-up example.com addresses: the app never emails or texts customers on example domains.

## International bank (version 3) / Международный банк (версия 3)

**Languages / Языки.** The globe menu at the top switches the whole site and Net Banking between the six
official UN languages; Arabic is shown right to left. The choice is remembered for the customer, and alerts,
emails and PDF statements use it too. Translations are in `translations/<lang>.json` (about 1,000 texts each);
`i18n.py` translates every finished page. Bank-staff pages stay in English.

**Currencies / Валюты.** Every account has its own currency, chosen when it is opened. Anyone who opens an
account with the **Russian** interface gets a **ruble (RUB)** account. Money and dates are written in the
style of the language with Babel (`1 234,56 ₽`, `₹1,00,000.00`, `€1,234.56`). Limits, minimum deposits and
loan amounts are set per currency (`world.py`). A transfer between accounts in different currencies uses the
day's exchange rate from open.er-api.com (downloaded once a day); without a rate such transfers are refused.

**Any country / Любая страна.** The account-opening form has every country (names from Unicode CLDR in each
language), the phone code of the country, postal-code checks and the right branch.

**Staff approval / Одобрение сотрудником.**
- An account opened online needs an ID document (PDF/JPG/PNG, max 2 MB) and stays *Pending* until a bank
  officer approves it in Back office → **New accounts & data**; the opening deposit is credited then.
- Changes of personal data (name, email, phone, address, country, occupation) are requests: nothing changes
  until staff approve them. A new name needs an ID proof, a new address or country an address proof.

**Version 3 extras / Дополнительно:**
- **Other banks** (sandbox, no real network): IMPS and UPI instant, NEFT in half-hourly batches, SWIFT abroad
  (IBAN checked with mod 97). An account ending in 0000 or a UPI ID starting with `fail` is "closed": the payment
  is returned and the money comes back. Staff see all payments in Back office → **Other-bank payments**.
- **Credit score** 300–900 made by PyBank itself from the account (history, income, savings, affordability,
  repayments) — not a credit bureau. Loan applications need an ID proof and an income proof.
- **EMI auto-debit** on the due date; if the money is not there, a late fee (2 % of the EMI, admin setting)
  is charged once and the debit is tried again every day.
- **Monthly e-statement** as PDF on the 1st of each month (download in Net Banking, emailed when email is set
  up); any month can also be downloaded. Chinese uses an embedded WenQuanYi font; Arabic PDFs keep English labels.
- **Push notifications:** Net Banking is installable as an app (web app manifest + service worker); every alert
  is also sent as a Web Push notification (RFC 8291 encryption + VAPID, written with `cryptography`).

## Features / Возможности

**Public website:** home page with products (savings, current, FD, loans, cards), interest rates & charges,
about us, branches & ATMs, FAQ, online account opening, net banking login.

**Net banking (after login):**
- Dashboard: balance on a debit-card view, money in/out this month, 6-month chart, recent transactions, last login
- Account details: account number, IFSC, branch, card, login history (successful and failed)
- **Deposit and withdrawal requests** (PIN, daily withdrawal limit ₹50,000): the money moves only after a
  **bank officer approves** the request; a waiting withdrawal keeps the money aside; cancel while pending
- **Loans with EMI schedule:** apply for a personal, vehicle or education loan (₹10,000 – ₹10,00,000,
  6–60 months) with a live EMI calculator; after approval the money is paid into the account; full month-by-month
  schedule (interest / principal / balance), pay EMI online, close early (balance + interest to date, no penalty)
- **Alerts for every transaction:** in-app alert page + email and SMS (customer chooses), also for
  request and loan decisions
- **Quarterly savings interest:** worked out on the balance of every day and credited after each quarter
- **Two-factor login (TOTP):** scan a QR code with Google / Microsoft Authenticator or Authy; after the
  password the 6-digit code is needed; each code works once
- Fund transfer in 3 steps: details → confirm (masked beneficiary name + PIN) → receipt (limit ₹2,00,000/day)
- Printable receipt with reference number for every transaction
- Statement with date and type filters, CSV download (opens in Excel), print / save as PDF
- Profile (edit email, mobile, address) and Security (change password, change PIN)

**My own things (every customer creates, changes and deletes them):**
| Thing | Create | Change | Delete |
|---|---|---|---|
| Beneficiaries (saved payees) | add by account number (checked) | rename | delete · "Pay" button fills the transfer form |
| Fixed deposits | open online (6/12/24/36 months, PIN) | — | close any time: full interest at maturity, 1 % less if early |
| Notes on transactions | write a note on any statement line | edit | empty note = delete |
| Service requests | cheque book, new card, nominee, complaint… | edit while *Open* | cancel while *Open* · the branch reply is shown |

**Back office (staff and admin):**
- Dashboard: customers, total deposits, active FDs, today's money in/out, frozen accounts, locked logins, open requests
- **Customers:** search, open a new account at the branch, edit all KYC details (name, date of birth, contact, address,
  occupation), unlock login, reset password / PIN, change account type, freeze / activate the account,
  manual credit/debit (cash at counter, charges, corrections), **close & delete** a customer (balance must be ₹0)
- **Transactions:** search all customers by text, type, category and dates; **reverse** a wrong entry
  (money records are never deleted — a reversal entry is added, like in a real bank)
- **Approvals:** approve or reject (with a reason) every customer deposit and withdrawal request
- **Loans:** approve (pays the money out) or reject applications; see active loans, outstanding amount and overdue EMIs
- **Service requests:** change status (Open → In progress → Completed / Rejected) and reply to the customer
- Admin only: **staff** (add, edit, change role, reset password, disable, delete), **notices** shown on the website and
  customer dashboard (add, edit, hide, delete), **bank settings** (daily limits, FD, savings and loan interest rates, online account
  opening on/off, pay quarterly interest now, system status), **backups** (download, restore, daily automatic copies),
  read-only **database viewer** with CSV export, **audit log** of every staff action
- **My profile:** every staff member, including the admin, changes their own name, email, phone, designation and password,
  and can turn on two-factor login; an admin can turn it off for a customer or staff member who lost the phone
- Safety: an admin cannot delete, disable or demote himself; staff cannot open admin pages (403)

**Security:**
- Passwords and PINs stored only as hashes (`werkzeug.security`)
- Login locked for 15 minutes after 5 wrong passwords; optional two-factor login (TOTP, RFC 6238) for customers and staff
- The key that signs login cookies is kept (environment or database), so logins survive a restart
- Automatic logout after 10 minutes without activity
- CSRF token in every form, parameterized SQL (no SQL injection), auto-escaped HTML (no XSS)
- Net banking pages are not cached by the browser; a customer can't open another customer's receipt

## Structure / Структура

```
banking_system/
├── app.py            Flask routes: public pages + /netbanking/... + /admin/... (back office)
├── bank.py           OOP logic: Transaction, Customer, BankAccount, Bank (SQLite) + customer self-service
├── admin.py          BankAdmin class: staff, customers, corrections, requests, notices, settings, audit log
├── world.py          countries, currencies, phone / postal codes, money and date formats (Babel)
├── i18n.py           translates every page into the chosen language (translations/*.json)
├── onboarding.py     staff approval of new accounts and personal-data changes, documents
├── payments.py       exchange rates, payments to other banks (sandbox)
├── credit.py         PyBank's own credit score
├── statements.py     monthly PDF e-statements (reportlab)
├── push.py           Web Push notifications (encryption + VAPID)
├── db.py             one database layer for SQLite (computer) and PostgreSQL (online, DATABASE_URL)
├── otp.py            two-factor login codes (TOTP / HOTP, RFC 6238 / 4226) and the QR-code link
├── notify.py         email (Brevo API or SMTP) and SMS (Twilio) alerts, sent in the background
├── demo_data.py      3 demo customers + 6 months of transactions, FDs, a loan with EMIs, payees, requests, 2 staff
├── requirements.txt
├── render.yaml       Render settings (free web service + DATABASE_URL)
├── templates/
│   ├── base.html             header, menu, footer
│   ├── public/               index, about, rates, contact, faq, login, register, registered, error
│   ├── netbanking/           layout (sidebar), dashboard, account, money (deposit/withdraw),
│   │                         transfer, transfer_confirm, receipt, statement, profile, security,
│   │                         payees, fixed_deposits, requests, loans, loan, alerts
│   └── admin/                back office: layout, login, dashboard, customers, customer, customer_new,
│                             transactions, approvals, loans, requests, notices, staff, settings,
│                             backups, database, audit, profile
├── static/
│   ├── style.css
│   └── images/       all PNG / JPG (logo, hero, products, card, branch, security)
└── tests/
    ├── test_bank.py  unit tests (classes, rules, limits, lockout, demo data)
    ├── test_app.py   integration tests (pages, login, transfer, statement, security)
    ├── test_admin_and_selfservice.py   back office (admin and staff roles) + customer's own items
    ├── test_new_features.py   approvals, 2FA, loans/EMI, alerts, interest, backups
    ├── test_international.py  languages, currencies, FX, account and data approval, other banks,
    │                          credit score, auto-debit, PDF statements, Web Push
    ├── support.py    test helpers (SQLite or TEST_DATABASE_URL)
    └── pgmini.py     tiny PostgreSQL client used only by the tests when psycopg is not installed
```

## Main functions / Основные функции

| Function | What it does / Что делает |
|---|---|
| `parse_amount(text)` | Checks user input ("1,500.50") and returns paise (150050). Money is kept in paise (integers) to avoid rounding errors. |
| `format_money(paise)` | Indian format: 10000000 → "₹1,00,000.00". |
| `BankAccount.credit()` / `debit()` | Add / remove money and return a `Transaction`; debit refuses if balance is too low. |
| `Bank.register(data)` | Checks all fields (age 18+, email, mobile, PIN code, password strength, minimum deposit), creates Customer ID `PB########` and 12-digit account number. |
| `Bank.authenticate()` | Login by Customer ID or email, counts wrong attempts, locks after 5. |
| `Bank.transfer()` | Debits sender and credits receiver in **one database transaction** — money can never be lost or created. |
| `Bank.transactions()` | Statement with date / type filters. |
| `Bank.monthly_chart()` | Money in / out per month for the dashboard chart. |
| `Bank.request_cash()` / `decide_cash_request()` | Customer asks for a deposit / withdrawal; a bank officer approves (money moves) or rejects it. Only a *Pending* request can be decided, so it is never paid twice. |
| `Bank.emi(P, rate, n)` | EMI = P·i / (1 − (1 + i)^−n), i = yearly rate / 12. `schedule()` gives the month-by-month plan. |
| `Bank.post_quarterly_interest()` | Savings interest = Σ(balance of each day) × rate / 365, credited once per quarter. |
| `otp.verify()` | Checks a 6-digit TOTP code (±30 s clock drift) and refuses a code that was already used. |
| `Bank.export_data()` / `import_data()` | Full backup as JSON (gzip) and restore in one database transaction. |
| `create_app()` | Builds the Flask app (used by `app.py` and the tests); uses `DATABASE_URL` when it is set. |

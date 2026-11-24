# PyBank — Python-Based Banking System

A complete **international** online banking web application built with **Python, Flask, HTML and CSS**
(BCA project, Mujyambere Jean Michel, MMDU Mullana).

**Version 5 (November 2026):** a **real branch counter** - staff take cash deposits, pay out withdrawals (after
checking the identity document and the signature), make transfers, pay other banks, exchange money, open FDs and
RDs and take EMIs **for the customer standing at the counter**, with a printed **counter receipt**, a **cash drawer**
per teller (opened in the morning, counted in the evening, differences accepted by a supervisor) and the four-eyes
rule for large amounts; the administrator **creates and edits job roles and gives or takes away every single
ability** (36 abilities in 6 groups - every page and button needs one) and a personal limit per person; staff keep their own
**personal details and photo**; the **name of the account holder appears while the account number is typed**
(PyBank, IMPS / NEFT with IFSC, UPI, SWIFT / SEPA with IBAN and BIC) with a *confirmation of payee* check;
**one smart search** everywhere (typo correction, *Did you mean*, synonyms in 6 languages, suggestions while
typing) and the **"Ask PyBank" assistant**; **recurring deposits** and **savings goals**; **sanctions screening**
and a **risk rating** for every customer; **open banking** (OAuth 2.0 + PKCE, customer consent, read-only API);
a **data manager** for every table of the site.

**Version 4:** the whole public website is managed by bank staff (contacts, pages, FAQ, products,
branches & ATMs, banners, charges, pictures - with history and translations in 6 languages); one customer can keep
**accounts in several currencies** and move money between them at the bank's buy / sell rates; **card controls**;
**bills & standing instructions**; **insights & budgets**; **ISO 20022** payment files (pain.001 / pain.002) and SEPA;
**passport MRZ check** (ICAO 9303); **job roles** for staff and the **four-eyes rule**; **suspicious-activity flags**
and **dormant accounts**; signed-in **devices**, rate limits and a **status page**; **download my data** and an
**interest certificate**; a **JSON API** for a mobile app; Arabic PDF statements; savings **interest slabs**,
**compound FD interest** and a **senior-citizen rate**; the back office in all 6 languages.

**Version 3:** customers from any country, accounts in 8 currencies (₹ ₽ $ € £ ¥ AED RWF), the interface in the
6 UN languages (English, Русский, Français, Español, العربية, 中文), staff approval of new accounts and of
changes of personal data, payments to other banks (IMPS / NEFT / UPI / SWIFT sandbox), credit score and loan
documents, EMI auto-debit with late fee, monthly PDF e-statements and push notifications on the phone.

Live demo: **https://michel-pybank.fly.dev/** (Fly.io + Neon PostgreSQL).

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
| PB10000007 | Kofi Mensah (Ghana) | Savings, **USD** - *dormant* (not used for 2 years) | Demo@1234 | 2580 |

Lucia Fernandez (Spain, EUR) has applied online (with her passport MRZ) and waits in Back office → *New accounts & data*.
Rahul also has a **USD account**, card controls, a paid bill, 2 standing instructions, 2 budgets and a signed-in app;
Anna also has a **EUR account** with a SEPA payment (when exchange rates are available).

**Back office (bank staff)** — open **http://127.0.0.1:5000/admin** (or the *Staff login* link in the footer):

| Login | Password | Who | Role |
|---|---|---|---|
| `admin@pybank.demo` (EMP001) | `Admin@1234` | **Jean Michel Mujyambere** – Branch Manager & System Administrator | Admin (everything) |
| `neha.sharma@pybank.demo` (EMP002) | `Staff@1234` | Neha Sharma – Customer Service Officer | Staff · officer |
| EMP003 | `Staff@1234` | Ravi Kumar – Cashier | Staff · teller (cash counter) |
| EMP004 | `Staff@1234` | Meera Joshi – Loan Officer | Staff · loan officer |
| EMP005 | `Staff@1234` | Sergey Volkov – Compliance Officer | Staff · compliance |
| EMP006 | `Staff@1234` | Aisha Khan – Website Editor | Staff · website editor |
| EMP007 | `Staff@1234` | Vikram Singh – Deputy Branch Manager | Staff · branch manager |

Each job role sees only its own pages (see *Job roles & abilities*). Ravi (EMP003) has an open cash drawer
today with three counter operations, a large deposit waiting in *Second approvals* and yesterday's drawer
200 rupees short, waiting for a supervisor. Kofi Mensah waits in *Screening & risk* (his name is close to a
made-up name on the demo sanctions list). Rahul has a recurring deposit, two savings goals and the demo
partner app *Budget Buddy* connected.

To start again with fresh demo data, delete `bank.db` and run `python app.py` again.

### Tests / Тесты
```bash
python -m unittest discover -s tests -v      # 202 tests, SQLite
# the same tests on PostgreSQL (the database is emptied - use a test database only!)
TEST_DATABASE_URL=postgresql://user:pass@localhost/pybank_test python -m unittest discover -s tests
python tools/release_check.py               # opens every page as 11 kinds of users and in 6 languages
python tools/a11y_check.py path/to/axe.min.js   # WCAG 2.1 AA check with axe-core (49 pages)
python tools/collect_strings.py --lang=ru   # texts that still have no translation
```

## Put it online (Fly.io + Neon) / Онлайн (Fly.io + Neon)

The app runs on **Fly.io** from the `Dockerfile`; the data lives in a free **Neon** PostgreSQL database, so
nothing is lost when the machine stops or a new version is deployed. Locally, SQLite (`bank.db`) is used.

1. Install `flyctl` and sign in: `fly auth login`.
2. First time only: `fly launch --no-deploy` (or keep the `fly.toml` in this folder: app `michel-pybank`).
3. Create a free database at **neon.com** and copy its connection string. Put it in Fly **as a secret** -
   type it in your own terminal, never in a chat, an e-mail or a file in the repository:
   `fly secrets set DATABASE_URL="postgresql://..." SECRET_KEY="any-long-random-text" --app michel-pybank`
4. `fly deploy` - the tables and demo data are created once; later deploys keep the data.
5. Check `https://michel-pybank.fly.dev/status` (or `/health` for machines).

If a database password was ever shown somewhere, reset it in Neon (*Roles → Reset password*) and run step 3 again.
`.github/workflows/fly-deploy.yml` runs the tests on every push and deploys when the repository has the secret
`FLY_API_TOKEN` (`fly tokens create deploy`).

**Backups / Резервные копии:** every day the app saves a full copy inside the database (the last 7 are kept)
and can email it to `BACKUP_EMAIL`. In Back office → **Backups** the admin can download a copy (`.json.gz`)
at any time and restore from a file (a copy of the current data is saved first). Neon also keeps its own
restore history.

**Optional alerts / Оповещения (необязательно):**

| Variable | For |
|---|---|
| `BREVO_API_KEY`, `ALERT_EMAIL_FROM` | email alerts through the Brevo API (an HTTPS API, works on any host) |
| `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`, `ALERT_EMAIL_FROM` | email through any SMTP server (on your own computer) |
| `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_FROM` | SMS alerts through Twilio |
| `BACKUP_EMAIL` | where the daily backup is emailed |

Without them, alerts are still saved and shown on the customer's **Alerts** page.
The demo customers have made-up example.com addresses: the app never emails or texts customers on example domains.

## Version 5 / Версия 5

**The counter - staff serve customers / Касса - сотрудники обслуживают клиентов.** Back office → **Counter**:
find the customer (ID, account number, name with typos, phone), check the ID proof, then: cash **deposit**,
cash **withdrawal** (document type and number + signature check are required), **transfer** to a PyBank
account, **payment to another bank** (IMPS / NEFT / UPI / SWIFT / SEPA), **exchange** between own accounts,
**fixed deposit**, **recurring deposit** and **loan EMI**. Every operation gets a receipt number (`CR...`),
a printable receipt and a line in the audit log. Cash goes through the teller's **cash drawer** (one per
currency and day): you cannot pay out more than the drawer holds; in the evening the teller counts the cash -
a difference needs a note and is accepted by another person (*All cash drawers*). An amount at or above the
bank's limit, or above the person's own limit, waits in **Second approvals** and is done when someone else
approves it.

**Abilities for everything / Права на всё.** 36 abilities in 6 groups (customers, counter, money, risk,
website, administration). **Job roles & abilities**: create a role, tick its abilities, rename, reset a
starting role, delete an unused role. **Staff → Abilities**: choose the role, then for each ability *Role /
Yes / No* for this person only, and a personal limit per counter operation. Nobody can change his own
abilities and the bank always keeps one administrator. A test checks that every back-office address needs
an ability.

**Personal details / Личные данные.** Every staff member edits his name, e-mail, phone, address, date of
birth, emergency contact and **photo** (made 160 × 160); the administrator edits everyone's, also the branch.
The **data manager** shows every table of the site (customers, accounts, staff, settings, notices, FAQ,
branches, charges, billers, payees, requests, goals, sanctions list, partner apps): search, change, add and
delete where allowed. Money and secrets are never editable there; every change is in the audit log.

**Name of the account holder / Имя получателя.** While the customer (or the teller) types the account number,
the name appears under the field: for PyBank accounts from the database, for other banks from a sandbox
*name enquiry* (IFSC → bank and branch, UPI handle, IBAN check digits, BIC → bank and country); the name and
the bank are filled in automatically. If the customer typed a name, it is compared: *match*, *close* or
*different* (confirmation of payee). Lookups are limited to 20 a minute.

**Smart search and the assistant / Умный поиск и помощник.** One search box on the website, in Net Banking and
in the back office: typing mistakes are corrected ("withdrawl", "Rahul Varma"), words with the same meaning
are found in 6 languages ("cash out" = "снять" = "retrait" = withdrawal, "FD" = fixed deposit), suggestions
appear while typing, amounts and references are found. Each person finds only what he may see. **Ask PyBank**
answers questions in plain words from the bank's own data: balance, spending by category and period, payments
to a person, rates, currency conversion, EMI; for staff the work lists (approvals, screening, cash drawers).
A language model can be switched on (`ANTHROPIC_API_KEY` + setting `assistant_llm = 1`); only the question is
sent, never account data.

**Also new / Также новое:** **recurring deposits** (6-36 months, FD rate, quarterly compounding, instalments
taken every month, stopped after 3 misses) and **savings goals** with progress; **sanctions screening** of
every new customer against a list (similar spellings found; a confirmed match freezes the accounts) and a
nightly **risk rating** (country, PEP, cash use, large transfers) that compliance can set by hand; **open
banking** - partner apps registered by the bank, the customer agrees in Net Banking, OAuth 2.0 authorization
code with PKCE (S256), read-only `/openbanking/v1/accounts`, 90-day consent, withdrawn in *Connected apps*.

## Version 4 / Версия 4

**Website managed by staff / Сайт управляется сотрудниками.** Back office → **Website content**: contacts and
short texts (phones, e-mail, hours, footer), pages (About, Terms, Privacy, Safe banking tips + new pages with a
safe mini-markdown), FAQ, product cards, branches & ATMs (with map links), banners with start / end dates, the
schedule of charges, billers and pictures (PNG / JPG / WEBP, type checked from the bytes, max 1 MB). Every change
keeps the old version in a **history** (one click puts it back) and can be **translated** into the 5 other
languages; the audit log records who changed what.

**Old future scope, now done / Бывшие планы, теперь сделано:**
- **ISO 20022** - SWIFT and SEPA payments can go out in a `pain.001.001.09` file and come back with a
  `pain.002.001.10` status report (accepted / rejected with a reason such as AC04 - the money returns). XML with
  a DOCTYPE is refused. **SEPA** works from EUR accounts to IBANs of the 41 SEPA countries.
- **Passport MRZ check** - the 2 lines of a passport (TD3) or 3 lines of an ID card (TD1) are read with the
  ICAO 9303 check digits (7-3-1) and compared with the application; only the last 3 characters are saved.
- **Several currency accounts** - one main account (with the card) and up to 4 more, one per currency; the
  dashboard can switch between them; money moves between own accounts at the bank's rate.
- **Arabic PDF statements** - Arabic letters are joined and ordered right to left (`arabic-reshaper`, `python-bidi`).
- **Back office in 6 languages** - each staff member chooses his language.
- **JSON API** for a mobile app - `/api/v1` with tokens (stored as SHA-256), limits per minute and an
  OpenAPI 3.0 description at `/api/v1/openapi.json` (see `/developers`).

**Old limitations, now solved / Бывшие ограничения, теперь решены:** compound FD interest (quarterly) and a
senior-citizen rate (+0.50 %, age 60+); savings **interest slabs** (higher rate above a limit per currency);
the bank's **exchange margin** (buy / sell rates, shown before the customer confirms, 30-day history);
**card controls** (block / unblock, lost card → new number, online / abroad / contactless on-off, daily ATM and
shop limits, a sandbox card network); a public **status page**.

**New / Новое:** bills & **standing instructions** (autopay, paused / stopped by the customer), **insights &
budgets** (alerts at 80 % and 100 %), **signed-in devices** (new-device alert, sign out others), **rate limits**
on login, registration, API and MRZ checks (HTTP 429), **job roles** (officer, teller, loan officer, compliance,
website editor, branch manager), the **four-eyes rule** (large manual entries and reversals wait for a second
person), **suspicious-activity flags** (large cash, structuring, pass-through, many new payees), **dormant
accounts** (24 months without a customer transaction), **download my data** (JSON), an **interest certificate**
(PDF, financial year), report a problem on a receipt (dispute) and a health page for staff.

## International bank (version 3) / Международный банк (версия 3)

**Languages / Языки.** The globe menu at the top switches the whole site and Net Banking between the six
official UN languages; Arabic is shown right to left. The choice is remembered for the customer, and alerts,
emails and PDF statements use it too. Translations are in `translations/<lang>.json` (about 1,000 texts each);
`i18n.py` translates every finished page. Since version 4 the back office is translated too (about 2,280 texts each).

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
├── demo_data.py      demo customers + 6 months of transactions, FDs, a loan with EMIs, payees, requests, 2 staff
├── demo_v4.py        version 4 demo: 5 staff with job roles, translations, USD / EUR accounts, card, bills, flags ...
├── roles.py          job roles and the pages each role may open (v4)
├── sitecontent.py    website content managed by staff: texts, pages, FAQ, products, branches, banners, charges (v4)
├── wallets.py        accounts in several currencies, buy / sell rates, exchange between own accounts (v4)
├── cards.py          debit-card controls and the sandbox card network (v4)
├── iso20022.py       pain.001 payment files and pain.002 status reports (v4)
├── mrz.py            passport / ID-card MRZ reading with ICAO 9303 check digits (v4)
├── autopay.py        billers, bill payments and standing instructions (v4)
├── insights.py       spending by category and monthly budgets (v4)
├── compliance.py     suspicious-activity rules, dormant accounts, four-eyes rule (v4)
├── devices.py        signed-in devices, rate limits, error log, health checks (v4)
├── privacy.py        download my data, interest certificate (v4)
├── api.py            JSON API /api/v1 with tokens and the OpenAPI description (v4)
├── v4_views.py       the web pages of version 4 (public, Net Banking and back office)
├── teller.py         the counter: work for the customer, cash drawers, counter receipts (v5)
├── staffperm.py      job roles in the database, abilities per person, own limits, staff details and photo (v5)
├── nameenquiry.py    the name of the account holder for PyBank, IMPS / NEFT, UPI, SWIFT, SEPA (v5)
├── smartsearch.py    search with typo correction, synonyms in 6 languages and suggestions (v5)
├── assistant.py      "Ask PyBank" - answers questions from the bank's data (v5)
├── deposits.py       recurring deposits and savings goals (v5)
├── screening.py      sanctions list, screening and customer risk rating (v5)
├── openbanking.py    partner apps, consent, OAuth 2.0 + PKCE, read-only API (v5)
├── datamanager.py    the data manager of the back office (v5)
├── v5_views.py       the web pages of version 5
├── demo_v5.py        version 5 demo: cash drawers, counter work, RD and goals, partner app, a sanctions match
├── requirements.txt
├── Dockerfile, fly.toml, .github/workflows/fly-deploy.yml   Fly.io deployment
├── templates/
│   ├── base.html             header, menu, footer
│   ├── public/               index, about, rates, contact, faq, login, register, registered, error,
│   │                         site_page, status, developers (v4), search (v5)
│   ├── netbanking/           layout (sidebar), dashboard, account, money (deposit/withdraw),
│   │                         transfer, transfer_confirm, receipt, statement, profile, security,
│   │                         payees, fixed_deposits, requests, loans, loan, alerts,
│   │                         accounts, card, bills, insights (v4); savings, search, assistant,
│   │                         connected_apps, consent (v5)
│   └── admin/                back office: layout, login, dashboard, customers, customer, customer_new,
│                             transactions, approvals, loans, requests, notices, staff, settings,
│                             backups, database, audit, profile; v4: content*, media, billers, iso,
│                             compliance, flag, dormant, checker, health, fx_history; v5: counter,
│                             counter_customer, counter_receipt, till, tills, roles, staff_access,
│                             screening, openbanking, data*, search, assistant
├── static/
│   ├── style.css
│   ├── v5.js         search suggestions, name of the account holder, the assistant chat (v5)
│   └── images/       all PNG / JPG (logo, hero, products, card, branch, security)
└── tests/
    ├── test_bank.py  unit tests (classes, rules, limits, lockout, demo data)
    ├── test_app.py   integration tests (pages, login, transfer, statement, security)
    ├── test_admin_and_selfservice.py   back office (admin and staff roles) + customer's own items
    ├── test_new_features.py   approvals, 2FA, loans/EMI, alerts, interest, backups
    ├── test_international.py  languages, currencies, FX, account and data approval, other banks,
    │                          credit score, auto-debit, PDF statements, Web Push
    ├── test_v4.py    website content, roles, currencies, cards, ISO 20022, MRZ, Arabic PDF, autopay,
    │                 budgets, compliance, four-eyes, devices & limits, API, privacy, health (42 tests)
    ├── test_v5.py    every address needs an ability, roles and per-person abilities, counter work and cash
    │                 drawers, name enquiry, smart search, assistant, RD and goals, screening, OAuth, data (59 tests)
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
| `Bank.quote(src, dst, amount)` | Customer rate = middle rate × (1 − margin); the amount is rounded half-up with `Decimal`. |
| `Bank.card_payment()` | Sandbox card network: checks card status, channel switches, country, daily limits and balance in that order. |
| `mrz.parse()` / `check_digit()` | ICAO 9303: weights 7-3-1, `<` = 0, letters A=10…Z=35; the sum mod 10 must equal the check digit. |
| `iso20022.build_pain001()` | One payment file with a group header, number of payments and control sum. |
| `Bank._check_aml()` | After every transaction: the four rules; one flag per rule, customer and day. |
| `CheckerOffice.manual_entry()` | Below the checker amount the entry is posted; above it waits for another staff member. |
| `Bank.rate_ok(kind, key)` | Counts requests per minute window in the database (works with several workers). |
| `create_app()` | Builds the Flask app (used by `app.py` and the tests); uses `DATABASE_URL` when it is set. |

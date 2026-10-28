# PyBank — Python-Based Banking System

A complete online banking web application built with **Python, Flask, HTML and CSS**
(BCA project, Mujyambere Jean Michel, MMDU Mullana).

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

**Back office (bank staff)** — open **http://127.0.0.1:5000/admin** (or the *Staff login* link in the footer):

| Login | Password | Who | Role |
|---|---|---|---|
| `admin@pybank.demo` (EMP001) | `Admin@1234` | **Jean Michel Mujyambere** – Branch Manager & System Administrator | Admin (everything) |
| `neha.sharma@pybank.demo` (EMP002) | `Staff@1234` | Neha Sharma – Customer Service Officer | Staff |

To start again with fresh demo data, delete `bank.db` and run `python app.py` again.

### Tests / Тесты
```bash
python -m unittest discover -s tests -v      # 62 tests
```

## Features / Возможности

**Public website:** home page with products (savings, current, FD, loans, cards), interest rates & charges,
about us, branches & ATMs, FAQ, online account opening, net banking login.

**Net banking (after login):**
- Dashboard: balance on a debit-card view, money in/out this month, 6-month chart, recent transactions, last login
- Account details: account number, IFSC, branch, card, login history (successful and failed)
- Deposit and withdraw (with transaction PIN, daily withdrawal limit ₹50,000)
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
- **Service requests:** change status (Open → In progress → Completed / Rejected) and reply to the customer
- Admin only: **staff** (add, edit, change role, reset password, disable, delete), **notices** shown on the website and
  customer dashboard (add, edit, hide, delete), **bank settings** (daily limits, FD interest rates, online account
  opening on/off), read-only **database viewer** with CSV export, **audit log** of every staff action
- **My profile:** every staff member, including the admin, changes their own name, email, phone, designation and password
- Safety: an admin cannot delete, disable or demote himself; staff cannot open admin pages (403)

**Security:**
- Passwords and PINs stored only as hashes (`werkzeug.security`)
- Login locked for 15 minutes after 5 wrong passwords
- Automatic logout after 10 minutes without activity
- CSRF token in every form, parameterized SQL (no SQL injection), auto-escaped HTML (no XSS)
- Net banking pages are not cached by the browser; a customer can't open another customer's receipt

## Structure / Структура

```
banking_system/
├── app.py            Flask routes: public pages + /netbanking/... + /admin/... (back office)
├── bank.py           OOP logic: Transaction, Customer, BankAccount, Bank (SQLite) + customer self-service
├── admin.py          BankAdmin class: staff, customers, corrections, requests, notices, settings, audit log
├── demo_data.py      3 demo customers + 6 months of transactions, FDs, payees, requests, notices, 2 staff
├── requirements.txt
├── templates/
│   ├── base.html             header, menu, footer
│   ├── public/               index, about, rates, contact, faq, login, register, registered, error
│   ├── netbanking/           layout (sidebar), dashboard, account, money (deposit/withdraw),
│   │                         transfer, transfer_confirm, receipt, statement, profile, security,
│   │                         payees, fixed_deposits, requests (customer's own items)
│   └── admin/                back office: layout, login, dashboard, customers, customer, customer_new,
│                             transactions, requests, notices, staff, settings, database, audit, profile
├── static/
│   ├── style.css
│   └── images/       all PNG / JPG (logo, hero, products, card, branch, security)
└── tests/
    ├── test_bank.py  unit tests (classes, rules, limits, lockout, demo data)
    ├── test_app.py   integration tests (pages, login, transfer, statement, security)
    └── test_admin_and_selfservice.py   back office (admin and staff roles) + customer's own items
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
| `create_app()` | Builds the Flask app (used by `app.py` and the tests). |

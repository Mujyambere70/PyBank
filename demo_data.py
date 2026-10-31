"""
demo_data.py - fills an empty PyBank database with 5 demo customers (India, Russia, Rwanda) and
about 6 months of realistic transactions (salary, bills, UPI, cards, ATM, ...), plus an online
application waiting for approval, a change of personal data, payments to other banks and loans.

The bank administrator is the developer of this project (Jean Michel Mujyambere).
All customers and companies here are fictional.
Run alone:  python demo_data.py   (only works on an empty database)
"""

import io
import json
import random
from datetime import date, datetime, timedelta

from bank import Bank, add_months
from world import format_money

DEMO_PASSWORD = "Demo@1234"
DEMO_PIN = "2580"
ADMIN_LOGIN, ADMIN_PASSWORD = "admin@pybank.demo", "Admin@1234"
STAFF_LOGIN, STAFF_PASSWORD = "neha.sharma@pybank.demo", "Staff@1234"

CUSTOMERS = [
    {
        "customer_id": "PB10000001", "full_name": "Rahul Verma", "dob": "1994-08-17", "gender": "Male",
        "email": "rahul.verma@example.com", "mobile": "9812345670", "address": "House 214, Sector 9",
        "city": "Ambala Cantt", "state": "Haryana", "pincode": "133001", "occupation": "Salaried",
        "account_type": "Savings", "initial_deposit": "25000", "salary": 72450,
        "employer": "Brightline Software Pvt Ltd", "fd": (125, 50000, 12),
    },
    {
        "customer_id": "PB10000002", "full_name": "Priya Nair", "dob": "1990-02-03", "gender": "Female",
        "email": "priya.nair@example.com", "mobile": "9876501234", "address": "Flat 3B, Model Town",
        "city": "Ambala City", "state": "Haryana", "pincode": "134003", "occupation": "Salaried",
        "account_type": "Savings", "initial_deposit": "15000", "salary": 48000,
        "employer": "Green Valley Public School", "fd": (70, 20000, 6),
        "loan": (100, 150000, 24, "Personal", "Home renovation - new kitchen and bathroom tiles"),
    },
    {
        "customer_id": "PB10000003", "full_name": "Arjun Malhotra", "dob": "1982-11-25", "gender": "Male",
        "email": "malhotra.traders@example.com", "mobile": "9467012345", "address": "Shop 17, Grain Market",
        "city": "Mullana", "state": "Haryana", "pincode": "133203", "occupation": "Business",
        "account_type": "Current", "initial_deposit": "150000", "salary": 0, "employer": "", "fd": (40, 300000, 24),
    },
    {   # Russian-speaking customer in Krasnoyarsk: ruble account, interface in Russian
        "customer_id": "PB10000004", "full_name": "Anna Smirnova", "dob": "1996-04-11", "gender": "Female",
        "email": "anna.smirnova@example.com", "mobile": "+7 913 000 41 26", "address": "ul. Karla Marksa 48, kv. 17",
        "city": "Krasnoyarsk", "state": "Krasnoyarsk Krai", "pincode": "660049", "occupation": "Salaried",
        "account_type": "Savings", "initial_deposit": "30000", "salary": 98000, "country": "RU", "lang": "ru",
        "employer": "ООО СИБИРЬ ДАТА", "fd": (60, 50000, 12), "style": "ru",
    },
    {   # French-speaking customer in Kigali: Rwandan franc account
        "customer_id": "PB10000005", "full_name": "Amani Uwase", "dob": "1993-09-28", "gender": "Female",
        "email": "amani.uwase@example.com", "mobile": "+250 780 000 512", "address": "KG 11 Ave, House 23",
        "city": "Kigali", "state": "Gasabo", "pincode": "00000", "occupation": "Salaried",
        "account_type": "Savings", "initial_deposit": "200000", "salary": 900000, "country": "RW", "lang": "fr",
        "employer": "UMUCYO DESIGN LTD", "fd": (50, 500000, 6), "style": "rw",
    },
]

# shops and bills for the customers outside India (all names are made up)
LOCAL = {
    "ru": {"salary": "ЗАРПЛАТА/{employer}", "rent": ("ПЕРЕВОД/АРЕНДА КВАРТИРЫ/И. ПЕТРОВ", 28000),
           "bills": [(10, "ЖКХ/УК СИБИРСКИЙ ДОМ/КОММУНАЛЬНЫЕ УСЛУГИ", (4200, 6900)),
                     (12, "ОПЛАТА/ТАЙГА-ТЕЛЕКОМ/ИНТЕРНЕТ", (650, 650)), (15, "ОПЛАТА/МОБИЛЬНАЯ СВЯЗЬ", (450, 690))],
           "shops": ["ПРОДУКТЫ СВЕЖИЙ ДОМ", "МАРКЕТ ЕНИСЕЙ", "ЛАВКА У ДОМА"], "food": ["КОФЕЙНЯ НА МИРА", "СТОЛОВАЯ СИБИРЬ"],
           "online": ["ИНТЕРНЕТ-МАГАЗИН ВСЁ ДЛЯ ДОМА", "КНИЖНЫЙ ДВОР"], "atm": "СНЯТИЕ НАЛИЧНЫХ/БАНКОМАТ КРАСНОЯРСК",
           "card": "ОПЛАТА КАРТОЙ", "scale": 1.0},
    "rw": {"salary": "VIREMENT/SALAIRE/{employer}", "rent": ("VIREMENT/LOYER/J. MUGISHA", 180000),
           "bills": [(10, "PAIEMENT/EAU ET ELECTRICITE", (18000, 32000)), (12, "PAIEMENT/INTERNET FIBRE", (25000, 25000)),
                     (15, "RECHARGE MOBILE", (5000, 10000))],
           "shops": ["MARCHE KIMIRONKO", "SUPERMARCHE IMBUTO", "EPICERIE DU COIN"], "food": ["CAFE UMUCYO", "RESTAURANT INZOZI"],
           "online": ["BOUTIQUE EN LIGNE AKAGERA", "LIBRAIRIE IKIREZI"], "atm": "RETRAIT DAB/PYBANK KIGALI",
           "card": "PAIEMENT CARTE", "scale": 9.0},
}

SHOPS = ["FreshBasket Mart", "Daily Needs Store", "Green Grocers", "Super Bazaar"]
FOOD = ["Spice Route Cafe", "Punjab Dhaba", "Pizza Corner", "Chai Point Express", "Biryani House"]
ONLINE = ["ShopKart Online", "StyleHub Fashion", "BookNest", "GadgetZone"]


def _when(day, rng):
    t = datetime.combine(day, datetime.min.time()) + timedelta(hours=rng.randint(8, 21),
                                                              minutes=rng.randint(0, 59),
                                                              seconds=rng.randint(0, 59))
    return t.strftime("%Y-%m-%d %H:%M:%S")


def _events_for(person, start, end, rng):
    """Return a list of (date, type, category, description, rupees) sorted by date."""
    ev = []
    d = start
    loc = LOCAL.get(person.get("style"))
    while d <= end:
        if loc:
            k = loc["scale"]
            if d.day == 1:
                ev.append((d, "CR", "Salary", loc["salary"].format(employer=person["employer"]), person["salary"]))
            if d.day == 3:
                ev.append((d, "DR", "Rent", loc["rent"][0], loc["rent"][1]))
            for day, text, (lo, hi) in loc["bills"]:
                if d.day == day:
                    ev.append((d, "DR", "Bill payment", text, rng.randint(lo, hi)))
            if d.weekday() in (2, 6) and rng.random() < 0.8:
                ev.append((d, "DR", "Card", f"{loc['card']}/{rng.choice(loc['shops'])}", int(rng.randint(400, 3200) * k)))
            if d.weekday() in (4, 5) and rng.random() < 0.6:
                ev.append((d, "DR", "Card", f"{loc['card']}/{rng.choice(loc['food'])}", int(rng.randint(300, 1800) * k)))
            if rng.random() < 0.07:
                ev.append((d, "DR", "Shopping", f"{loc['card']}/{rng.choice(loc['online'])}", int(rng.randint(900, 7000) * k)))
            if rng.random() < 0.06:
                ev.append((d, "DR", "Withdrawal", loc["atm"], int(rng.choice([3000, 5000]) * k)))
        elif person["occupation"] == "Salaried":
            if d.day == 1:
                ev.append((d, "CR", "Salary", f"NEFT/SALARY/{person['employer'].upper()}", person["salary"]))
            if d.day == 3:
                ev.append((d, "DR", "Rent", "IMPS/RENT/S KAPOOR/MONTHLY RENT", 15000 if person["salary"] > 60000 else 11000))
            if d.day == 7:
                ev.append((d, "DR", "Investment", "ACH/MUTUAL FUND SIP/AXIOM AMC", 5000 if person["salary"] > 60000 else 3000))
            if d.day == 10:
                ev.append((d, "DR", "Bill payment", "BILLPAY/CITY POWER BOARD/ELECTRICITY", rng.randint(900, 2800)))
            if d.day == 12:
                ev.append((d, "DR", "Bill payment", "BILLPAY/JIOLINK FIBER/BROADBAND", 799))
            if d.day == 15:
                ev.append((d, "DR", "Recharge", "UPI/MOBILE PREPAID RECHARGE", rng.choice([299, 479, 719])))
            if d.day == 18 and person["salary"] > 60000:
                ev.append((d, "DR", "EMI", "ACH/PYBANK CAR LOAN EMI", 9850))
            if d.weekday() in (2, 6) and rng.random() < 0.8:
                ev.append((d, "DR", "UPI", f"UPI/{rng.choice(SHOPS).upper()}/GROCERIES", rng.randint(350, 2600)))
            if d.weekday() in (4, 5) and rng.random() < 0.6:
                ev.append((d, "DR", "UPI", f"UPI/{rng.choice(FOOD).upper()}", rng.randint(250, 1900)))
            if rng.random() < 0.07:
                ev.append((d, "DR", "Shopping", f"POS/{rng.choice(ONLINE).upper()}", rng.randint(700, 6500)))
            if rng.random() < 0.06:
                ev.append((d, "DR", "Withdrawal", "ATM WDL/PYBANK ATM AMBALA CANTT", rng.choice([2000, 3000, 5000])))
            if rng.random() < 0.03:
                ev.append((d, "CR", "UPI", "UPI/REFUND/SHOPKART ONLINE", rng.randint(300, 2000)))
        else:   # business current account
            if d.weekday() < 6 and rng.random() < 0.55:
                ev.append((d, "CR", "Deposit", "CASH DEPOSIT/BRANCH/DAILY SALES", rng.randint(18, 85) * 1000))
            if d.weekday() < 5 and rng.random() < 0.25:
                ev.append((d, "CR", "Transfer", f"NEFT/{rng.choice(['SHARMA AGRO', 'GUPTA RETAIL', 'NEW INDIA STORES'])}/INV PAYMENT",
                           rng.randint(25, 140) * 1000))
            if d.weekday() < 5 and rng.random() < 0.22:
                ev.append((d, "DR", "Transfer", f"RTGS/{rng.choice(['PUNJAB GRAINS LTD', 'KISAN SUPPLIERS', 'HARYANA PULSES CO'])}/PURCHASE",
                           rng.randint(40, 160) * 1000))
            if d.day == 20:
                ev.append((d, "DR", "Tax", "GST PAYMENT/GSTIN 06ABCDE1234F1Z5", rng.randint(18, 42) * 1000))
            if d.day == 5:
                ev.append((d, "DR", "Salary", "BULK NEFT/STAFF SALARY/4 EMPLOYEES", 68000))
            if d.day == 10:
                ev.append((d, "DR", "Rent", "NEFT/SHOP RENT/GRAIN MARKET COMMITTEE", 22000))
        # savings interest at the end of each quarter
        if d.month in (3, 6, 9, 12) and (d + timedelta(days=1)).day == 1 and person["account_type"] == "Savings":
            ev.append((d, "CR", "Interest", "SAVINGS A/C INTEREST CREDIT", None))
        d += timedelta(days=1)
    return ev


def seed_staff(office):
    """Back-office accounts. The admin is the author of the project."""
    if office.has_staff():
        return
    opened = (datetime.now() - timedelta(days=200)).strftime("%Y-%m-%d %H:%M:%S")
    office.create_staff(None, "Jean Michel Mujyambere", ADMIN_LOGIN, "", "Branch Manager & System Administrator",
                        "admin", ADMIN_PASSWORD, staff_id="EMP001", when=opened)
    office.create_staff(None, "Neha Sharma", STAFF_LOGIN, "+91 98120 45671", "Customer Service Officer",
                        "staff", STAFF_PASSWORD, staff_id="EMP002", when=opened)


def seed(bank, office=None, months=6, seed_value=42):
    if office is not None:
        seed_staff(office)
    if not bank.is_empty():
        return
    with bank.alerts_paused():            # demo customers are fictional: never email or text them
        _seed(bank, office, months, seed_value)


def _seed(bank, office, months, seed_value):
    rng = random.Random(seed_value)
    end = date.today()
    start = (end.replace(day=1) - timedelta(days=31 * (months - 1))).replace(day=1)
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    for person in CUSTOMERS:
        data = {k: v for k, v in person.items() if k not in ("customer_id", "salary", "employer", "fd", "style", "loan")}
        data.update(password=DEMO_PASSWORD, pin=DEMO_PIN)
        opened = datetime.combine(start - timedelta(days=12), datetime.min.time()) + timedelta(hours=11)
        cid, account = bank.register(data, when=opened.strftime("%Y-%m-%d %H:%M:%S"),
                                     customer_id=person["customer_id"])
        balance = account.balance
        # give every event a time, then post them in time order (so balances run correctly)
        events = sorted(((day.isoformat() + " 23:59:00") if rupees is None else _when(day, rng),
                         typ, cat, desc, rupees)
                        for day, typ, cat, desc, rupees in _events_for(person, start, end, rng))
        fd_days, fd_rupees, fd_months = person["fd"]
        fd_when = (datetime.now() - timedelta(days=fd_days)).replace(hour=12, minute=5).strftime("%Y-%m-%d %H:%M:%S")
        fd_done = False
        loan_id = None
        if person.get("loan"):            # a personal loan paid out ~3 months ago, EMIs paid every month since
            l_days, l_rupees, l_months, l_product, l_purpose = person["loan"]
            l_when = (datetime.now() - timedelta(days=l_days)).replace(hour=14, minute=10, second=0)
            loan_marks = [(l_when.strftime("%Y-%m-%d %H:%M:%S"), "LOAN", "disburse")]
            due = add_months(l_when.date(), 1)
            while datetime.combine(due, datetime.min.time()) + timedelta(hours=9) < datetime.now():
                loan_marks.append(((datetime.combine(due, datetime.min.time()) + timedelta(hours=9, minutes=30))
                                   .strftime("%Y-%m-%d %H:%M:%S"), "LOAN", "emi"))
                due = add_months(due, 1)
            events = sorted(events + [(w, t, k, "", 0) for w, t, k in loan_marks])
        for when, typ, cat, desc, rupees in events:
            if when > now:
                continue
            if not fd_done and when > fd_when and balance >= fd_rupees * 100:
                bank.open_fd(cid, fd_rupees * 100, fd_months, DEMO_PIN, when=fd_when)   # customer booked an FD
                balance = bank.get_account(cid).balance
                fd_done = True
            if typ == "LOAN":
                if cat == "disburse":
                    loan_id = _demo_loan(bank, cid, account.number, person["loan"], when)
                else:
                    bank.pay_emi(cid, loan_id, DEMO_PIN, when=when)
                balance = bank.get_account(cid).balance
                continue
            if rupees is None:          # quarterly interest ~3.5% a year (posted at day end)
                amount = int(balance * 0.035 / 4)
            else:
                amount = rupees * 100
            if amount <= 0 or (typ == "DR" and amount > balance):
                continue
            balance = bank.post(account.number, typ, cat, desc, amount, when).balance_after

        # a few earlier logins, so "last login" looks real
        with bank._connect() as conn:
            for days_ago in (9, 4, 1):
                at = (datetime.now() - timedelta(days=days_ago, hours=rng.randint(1, 8))).strftime("%Y-%m-%d %H:%M:%S")
                conn.execute("INSERT INTO login_history (customer_id, at, ip, success) VALUES (?, ?, ?, 1)",
                             (cid, at, f"49.36.{rng.randint(1, 250)}.{rng.randint(1, 250)}"))
            conn.execute("UPDATE customers SET last_login = ? WHERE customer_id = ?", (at, cid))

    _seed_customer_items(bank, rng)
    _seed_version2(bank, office)


def _demo_loan(bank, cid, number, loan, when):
    """Insert an approved loan and pay it out at the given (past) time."""
    _, rupees, months, product, purpose = loan
    rate = bank.loan_rate(product)
    principal = rupees * 100
    start = datetime.strptime(when, "%Y-%m-%d %H:%M:%S").date()
    with bank._connect() as conn:
        cur = conn.execute(
            "INSERT INTO loans (loan_number, customer_id, account_number, product, principal, rate, months, emi, purpose, "
            "status, applied_at, decided_at, decided_by, disbursed_at, outstanding, next_due) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'Active', ?, ?, ?, ?, ?, ?)",
            ("LN40271839", cid, number, product, principal, rate, months, bank.emi(principal, rate, months), purpose,
             (datetime.strptime(when, "%Y-%m-%d %H:%M:%S") - timedelta(days=3)).strftime("%Y-%m-%d %H:%M:%S"),
             when, "Jean Michel Mujyambere", when, principal, add_months(start, 1).isoformat()))
        loan_id = cur.lastrowid
    bank.post(number, "CR", "Loan", f"LOAN DISBURSAL/LN40271839/{product.upper()} LOAN", principal, when)
    return loan_id


def _seed_version2(bank, office):
    """Deposit/withdrawal requests waiting for staff, a loan application, alerts and paid interest quarters."""
    rahul, priya, arjun, anna, amani = "PB10000001", "PB10000002", "PB10000003", "PB10000004", "PB10000005"
    # savings interest of the past quarters is already in the demo history - mark those quarters as paid
    with bank._connect() as conn:
        for name, start, end in bank.due_quarters():
            rows = conn.execute("SELECT a.currency, COUNT(DISTINCT t.account_number), SUM(t.amount) FROM transactions t "
                                "JOIN accounts a ON a.number = t.account_number WHERE t.category = 'Interest' "
                                "AND t.created_at >= ? AND t.created_at <= ? GROUP BY a.currency",
                                (start.isoformat(), end.isoformat() + " 23:59:59")).fetchall()
            totals = {r[0]: r[2] for r in rows}
            conn.execute("INSERT INTO interest_runs (quarter, run_at, accounts, total, totals) VALUES (?, ?, ?, ?, ?)",
                         (name, end.isoformat() + " 23:59:00", sum(r[1] for r in rows), sum(totals.values()),
                          json.dumps(totals)))
    # one handled request of each kind, so the history is not empty
    ok = bank.request_cash(rahul, "Deposit", 3_000 * 100, DEMO_PIN, "Birthday gift cash")
    no = bank.request_cash(arjun, "Withdrawal", 40_000 * 100, DEMO_PIN, "Supplier payment")
    staff = {"staff_id": "EMP002", "full_name": "Neha Sharma"}
    with bank._connect() as conn:
        ids = {r["ref"]: r["id"] for r in conn.execute("SELECT id, ref FROM cash_requests")}
    if office is not None:
        office.decide_cash(staff, ids[ok], True) if hasattr(office, "decide_cash") else bank.decide_cash_request(ids[ok], True, "Neha Sharma")
        office.decide_cash(staff, ids[no], False, "Signature does not match our records - please visit the branch with ID proof") \
            if hasattr(office, "decide_cash") else bank.decide_cash_request(ids[no], False, "Neha Sharma", "Signature mismatch")
    else:
        bank.decide_cash_request(ids[ok], True, "Neha Sharma")
        bank.decide_cash_request(ids[no], False, "Neha Sharma", "Signature does not match our records - please visit the branch")
    # waiting for approval right now
    bank.request_cash(rahul, "Withdrawal", 5_000 * 100, DEMO_PIN, "Household expenses")
    bank.request_cash(priya, "Deposit", 12_000 * 100, DEMO_PIN, "Tuition fees received")
    bank.apply_loan(arjun, "Vehicle", 6_50_000 * 100, 48, "Small delivery truck for the grain business", DEMO_PIN,
                    documents=[("ID proof", "arjun_malhotra_id.pdf", sample_pdf("ID proof", "Arjun Malhotra")),
                               ("Income proof", "malhotra_traders_tax_return.pdf",
                                sample_pdf("Income proof - tax return summary", "Malhotra Traders"))])
    # in-app alerts for the latest transactions (shown on the Alerts page)
    for cid in (rahul, priya, arjun, anna, amani):
        number = bank.get_account(cid).number
        with bank._connect() as conn:
            for t in reversed(bank.transactions(number, limit=8)):
                word = "credited with" if t.type == "CR" else "debited by"
                when = datetime.strptime(t.created_at, "%Y-%m-%d %H:%M:%S").strftime("%d-%m-%y %H:%M")
                conn.execute("INSERT INTO alerts (customer_id, channel, destination, subject, body, status, created_at) "
                             "VALUES (?, 'in-app', '', ?, ?, 'Delivered', ?)",
                             (cid, f"A/c XX{number[-4:]} {word} {format_money(t.amount, t.currency, 'en')}",
                              f"PyBank: A/c XX{number[-4:]} {word} {format_money(t.amount, t.currency, 'en')} on {when} "
                              f"({t.category}, Ref {t.ref}). Avl bal {format_money(t.balance_after, t.currency, 'en')}. "
                              f"Not you? Call 1800-202-5050.",
                              t.created_at))


    _seed_version3(bank, office)


def sample_pdf(title, name):
    """A small PDF that stands for an uploaded document (demo only)."""
    from reportlab.lib.pagesizes import A5
    from reportlab.pdfgen import canvas
    buf = io.BytesIO()
    pdf = canvas.Canvas(buf, pagesize=A5)
    pdf.setFont("Helvetica-Bold", 16)
    pdf.drawString(40, 520, title)
    pdf.setFont("Helvetica", 11)
    pdf.drawString(40, 495, f"Name: {name}")
    pdf.drawString(40, 470, "SAMPLE DOCUMENT - PyBank demo data, not a real document.")
    pdf.rect(30, 420, 360, 140)
    pdf.save()
    return buf.getvalue()


def _seed_version3(bank, office):
    """International demo: an application waiting for approval, a change of personal data, payments to other banks."""
    rahul, anna = "PB10000001", "PB10000004"
    # 1. a new customer from Spain applied online yesterday (Spanish interface, euro account)
    bank.register({"full_name": "Lucia Fernandez", "dob": "1998-06-21", "gender": "Female",
                   "email": "lucia.fernandez@example.com", "mobile": "+34 600 000 318", "address": "Calle Mayor 12, 3B",
                   "city": "Madrid", "state": "Madrid", "pincode": "28013", "occupation": "Student", "country": "ES",
                   "lang": "es", "account_type": "Savings", "initial_deposit": "500", "password": DEMO_PASSWORD,
                   "pin": DEMO_PIN}, when=_ago(1, 18), customer_id="PB10000006", online=True,
                  documents=[("ID proof", "lucia_fernandez_passport.pdf", sample_pdf("Passport", "Lucia Fernandez"))])
    # 2. Rahul moved to a new flat and asked to change his address (waiting for approval)
    bank.request_profile_change(rahul, {"address": "Flat 12, Green Park Apartments", "pincode": "133001",
                                        "city": "Ambala Cantt"},
                                documents=[("Address proof", "electricity_bill.pdf",
                                            sample_pdf("Address proof - electricity bill", "Rahul Verma"))])
    # 3. payments to other banks: IMPS (done), NEFT to a closed account (returned), SWIFT abroad (in progress)
    bank.send_external(rahul, "IMPS", {"name": "Sunita Kapoor", "account": "50100234567812", "ifsc": "NOVA0001234",
                                       "remarks": "Rent November"}, 15000 * 100, DEMO_PIN)
    bank.send_external(rahul, "NEFT", {"name": "Vikas Sharma", "account": "38291100000", "ifsc": "SUNR0004521",
                                       "remarks": "Old account"}, 2000 * 100, DEMO_PIN)
    bank.process_external(datetime.now() + timedelta(hours=1))         # the NEFT batch ran
    bank.send_external(anna, "SWIFT", {"name": "Max Weber", "account": "DE89370400440532013000", "bic": "RHMUDEFF",
                                       "bank_name": "Rheinland Musterbank", "remarks": "Language course fee"},
                       45000 * 100, DEMO_PIN)


def _ago(days, hour=11):
    return (datetime.now() - timedelta(days=days)).replace(hour=hour, minute=20, second=0).strftime("%Y-%m-%d %H:%M:%S")


def _seed_customer_items(bank, rng):
    """Payees, notes, service requests and notices - so every page has realistic content."""
    acc = {c["customer_id"]: bank.get_account(c["customer_id"]).number for c in CUSTOMERS}
    rahul, priya, arjun = "PB10000001", "PB10000002", "PB10000003"
    bank.add_beneficiary(rahul, acc[priya], "Priya (sister)")
    bank.add_beneficiary(rahul, acc[arjun], "Malhotra Traders")
    bank.add_beneficiary(priya, acc[rahul], "Rahul")
    bank.add_beneficiary(arjun, acc[rahul], "Rahul Verma")

    # personal notes on a few of Rahul's transactions
    for t in bank.transactions(acc[rahul], limit=80):
        if t.category == "Salary":
            bank.set_tx_note(rahul, t.ref, f"Salary for {datetime.strptime(t.created_at[:7], '%Y-%m').strftime('%B')}")
        elif t.category == "Rent":
            bank.set_tx_note(rahul, t.ref, "House rent - paid on time")
        elif t.category == "EMI":
            bank.set_tx_note(rahul, t.ref, "Car loan EMI")

    r1 = bank.create_request(rahul, "Cheque book", "Please send a 25-leaf cheque book to my home address.", when=_ago(12))
    r2 = bank.create_request(arjun, "Complaint", "A customer NEFT of 45,000 from Gupta Retail is not visible yet. "
                                                 "UTR GR2410-77812.", when=_ago(2, 15))
    bank.create_request(priya, "Update nominee", "Please add my mother, Lakshmi Nair, as nominee for my savings account.",
                        when=_ago(1, 10))
    bank.create_request(rahul, "New debit card", "My card is worn out and the chip does not work. Please issue a new one.",
                        when=_ago(0, 9))
    with bank._connect() as conn:
        conn.execute("UPDATE service_requests SET status = 'Completed', reply = ?, replied_by = ?, updated_at = ? "
                     "WHERE id = ?", ("Cheque book (25 leaves) was sent by speed post, it will arrive in 3-4 days.",
                                      "Jean Michel Mujyambere", _ago(10), r1))
        conn.execute("UPDATE service_requests SET status = 'In progress', reply = ?, replied_by = ?, updated_at = ? "
                     "WHERE id = ?", ("We have asked the sender's bank for the UTR details. Update within 24 hours.",
                                      "Neha Sharma", _ago(1, 16), r2))
        conn.execute("INSERT INTO notices (title, body, level, active, created_by, created_at) VALUES (?, ?, ?, 1, ?, ?)",
                     ("Higher FD rates this festive season", "Book a 3-year fixed deposit in Net Banking and earn "
                      "7.25% a year. Minimum deposit just ₹1,000.", "offer", "Jean Michel Mujyambere", _ago(6)))
        conn.execute("INSERT INTO notices (title, body, level, active, created_by, created_at) VALUES (?, ?, ?, 1, ?, ?)",
                     ("Beware of fake KYC calls", "PyBank will never call you to ask for your password, PIN or OTP. "
                      "Do not share them with anyone.", "warning", "Jean Michel Mujyambere", _ago(3)))
        for days, sid, action, target, details in [
                (14, "EMP001", "Bank settings changed", "", "fd_rate_36=7.25"),
                (10, "EMP001", "Service request answered", f"#{r1}", "Completed: cheque book sent"),
                (6, "EMP001", "Notice published", "", "Higher FD rates this festive season"),
                (3, "EMP001", "Notice published", "", "Beware of fake KYC calls"),
                (1, "EMP002", "Service request answered", f"#{r2}", "In progress: asked sender's bank")]:
            conn.execute("INSERT INTO audit_log (staff_id, action, target, details, at) VALUES (?, ?, ?, ?, ?)",
                         (sid, action, target, details, _ago(days, 16)))


if __name__ == "__main__":
    from admin import BankAdmin
    b = Bank("bank.db")
    seed(b, BankAdmin(b))
    print("Demo data ready. Customer: PB10000001 /", DEMO_PASSWORD, "| PIN", DEMO_PIN)
    print("Back office:", ADMIN_LOGIN, "/", ADMIN_PASSWORD)

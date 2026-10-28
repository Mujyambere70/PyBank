"""
demo_data.py - fills an empty PyBank database with 3 demo customers and
about 6 months of realistic transactions (salary, bills, UPI, ATM, ...).

The bank administrator is the developer of this project (Jean Michel Mujyambere).
All customers and companies here are fictional.
Run alone:  python demo_data.py   (only works on an empty database)
"""

import random
from datetime import date, datetime, timedelta

from bank import Bank

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
    },
    {
        "customer_id": "PB10000003", "full_name": "Arjun Malhotra", "dob": "1982-11-25", "gender": "Male",
        "email": "malhotra.traders@example.com", "mobile": "9467012345", "address": "Shop 17, Grain Market",
        "city": "Mullana", "state": "Haryana", "pincode": "133203", "occupation": "Business",
        "account_type": "Current", "initial_deposit": "150000", "salary": 0, "employer": "", "fd": (40, 300000, 24),
    },
]

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
    while d <= end:
        if person["occupation"] == "Salaried":
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
    rng = random.Random(seed_value)
    end = date.today()
    start = (end.replace(day=1) - timedelta(days=31 * (months - 1))).replace(day=1)
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    for person in CUSTOMERS:
        data = {k: v for k, v in person.items() if k not in ("customer_id", "salary", "employer", "fd")}
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
        for when, typ, cat, desc, rupees in events:
            if when > now:
                continue
            if not fd_done and when > fd_when and balance >= fd_rupees * 100:
                bank.open_fd(cid, fd_rupees * 100, fd_months, DEMO_PIN, when=fd_when)   # customer booked an FD
                balance = bank.get_account(cid).balance
                fd_done = True
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

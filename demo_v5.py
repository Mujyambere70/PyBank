"""
demo_v5.py - demo content for the version 5 pages (runs once, also on an existing online database).

Everything here is made up: the people, the addresses and the partner app. No real person, company
or web address is used (".example" addresses never belong to anybody).
"""

from datetime import date, timedelta

from core import COUNTER, now


def seed_v5(bank, office):
    with bank._connect() as conn:
        if conn.execute("SELECT 1 FROM settings WHERE key = 'demo_v5'").fetchone():
            return False
        conn.execute("INSERT INTO settings (key, value) VALUES ('demo_v5', ?)", (now(),))
    admin, ravi = office.get_staff("EMP001"), office.get_staff("EMP003")
    if admin is None or ravi is None or bank.is_empty():
        return False
    rahul, priya = "PB10000001", "PB10000002"
    r_acc, p_acc = bank.get_account(rahul).number, bank.get_account(priya).number

    with bank.alerts_paused():
        # ---- staff personal details (made up)
        office.update_staff_details(admin, "EMP003", {"full_name": ravi["full_name"], "email": ravi["email"],
                                                      "phone": ravi["phone"], "designation": ravi["designation"],
                                                      "address": "14 Demo Colony, Ambala Cantt", "dob": "1991-07-09",
                                                      "emergency_contact": "Sunita Kumar, +91 98120 00000",
                                                      "branch": "Ambala Cantt Main Branch", "role": "staff"})

        # ---- yesterday: Ravi's drawer closed 200 rupees short, waiting for a supervisor
        y = (date.today() - timedelta(days=1)).isoformat()
        with bank._connect() as conn:
            conn.execute("INSERT INTO tills (staff_id, day, currency, opening, cash_in, cash_out, counted, difference, "
                         "status, note, opened_at, closed_at) VALUES (?, ?, 'INR', ?, ?, ?, ?, ?, 'Difference', ?, ?, ?)",
                         ("EMP003", y, 20_000_000, 4_550_000, 3_120_000, 21_410_000, -20_000,
                          "Short by 200 - two 100 notes probably given twice", y + " 09:05:00", y + " 17:10:00"))

        # ---- today: Ravi opens his drawer and serves three customers
        office.open_till(ravi, "INR", 20_000_000)
        office.counter(ravi, rahul, "deposit", {"account": r_acc, "amount": "12500", "depositor": "Rahul Verma",
                                                "note": "Salary cash"})
        office.counter(ravi, rahul, "transfer", {"account": r_acc, "to_account": p_acc, "amount": "3000",
                                                 "remarks": "Shared trip"})
        office.counter(ravi, priya, "withdraw", {"account": p_acc, "amount": "8000", "id_checked": "1",
                                                 "id_type": "Aadhaar card", "id_number": "XXXX-XXXX-4417", "signature": "1"})
        # a large deposit that waits for a second person
        office.counter(ravi, priya, "deposit", {"account": p_acc, "amount": "75000", "depositor": "Priya Nair",
                                                "note": "Sale of old car"})

        # ---- Rahul's recurring deposit and two savings goals
        bank.open_rd(rahul, r_acc, 100_000, 12, COUNTER)
        g1 = bank.create_goal(rahul, r_acc, "New laptop", 8_500_000, (date.today() + timedelta(days=150)).isoformat())
        bank.move_goal(rahul, g1, 1_200_000, True, COUNTER)
        g2 = bank.create_goal(rahul, r_acc, "Trip to Shimla", 3_000_000, (date.today() + timedelta(days=70)).isoformat())
        bank.move_goal(rahul, g2, 400_000, True, COUNTER)

        # ---- an open-banking partner app and Rahul's consent
        client_id, _secret = bank.register_ob_app(admin, "Budget Buddy", "Demo Apps Ltd",
                                                  "https://budget-buddy.example/callback")
        bank.give_consent(rahul, bank.ob_app(client_id), ["accounts", "transactions"])

        # ---- the version 4 demo customer Kofi Mensah has a name close to one on the demo sanctions list
        with bank._connect() as conn:
            row = conn.execute("SELECT customer_id FROM customers WHERE full_name = 'Kofi Mensah'").fetchone()
        if row:
            bank.screen_customer(row[0])
        bank.update_all_risks()
        bank.set_risk(admin, "PB10000003", "Medium", True, "Customer is a local politician (demo)")
    return True

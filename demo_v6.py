"""
demo_v6.py - demo content for the version 6 pages (runs once, also on an existing online database).

Made-up data only: a vault top-up waiting for a second person, two cheques (one in clearing, one
returned), nominees, a credit card with a bill and one new application, a payment held by the sanctions
check, answers taught to the assistant, an own ability with a page rule, a temporary ability, and a
short chat. No real person, bank or address is used.
"""

from datetime import date, timedelta

from core import now
from demo_data import DEMO_PIN


def seed_v6(bank, office):
    with bank._connect() as conn:
        if conn.execute("SELECT 1 FROM settings WHERE key = 'demo_v6'").fetchone():
            return False
        conn.execute("INSERT INTO settings (key, value) VALUES ('demo_v6', ?)", (now(),))
    admin, ravi, neha = office.get_staff("EMP001"), office.get_staff("EMP003"), office.get_staff("EMP002")
    if admin is None or ravi is None or neha is None or bank.is_empty():
        return False
    rahul, priya, arjun = "PB10000001", "PB10000002", "PB10000003"
    today = date.today()

    with bank.alerts_paused():
        # ---- the vault: the cash van came this morning (confirmed), Ravi asks for more cash (waiting)
        office.ensure_vault()
        mid = office.head_office_move(admin, "INR", 5_00_000 * 100, True, "Seal CV-2207 / slip 4471")
        office.decide_vault_move(neha, mid, True, "Counted with the van guard")
        try:
            office.request_topup(ravi, "INR", 50_000 * 100, "Pension day - many withdrawals")
        except Exception:
            pass                                  # Ravi's drawer may be closed on this database

        # ---- cheques: one waits for clearing, one bounced earlier
        bank.deposit_cheque(rahul, bank.get_account(rahul).number, 18_500 * 100, "402187", "Sunrise Bank",
                            "Kavita Malhotra", (today - timedelta(days=2)).isoformat(), "EMP003", today)
        old = bank.deposit_cheque(priya, bank.get_account(priya).number, 7_200 * 100, "118000", "Metro City Bank",
                                  "Rohan Verma", (today - timedelta(days=10)).isoformat(), "EMP003",
                                  today - timedelta(days=6))
        with bank._connect() as conn:
            qid = conn.execute("SELECT id FROM cheques WHERE ref = ?", (old["ref"],)).fetchone()[0]
        bank.decide_cheque(qid, False, "clearing", "Funds insufficient")

        # ---- nominees
        bank.save_nominees(rahul, [{"name": "Sunita Kumar", "relation": "Mother", "dob": "1968-04-12", "share": 60,
                                    "address": "12 Demo Colony, Ambala Cantt"},
                                   {"name": "Aarav Kumar", "relation": "Brother", "dob": "2001-09-30", "share": 40,
                                    "address": "12 Demo Colony, Ambala Cantt"}])
        bank.save_nominees(priya, [{"name": "Meera Sharma", "relation": "Sister", "share": 100}])

        # ---- credit cards: Rahul has one with a bill, Arjun has just applied
        card = bank.apply_credit_card(rahul, 65_000 * 100, 1_50_000 * 100)
        bank.decide_credit_card(admin, card, True, 1_50_000 * 100)
        k = bank.credit_card(rahul)
        bill = today.replace(day=5) if today.day >= 5 else (today.replace(day=1) - timedelta(days=1)).replace(day=5)
        with bank._connect() as conn:                         # the card was given a month before the last bill
            conn.execute("UPDATE credit_cards SET decided_at = ? WHERE id = ?",
                         ((bill - timedelta(days=33)).isoformat() + " 11:00:00", k["id"]))
        for amount, shop, back in ((2_349_00, "Demo Fuel Station", 27), (5_890_00, "City Mart Groceries", 20),
                                   (12_499_00, "Bright Electronics", 12), (899_00, "StreamBox Monthly", 6)):
            bank.cc_purchase(k["id"], amount, shop, (bill - timedelta(days=back)).isoformat() + " 12:00:00")
        bank.make_cc_statements(bill)
        bank.pay_credit_card(rahul, "minimum", 0, DEMO_PIN)          # Rahul paid the minimum of the bill
        bank.cc_purchase(k["id"], 640_00, "Cafe Demo", now())
        bank.apply_credit_card(arjun, 48_000 * 100, 0)

        # ---- a payment to another bank held by the sanctions check
        try:
            bank.send_external(priya, "NEFT", {"name": "Viktor Grauberg", "account": "55019922000", "ifsc": "NOVA0001207",
                                               "remarks": "Invoice 77"}, 3_500 * 100, DEMO_PIN)
        except Exception:
            pass

    # ---- the assistant learns two answers of the branch
    from chatbot import ChatBot
    bot = ChatBot(bank, office)
    bot.save_answer(admin, {"question": "Do you have lockers?", "keywords": "locker, safe deposit box, safe",
                            "answer": "Yes. The Ambala Cantt branch has small and medium lockers (from ₹1,500 a year). "
                                      "Ask at the counter with your Customer ID.", "link": "/contact"})
    bot.save_answer(admin, {"question": "What time does the branch open?", "keywords": "timing, hours, open, close",
                            "answer": "Monday to Saturday, 10:00 to 16:00. Closed on Sundays and bank holidays.",
                            "link": "/contact"})
    # a short chat of Rahul, so the history shows
    who = "customer:" + rahul
    customer = bank.get_customer(rahul)
    for q in ("hello", "my balance", "do you have a locker?"):
        bot.ask(who, q, [], customer=customer, account=bank.get_account(rahul))

    # ---- an own ability with a page rule, given to the branch manager role; a temporary ability for Ravi
    code = office.create_ability(admin, "See held payments", "Compliance",
                                 "Open the list of payments stopped by the sanctions check", ["/admin/payment-holds"])
    roles_now = office.job_roles()
    if "branch_manager" in roles_now:
        r = roles_now["branch_manager"]
        office.save_role(admin, "branch_manager", r["label"], list(r["abilities"] | {code}))
    office.grant_temporary(admin, "EMP003", "cheques.clear", (today + timedelta(days=5)).isoformat(),
                           "Cover for Neha on leave")
    return True

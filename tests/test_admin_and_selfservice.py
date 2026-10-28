"""Tests for the back office (admin / staff) and the customer's own items
(beneficiaries, fixed deposits, notes, service requests)."""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import create_app  # noqa: E402
from bank import BankError  # noqa: E402

PIN = "2580"
RAHUL, PRIYA, ARJUN = "PB10000001", "PB10000002", "PB10000003"


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app = create_app(os.path.join(self.tmp.name, "t.db"))
        self.bank, self.office = self.app.bank, self.app.office
        self.c = self.app.test_client()
        with self.c.session_transaction() as s:
            s["csrf_token"] = "test-token"

    def tearDown(self):
        self.tmp.cleanup()

    def post(self, url, **data):
        data["csrf_token"] = "test-token"
        return self.c.post(url, data=data)

    def customer_login(self, cid=RAHUL):
        return self.post("/login", login=cid, password="Demo@1234")

    def staff_login(self, login="admin@pybank.demo", password="Admin@1234"):
        return self.post("/admin/login", login=login, password=password)

    def balance(self, cid):
        return self.bank.get_account(cid).balance


class TestCustomerOwnItems(Base):
    def setUp(self):
        super().setUp()
        self.customer_login()

    def test_pages(self):
        for url in ["/netbanking", "/netbanking/beneficiaries", "/netbanking/fixed-deposits", "/netbanking/requests",
                    "/netbanking/statement", "/netbanking/transfer"]:
            self.assertEqual(self.c.get(url).status_code, 200, url)

    def test_beneficiary_add_rename_delete(self):
        number = self.bank.get_account(PRIYA).number
        for b in self.bank.beneficiaries(RAHUL):                     # start with an empty list
            self.bank.delete_beneficiary(RAHUL, b["id"])
        self.post("/netbanking/beneficiaries", account_number=number, nickname="Sister")
        b = self.bank.beneficiaries(RAHUL)[0]
        self.assertEqual((b["nickname"], b["account_number"]), ("Sister", number))
        self.post(f"/netbanking/beneficiaries/{b['id']}/rename", nickname="Priya N")
        self.assertEqual(self.bank.beneficiaries(RAHUL)[0]["nickname"], "Priya N")
        self.post(f"/netbanking/beneficiaries/{b['id']}/delete")
        self.assertEqual(self.bank.beneficiaries(RAHUL), [])

    def test_beneficiary_rules(self):
        own = self.bank.get_account(RAHUL).number
        for number in [own, "999999999999", "12ab"]:
            with self.assertRaises(BankError):
                self.bank.add_beneficiary(RAHUL, number, "Test")
        with self.assertRaises(BankError):                      # already saved in demo data
            self.bank.add_beneficiary(RAHUL, self.bank.get_account(PRIYA).number, "Again")

    def test_cannot_touch_other_customers_beneficiary(self):
        other = self.bank.beneficiaries(PRIYA)[0]
        self.post(f"/netbanking/beneficiaries/{other['id']}/delete")
        self.assertEqual(len(self.bank.beneficiaries(PRIYA)), 1)

    def test_fixed_deposit_open_and_close(self):
        before = self.balance(RAHUL)
        self.post("/netbanking/fixed-deposits", amount="10000", months="12", pin=PIN)
        fd = next(f for f in self.bank.fixed_deposits(RAHUL) if f["principal"] == 10000 * 100)
        self.assertEqual(self.balance(RAHUL), before - 10000 * 100)
        self.assertEqual(fd["rate"], self.bank.fd_rates()[12])
        self.post(f"/netbanking/fixed-deposits/{fd['id']}/close", pin=PIN)
        closed = next(f for f in self.bank.fixed_deposits(RAHUL) if f["id"] == fd["id"])
        self.assertEqual(closed["status"], "Closed")
        self.assertEqual(self.balance(RAHUL), before)          # closed the same day: no interest yet

    def test_fixed_deposit_rules(self):
        with self.assertRaises(BankError):
            self.bank.open_fd(RAHUL, 500 * 100, 12, PIN)        # below minimum
        with self.assertRaises(BankError):
            self.bank.open_fd(RAHUL, 5000 * 100, 7, PIN)        # wrong period
        with self.assertRaises(BankError):
            self.bank.open_fd(RAHUL, 5000 * 100, 12, "1111")    # wrong PIN
        with self.assertRaises(BankError):
            self.bank.open_fd(RAHUL, 10 ** 12, 12, PIN)         # not enough money

    def test_old_fd_pays_interest(self):
        fd = next(f for f in self.bank.fixed_deposits(RAHUL) if f["status"] == "Active")
        self.assertGreater(fd["value_today"], fd["principal"])  # demo FD is a few months old

    def test_transaction_note(self):
        ref = self.bank.transactions(self.bank.get_account(RAHUL).number, limit=1)[0].ref
        self.post(f"/netbanking/note/{ref}", note="Birthday gift")
        self.assertEqual(self.bank.tx_notes(RAHUL)[ref], "Birthday gift")
        self.post(f"/netbanking/note/{ref}", note="")
        self.assertNotIn(ref, self.bank.tx_notes(RAHUL))
        other_ref = self.bank.transactions(self.bank.get_account(PRIYA).number, limit=1)[0].ref
        self.assertEqual(self.post(f"/netbanking/note/{other_ref}", note="x").status_code, 404)

    def test_service_request_create_edit_delete(self):
        self.post("/netbanking/requests", type="Cheque book", details="Please send 50 leaves.")
        r = self.bank.service_requests(RAHUL)[0]
        self.post(f"/netbanking/requests/{r['id']}/edit", type="Cheque book", details="Please send 100 leaves.")
        self.assertEqual(self.bank.service_requests(RAHUL)[0]["details"], "Please send 100 leaves.")
        self.post(f"/netbanking/requests/{r['id']}/delete")
        self.assertNotIn(r["id"], [x["id"] for x in self.bank.service_requests(RAHUL)])

    def test_cannot_change_answered_request(self):
        done = next(r for r in self.bank.service_requests(RAHUL) if r["status"] == "Completed")
        with self.assertRaises(BankError):
            self.bank.update_request(RAHUL, done["id"], "Other", "Changing a closed one")
        with self.assertRaises(BankError):
            self.bank.delete_request(RAHUL, done["id"])

    def test_customer_cannot_open_back_office(self):
        r = self.c.get("/admin")
        self.assertEqual(r.status_code, 302)
        self.assertIn("/admin/login", r.location)


class TestBackOffice(Base):
    def setUp(self):
        super().setUp()
        self.staff_login()
        self.me = self.office.get_staff("EMP001")

    def test_admin_is_the_developer(self):
        self.assertEqual(self.me["full_name"], "Jean Michel Mujyambere")
        self.assertEqual(self.me["role"], "admin")
        self.assertIn("Jean Michel Mujyambere", self.c.get("/about").get_data(as_text=True))

    def test_wrong_staff_password(self):
        self.c.get("/admin/logout")
        self.assertEqual(self.staff_login(password="nope").status_code, 401)

    def test_all_pages(self):
        for url in ["/admin", "/admin/customers", "/admin/customers/new", f"/admin/customers/{RAHUL}",
                    "/admin/transactions", "/admin/requests", "/admin/notices", "/admin/staff", "/admin/settings",
                    "/admin/database", "/admin/database?table=staff", "/admin/audit", "/admin/profile"]:
            self.assertEqual(self.c.get(url).status_code, 200, url)
        html = self.c.get("/admin/database?table=customers").get_data(as_text=True)
        self.assertNotIn("pbkdf2", html)
        self.assertNotIn("scrypt", html)

    def test_open_account_for_customer(self):
        r = self.post("/admin/customers/new", full_name="Olga Kim", dob="1995-05-05", gender="Female",
                      email="olga@example.com", mobile="9123456780", address="Flat 7, Lake View", city="Ambala",
                      state="Haryana", pincode="133001", occupation="Salaried", account_type="Savings",
                      initial_deposit="2000", password="Start1234", pin="4826")
        cid = r.location.rsplit("/", 1)[1]
        self.assertEqual(self.bank.get_customer(cid).full_name, "Olga Kim")
        self.assertEqual(self.balance(cid), 2000 * 100)

    def test_edit_customer_details(self):
        self.post(f"/admin/customers/{RAHUL}/edit", full_name="Rahul K Verma", dob="1994-08-17", gender="Male",
                  email="rahul.k@example.com", mobile="9812345670", address="House 99, Sector 1", city="Ambala",
                  state="Haryana", pincode="133001", occupation="Business")
        c = self.bank.get_customer(RAHUL)
        self.assertEqual((c.full_name, c.email, c.occupation), ("Rahul K Verma", "rahul.k@example.com", "Business"))

    def test_reset_password_pin_and_unlock(self):
        for _ in range(5):
            try:
                self.bank.authenticate(PRIYA, "wrong")
            except BankError:
                pass
        with self.assertRaises(BankError):
            self.bank.authenticate(PRIYA, "Demo@1234")       # locked now
        self.post(f"/admin/customers/{PRIYA}/unlock")
        self.assertTrue(self.bank.authenticate(PRIYA, "Demo@1234"))
        self.post(f"/admin/customers/{PRIYA}/password", password="NewPass123")
        self.assertTrue(self.bank.authenticate(PRIYA, "NewPass123"))
        self.post(f"/admin/customers/{PRIYA}/pin", pin="4826")
        self.bank.deposit(PRIYA, 100, "4826")

    def test_freeze_blocks_money_movement(self):
        self.post(f"/admin/customers/{RAHUL}/freeze", reason="Lost card")
        with self.assertRaises(BankError):
            self.bank.withdraw(RAHUL, 100 * 100, PIN)
        with self.assertRaises(BankError):                  # nobody can send money to a frozen account
            self.bank.transfer(PRIYA, self.bank.get_account(RAHUL).number, 100, PIN)
        self.post(f"/admin/customers/{RAHUL}/activate")
        self.bank.withdraw(RAHUL, 100 * 100, PIN)

    def test_manual_entry_and_reversal(self):
        before = self.balance(ARJUN)
        self.post(f"/admin/customers/{ARJUN}/entry", kind="CR", amount="1500", description="Cash deposit at counter")
        self.assertEqual(self.balance(ARJUN), before + 1500 * 100)
        with self.bank._connect() as conn:
            tx_id = conn.execute("SELECT id FROM transactions WHERE category = 'Branch' ORDER BY id DESC").fetchone()[0]
        self.post(f"/admin/transactions/{tx_id}/reverse", reason="Entered twice by mistake")
        self.assertEqual(self.balance(ARJUN), before)
        with self.assertRaises(BankError):                  # cannot reverse twice
            self.office.reverse_transaction(self.me, tx_id, "Again please")

    def test_delete_customer_only_with_zero_balance(self):
        self.post(f"/admin/customers/{PRIYA}/delete")
        self.assertTrue(self.bank.get_customer(PRIYA))      # still there: balance and FD
        for f in self.bank.fixed_deposits(PRIYA):
            if f["status"] == "Active":
                self.bank.close_fd(PRIYA, f["id"], PIN)
        self.office.manual_entry(self.me, PRIYA, "DR", self.balance(PRIYA), "Account closure payout")
        self.post(f"/admin/customers/{PRIYA}/delete")
        with self.assertRaises(BankError):
            self.bank.get_customer(PRIYA)
        self.assertNotIn("Priya (sister)", [b["nickname"] for b in self.bank.beneficiaries(RAHUL)])

    def test_answer_request(self):
        r = next(x for x in self.office.requests() if x["status"] == "Open")
        self.post(f"/admin/requests/{r['id']}/answer", status="Completed", reply="Done, card is on the way.")
        mine = next(x for x in self.bank.service_requests(r["customer_id"]) if x["id"] == r["id"])
        self.assertEqual((mine["status"], mine["reply"], mine["replied_by"]),
                         ("Completed", "Done, card is on the way.", "Jean Michel Mujyambere"))

    def test_notices_add_edit_delete(self):
        self.post("/admin/notices", title="Branch closed on Sunday", body="The branch is closed this Sunday.",
                  level="warning", active="1")
        n = self.office.notices()[0]
        self.assertIn("Branch closed on Sunday", self.c.get("/").get_data(as_text=True))
        self.post("/admin/notices", id=str(n["id"]), title="Branch open on Sunday", body="Open 10-2.", level="info")
        self.assertEqual(self.office.notices()[0]["active"], 0)
        self.post(f"/admin/notices/{n['id']}/delete")
        self.assertNotIn(n["id"], [x["id"] for x in self.office.notices()])

    def test_settings_change_limits_and_rates(self):
        self.post("/admin/settings", daily_withdraw_limit="1000", daily_transfer_limit="200000", fd_rate_6="6",
                  fd_rate_12="7.5", fd_rate_24="7", fd_rate_36="7.25", online_account_opening="1")
        self.assertEqual(self.bank.fd_rates()[12], 7.5)
        with self.assertRaises(BankError):
            self.bank.withdraw(RAHUL, 1500 * 100, PIN)       # over the new ₹1,000 limit
        self.post("/admin/settings", daily_withdraw_limit="50000", daily_transfer_limit="200000", fd_rate_6="6",
                  fd_rate_12="6.8", fd_rate_24="7", fd_rate_36="7.25")
        self.assertEqual(self.c.get("/open-account").status_code, 302)   # online opening switched off

    def test_staff_management(self):
        self.post("/admin/staff", full_name="Amit Kumar", email="amit@pybank.demo", phone="", designation="Cashier",
                  role="staff", password="Cash1234")
        s = next(x for x in self.office.staff_list() if x["email"] == "amit@pybank.demo")
        self.post(f"/admin/staff/{s['staff_id']}/edit", full_name="Amit Kumar Singh", email="amit@pybank.demo",
                  phone="9876543210", designation="Head Cashier", role="staff")
        self.assertEqual(self.office.get_staff(s["staff_id"])["designation"], "Head Cashier")
        self.post(f"/admin/staff/{s['staff_id']}/delete")
        self.assertIsNone(self.office.get_staff(s["staff_id"]))
        self.post("/admin/staff/EMP001/delete")              # cannot delete yourself
        self.assertIsNotNone(self.office.get_staff("EMP001"))

    def test_admin_changes_own_details_and_password(self):
        self.post("/admin/profile", form="details", full_name="Jean Michel Mujyambere", email="jm@pybank.demo",
                  phone="", designation="Head of Branch Operations")
        self.assertEqual(self.office.get_staff("EMP001")["designation"], "Head of Branch Operations")
        self.post("/admin/profile", form="password", old="Admin@1234", new="Better1234", new2="Better1234")
        self.assertTrue(self.office.authenticate_staff("jm@pybank.demo", "Better1234"))

    def test_audit_log_records_actions(self):
        self.post(f"/admin/customers/{RAHUL}/unlock")
        self.assertEqual(self.office.audit_log(1)[0]["action"], "Login unlocked")


class TestStaffRole(Base):
    """A normal staff member serves customers but cannot use admin pages."""

    def setUp(self):
        super().setUp()
        self.staff_login("neha.sharma@pybank.demo", "Staff@1234")

    def test_allowed_pages(self):
        for url in ["/admin", "/admin/customers", f"/admin/customers/{RAHUL}", "/admin/transactions",
                    "/admin/requests", "/admin/profile"]:
            self.assertEqual(self.c.get(url).status_code, 200, url)

    def test_forbidden_pages(self):
        for url in ["/admin/staff", "/admin/settings", "/admin/database", "/admin/audit", "/admin/notices"]:
            self.assertEqual(self.c.get(url).status_code, 403, url)
        self.assertEqual(self.post(f"/admin/customers/{RAHUL}/delete").status_code, 403)


if __name__ == "__main__":
    unittest.main()

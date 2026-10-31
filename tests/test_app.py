"""Integration tests: Flask routes + Bank + database together (Flask test client)."""

import io
import os
import re
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from support import DRIVER, cash, target  # noqa: E402

from app import create_app  # noqa: E402

DEMO = {"login": "PB10000001", "password": "Demo@1234"}
PIN = "2580"


class TestRoutes(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def setUp(self):
        db = os.path.join(self.tmp.name, f"{self._testMethodName}.db")
        self.app = create_app(target(db), driver=DRIVER)            # with demo data
        self.client = self.app.test_client()

    # ---------- helpers ----------

    def post(self, url, **data):
        html = self.client.get(url).get_data(as_text=True)
        data["csrf_token"] = re.search(r'name="csrf_token" value="([^"]+)"', html).group(1)
        return self.client.post(url, data=data, follow_redirects=True)

    def text(self, r):
        return r.get_data(as_text=True)

    def login(self, **creds):
        return self.post("/login", **(creds or DEMO))

    # ---------- public site ----------

    def test_public_pages(self):
        for url in ["/", "/about", "/rates", "/contact", "/faq", "/login", "/open-account"]:
            r = self.client.get(url)
            self.assertEqual(r.status_code, 200, url)
            self.assertIn("not a real bank", self.text(r), url)       # disclaimer on every page
        self.assertEqual(self.client.get("/nope").status_code, 404)

    def test_all_images_are_jpg_or_png(self):
        folder = os.path.join(self.app.root_path, "static", "images")
        for name in os.listdir(folder):
            self.assertRegex(name, r"\.(jpg|png)$")
        html = "".join(self.text(self.client.get(u)) for u in ["/", "/about", "/contact", "/login"])
        for src in re.findall(r'src="([^"]+)"', html):
            self.assertRegex(src, r"\.(jpg|png)$")
            with self.client.get(src) as r:
                self.assertEqual(r.status_code, 200, src)

    def test_netbanking_needs_login(self):
        for url in ["/netbanking", "/netbanking/statement", "/netbanking/transfer"]:
            r = self.client.get(url)
            self.assertEqual(r.status_code, 302)
            self.assertIn("/login", r.location)

    # ---------- account opening & login ----------

    def test_open_account_then_login(self):
        r = self.post("/open-account", full_name="Neha Gupta", dob="1999-01-15", gender="Female",
                      occupation="Student", email="neha@example.com", mobile="9700000001",
                      address="45 Model Town", city="Ambala", state="Haryana", pincode="134003",
                      account_type="Savings", initial_deposit="1000", password="Neha2026x",
                      password2="Neha2026x", pin="4826", pin2="4826", terms="yes", country="IN",
                      id_doc=(io.BytesIO(b"%PDF-1.4 test id"), "passport.pdf"))
        html = self.text(r)
        self.assertIn("Thank you, Neha", html)                  # the application waits for the bank
        cid = re.search(r'id="customerId">(PB\d{8})<', html).group(1)
        r = self.login(login=cid, password="Neha2026x")
        self.assertIn("being checked by the bank", self.text(r))
        self.app.bank.decide_customer(cid, True, "Officer")      # staff approve it
        r = self.login(login=cid, password="Neha2026x")
        self.assertIn("Neha Gupta", self.text(r))
        self.assertIn("₹1,000.00", self.text(r))

    def test_open_account_errors(self):
        r = self.post("/open-account", full_name="Neha Gupta", password="a", password2="b")
        self.assertIn("passwords do not match", self.text(r))

    def test_wrong_password(self):
        r = self.login(login="PB10000001", password="nope")
        self.assertEqual(r.status_code, 401)
        self.assertIn("4 attempt(s) left", self.text(r))

    def test_dashboard_and_logout(self):
        html = self.text(self.login())
        for words in ["Rahul Verma", "Available balance", "Recent transactions", "Last 6 months", "PYBK0001024"]:
            self.assertIn(words, html)
        r = self.client.get("/logout", follow_redirects=True)
        self.assertIn("logged out securely", self.text(r))
        self.assertEqual(self.client.get("/netbanking").status_code, 302)

    # ---------- money ----------

    def balance(self):
        html = self.text(self.client.get("/netbanking"))
        return re.search(r'id="balance">([^<]+)<', html).group(1)

    def test_deposit_and_withdraw_with_receipt(self):
        self.login()
        r = self.post("/netbanking/deposit", amount="1500", note="Gift", pin=PIN)
        self.assertIn("was sent to the branch", self.text(r))      # waits for a bank officer
        req = self.app.bank.cash_requests("PB10000001")[0]
        self.assertEqual((req["status"], req["amount"]), ("Pending", 150000))
        t = self.app.bank.decide_cash_request(req["id"], True, "Officer")[1]
        r = self.client.get(f"/netbanking/receipt/{t.ref}")
        self.assertIn("CASH DEPOSIT/", self.text(r))
        self.assertIn("Gift", self.text(r))
        r = self.post("/netbanking/withdraw", amount="999999", pin=PIN)
        self.assertIn("limit", self.text(r))
        r = self.post("/netbanking/withdraw", amount="500", pin="0000")
        self.assertIn("Incorrect transaction PIN", self.text(r))

    def test_transfer_two_steps(self):
        self.login()
        priya = self.app.bank.get_account("PB10000002")
        before = priya.balance
        r = self.post("/netbanking/transfer", step="details", to_account=priya.number,
                      to_account2=priya.number, amount="2500", remarks="Dinner")
        self.assertIn("P**** N***", self.text(r))                  # masked beneficiary name
        self.assertEqual(self.app.bank.get_account("PB10000002").balance, before)   # not sent yet
        r = self.post("/netbanking/transfer", step="confirm", to_account=priya.number,
                      to_account2=priya.number, amount="2500", remarks="Dinner", pin=PIN)
        self.assertIn("Transaction successful", self.text(r))
        self.assertEqual(self.app.bank.get_account("PB10000002").balance, before + 250000)

    def test_transfer_errors(self):
        self.login()
        r = self.post("/netbanking/transfer", step="details", to_account="111111111111",
                      to_account2="222222222222", amount="10")
        self.assertIn("do not match", self.text(r))
        r = self.post("/netbanking/transfer", step="details", to_account="111111111111",
                      to_account2="111111111111", amount="10")
        self.assertIn("not found", self.text(r))

    def test_statement_and_csv(self):
        self.login()
        html = self.text(self.client.get("/netbanking/statement?from=2000-01-01&type=CR"))
        self.assertIn("SALARY", html)
        self.assertNotIn("ATM WDL", html)
        r = self.client.get("/netbanking/statement.csv?from=2000-01-01")
        self.assertEqual(r.mimetype, "text/csv")
        self.assertIn("Reference", self.text(r))

    def test_receipt_of_other_customer_is_hidden(self):
        priya = self.app.bank.get_account("PB10000002")
        ref = self.app.bank.transactions(priya.number, limit=1)[0].ref
        self.login()
        self.assertEqual(self.client.get(f"/netbanking/receipt/{ref}").status_code, 404)

    def test_profile_and_security(self):
        self.login()
        for p in self.app.bank.profile_changes("PB10000001", "Pending"):      # the demo already has one waiting
            self.app.bank.cancel_profile_change("PB10000001", p["id"])
        r = self.post("/netbanking/profile", full_name="Rahul Verma", email="rahul.new@example.com",
                      mobile="9812345670", address="House 214, Sector 9", city="Ambala Cantt", state="Haryana",
                      pincode="133001", country="IN", occupation="Salaried")
        self.assertIn("sent to the bank", self.text(r))
        self.assertEqual(self.app.bank.get_customer("PB10000001").email, "rahul.verma@example.com")   # not yet
        pid = self.app.bank.profile_changes("PB10000001", "Pending")[0]["id"]
        self.app.bank.decide_profile_change(pid, True, "Officer")
        self.assertEqual(self.app.bank.get_customer("PB10000001").email, "rahul.new@example.com")
        r = self.post("/netbanking/security", action="pin", password="Demo@1234", new_pin="9035", new_pin2="9035")
        self.assertIn("PIN was changed", self.text(r))

    # ---------- security ----------

    def test_csrf_and_escaping(self):
        self.assertEqual(self.client.post("/login", data=DEMO).status_code, 400)
        self.login()
        r = self.post("/netbanking/deposit", amount="10", note="<script>x</script>", pin=PIN)
        self.assertNotIn("<script>x</script>", self.text(r))

    def test_session_timeout(self):
        self.login()
        with self.client.session_transaction() as s:
            s["last_active"] -= 11 * 60
        r = self.client.get("/netbanking", follow_redirects=True)
        self.assertIn("session expired", self.text(r))


if __name__ == "__main__":
    unittest.main()

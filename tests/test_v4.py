"""Tests for version 4: website content managed by staff, job roles, accounts in several currencies with
buy / sell rates, card controls, ISO 20022 payment files and SEPA, MRZ check, Arabic PDF, bills and standing
instructions, budgets, suspicious-activity flags, dormant accounts, the four-eyes rule, devices, rate limits,
the JSON API, "download my data", the interest certificate, compound FD interest and savings slabs, health."""

import json
import os
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from support import DRIVER, cash, target  # noqa: E402

import iso20022  # noqa: E402
import mrz  # noqa: E402
import roles  # noqa: E402
import sitecontent  # noqa: E402
import statements  # noqa: E402
from app import create_app  # noqa: E402
from bank import Bank, BankError  # noqa: E402
from demo_v4 import td3  # noqa: E402

PIN = "4826"
RATES = {"USD": 1.0, "RUB": 80.0, "INR": 88.0, "EUR": 0.86, "GBP": 0.76}     # made-up test rates (units per 1 USD)
PERSON = dict(full_name="Test User", dob="1995-05-05", gender="Female", email="test.user@mailbox.in",
              mobile="9811111111", address="12 Main Road", city="Ambala", state="Haryana", pincode="133001",
              occupation="Salaried", account_type="Savings", initial_deposit="100000", password="Secret123", pin=PIN,
              country="IN")
OTHER = dict(PERSON, full_name="Asha Rao", email="asha.rao@mailbox.in", mobile="9822222222")
SENIOR = dict(PERSON, full_name="Kamla Devi", email="kamla.devi@mailbox.in", mobile="9833333333", dob="1950-01-10")
SPECIMEN = "P<UTOERIKSSON<<ANNA<MARIA<<<<<<<<<<<<<<<<<<<\nL898902C36UTO7408122F1204159ZE184226B<<<<<10"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 40


class BankBase(unittest.TestCase):
    """A bank without demo data (fast)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.bank = Bank(target(os.path.join(self.tmp.name, "t.db")), driver=DRIVER, fx_fetch=lambda: RATES)
        self.cid, self.acc = self.bank.register(PERSON)
        self.other, self.other_acc = self.bank.register(OTHER)
        self.bank.refresh_fx_rates()

    def tearDown(self):
        self.tmp.cleanup()

    def bal(self, number):
        with self.bank._connect() as conn:
            return conn.execute("SELECT balance FROM accounts WHERE number = ?", (number,)).fetchone()[0]


class AppBase(unittest.TestCase):
    """The web app without the big demo, with an admin, an editor, a teller, a loan officer and two customers."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app = create_app(target(os.path.join(self.tmp.name, "t.db")), seed=False, driver=DRIVER,
                              housekeeping=False, fx_fetch=lambda: RATES)
        self.bank, self.office = self.app.bank, self.app.office
        o = self.office
        o.create_staff(None, "Main Admin", "admin@test.example", "", "Manager", "admin", "Admin@1234", staff_id="EMP001")
        self.admin = o.get_staff("EMP001")
        for sid, name, job in (("EMP002", "Web Editor", "content_editor"), ("EMP003", "Cash Teller", "teller"),
                               ("EMP004", "Loan Person", "loan_officer"), ("EMP005", "Second Officer", "officer")):
            o.create_staff(self.admin, name, f"{sid.lower()}@test.example", "", job, "staff", "Staff@1234", staff_id=sid)
            o.set_job_role(self.admin, sid, job)
        self.cid, self.acc = self.bank.register(PERSON)
        self.other, self.other_acc = self.bank.register(OTHER)
        self.bank.refresh_fx_rates()
        self.c = self.app.test_client()
        self.csrf()

    def tearDown(self):
        self.tmp.cleanup()

    def csrf(self, client=None):
        with (client or self.c).session_transaction() as s:
            s["csrf_token"] = "test-token"

    def post(self, url, client=None, **data):
        data["csrf_token"] = "test-token"
        return (client or self.c).post(url, data=data)

    def staff(self, sid, password="Staff@1234"):
        return self.post("/admin/login", login=sid, password=password)

    def customer(self, client=None, ua="Mozilla/5.0 (Windows NT 10.0) Chrome/131.0"):
        client = client or self.c
        self.csrf(client)
        return client.post("/login", data={"login": self.cid, "password": "Secret123", "csrf_token": "test-token"},
                           headers={"User-Agent": ua})


# =========================================================================== website content

class TestSiteContent(AppBase):
    def test_contacts_change_show_and_restore(self):
        self.assertIn("1800-000-7926", self.c.get("/contact").get_data(as_text=True))
        self.staff("EMP002")
        self.post("/admin/content/texts", care_phone="1800-111-2222", care_email="help@pybank.example")
        page = self.c.get("/contact").get_data(as_text=True)
        self.assertIn("1800-111-2222", page)
        self.assertIn("help@pybank.example", self.c.get("/").get_data(as_text=True))       # footer
        hist = self.bank.history("text", "care_phone")
        self.assertEqual(hist[0]["data"]["value"], "1800-000-7926")                        # the old value is kept
        self.post(f"/admin/content/history/{hist[0]['id']}/restore")
        self.assertEqual(self.bank.site_texts()["care_phone"], "1800-000-7926")
        self.assertEqual(self.bank.history("text", "care_phone")[0]["data"]["value"], "1800-111-2222")

    def test_page_text_is_safe(self):
        html = sitecontent.render_body("## Title\nHello **world** <script>alert(1)</script>\n- one\n- [two](/rates)\n"
                                       "[bad](javascript:alert(1)) [ext](https://example.org)")
        self.assertIn("<h2>Title</h2>", html)
        self.assertIn("<strong>world</strong>", html)
        self.assertNotIn("<script>", html)
        self.assertIn("&lt;script&gt;", html)
        self.assertIn('<a href="/rates">two</a>', html)
        self.assertNotIn('href="javascript', html)
        self.assertIn('rel="noopener"', html)

    def test_pages_new_hidden_preview_delete(self):
        self.staff("EMP002")
        self.post("/admin/content/pages/new", slug="fees-2027", title="Fees 2027", body="## New fees\nNothing changes.",
                  published="1", in_footer="1")
        self.assertIn("Nothing changes.", self.c.get("/page/fees-2027").get_data(as_text=True))
        self.assertIn("Fees 2027", self.c.get("/").get_data(as_text=True))                   # footer link
        self.post("/admin/content/pages/fees-2027", title="Fees 2027", body="## New fees\nHidden now.")  # unpublished
        self.assertEqual(self.c.get("/page/fees-2027?preview=1").status_code, 200)
        visitor = self.app.test_client()
        self.assertEqual(visitor.get("/page/fees-2027").status_code, 404)
        self.assertEqual(visitor.get("/page/fees-2027?preview=1").status_code, 404)        # only staff can preview
        self.post("/admin/content/pages/fees-2027", action="delete")
        self.assertEqual(self.c.get("/page/fees-2027?preview=1").status_code, 404)
        with self.assertRaises(BankError):
            self.bank.save_page(self.admin, "Bad Slug!", {"title": "x", "body": "y"}, new=True)
        with self.assertRaises(BankError):
            self.bank.delete_page(self.admin, "about")

    def test_lists_faq_products_branches_banners_charges(self):
        b = self.bank
        self.assertGreaterEqual(len(b.faqs()), 8)
        self.assertTrue(b.faqs(q="locked"))
        self.assertFalse(b.faqs(q="zzzz"))
        pid = b.save_item(self.admin, "product", {"title": "Gold Loan", "text": "Loan against gold jewellery.",
                                                  "button": "Know more", "link": "/rates", "image": "static:loans.jpg",
                                                  "active": "1"})
        self.assertIn("Gold Loan", self.c.get("/").get_data(as_text=True))
        with self.assertRaises(BankError):
            b.save_item(self.admin, "product", {"title": "X", "text": "y", "button": "b", "link": "javascript:x()"})
        b.save_item(self.admin, "product", {**b.content_item("product", pid), "active": ""}, pid)
        self.assertNotIn("Gold Loan", self.c.get("/").get_data(as_text=True))
        today = date.today()
        b.save_item(self.admin, "banner", {"title": "Old offer", "starts": (today - timedelta(days=9)).isoformat(),
                                           "ends": (today - timedelta(days=1)).isoformat(), "active": "1"})
        self.assertNotIn("Old offer", [x["title"] for x in b.banners()])
        with self.assertRaises(BankError):
            b.save_item(self.admin, "banner", {"title": "Wrong", "starts": "2027-02-01", "ends": "2027-01-01"})
        b.save_item(self.admin, "branch", {"name": "Moscow Office", "kind": "Branch", "address": "1 Tverskaya St",
                                           "city": "Moscow", "country": "RU", "lat": "55.757", "lng": "37.615",
                                           "active": "1"})
        page = self.c.get("/contact?country=RU").get_data(as_text=True)
        self.assertIn("Moscow Office", page)
        self.assertNotIn("Mullana Branch", page)
        self.assertIn("openstreetmap.org/?mlat=55.757", page)
        with self.assertRaises(BankError):
            b.save_item(self.admin, "branch", {"name": "Nowhere", "kind": "ATM", "address": "x x", "city": "y",
                                               "country": "IN", "lat": "123", "lng": "0"})
        cid_ = b.save_item(self.admin, "charge", {"service": "Locker rent (small)", "savings_fee": "₹1,500 a year",
                                                  "current_fee": "₹1,500 a year", "active": "1"})
        self.assertIn("Locker rent (small)", self.c.get("/rates").get_data(as_text=True))
        b.delete_item(self.admin, "charge", cid_)
        self.assertNotIn("Locker rent (small)", self.c.get("/rates").get_data(as_text=True))

    def test_translations(self):
        banner = self.bank.content_items("banner")[0]["id"]
        self.staff("EMP002")
        self.post(f"/admin/content/translate/banner/{banner}", lang="fr", title="Ouvrez un compte en 5 minutes",
                  eyebrow="", text="", button="Ouvrir")
        fr = self.app.test_client()
        fr.get("/lang/fr")
        self.assertIn("Ouvrez un compte en 5 minutes", fr.get("/").get_data(as_text=True))
        self.assertIn("Open a savings account in 5 minutes", self.app.test_client().get("/").get_data(as_text=True))
        self.assertEqual(self.bank.translation_status()[("banner", str(banner))], {"fr"})
        with self.assertRaises(BankError):
            self.bank.save_translation(self.admin, "banner", banner, "en", {"title": "x"})

    def test_pictures(self):
        self.staff("EMP002")
        with self.assertRaises(BankError):
            self.bank.add_media(self.admin, "virus.png", b"MZ\x90\x00 not a picture")
        mid = self.bank.add_media(self.admin, "logo.png", PNG, "Logo")
        r = self.c.get(f"/media/{mid}")
        self.assertEqual((r.status_code, r.mimetype), (200, "image/png"))
        pid = self.bank.content_items("product")[0]["id"]
        self.bank.save_item(self.admin, "product", {**self.bank.content_item("product", pid), "image": f"media:{mid}",
                                                    "active": "1"}, pid)
        self.assertIn(f"/media/{mid}", self.c.get("/").get_data(as_text=True))
        with self.assertRaises(BankError):
            self.bank.delete_media(self.admin, mid)                                    # still used by a product
        with self.assertRaises(BankError):
            self.bank.add_media(self.admin, "big.png", PNG + b"0" * 1024 * 1024)

    def test_billers_managed(self):
        n = len(self.bank.billers())
        self.staff("EMP002")
        self.post("/admin/content/billers", name="Metro Water (test)", category="Water", country="IN", currency="INR",
                  number_label="Connection ID", number_regex=r"^[0-9]{6}$", active="1")
        self.assertEqual(len(self.bank.billers()), n + 1)
        with self.assertRaises(BankError):
            self.bank.save_biller(self.admin, {"name": "Bad", "category": "Water", "number_label": "x",
                                               "number_regex": "[0-9"})


# =========================================================================== roles

class TestRoles(AppBase):
    def test_ability_map(self):
        self.assertEqual(roles.ability_for("/admin/content/pages/x"), "content.manage")
        self.assertEqual(roles.ability_for("/admin/customers/PB1/edit", "POST"), "customers.edit")
        self.assertEqual(roles.ability_for("/admin/customers/PB1"), "customers.view")
        self.assertEqual(roles.ability_for("/admin/customers/new"), "customers.edit")
        self.assertEqual(roles.ability_for("/admin/staff/EMP2/job", "POST"), "staff.manage")
        self.assertIsNone(roles.ability_for("/admin"))
        self.assertTrue(roles.can(self.admin, "backups"))
        self.assertFalse(roles.can(self.office.get_staff("EMP003"), "loans.decide"))

    def test_each_role_sees_only_its_pages(self):
        cases = {"EMP002": (["/admin/content", "/admin/notices", "/admin/media"], ["/admin/customers", "/admin/approvals",
                                                                                 "/admin/settings", "/admin/staff"]),
                 "EMP003": (["/admin/approvals", "/admin/customers", "/admin/requests"], ["/admin/loans", "/admin/content",
                                                                                         "/admin/customers/new"]),
                 "EMP004": (["/admin/loans", "/admin/applications"], ["/admin/approvals", "/admin/compliance"])}
        for sid, (ok, no) in cases.items():
            c = self.app.test_client()
            self.csrf(c)
            self.post("/admin/login", client=c, login=sid, password="Staff@1234")
            for url in ok:
                self.assertEqual(c.get(url).status_code, 200, (sid, url))
            for url in no:
                self.assertEqual(c.get(url).status_code, 403, (sid, url))
            self.assertEqual(c.get("/admin").status_code, 200)

    def test_no_links_to_forbidden_pages(self):
        """A teller and a loan officer never see a link that would answer 403 (version 4 release check)."""
        import re
        for sid in ("EMP002", "EMP003", "EMP004"):
            c = self.app.test_client()
            self.csrf(c)
            self.post("/admin/login", client=c, login=sid, password="Staff@1234")
            for page in ("/admin", "/admin/customers", f"/admin/customers/{self.cid}"):
                r = c.get(page)
                if r.status_code != 200:
                    continue
                for link in set(re.findall(r'href="(/admin[^"#?]*)"', r.get_data(as_text=True))):
                    if "logout" in link:
                        continue
                    self.assertNotEqual(c.get(link).status_code, 403, (sid, page, link))

    def test_change_role(self):
        self.staff("EMP001", "Admin@1234")
        self.post("/admin/staff/EMP003/job", job_role="loan_officer")
        self.assertEqual(self.office.get_staff("EMP003")["job_role"], "loan_officer")
        with self.assertRaises(BankError):
            self.office.set_job_role(self.admin, "EMP001", "teller")                    # not your own role
        with self.assertRaises(BankError):
            self.office.set_job_role(self.admin, "EMP003", "king")
        officer = self.office.get_staff("EMP005")
        with self.assertRaises(BankError):
            self.office.set_job_role(officer, "EMP005", "branch_manager")              # nobody raises his own role


# =========================================================================== currencies

class TestWallets(BankBase):
    def test_open_exchange_close(self):
        usd = self.bank.open_currency_account(self.cid, "USD", PIN)
        self.assertEqual([a.currency for a in self.bank.accounts_of(self.cid)], ["INR", "USD"])
        with self.assertRaises(BankError):
            self.bank.open_currency_account(self.cid, "USD", PIN)                       # one per currency
        self.assertEqual(self.bank.get_account(self.cid).number, self.acc.number)       # main account stays first
        t, q = self.bank.exchange(self.cid, self.acc.number, usd.number, 88_000, PIN)    # ₹880 -> $10 - 1 %
        self.assertEqual(q["amount"], 990)
        self.assertEqual(q["cost"], 10)
        self.assertEqual(self.bal(usd.number), 990)
        self.assertEqual(self.bal(self.acc.number), 10_000_000 - 88_000)
        with self.assertRaises(BankError):
            self.bank.close_currency_account(self.cid, usd.number, PIN)                 # not empty
        self.bank.exchange(self.cid, usd.number, self.acc.number, 990, PIN)
        self.bank.close_currency_account(self.cid, usd.number, PIN)
        self.assertEqual(self.bank.account_of(self.cid, usd.number).status, "Closed")
        with self.assertRaises(BankError):
            self.bank.close_currency_account(self.cid, self.acc.number, PIN)             # main account

    def test_rate_table_change_needs_an_older_day(self):
        table = {r["currency"]: r for r in self.bank.rate_table("INR")}
        self.assertIsNone(table["USD"]["change"])                                    # only today's rates: no change
        self.assertLess(table["USD"]["buy"], table["USD"]["mid"])
        self.assertGreater(table["USD"]["sell"], table["USD"]["mid"])
        with self.bank._connect() as conn:
            old = {c: r["per_usd"] * 1.02 for c, r in self.bank.fx_rates().items()}
            self.bank.save_fx_history(conn, old, date.today() - timedelta(days=7))
        table = {r["currency"]: r for r in self.bank.rate_table("INR")}
        self.assertIsNotNone(table["USD"]["change"])

    def test_limits_and_ownership(self):
        for cur in ("USD", "EUR", "GBP", "RUB"):
            self.bank.open_currency_account(self.cid, cur, PIN)
        with self.assertRaises(BankError):
            self.bank.open_currency_account(self.cid, "AED", PIN)                       # at most 4 extra
        with self.assertRaises(BankError):
            self.bank.account_of(self.cid, self.other_acc.number)                       # never another person's
        with self.assertRaises(BankError):
            self.bank.exchange(self.cid, self.acc.number, self.other_acc.number, 100, PIN)
        with self.assertRaises(BankError):
            self.bank.open_currency_account(self.cid, "EUR", "0000")                    # wrong PIN

    def test_transfer_from_wallet_and_rates(self):
        eur = self.bank.open_currency_account(self.cid, "EUR", PIN)
        self.bank.exchange(self.cid, self.acc.number, eur.number, 880_000, PIN)
        before = self.bal(self.other_acc.number)
        self.bank.transfer(self.cid, self.other_acc.number, 1_000, PIN, from_number=eur.number)   # €10 -> ₹
        got = self.bal(self.other_acc.number) - before
        self.assertEqual(got, int((Decimal(1_000) * Decimal(str(88 / 0.86)) * Decimal("0.99")).to_integral_value()))
        with self.assertRaises(BankError):
            self.bank.transfer(self.cid, eur.number, 100, PIN)                           # own account: use exchange
        table = {r["currency"]: r for r in self.bank.rate_table("INR")}
        self.assertAlmostEqual(table["USD"]["mid"], 88.0)
        self.assertAlmostEqual(table["USD"]["buy"], 87.12)
        self.assertAlmostEqual(table["USD"]["sell"], 88.88)
        self.assertTrue(self.bank.fx_history_days(1))                                   # saved with the rates


# =========================================================================== cards

class TestCards(BankBase):
    def test_controls_and_network(self):
        card = self.bank.card(self.cid)
        n = self.acc.number
        res, t = self.bank.card_payment(card["last4"], n, 50_000, "online", "BookNest", "IN")
        self.assertEqual(res, "APPROVED")
        self.assertEqual(self.bal(n), 10_000_000 - 50_000)
        res, _ = self.bank.card_payment(card["last4"], n, 50_000, "pos", "Duty Free", "AE")
        self.assertIn("abroad", res)                                                     # abroad is off by default
        self.bank.set_card_controls(self.cid, {"online": "", "abroad": "1", "contactless": "1", "atm_limit": "1000",
                                               "pos_limit": "5000"})
        self.assertIn("online payments", self.bank.card_payment(card["last4"], n, 100, "online", "X", "IN")[0])
        self.assertEqual(self.bank.card_payment(card["last4"], n, 100_000, "pos", "Duty Free", "AE")[0], "APPROVED")
        self.assertIn("limit", self.bank.card_payment(card["last4"], n, 450_000, "pos", "Mall", "IN")[0])
        self.assertIn("limit", self.bank.card_payment(card["last4"], n, 200_000, "atm", "ATM", "IN")[0])
        with self.assertRaises(BankError):
            self.bank.set_card_controls(self.cid, {"atm_limit": "99999999", "pos_limit": "1"})
        self.bank.block_card(self.cid)
        self.assertIn("blocked", self.bank.card_payment(card["last4"], n, 100, "pos", "Shop", "IN")[0])
        self.bank.block_card(self.cid, False)
        before = self.bal(n)
        new = self.bank.report_card_lost(self.cid, PIN)
        self.assertNotEqual(new["last4"], card["last4"])
        self.assertEqual(self.bank.get_account(self.cid).card_last4, new["last4"])
        self.assertEqual(before - self.bal(n), 15_000)                                   # ₹150 replacement fee
        self.assertIn("lost", self.bank.card_payment(card["last4"], n, 100, "pos", "Shop", "IN")[0])
        self.assertGreaterEqual(len(self.bank.card_events(self.cid)), 8)                # history of all cards of the account
        self.assertTrue(any("declined" in a["subject"] for a in self.bank.alerts(self.cid)))


# =========================================================================== ISO 20022 and SEPA

class TestIso20022(BankBase):
    def test_build_and_parse(self):
        xml = iso20022.build_pain001("MSG1", [{"ref": "EX1", "rail": "SEPA", "currency": "EUR", "account_number": "5021",
                                               "amount": 12_345, "debtor_name": "A B", "bank_code": "",
                                               "beneficiary_name": "C D", "beneficiary_account": "FR7630006000011234567890189",
                                               "remarks": "rent"}])
        self.assertIn("urn:iso:std:iso:20022:tech:xsd:pain.001.001.09", xml)
        self.assertIn("<CtrlSum>123.45</CtrlSum>", xml)
        self.assertIn("<IBAN>FR7630006000011234567890189</IBAN>", xml)
        self.assertIn("<Cd>SEPA</Cd>", xml)
        ans = iso20022.build_pain002("A1", "MSG1", [("EX1", "RJCT", "AC04")])
        self.assertEqual(iso20022.parse_pain002(ans), ("MSG1", [("EX1", "RJCT", "AC04")]))
        with self.assertRaises(BankError):
            iso20022.parse_pain002('<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "b">]><Document/>')
        with self.assertRaises(BankError):
            iso20022.parse_pain002(xml)                                                  # a pain.001 is not a report

    def test_payments_wait_for_the_file_and_return(self):
        with self.bank._connect() as conn:
            self.bank.save_setting(conn, "payments_mode", "iso_files")
        ok = self.bank.send_external(self.cid, "SWIFT", {"name": "Max Weber", "account": "DE89370400440532013000",
                                                          "bic": "RHMUDEFF"}, 100_000, PIN)
        bad = self.bank.send_external(self.cid, "SWIFT", {"name": "Closed Person", "account": "GB56BUKB20201555550000",
                                                           "bic": "BUKBGB22"}, 50_000, PIN)
        self.bank.process_external(datetime.now() + timedelta(days=3))                   # nothing settles by itself
        self.assertEqual(self.bank.external_transfer(self.cid, ok)["status"], "Processing")
        fid = self.bank.create_pain001("EMP001")
        self.assertFalse(self.bank.waiting_for_file())
        with self.assertRaises(BankError):
            self.bank.create_pain001("EMP001")                                           # nothing left
        before = self.bal(self.acc.number)
        done = self.bank.apply_pain002(self.bank.test_pain002(fid), "EMP001")
        self.assertEqual((done["completed"], done["returned"]), (1, 1))
        self.assertEqual(self.bank.external_transfer(self.cid, ok)["status"], "Completed")
        p = self.bank.external_transfer(self.cid, bad)
        self.assertEqual(p["status"], "Returned")
        self.assertIn("AC04", p["reason"])
        self.assertEqual(self.bal(self.acc.number) - before, 50_000)                    # money back
        self.assertEqual(self.bank.apply_pain002(self.bank.test_pain002(fid))["already"], 2)   # read twice: no change
        self.assertEqual(self.bank.iso_file(fid)["status"], "Answered")

    def test_sepa_rules(self):
        eur = self.bank.open_currency_account(self.cid, "EUR", PIN)
        self.bank.exchange(self.cid, self.acc.number, eur.number, 880_000, PIN)
        with self.assertRaises(BankError):                                               # from a rupee account
            self.bank.send_external(self.cid, "SEPA", {"name": "Claire Martin", "account": "FR7630006000011234567890189"},
                                    1_000, PIN)
        with self.assertRaises(BankError):                                               # IBAN outside SEPA
            self.bank.send_external(self.cid, "SEPA", {"name": "Claire Martin", "account": "BR1500000000000010932840814P2"},
                                    1_000, PIN, from_number=eur.number)
        ref = self.bank.send_external(self.cid, "SEPA", {"name": "Claire Martin", "account": "FR76 3000 6000 0112 3456 7890 189"},
                                      1_000, PIN, from_number=eur.number)
        self.assertEqual(self.bank.external_transfer(self.cid, ref)["currency"], "EUR")


# =========================================================================== MRZ

class TestMrz(BankBase):
    def test_check_digits(self):
        m = mrz.parse(SPECIMEN)
        self.assertEqual((m["kind"], m["surname"], m["given"], m["errors"]), ("TD3", "ERIKSSON", "ANNA MARIA", []))
        self.assertEqual(mrz.check_digit("L898902C3"), "6")
        broken = SPECIMEN.replace("L898902C36", "L898902C46")
        self.assertIn("check digit of the document number", mrz.parse(broken)["errors"])
        with self.assertRaises(BankError):
            mrz.parse("P<UTO\nshort")
        self.assertIn("the document has expired", mrz.compare(m, "Anna Maria Eriksson", "1974-08-12"))
        self.assertNotIn("the name does not match the application", mrz.compare(m, "Anna Eriksson", "1974-08-12"))
        self.assertIn("the name does not match the application", mrz.compare(m, "Ivan Petrov", "1974-08-12"))
        lines = td3("User", "Test", "Z1234567", "IND", date(1995, 5, 5), "F", date(2033, 1, 1))
        self.assertEqual(mrz.parse(lines)["errors"], [])

    def test_saved_without_full_number(self):
        lines = td3("User", "Test", "Z1234567", "IND", date(1995, 5, 5), "F", date(2033, 1, 1))
        r = self.bank.check_mrz(self.cid, lines, "EMP001")
        self.assertTrue(r["ok"])
        saved = self.bank.mrz_checks(self.cid)[0]
        self.assertEqual(saved["doc_end"], "567")
        self.assertNotIn("Z1234567", json.dumps(saved))
        r = self.bank.check_mrz(self.cid, SPECIMEN, "EMP001")
        self.assertFalse(r["ok"])


class TestMrzOnForms(AppBase):
    def test_register_with_wrong_mrz_and_staff_check(self):
        form = {k: v for k, v in PERSON.items() if k not in ("password", "pin")}
        form.update(email="new.person@mailbox.in", mobile="9844444444", password="Secret123", password2="Secret123",
                    pin=PIN, pin2=PIN, terms="yes", mrz=SPECIMEN.replace("L898902C36", "L898902C46"))
        from io import BytesIO
        form["id_doc"] = (BytesIO(b"%PDF-1.4 passport"), "passport.pdf")
        form["csrf_token"] = "test-token"
        r = self.c.post("/open-account", data=form, content_type="multipart/form-data")
        self.assertEqual(r.status_code, 400)
        self.assertIn("check digit", r.get_data(as_text=True))
        self.staff("EMP004")                                                             # loan officer: applications
        self.post(f"/admin/mrz/{self.cid}", mrz=td3("User", "Test", "Z1234567", "IND", date(1995, 5, 5), "F",
                                                    date(2033, 1, 1)))
        self.assertTrue(self.bank.mrz_checks(self.cid)[0]["ok"])


# =========================================================================== Arabic PDF

class TestArabicPdf(BankBase):
    def test_shaping_and_pdf(self):
        shaped = statements.shape("كشف الحساب", "ar")
        self.assertTrue(any(0xFE70 <= ord(ch) <= 0xFEFF for ch in shaped))             # joined letter forms
        self.assertEqual(statements.shape("Statement", "en"), "Statement")
        pdf = self.bank.statement_pdf(self.cid, date.today().year, date.today().month, "ar")
        self.assertTrue(pdf.startswith(b"%PDF"))
        label, cert = self.bank.interest_certificate(self.cid, date.today().year - 1, "ar")
        self.assertTrue(cert.startswith(b"%PDF"))


# =========================================================================== bills and standing instructions

class TestAutopay(BankBase):
    def test_bills(self):
        b = self.bank.billers(currency="INR")[0]
        due = self.bank.fetch_bill(b["id"], "1004567891")["amount"]
        self.assertEqual(due, self.bank.fetch_bill(b["id"], "1004567891")["amount"])   # always the same answer
        with self.assertRaises(BankError):
            self.bank.fetch_bill(b["id"], "1004567000")                                   # unknown at the biller
        with self.assertRaises(BankError):
            self.bank.fetch_bill(b["id"], "12")                                           # wrong pattern
        ref, t = self.bank.pay_bill(self.cid, b["id"], "1004567891", due, PIN)
        self.assertEqual(t.category, "Bill payment")
        self.assertEqual(self.bank.bill_payments(self.cid)[0]["ref"], ref)
        rub = self.bank.billers(currency="RUB")[0]
        with self.assertRaises(BankError):
            self.bank.pay_bill(self.cid, rub["id"], "123456789", 100, PIN)                # rupee account, ruble biller

    def test_standing_instructions(self):
        today = date.today()
        iid = self.bank.create_instruction(self.cid, {"kind": "transfer", "payee": self.other_acc.number,
                                                      "amount": "500", "frequency": "monthly",
                                                      "start": today.isoformat(),
                                                      "ends": (today + timedelta(days=40)).isoformat()}, PIN)
        before = self.bal(self.other_acc.number)
        self.assertEqual(self.bank.run_instructions(today), (1, 0))
        self.assertEqual(self.bank.run_instructions(today), (0, 0))                       # never twice on a day
        self.assertEqual(self.bal(self.other_acc.number) - before, 50_000)
        s = self.bank.instructions(self.cid)[0]
        self.assertGreater(s["next_run"], today.isoformat())
        nxt = date.fromisoformat(s["next_run"])
        self.assertEqual(self.bank.run_instructions(nxt), (1, 0))
        self.assertEqual(self.bank.instructions(self.cid)[0]["status"], "Ended")          # after the last date
        big = self.bank.create_instruction(self.cid, {"kind": "transfer", "payee": self.other_acc.number,
                                                      "amount": "200000", "frequency": "weekly",
                                                      "start": today.isoformat()}, PIN)
        self.bank.set_instruction_status(self.cid, big, "pause")
        self.assertEqual(self.bank.run_instructions(today), (0, 0))
        self.bank.set_instruction_status(self.cid, big, "resume")
        self.bank.post(self.acc.number, "DR", "Charges", "TEST EMPTY", self.bal(self.acc.number) - 100, None)
        self.assertEqual(self.bank.run_instructions(today), (0, 1))                       # no money: skipped, alert
        self.assertTrue(any("not paid" in a["subject"] for a in self.bank.alerts(self.cid)))
        with self.assertRaises(BankError):
            self.bank.create_instruction(self.cid, {"kind": "transfer", "payee": self.acc.number, "amount": "5",
                                                    "frequency": "monthly", "start": today.isoformat()}, PIN)
        with self.assertRaises(BankError):
            self.bank.create_instruction(self.cid, {"kind": "transfer", "payee": self.other_acc.number, "amount": "5",
                                                    "frequency": "daily", "start": today.isoformat()}, PIN)


# =========================================================================== budgets

class TestBudgets(BankBase):
    def test_alerts_at_80_and_100(self):
        n = self.acc.number
        self.bank.set_budget(self.cid, n, "Bill payment", 10_000)
        b = self.bank.billers(currency="INR")[0]
        self.bank.pay_bill(self.cid, b["id"], "1004567891", 7_000, PIN)
        self.assertFalse([a for a in self.bank.alerts(self.cid) if "Budget" in a["subject"]])
        self.bank.pay_bill(self.cid, b["id"], "1004567891", 1_500, PIN)                   # 85 %
        self.bank.pay_bill(self.cid, b["id"], "1004567891", 100, PIN)                     # still 86 %: no new alert
        self.bank.pay_bill(self.cid, b["id"], "1004567891", 2_000, PIN)                   # 106 %
        subjects = [a["subject"] for a in self.bank.alerts(self.cid) if "Budget" in a["subject"]]
        self.assertEqual(sorted(subjects), ["Budget for Bill payment: 100% used", "Budget for Bill payment: 80% used"])
        self.assertEqual(self.bank.budgets(self.cid, n)[0]["percent"], 106)
        with self.assertRaises(BankError):
            self.bank.set_budget(self.cid, n, "Holidays on Mars", 100)
        ins = self.bank.insights(n)
        self.assertEqual(ins["rows"][0]["category"], "Bill payment")


# =========================================================================== compliance

class TestCompliance(BankBase):
    def test_rules(self):
        cash(self.bank, self.cid, 1_200_000 * 100, PIN)                                   # ₹12 lakh in cash
        rules = {f["rule"] for f in self.bank.aml_flags()}
        self.assertIn("LARGE_CASH", rules)
        for _ in range(3):
            cash(self.bank, self.other, 400_000 * 100, PIN)                               # 3 x ₹4 lakh
        self.assertIn("STRUCTURING", {f["rule"] for f in self.bank.aml_flags() if f["customer_id"] == self.other})
        third, third_acc = self.bank.register(dict(PERSON, full_name="Ravi Mehta", email="ravi.m@mailbox.in",
                                                   mobile="9855555555"))
        self.bank.post(third_acc.number, "CR", "Transfer", "NEFT/FROM SOMEONE", 600_000 * 100, None)
        with self.bank._connect() as conn:
            self.bank.save_setting(conn, "daily_transfer_limit", str(10_000_000 * 100))
        self.bank.transfer(third, self.acc.number, 680_000 * 100, PIN)                    # 97 % of ₹7 lakh out in a day
        self.assertIn("PASS_THROUGH", {f["rule"] for f in self.bank.aml_flags() if f["customer_id"] == third})
        payees = []
        for i in range(5):
            c, a = self.bank.register(dict(PERSON, full_name=f"Payee Number{chr(65 + i)}", email=f"p{i}@mailbox.in",
                                           mobile=f"98666666{i:02d}", initial_deposit="500"))
            payees.append(a.number)
        for number in payees:
            self.bank.transfer(self.cid, number, 1_000, PIN)
        self.assertIn("NEW_PAYEES", {f["rule"] for f in self.bank.aml_flags() if f["customer_id"] == self.cid})
        n = len(self.bank.aml_flags())
        cash(self.bank, self.cid, 1_100_000 * 100, PIN)                                   # same rule, same day: no new flag
        self.assertEqual(len(self.bank.aml_flags()), n)

    def test_review(self):
        cash(self.bank, self.cid, 1_200_000 * 100, PIN)
        f = self.bank.aml_flags()[0]
        staff = {"staff_id": "EMP005", "full_name": "Officer Test"}
        with self.assertRaises(BankError):
            self.bank.review_flag(staff, f["id"], "clear", "ok")                          # a real note is needed
        report = self.bank.review_flag(staff, f["id"], "report", "Cash of a new customer without a source of funds.")
        self.assertIn("SUSPICIOUS TRANSACTION REPORT", report)
        self.assertEqual(self.bank.aml_flag(f["id"])["status"], "Reported")
        with self.assertRaises(BankError):
            self.bank.review_flag(staff, f["id"], "clear", "second time is not possible")

    def test_dormant(self):
        old, acc = self.bank.register(dict(PERSON, full_name="Old Customer", email="old.c@mailbox.in",
                                           mobile="9877777777"),
                                      when=(datetime.now() - timedelta(days=900)).strftime("%Y-%m-%d %H:%M:%S"))
        self.bank.post(acc.number, "CR", "Interest", "SAVINGS INTEREST", 100, None)        # interest does not count
        self.assertEqual(self.bank.mark_dormant(), 1)
        self.assertEqual(self.bank.get_account(old).status, "Dormant")
        row = [a for a in self.bank.dormant_accounts() if a["number"] == acc.number][0]
        self.assertLess(row["last"] or "", (datetime.now() - timedelta(days=700)).strftime("%Y-%m-%d"))  # not the interest
        with self.assertRaises(BankError) as e:
            self.bank.transfer(old, self.acc.number, 100, PIN)
        self.assertIn("dormant", str(e.exception))
        self.assertEqual(self.bank.mark_dormant(), 0)                                      # the new customers are fine
        with self.assertRaises(BankError):
            self.bank.activate_dormant({"staff_id": "EMP002"}, acc.number, "ok")
        self.bank.activate_dormant({"staff_id": "EMP002"}, acc.number, "Passport seen at the branch")
        self.bank.transfer(old, self.acc.number, 100, PIN)


class TestFourEyes(AppBase):
    def test_large_entry_needs_a_second_person(self):
        neha = self.office.get_staff("EMP005")
        res = self.office.manual_entry(neha, self.cid, "CR", 75_000 * 100, "Cash shortage of last week returned")
        self.assertEqual(set(res), {"pending"})
        self.assertEqual(self.bank.get_account(self.cid).balance, 10_000_000)            # nothing posted yet
        with self.assertRaises(BankError):
            self.office.decide_entry(neha, res["pending"], True)                         # not your own entry
        t = self.office.decide_entry(self.admin, res["pending"], True)
        self.assertEqual(self.bank.get_account(self.cid).balance, 10_000_000 + 7_500_000)
        with self.assertRaises(BankError):
            self.office.decide_entry(self.admin, res["pending"], True)                   # never twice
        small = self.office.manual_entry(neha, self.cid, "DR", 100, "Small correction of a fee")
        self.assertTrue(hasattr(small, "ref"))                                            # posted at once
        rev = self.office.reverse_transaction(neha, self._tx_id(t.ref), "Posted to the wrong customer")
        self.assertIn("pending", rev)
        self.assertIsNone(self.office.decide_entry(self.admin, rev["pending"], False, "Not wrong after all"))
        self.staff("EMP003")                                                             # a teller has no "checker"
        self.assertEqual(self.c.get("/admin/checker").status_code, 403)

    def _tx_id(self, ref):
        with self.bank._connect() as conn:
            return conn.execute("SELECT id FROM transactions WHERE ref = ? AND type = 'CR'", (ref,)).fetchone()[0]


# =========================================================================== devices, limits, API

class TestDevicesAndLimits(AppBase):
    def test_devices(self):
        self.customer()
        self.assertEqual(self.c.get("/netbanking").status_code, 200)
        phone = self.app.test_client()
        self.customer(phone, ua="Mozilla/5.0 (Linux; Android 14) Chrome/131.0 Mobile")
        self.assertTrue(any(a["subject"].startswith("New sign-in") for a in self.bank.alerts(self.cid)))
        devices = self.bank.sessions(self.cid)
        self.assertEqual(sorted(d["device"] for d in devices), ["Chrome on Android", "Chrome on Windows"])
        self.post("/netbanking/security/devices", action="end_others")                   # from the computer
        r = phone.get("/netbanking")
        self.assertEqual(r.status_code, 302)                                              # the phone is signed out
        self.assertEqual(self.c.get("/netbanking").status_code, 200)

    def test_login_rate_limit(self):
        codes = [self.post("/login", login=self.cid, password="wrong").status_code for _ in range(11)]
        self.assertEqual(codes[-1], 429)
        self.assertTrue(self.bank.rate_report())


class TestApi(AppBase):
    def token(self, **extra):
        r = self.c.post("/api/v1/token", json={"customer_id": self.cid, "password": "Secret123", **extra})
        return r.status_code, r.get_json()

    def test_token_and_calls(self):
        self.assertEqual(self.c.post("/api/v1/token", json={"customer_id": self.cid, "password": "x"}).status_code, 401)
        code, data = self.token(device="Test phone")
        self.assertEqual(code, 201)
        h = {"Authorization": "Bearer " + data["token"]}
        self.assertEqual(self.c.get("/api/v1/me").status_code, 401)
        self.assertEqual(self.c.get("/api/v1/me", headers=h).get_json()["customer_id"], self.cid)
        accounts = self.c.get("/api/v1/accounts", headers=h).get_json()["accounts"]
        self.assertEqual(accounts[0]["balance"], 10_000_000)
        self.assertEqual(self.c.get(f"/api/v1/accounts/{self.other_acc.number}/transactions", headers=h).status_code, 404)
        r = self.c.post("/api/v1/transfers", headers=h, json={"to": self.other_acc.number, "amount": "250", "pin": PIN})
        self.assertEqual(r.status_code, 201)
        self.assertEqual(self.bank.get_account(self.other).balance, 10_000_000 + 25_000)
        self.assertEqual(self.c.post("/api/v1/transfers", headers=h, json={"to": self.other_acc.number, "amount": "1",
                                                                           "pin": "0000"}).status_code, 400)
        self.assertEqual(self.c.get("/api/v1/card", headers=h).status_code, 200)
        with self.bank._connect() as conn:
            stored = conn.execute("SELECT token_hash FROM api_tokens").fetchone()[0]
        self.assertNotEqual(stored, data["token"])                                       # only a hash is kept
        self.assertEqual(self.c.delete("/api/v1/token", headers=h).status_code, 200)
        self.assertEqual(self.c.get("/api/v1/me", headers=h).status_code, 401)
        spec = self.c.get("/api/v1/openapi.json").get_json()
        self.assertEqual(spec["openapi"], "3.0.3")
        self.assertIn("/transfers", spec["paths"])

    def test_two_factor_and_revoke_in_security_page(self):
        import otp
        secret = otp.new_secret()
        self.bank.enable_2fa(self.cid, secret, otp.totp(secret))
        code, data = self.token()
        self.assertEqual(code, 401)
        self.assertIn("6-digit code", data["error"])
        code, data = self.token(code=otp.totp(secret, at=datetime.now().timestamp() + 30))
        self.assertEqual(code, 201)
        tid = self.bank.api_tokens(self.cid)[0]["id"]
        self.assertEqual(self.bank.revoke_token(self.other, tid), 0)                      # not another person's app
        self.assertEqual(self.bank.revoke_token(self.cid, tid), 1)
        self.assertIsNone(self.bank.token_owner(data["token"]))


# =========================================================================== privacy, certificates, interest

class TestPrivacyAndInterest(BankBase):
    def test_my_data(self):
        data = json.loads(self.bank.export_my_data(self.cid))
        text = json.dumps(data)
        self.assertEqual(data["customer"]["customer_id"], self.cid)
        self.assertNotIn("password_hash", text)
        self.assertNotIn("pin_hash", text)
        self.assertEqual(data["accounts"][0]["number"], self.acc.number)
        self.assertNotIn(self.other_acc.number, json.dumps(data["accounts"]))

    def test_certificate_year(self):
        from privacy import financial_year
        self.assertEqual(financial_year("IN", 2025)[2], "FY 2025-26")
        self.assertEqual(financial_year("RU", 2025)[:2], (date(2025, 1, 1), date(2025, 12, 31)))
        with self.assertRaises(BankError):
            self.bank.interest_summary(self.cid, date.today().year + 2)

    def test_compound_fd_and_senior_rate(self):
        interest = self.bank._fd_interest(10_000_000, 7.0, 365, "quarterly")
        expected = int(Decimal(10_000_000) * (Decimal("1.0175") ** 4) - 10_000_000)
        self.assertAlmostEqual(interest, expected, delta=1)
        self.assertGreater(interest, self.bank._fd_interest(10_000_000, 7.0, 365, "simple"))   # compound > simple
        senior, _ = self.bank.register(SENIOR)
        self.assertEqual(self.bank.fd_rate_for(senior, 12), round(self.bank.fd_rates()[12] + 0.5, 2))
        self.assertEqual(self.bank.fd_rate_for(self.cid, 12), self.bank.fd_rates()[12])
        fd = self.bank.open_fd(senior, 100_000 * 100, 12, PIN)
        row = [f for f in self.bank.fixed_deposits(senior) if f["fd_number"] == fd][0]
        self.assertEqual((row["compounding"], row["senior"]), ("quarterly", 1))

    def test_savings_slabs(self):
        slab = 100_000 * 100
        one_day = self.bank.savings_interest([slab * 2], "INR", 3.5, 4.0)
        self.assertEqual(one_day, round((slab * 3.5 + slab * 4.0) / 100 / 365))
        self.assertEqual(self.bank.savings_interest([slab // 2], "INR", 3.5, 4.0), round(slab / 2 * 3.5 / 100 / 365))


# =========================================================================== health, backup, languages

class TestHealthBackupLanguages(AppBase):
    def test_health_and_status(self):
        r = self.c.get("/health")
        data = r.get_json()
        self.assertIn(data["status"], ("ok", "degraded"))
        self.assertTrue(data["checks"]["database"]["ok"])
        self.assertIn("System status", self.c.get("/status").get_data(as_text=True))
        self.bank.log_error("/test", RuntimeError("boom"))
        self.assertEqual(self.bank.errors()[0]["path"], "/test")
        self.staff("EMP001", "Admin@1234")
        self.assertIn("boom", self.c.get("/admin/health").get_data(as_text=True))

    def test_backup_has_v4_tables(self):
        self.bank.open_currency_account(self.cid, "USD", PIN)
        self.bank.save_text(self.admin, "care_phone", "1800-999-0000")
        data = self.bank.export_data()
        self.assertEqual(data["version"], 4)
        for t in ("site_texts", "cards", "aml_flags", "standing_instructions", "api_tokens"):
            self.assertIn(t, data["tables"])
        self.bank.save_text(self.admin, "care_phone", "1800-555-5555")
        self.bank.import_data(data)
        self.assertEqual(self.bank.site_texts()["care_phone"], "1800-999-0000")
        self.assertEqual(len(self.bank.accounts_of(self.cid)), 2)

    def test_back_office_language(self):
        self.staff("EMP002")
        self.post("/admin/lang/ru", next="/admin/content")
        self.assertEqual(self.office.get_staff("EMP002")["lang"], "ru")
        page = self.c.get("/admin/content").get_data(as_text=True)
        self.assertIn('lang="ru"', page)
        self.assertIn("Содержимое сайта", page)                                          # "Website content"

    def test_customer_pages_and_switch(self):
        usd = self.bank.open_currency_account(self.cid, "USD", PIN)
        self.customer()
        for url in ("/netbanking/accounts", "/netbanking/card", "/netbanking/bills", "/netbanking/insights",
                    "/netbanking/security", "/netbanking/e-statements", "/netbanking/my-data.json",
                    f"/netbanking/interest-certificate.pdf?year={date.today().year - 1}"):
            self.assertEqual(self.c.get(url).status_code, 200, url)
        self.post("/netbanking/switch", account=usd.number, next="/netbanking/statement")
        self.assertIn(usd.number, self.c.get("/netbanking/statement").get_data(as_text=True))
        self.post("/netbanking/switch", account=self.other_acc.number)                   # someone else's account
        self.assertNotIn(self.other_acc.number, self.c.get("/netbanking").get_data(as_text=True))
        r = self.post("/netbanking/accounts", action="quote", **{"from": self.acc.number, "to": usd.number, "amount": "880"})
        self.assertIn("1.00%", r.get_data(as_text=True))                                 # the margin is shown first


if __name__ == "__main__":
    unittest.main()

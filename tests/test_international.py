"""Tests for version 3: international bank (countries, currencies, exchange rates, languages),
staff approval of new accounts and personal-data changes, payments to other banks (sandbox),
credit score and loan documents, EMI auto-debit with late fee, PDF e-statements and Web Push."""

import json
import os
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from support import DRIVER, cash, target  # noqa: E402

import i18n  # noqa: E402
import payments  # noqa: E402
import push  # noqa: E402
import world  # noqa: E402
from bank import Bank, BankError  # noqa: E402

PIN = "4826"
PDF = b"%PDF-1.4 test document"
PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 20
INDIAN = dict(full_name="Test User", dob="1995-05-05", gender="Female", email="test.user@mailbox.in",
              mobile="9811111111", address="12 Main Road", city="Ambala", state="Haryana", pincode="133001",
              occupation="Salaried", account_type="Savings", initial_deposit="10000", password="Secret123", pin=PIN,
              country="IN")
RUSSIAN = dict(INDIAN, full_name="Ivan Petrov", email="ivan.petrov@mailbox.ru", mobile="+7 913 123-45-67",
               city="Krasnoyarsk", state="", pincode="660041", country="RU", lang="ru", initial_deposit="5000")
RATES = {"USD": 1.0, "RUB": 80.0, "INR": 88.0, "EUR": 0.86}       # made-up test rates (units per 1 USD)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.bank = Bank(target(os.path.join(self.tmp.name, "t.db")), driver=DRIVER, fx_fetch=lambda: RATES)
        self.cid, self.acc = self.bank.register(INDIAN)

    def tearDown(self):
        self.tmp.cleanup()

    def balance(self, cid=None):
        return self.bank.get_account(cid or self.cid).balance


class TestWorld(unittest.TestCase):
    def test_money_formats(self):
        self.assertEqual(world.format_money(10000000, "INR", "en"), "₹1,00,000.00")
        self.assertEqual(world.format_money(123456, "RUB", "ru").replace("\xa0", " "), "1 234,56 ₽")
        self.assertIn("1,234.56", world.format_money(123456, "USD", "en"))

    def test_phone_postcode_currency(self):
        self.assertEqual(world.clean_mobile("RU", "+7 913 123-45-67"), "+79131234567")
        self.assertEqual(world.clean_mobile("IN", "+91 98111 11111"), "9811111111")
        self.assertIsNone(world.clean_mobile("RU", "+1 202 555 0100"))           # not a Russian number
        self.assertEqual(world.clean_postcode("RU", "660041"), "660041")
        self.assertIsNone(world.clean_postcode("IN", "12345"))
        self.assertEqual(world.default_currency("FR", "fr"), "EUR")
        self.assertEqual(world.default_currency("US", "ru"), "RUB")             # Russian interface -> rubles
        self.assertEqual(world.country_name("RW", "fr"), "Rwanda")


class TestLanguages(unittest.TestCase):
    def test_catalogs_complete(self):
        keys = set(json.load(open(os.path.join(i18n.CATALOG_DIR, "ru.json"), encoding="utf-8")))
        for lang in ("fr", "es", "ar", "zh"):
            data = json.load(open(os.path.join(i18n.CATALOG_DIR, f"{lang}.json"), encoding="utf-8"))
            self.assertEqual(set(data), keys, lang)
            self.assertFalse([k for k, v in data.items() if not v], lang)

    def test_translate_exact_and_pattern(self):
        self.assertNotEqual(i18n.translate_text("Fund Transfer", "ru"), "Fund Transfer")
        self.assertEqual(i18n.translate_text("Fund Transfer", "en"), "Fund Transfer")
        page = i18n.translate_html('<p title="Fund Transfer">Fund Transfer</p><script>"Fund Transfer"</script>', "zh")
        self.assertIn('"Fund Transfer"</script>', page)                         # code is never touched
        self.assertNotIn(">Fund Transfer<", page)


class TestOnlineAccountApproval(Base):
    def test_application_waits_for_staff(self):
        with self.assertRaises(BankError):                                       # an ID document is required
            self.bank.register(dict(RUSSIAN), online=True)
        cid, acc = self.bank.register(dict(RUSSIAN), online=True, documents=[("ID proof", "passport.pdf", PDF)])
        self.assertEqual(acc.currency, "RUB")                                     # Russian interface -> rubles
        self.assertEqual(self.bank.get_customer(cid).status, "Pending")
        self.assertEqual(self.balance(cid), 0)
        with self.assertRaises(BankError):                                        # cannot log in yet
            self.bank.authenticate(cid, "Secret123")
        with self.assertRaises(BankError):                                        # rejecting needs a reason
            self.bank.decide_customer(cid, False, "Officer", "")
        self.bank.decide_customer(cid, True, "Officer")
        self.assertEqual(self.balance(cid), 500000)                               # 5 000 ₽ opening deposit
        self.assertTrue(self.bank.authenticate(cid, "Secret123"))
        with self.assertRaises(BankError):                                        # only once
            self.bank.decide_customer(cid, True, "Officer")

    def test_rejected_application(self):
        cid, _ = self.bank.register(dict(RUSSIAN), online=True, documents=[("ID proof", "id.png", PNG)])
        self.bank.decide_customer(cid, False, "Officer", "Document is not readable")
        with self.assertRaises(BankError):
            self.bank.authenticate(cid, "Secret123")

    def test_document_checks(self):
        with self.assertRaises(BankError):
            self.bank.check_document("ID proof", "virus.exe", b"MZ\x90\x00")    # content, not the name, is checked
        self.assertEqual(self.bank.check_document("ID proof", "scan", PDF)[1:3], ("scan.pdf", "application/pdf"))


class TestProfileChangeApproval(Base):
    def form(self, **kw):
        c = self.bank.get_customer(self.cid)
        return {**{k: getattr(c, k) for k in ("full_name", "email", "mobile", "address", "city", "state", "pincode",
                                              "country", "occupation")}, **kw}

    def test_change_waits_and_needs_proof(self):
        with self.assertRaises(BankError):                                        # new address needs a proof
            self.bank.request_profile_change(self.cid, self.form(address="44 New Colony"))
        pid = self.bank.request_profile_change(self.cid, self.form(address="44 New Colony", email="new.me@mailbox.in"),
                                               [("Address proof", "bill.pdf", PDF)])
        self.assertEqual(self.bank.get_customer(self.cid).address, "12 Main Road")     # nothing changed yet
        with self.assertRaises(BankError):                                        # one request at a time
            self.bank.request_profile_change(self.cid, self.form(occupation="Business"))
        self.bank.decide_profile_change(pid, True, "Officer")
        c = self.bank.get_customer(self.cid)
        self.assertEqual((c.address, c.email), ("44 New Colony", "new.me@mailbox.in"))

    def test_reject_and_cancel(self):
        pid = self.bank.request_profile_change(self.cid, self.form(occupation="Business"))
        self.bank.decide_profile_change(pid, False, "Officer", "Please visit the branch")
        self.assertEqual(self.bank.get_customer(self.cid).occupation, "Salaried")
        pid = self.bank.request_profile_change(self.cid, self.form(occupation="Business"))
        self.bank.cancel_profile_change(self.cid, pid)
        with self.assertRaises(BankError):
            self.bank.decide_profile_change(pid, True, "Officer")
        with self.assertRaises(BankError):
            self.bank.request_profile_change(self.cid, self.form())                # nothing changed


class TestCurrencies(Base):
    def setUp(self):
        super().setUp()
        self.rub, self.rub_acc = self.bank.register(dict(RUSSIAN))              # opened at the branch: active

    def test_limits_per_currency(self):
        self.assertEqual(self.bank.limit_for("withdraw", "RUB"), 200_000 * 100)
        self.assertEqual(self.bank.limit_for("withdraw", "INR"), 50_000 * 100)

    def test_cross_currency_transfer_needs_rate(self):
        with self.assertRaises(BankError):                                        # no rate downloaded yet
            self.bank.transfer(self.cid, self.rub_acc.number, 88_000, PIN)
        self.bank.refresh_fx_rates()
        before = self.balance(self.rub)
        self.bank.transfer(self.cid, self.rub_acc.number, 88_000, PIN)           # ₹880 -> 800 ₽
        self.assertEqual(self.balance(self.rub) - before, 80_000)
        self.assertEqual(self.balance(), 1_000_000 - 88_000)
        q = self.bank.quote_internal("INR", self.rub_acc.number, 8_800)
        self.assertEqual((q["currency"], q["amount"]), ("RUB", 8_000))


class TestOtherBanks(Base):
    def test_iban_and_codes(self):
        self.assertTrue(payments.iban_ok("GB82WEST12345698765432"))
        self.assertFalse(payments.iban_ok("GB82WEST12345698765433"))
        self.assertEqual(payments.next_neft_batch(datetime(2026, 5, 1, 10, 7)), datetime(2026, 5, 1, 10, 30))

    def test_imps_instant_and_return(self):
        ref = self.bank.send_external(self.cid, "IMPS", {"name": "Asha Rao", "account": "123456789012",
                                                         "ifsc": "NOVA0001234"}, 100_000, PIN)
        self.assertEqual(self.bank.external_transfer(self.cid, ref)["status"], "Completed")
        before = self.balance()
        ref = self.bank.send_external(self.cid, "IMPS", {"name": "Closed Account", "account": "123456780000",
                                                         "ifsc": "NOVA0001234"}, 50_000, PIN)
        p = self.bank.external_transfer(self.cid, ref)
        self.assertEqual(p["status"], "Returned")                                 # sandbox: account ending 0000
        self.assertEqual(self.balance(), before)                                  # money came back

    def test_neft_batch_and_rules(self):
        ref = self.bank.send_external(self.cid, "NEFT", {"name": "Asha Rao", "account": "123456789012",
                                                         "ifsc": "SUNR0004321"}, 20_000, PIN)
        self.assertEqual(self.bank.external_transfer(self.cid, ref)["status"], "Processing")
        self.bank.process_external(datetime.now() + timedelta(minutes=31))
        self.assertEqual(self.bank.external_transfer(self.cid, ref)["status"], "Completed")
        for rail, data in [("UPI", {"upi": "not an id"}), ("IMPS", {"name": "A B", "account": "1", "ifsc": "NOVA0001234"}),
                           ("IMPS", {"name": "A B", "account": "123456789", "ifsc": "BAD"}),
                           ("SWIFT", {"name": "A B", "account": "GB82WEST12345698765433", "bic": "WESTGB2L"})]:
            with self.assertRaises(BankError, msg=rail):
                self.bank.send_external(self.cid, rail, data, 10_000, PIN)
        rub, _ = self.bank.register(dict(RUSSIAN))
        with self.assertRaises(BankError):                                        # IMPS/UPI/NEFT are rupee-only
            self.bank.send_external(rub, "UPI", {"upi": "asha@okbank"}, 10_000, PIN)


class TestCreditAndLoans(Base):
    def test_credit_score_range_and_documents(self):
        r = self.bank.credit_report(self.cid, 100_000)
        self.assertTrue(300 <= r["score"] <= 900)
        self.assertIn(r["band"], ("Excellent", "Good", "Fair", "Poor"))
        with self.assertRaises(BankError):                                        # income proof is missing
            self.bank.apply_loan(self.cid, "Personal", 2_000_000, 12, "New laptop for work", PIN,
                                 [("ID proof", "id.pdf", PDF)])
        number = self.bank.apply_loan(self.cid, "Personal", 2_000_000, 12, "New laptop for work", PIN,
                                      [("ID proof", "id.pdf", PDF), ("Income proof", "salary.pdf", PDF)])
        ln = self.bank.loans(self.cid)[0]
        self.assertEqual(ln["loan_number"], number)
        self.assertTrue(300 <= ln["credit_score"] <= 900)
        self.assertEqual(len(self.bank.documents(self.cid, f"loan:{ln['id']}")), 2)

    def test_auto_debit_and_late_fee(self):
        from admin import BackOffice
        self.bank.apply_loan(self.cid, "Personal", 2_000_000, 12, "New laptop for work", PIN)
        ln = self.bank.loans(self.cid)[0]
        BackOffice(self.bank).decide_loan({"full_name": "Officer", "staff_id": "EMP9", "role": "staff"}, ln["id"], True)
        due = date.fromisoformat(self.bank.get_loan(self.cid, ln["id"])["next_due"])
        self.assertEqual(self.bank.collect_due_emis(due)["auto_debited"], 1)     # enough money: paid on the due date
        self.assertEqual(self.bank.get_loan(self.cid, ln["id"])["emis_paid"], 1)
        # empty the account, then the next EMI cannot be taken
        self.bank.post(self.acc.number, "DR", "Charges", "TEST EMPTY ACCOUNT", self.balance(), None)
        due2 = date.fromisoformat(self.bank.get_loan(self.cid, ln["id"])["next_due"])
        r = self.bank.collect_due_emis(due2)
        self.assertEqual((r["auto_debited"], r["late"]), (0, 1))
        self.assertEqual(self.bank.collect_due_emis(due2)["late"], 0)            # the fee is charged once per EMI
        loan = self.bank.get_loan(self.cid, ln["id"])
        self.assertEqual(loan["penalty_due"], int(loan["emi"] * 2 / 100 + 0.5))


class TestStatementsAndPush(Base):
    def test_pdf_statement(self):
        cash(self.bank, self.cid, 25_000, PIN)
        today = date.today()
        for lang in ("en", "ru", "zh"):
            blob = self.bank.statement_pdf(self.cid, today.year, today.month, lang)
            self.assertTrue(blob.startswith(b"%PDF"), lang)
        with self.assertRaises(BankError):
            self.bank.statement_pdf(self.cid, today.year + 1, 1)

    def test_monthly_estatement_once(self):
        first = date.today().replace(day=1)
        self.bank.decide_customer  # noqa: B018 (customers opened at the branch are active)
        with self.bank._connect() as conn:                                        # account opened last month
            conn.execute("UPDATE accounts SET opened_at = ?", ((first - timedelta(days=20)).isoformat() + " 10:00:00",))
        self.assertEqual(self.bank.monthly_estatements(first), 1)
        self.assertEqual(self.bank.monthly_estatements(first), 0)
        self.assertEqual(len(self.bank.estatements(self.cid)), 1)

    def test_web_push_encryption_round_trip(self):
        """Encrypt as the server (RFC 8291) and decrypt as the browser would."""
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        ua = ec.generate_private_key(ec.SECP256R1())
        ua_pub = ua.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
        auth = os.urandom(16)
        body = push.encrypt(b'{"title":"PyBank"}', push.b64u(ua_pub), push.b64u(auth))
        salt, keylen = body[:16], body[20]
        as_pub = body[21:21 + keylen]
        shared = ua.exchange(ec.ECDH(), ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), as_pub))
        ikm = push._hmac(push._hmac(auth, shared), b"WebPush: info\x00" + ua_pub + as_pub + b"\x01")
        prk = push._hmac(salt, ikm)
        cek = push._hmac(prk, b"Content-Encoding: aes128gcm\x00\x01")[:16]
        nonce = push._hmac(prk, b"Content-Encoding: nonce\x00\x01")[:12]
        plain = AESGCM(cek).decrypt(nonce, body[21 + keylen:], None)
        self.assertEqual(plain, b'{"title":"PyBank"}\x02')

    def test_vapid_and_subscriptions(self):
        keys = self.bank.vapid_keys()
        self.assertEqual(keys, self.bank.vapid_keys())                           # created once, then kept
        header = push.vapid_header("https://push.example.net/abc", keys["private"], keys["public"])
        self.assertTrue(header.startswith("vapid t=") and header.endswith("k=" + keys["public"]))
        with self.assertRaises(BankError):
            self.bank.add_push_subscription(self.cid, {"endpoint": "http://bad"})
        ua = push.new_vapid_keys()["public"]
        self.bank.add_push_subscription(self.cid, {"endpoint": "https://push.example.net/abc",
                                                   "keys": {"p256dh": ua, "auth": push.b64u(os.urandom(16))}}, "Phone")
        self.assertEqual(len(self.bank.push_subscriptions(self.cid)), 1)


if __name__ == "__main__":
    unittest.main()

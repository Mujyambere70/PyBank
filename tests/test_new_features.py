"""Tests for version 2: staff approval of deposits/withdrawals, two-factor login (TOTP),
loans with EMI, transaction alerts, quarterly savings interest, backups and restore."""

import os
import sys
import tempfile
import time
import unittest
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from support import DRIVER, cash, target  # noqa: E402

import otp  # noqa: E402
from bank import Bank, BankError  # noqa: E402
from notify import Notifier  # noqa: E402

PERSON = dict(full_name="Test User", dob="1995-05-05", gender="Female", email="test.user@mailbox.in",
              mobile="9811111111", address="12 Main Road", city="Ambala", state="Haryana",
              pincode="133001", occupation="Salaried", account_type="Savings",
              initial_deposit="10000", password="Secret123", pin="4826")
PIN = "4826"


class FakeResponse:
    status = 201

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeHttp:
    """Records the HTTPS requests instead of sending them."""

    def __init__(self):
        self.sent = []

    def __call__(self, req, timeout=None):
        self.sent.append(req)
        return FakeResponse()


class Base(unittest.TestCase):
    notifier = None

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.bank = Bank(target(os.path.join(self.tmp.name, "t.db")), notifier=self.notifier, driver=DRIVER)
        self.cid, self.acc = self.bank.register(PERSON)

    def tearDown(self):
        self.tmp.cleanup()

    def balance(self):
        return self.bank.get_account(self.cid).balance


class TestOtp(unittest.TestCase):
    def test_rfc6238_vector(self):
        # RFC 6238 test secret "12345678901234567890" (Base32 below), time 59 -> 94287082 (8 digits) -> 287082
        secret = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"
        self.assertEqual(otp.totp(secret, 59), "287082")

    def test_verify_window_and_replay(self):
        s = otp.new_secret()
        now = 1_800_000_000
        step = otp.verify(s, otp.totp(s, now), at=now)
        self.assertEqual(step, now // 30)
        self.assertIsNotNone(otp.verify(s, otp.totp(s, now - 30), at=now))      # 30 s clock drift is fine
        self.assertIsNone(otp.verify(s, otp.totp(s, now - 90), at=now))         # too old
        self.assertIsNone(otp.verify(s, otp.totp(s, now), last_step=step, at=now))   # used once already
        self.assertIsNone(otp.verify(s, "12345", at=now))
        self.assertIn("otpauth://totp/PyBank%3APB1", otp.uri(s, "PB1"))


class TestCashApproval(Base):
    def test_request_does_not_move_money_until_approved(self):
        before = self.balance()
        ref = self.bank.request_cash(self.cid, "Deposit", 50000, PIN, "Shop")
        self.assertTrue(ref.startswith("CR"))
        self.assertEqual(self.balance(), before)
        rid = self.bank.cash_requests(self.cid)[0]["id"]
        r, t = self.bank.decide_cash_request(rid, True, "Officer")
        self.assertEqual(self.balance(), before + 50000)
        self.assertEqual(self.bank.cash_requests(self.cid)[0]["tx_ref"], t.ref)
        with self.assertRaises(BankError):                       # cannot approve twice
            self.bank.decide_cash_request(rid, True, "Officer")

    def test_reject_needs_reason_and_moves_nothing(self):
        before = self.balance()
        self.bank.request_cash(self.cid, "Withdrawal", 20000, PIN)
        rid = self.bank.cash_requests(self.cid)[0]["id"]
        with self.assertRaises(BankError):
            self.bank.decide_cash_request(rid, False, "Officer", "")
        self.bank.decide_cash_request(rid, False, "Officer", "Signature does not match")
        self.assertEqual(self.balance(), before)
        self.assertEqual(self.bank.cash_requests(self.cid)[0]["status"], "Rejected")

    def test_pending_withdrawal_reserves_money(self):
        bal = self.balance()                                     # ₹10,000
        self.bank.request_cash(self.cid, "Withdrawal", bal - 10000, PIN)
        self.assertEqual(self.bank.available_balance(self.acc.number), 10000)
        with self.assertRaises(BankError):                       # cannot ask for the same money twice
            self.bank.request_cash(self.cid, "Withdrawal", 20000, PIN)
        other, other_acc = self.bank.register({**PERSON, "full_name": "Other Person", "email": "o@example.com",
                                               "mobile": "9822222222"})
        with self.assertRaises(BankError):                       # ... or send it away
            self.bank.transfer(self.cid, other_acc.number, 20000, PIN)

    def test_cancel_and_wrong_pin(self):
        with self.assertRaises(BankError):
            self.bank.request_cash(self.cid, "Deposit", 100, "0000")
        self.bank.request_cash(self.cid, "Deposit", 100, PIN)
        rid = self.bank.cash_requests(self.cid)[0]["id"]
        self.bank.cancel_cash_request(self.cid, rid)
        with self.assertRaises(BankError):
            self.bank.decide_cash_request(rid, True, "Officer")


class TestTwoFactor(Base):
    def test_enable_verify_disable(self):
        s = otp.new_secret()
        with self.assertRaises(BankError):
            self.bank.enable_2fa(self.cid, s, "000000")
        self.bank.enable_2fa(self.cid, s, otp.totp(s, time.time() - 30))
        self.assertTrue(self.bank.get_customer(self.cid).totp_enabled)
        with self.assertRaises(BankError):                       # same code cannot be used again
            self.bank.verify_2fa(self.cid, otp.totp(s, time.time() - 30))
        self.bank.verify_2fa(self.cid, otp.totp(s))
        with self.assertRaises(BankError):
            self.bank.disable_2fa(self.cid, "wrong", otp.totp(s, time.time() + 30))
        self.bank.disable_2fa(self.cid, "Secret123", otp.totp(s, time.time() + 30))
        self.assertFalse(self.bank.get_customer(self.cid).totp_enabled)


class TestLoans(Base):
    def test_emi_formula(self):
        self.assertEqual(Bank.emi(10_000_000, 12, 12), 888_488)          # ₹1,00,000 @12% for 12 m = ₹8,884.88
        self.assertEqual(Bank.emi(1_200_000, 0, 12), 100_000)

    def test_schedule_pays_off_exactly(self):
        p, rate, n = 15_000_000, 11.5, 24
        rows = Bank.schedule(p, rate, n, Bank.emi(p, rate, n), date(2026, 1, 15))
        self.assertEqual(len(rows), n)
        self.assertEqual(sum(r["principal"] for r in rows), p)
        self.assertEqual(rows[-1]["balance"], 0)
        self.assertEqual(rows[1]["due"], "2026-02-15")

    def test_apply_approve_pay_close(self):
        number = self.bank.apply_loan(self.cid, "Personal", 20_000_000, 12, "Wedding expenses", PIN)
        ln = self.bank.loans(self.cid)[0]
        self.assertEqual((ln["loan_number"], ln["status"]), (number, "Pending"))
        self.bank.apply_loan(self.cid, "Education", 2_000_000, 12, "Course fees", PIN)
        with self.assertRaises(BankError):                       # at most 2 loans at a time
            self.bank.apply_loan(self.cid, "Vehicle", 2_000_000, 12, "Scooter", PIN)
        from admin import BackOffice
        office = BackOffice(self.bank)
        before = self.balance()
        office.decide_loan({"full_name": "Officer", "staff_id": "EMP9", "role": "staff"}, ln["id"], True)
        self.assertEqual(self.balance(), before + 20_000_000)
        ln = self.bank.get_loan(self.cid, ln["id"])
        self.assertEqual(ln["status"], "Active")
        t = self.bank.pay_emi(self.cid, ln["id"], PIN)
        self.assertEqual(t.amount, ln["plan"][0]["emi"])
        ln = self.bank.get_loan(self.cid, ln["id"])
        self.assertEqual(ln["emis_paid"], 1)
        self.assertLess(ln["outstanding"], 20_000_000)
        self.bank.close_loan(self.cid, ln["id"], PIN)
        ln = self.bank.get_loan(self.cid, ln["id"])
        self.assertEqual((ln["status"], ln["outstanding"]), ("Closed", 0))

    def test_limits(self):
        for product, amount, months in [("Gold", 2_000_000, 12), ("Personal", 100_000, 12), ("Personal", 2_000_000, 7)]:
            with self.assertRaises(BankError):
                self.bank.apply_loan(self.cid, product, amount, months, "Test purpose", PIN)


class TestAlerts(Base):
    def setUp(self):
        self.http = FakeHttp()
        self.notifier = Notifier(env={"BREVO_API_KEY": "k", "ALERT_EMAIL_FROM": "alerts@pybank.test",
                                      "TWILIO_ACCOUNT_SID": "AC1", "TWILIO_AUTH_TOKEN": "t", "TWILIO_FROM": "+15550001"},
                                 http=self.http, background=False)
        super().setUp()

    def test_every_transaction_alerts_by_email_and_sms(self):
        self.http.sent.clear()
        cash(self.bank, self.cid, 30000, PIN)
        urls = sorted(r.full_url for r in self.http.sent)
        self.assertTrue(any("api.brevo.com" in u for u in urls))
        self.assertTrue(any("api.twilio.com" in u for u in urls))
        rows = self.bank.alerts(self.cid)
        self.assertTrue({"in-app", "email", "sms"} <= {a["channel"] for a in rows})
        self.assertTrue(all(a["status"] in ("Sent", "Delivered") for a in rows))

    def test_preferences_and_failures(self):
        self.bank.set_alert_prefs(self.cid, None, "1")             # SMS only
        self.http.sent.clear()
        cash(self.bank, self.cid, 30000, PIN)
        self.assertFalse(any("brevo" in r.full_url for r in self.http.sent))

        def broken(req, timeout=None):
            raise OSError("network down")
        self.notifier.http = broken
        t = cash(self.bank, self.cid, 30000, PIN)                   # the payment still works
        self.assertTrue(t.ref)
        self.assertTrue(self.bank.alerts(self.cid)[0]["status"].startswith(("Failed", "Delivered")))
        self.assertTrue(any(a["status"].startswith("Failed") for a in self.bank.alerts(self.cid)))

    def test_demo_customers_are_never_contacted(self):
        demo, _ = self.bank.register({**PERSON, "full_name": "Demo Person", "email": "demo@example.com",
                                      "mobile": "9844444444"})
        self.http.sent.clear()
        cash(self.bank, demo, 30000, PIN)
        self.assertEqual(self.http.sent, [])
        self.assertTrue(self.bank.alerts(demo))                    # still shown in the app

    def test_paused_alerts(self):
        n = len(self.bank.alerts(self.cid))
        with self.bank.alerts_paused():
            cash(self.bank, self.cid, 100, PIN)
        self.assertEqual(len(self.bank.alerts(self.cid)), n)


class TestInterest(Base):
    def test_quarterly_interest_on_daily_balance(self):
        bank = self.bank
        cid, acc = bank.register({**PERSON, "email": "q@example.com", "mobile": "9833333333"},
                                 when="2026-01-10 10:00:00")
        # 10 Jan - 31 Mar = 81 days with ₹10,000 at 3.5 %  ->  ₹10,000 x 3.5 % x 81 / 365 = ₹77.67
        results = bank.post_quarterly_interest(date(2026, 4, 2))
        self.assertEqual(results[0]["quarter"], "2026-Q1")
        credit = [t for t in bank.transactions(acc.number) if t.category == "Interest"]
        self.assertEqual(credit[0].amount, 7767)
        self.assertEqual(bank.post_quarterly_interest(date(2026, 4, 3)), [])     # never paid twice
        self.assertIn("2026-Q1", [r["quarter"] for r in bank.interest_runs()])


class TestBackups(Base):
    def test_export_import_round_trip(self):
        cash(self.bank, self.cid, 12345, PIN)
        blob = self.bank.pack(self.bank.export_data())
        before = self.balance()
        cash(self.bank, self.cid, 99900, PIN)
        counts = self.bank.import_data(self.bank.unpack(blob))
        self.assertEqual(self.balance(), before)
        self.assertGreaterEqual(counts["transactions"], 2)
        cash(self.bank, self.cid, 100, PIN)                        # ids continue correctly after a restore
        with self.assertRaises(BankError):
            self.bank.unpack(b"not a backup")

    def test_daily_tasks_once_a_day_and_keep_seven(self):
        for day in range(1, 10):
            self.bank.daily_tasks(date(2026, 5, day))
        self.assertIsNone(self.bank.daily_tasks(date(2026, 5, 9)))
        autos = [b for b in self.bank.backups() if b["kind"] == "auto"]
        self.assertEqual(len(autos), 7)
        self.assertEqual(self.bank.unpack(self.bank.backup_blob(autos[0]["id"]))["app"], "PyBank")

    def test_secret_key_is_kept(self):
        self.assertEqual(self.bank.secret_key(), Bank(self.bank.db.target, driver=DRIVER).secret_key())


if __name__ == "__main__":
    unittest.main()

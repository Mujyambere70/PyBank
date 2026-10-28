"""Unit tests: Bank, BankAccount and helper functions (no web server)."""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bank import (Bank, BankAccount, BankError, format_money, mask_name,  # noqa: E402
                  parse_amount)

PERSON = dict(full_name="Test User", dob="1995-05-05", gender="Female", email="test@example.com",
              mobile="9811111111", address="12 Main Road", city="Ambala", state="Haryana",
              pincode="133001", occupation="Salaried", account_type="Savings",
              initial_deposit="10000", password="Secret123", pin="4826")


class TestHelpers(unittest.TestCase):
    def test_parse_amount(self):
        self.assertEqual(parse_amount("500"), 50000)
        self.assertEqual(parse_amount("1,250.75"), 125075)
        for bad in ["", "abc", "-5", "0", "1.234", "nan", "inf", "99999999999"]:
            with self.assertRaises(BankError, msg=bad):
                parse_amount(bad)

    def test_format_money_indian_style(self):
        self.assertEqual(format_money(0), "₹0.00")
        self.assertEqual(format_money(150050), "₹1,500.50")
        self.assertEqual(format_money(10000000), "₹1,00,000.00")
        self.assertEqual(format_money(1234567890), "₹1,23,45,678.90")

    def test_mask_name(self):
        self.assertEqual(mask_name("Rahul Verma"), "R**** V****")


class TestBankAccount(unittest.TestCase):
    def setUp(self):
        self.acc = BankAccount("502100000001", "PB1", "Savings", 10000, "PYBK0001024", "Main", "1234", "Active", "")

    def test_credit_debit(self):
        self.acc.credit(5000, "Deposit", "Cash")
        t = self.acc.debit(3000, "Withdrawal", "Cash")
        self.assertEqual(self.acc.balance, 12000)
        self.assertEqual((t.type, t.balance_after), ("DR", 12000))
        self.assertTrue(t.ref.startswith("PB"))

    def test_cannot_go_below_zero(self):
        with self.assertRaises(BankError):
            self.acc.debit(10001, "Withdrawal", "Cash")
        self.assertEqual(self.acc.balance, 10000)

    def test_synopsis_methods(self):
        self.acc.deposit(100)
        self.acc.withdraw(50)
        self.assertEqual(self.acc.balance, 10050)


class TestBank(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.bank = Bank(os.path.join(self.tmp.name, "test.db"))
        self.cid, self.acc = self.bank.register(PERSON)
        self.cid2, self.acc2 = self.bank.register({**PERSON, "full_name": "Other Person",
                                                   "email": "o@example.com", "mobile": "9822222222"})

    def tearDown(self):
        self.tmp.cleanup()

    def test_register(self):
        self.assertRegex(self.cid, r"^PB\d{8}$")
        self.assertRegex(self.acc.number, r"^\d{12}$")
        self.assertEqual(self.acc.balance, 1_000_000)
        c = self.bank.get_customer(self.cid)
        self.assertNotIn("Secret123", c.password_hash)       # stored as a hash

    def test_register_validation(self):
        cases = [({"full_name": "Ana"}, "full name"), ({"dob": "2015-01-01"}, "18"),
                 ({"email": "bad"}, "email"), ({"mobile": "12345"}, "mobile"),
                 ({"pincode": "12"}, "PIN code"), ({"password": "short"}, "Password"),
                 ({"pin": "11a1"}, "PIN"), ({"pin": "7777"}, "easy"),
                 ({"initial_deposit": "100"}, "Minimum"),
                 ({"account_type": "Current", "initial_deposit": "1000"}, "Minimum")]
        for change, words in cases:
            with self.assertRaises(BankError, msg=change) as e:
                self.bank.register({**PERSON, "email": "x@y.com", "mobile": "9833333333", **change})
            self.assertIn(words.lower(), str(e.exception).lower())

    def test_duplicate_email(self):
        with self.assertRaises(BankError):
            self.bank.register({**PERSON, "mobile": "9844444444"})

    def test_login_and_lockout(self):
        self.assertEqual(self.bank.authenticate(self.cid.lower(), "Secret123").customer_id, self.cid)
        self.assertEqual(self.bank.authenticate("TEST@example.com", "Secret123").customer_id, self.cid)
        for _ in range(4):
            with self.assertRaises(BankError):
                self.bank.authenticate(self.cid, "wrong")
        with self.assertRaises(BankError) as e:
            self.bank.authenticate(self.cid, "wrong")
        self.assertIn("locked", str(e.exception))
        with self.assertRaises(BankError):                 # even the right password is refused now
            self.bank.authenticate(self.cid, "Secret123")

    def test_deposit_withdraw_need_pin(self):
        with self.assertRaises(BankError):
            self.bank.deposit(self.cid, 100, "0000")
        self.bank.deposit(self.cid, 50000, "4826")
        self.bank.withdraw(self.cid, 20000, "4826")
        self.assertEqual(self.bank.get_account(self.cid).balance, 1_030_000)

    def test_daily_withdraw_limit(self):
        self.bank.deposit(self.cid, 10_000_000, "4826")
        self.bank.withdraw(self.cid, 4_000_000, "4826")
        with self.assertRaises(BankError) as e:
            self.bank.withdraw(self.cid, 1_500_000, "4826")
        self.assertIn("limit", str(e.exception))

    def test_transfer_moves_money_both_sides(self):
        t = self.bank.transfer(self.cid, self.acc2.number, 250000, "4826", "Rent")
        self.assertEqual(self.bank.get_account(self.cid).balance, 750000)
        self.assertEqual(self.bank.get_account(self.cid2).balance, 1_250_000)
        received = self.bank.transactions(self.acc2.number, limit=1)[0]
        self.assertEqual((received.type, received.ref), ("CR", t.ref))
        self.assertIn("Test User", received.description)

    def test_transfer_errors(self):
        with self.assertRaises(BankError):
            self.bank.transfer(self.cid, self.acc.number, 100, "4826")          # same account
        with self.assertRaises(BankError):
            self.bank.transfer(self.cid, "999999999999", 100, "4826")          # unknown account
        with self.assertRaises(BankError):
            self.bank.transfer(self.cid, self.acc2.number, 99_999_999, "4826")  # too much
        self.assertEqual(self.bank.get_account(self.cid).balance, 1_000_000)  # nothing changed

    def test_find_beneficiary_is_masked(self):
        self.assertEqual(self.bank.find_beneficiary(self.acc2.number), "O**** P*****")

    def test_statement_filters(self):
        self.bank.deposit(self.cid, 100, "4826")
        self.bank.withdraw(self.cid, 50, "4826")
        all_rows = self.bank.transactions(self.acc.number)
        self.assertEqual([t.category for t in all_rows][:2], ["Withdrawal", "Deposit"])
        self.assertTrue(all(t.type == "CR" for t in self.bank.transactions(self.acc.number, kind="CR")))
        self.assertEqual(self.bank.transactions(self.acc.number, "2000-01-01", "2000-12-31"), [])

    def test_change_password_and_pin(self):
        with self.assertRaises(BankError):
            self.bank.change_password(self.cid, "wrong", "NewPass123")
        self.bank.change_password(self.cid, "Secret123", "NewPass123")
        self.bank.authenticate(self.cid, "NewPass123")
        self.bank.change_pin(self.cid, "NewPass123", "9035")
        self.bank.deposit(self.cid, 100, "9035")

    def test_update_contact(self):
        self.bank.update_contact(self.cid, "new@example.com", "9855555555", "1 New Street",
                                 "karnal", "Haryana", "132001")
        c = self.bank.get_customer(self.cid)
        self.assertEqual((c.email, c.city), ("new@example.com", "Karnal"))
        with self.assertRaises(BankError):          # email of another customer
            self.bank.update_contact(self.cid, "o@example.com", "9855555555", "1 New Street",
                                     "Karnal", "Haryana", "132001")


class TestDemoData(unittest.TestCase):
    def test_seed(self):
        import demo_data
        with tempfile.TemporaryDirectory() as d:
            bank = Bank(os.path.join(d, "t.db"))
            demo_data.seed(bank)
            for cid in ("PB10000001", "PB10000002", "PB10000003"):
                acc = bank.get_account(cid)
                rows = bank.transactions(acc.number)
                self.assertGreater(len(rows), 50)
                self.assertEqual(rows[0].balance_after, acc.balance)
                # every row's balance = previous balance +/- amount (oldest first)
                old = list(reversed(rows))
                for prev, t in zip(old, old[1:]):
                    change = t.amount if t.is_credit else -t.amount
                    self.assertEqual(prev.balance_after + change, t.balance_after, t.ref)
            demo_data.seed(bank)                    # second call does nothing
            self.assertEqual(len(bank.transactions(bank.get_account("PB10000001").number)), len(
                bank.transactions(bank.get_account("PB10000001").number)))


if __name__ == "__main__":
    unittest.main()

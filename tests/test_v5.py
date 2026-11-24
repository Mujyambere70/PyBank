"""Tests for version 5: staff work at the counter (cash in and out, transfers, other banks, exchange,
deposits, EMIs) with cash drawers and the four-eyes rule; job roles and abilities managed by the
administrator; staff personal details and photo; the name of the account holder shown while typing
(name enquiry) for PyBank, IMPS/NEFT, UPI, SWIFT and SEPA; one smart search with typo correction,
synonyms and suggestions; the "Ask PyBank" assistant; recurring deposits and savings goals; sanctions
screening and risk rating; open banking with OAuth 2.0 + PKCE; the data manager."""

import base64
import io
import os
import re
import secrets
import sys
import unittest
from datetime import date, timedelta
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_v4 import PIN, AppBase, BankBase  # noqa: E402

import roles  # noqa: E402
import smartsearch  # noqa: E402
from assistant import parse_amount_words, parse_period  # noqa: E402
from bank import BankError  # noqa: E402
from deposits import rd_value  # noqa: E402
from nameenquiry import compare_names  # noqa: E402
from openbanking import pkce_challenge  # noqa: E402
from screening import name_score  # noqa: E402


# =========================================================================== roles and abilities

class TestRolesAndAbilities(AppBase):
    def test_every_admin_address_needs_an_ability(self):
        """No back-office page or button is open to every staff member by mistake."""
        sample = {"cid": self.cid, "code": "teller", "sid": "EMP003", "table": "customers", "op": "deposit",
                  "key": "1", "op_id": "1"}
        missing = []
        for r in self.app.url_map.iter_rules():
            if not r.rule.startswith("/admin") or r.rule in roles.OPEN_PATHS:
                continue
            path = re.sub(r"<(?:[^:>]+:)?([^>]+)>", lambda m: sample.get(m.group(1), "x1"), r.rule)
            for method in r.methods - {"HEAD", "OPTIONS"}:
                if roles.ability_for(path, method) is None:
                    missing.append((method, r.rule))
        self.assertEqual(missing, [])

    def test_teller_has_counter_abilities(self):
        teller = self.office.get_staff("EMP003")
        for a in ("counter.deposit", "counter.withdraw", "counter.transfer", "till.own"):
            self.assertTrue(roles.can(teller, a), a)
        self.assertFalse(roles.can(teller, "staff.manage"))
        self.assertFalse(roles.can(self.office.get_staff("EMP002"), "counter.deposit"))

    def test_admin_creates_and_edits_a_role(self):
        code = self.office.save_role(self.admin, "", "Head cashier", ["counter.deposit", "till.supervise"])
        self.assertIn(code, self.office.job_roles())
        self.office.set_job_role(self.admin, "EMP005", code)
        five = self.office.get_staff("EMP005")
        self.assertTrue(roles.can(five, "till.supervise"))
        self.assertFalse(roles.can(five, "customers.edit"))
        self.assertEqual(five["_job_label"], "Head cashier")
        self.office.save_role(self.admin, code, "Head cashier", ["counter.deposit"])
        self.assertFalse(roles.can(self.office.get_staff("EMP005"), "till.supervise"))
        with self.assertRaises(BankError):
            self.office.delete_role(self.admin, code)               # it still has a person
        self.office.set_job_role(self.admin, "EMP005", "officer")
        self.office.delete_role(self.admin, code)
        self.assertNotIn(code, self.office.job_roles())

    def test_unknown_ability_is_refused(self):
        with self.assertRaises(BankError):
            self.office.save_role(self.admin, "", "Odd", ["counter.deposit", "fly.to.moon"])

    def test_builtin_role_reset(self):
        self.office.save_role(self.admin, "teller", "Teller", ["counter.deposit"])
        self.assertFalse(roles.can(self.office.get_staff("EMP003"), "counter.withdraw"))
        self.office.reset_role(self.admin, "teller")
        self.assertTrue(roles.can(self.office.get_staff("EMP003"), "counter.withdraw"))

    def test_grant_and_deny_one_person(self):
        form = {"job_role": "teller", "max_amount": "20000", "ab_counter.fx": "no", "ab_audit.view": "yes"}
        self.office.set_staff_access(self.admin, "EMP003", form)
        t = self.office.get_staff("EMP003")
        self.assertFalse(roles.can(t, "counter.fx"))
        self.assertTrue(roles.can(t, "audit.view"))
        self.assertEqual(t["max_amount"], 2_000_000)
        with self.assertRaises(BankError):
            self.office.set_staff_access(self.admin, "EMP001", {"job_role": "teller"})    # not yourself

    def test_role_change_works_on_next_click(self):
        self.staff("EMP002")
        self.assertEqual(self.c.get("/admin/counter").status_code, 403)
        self.office.save_role(self.admin, "content_editor", "Website editor",
                              sorted(self.office.job_roles()["content_editor"]["abilities"] | {"customers.view"}))
        self.assertEqual(self.c.get("/admin/counter").status_code, 200)

    def test_pages(self):
        self.staff("EMP001", "Admin@1234")
        for url in ("/admin/roles", "/admin/staff/EMP003/access", "/admin/screening", "/admin/openbanking",
                    "/admin/data", "/admin/data/customers", f"/admin/data/customers/{self.cid}", "/admin/tills",
                    "/admin/counter", f"/admin/counter/{self.cid}", "/admin/till", "/admin/search?q=tset",
                    "/admin/assistant"):
            self.assertEqual(self.c.get(url).status_code, 200, url)
        r = self.post("/admin/roles", label="Night teller", abilities="counter.deposit")
        self.assertEqual(r.status_code, 302)
        self.assertIn("Night teller", self.c.get("/admin/roles").get_data(as_text=True))


class TestStaffDetails(AppBase):
    def test_own_details_and_photo(self):
        me = self.office.get_staff("EMP003")
        self.office.update_own_details(me, {"full_name": "Cash Teller", "email": "emp003@test.example", "phone": "9800000000",
                                            "designation": "Teller", "address": "4 Demo Lane", "dob": "1990-02-03",
                                            "emergency_contact": "A. Friend 9800000001"})
        s = self.office.get_staff("EMP003")
        self.assertEqual(s["address"], "4 Demo Lane")
        self.assertEqual(s["dob"], "1990-02-03")
        with self.assertRaises(BankError):
            self.office.update_own_details(me, {"full_name": "X", "email": "bad"})

    def test_photo_upload_is_made_small(self):
        try:
            from PIL import Image
        except ImportError:
            self.skipTest("Pillow not installed")
        buf = io.BytesIO()
        Image.new("RGB", (800, 600), (10, 120, 90)).save(buf, "PNG")
        self.staff("EMP003")
        self.c.post("/admin/profile/photo", data={"csrf_token": "test-token", "photo": (io.BytesIO(buf.getvalue()), "me.png")},
                    content_type="multipart/form-data")
        photo = self.office.get_staff("EMP003")["photo"]
        self.assertTrue(photo)
        img = Image.open(io.BytesIO(base64.b64decode(photo)))
        self.assertEqual(img.size, (160, 160))
        self.assertEqual(self.c.get("/admin/staff-photo/EMP003.jpg").mimetype, "image/jpeg")

    def test_admin_edits_staff_details_but_keeps_one_admin(self):
        self.office.update_staff_details(self.admin, "EMP004", {"full_name": "Loan Person", "email": "emp004@test.example",
                                                                "branch": "Ambala", "role": "staff"})
        self.assertEqual(self.office.get_staff("EMP004")["branch"], "Ambala")
        with self.assertRaises(BankError):
            self.office.update_staff_details(self.admin, "EMP001", {"full_name": "Main Admin",
                                                                    "email": "admin@test.example", "role": "staff"})


# =========================================================================== the counter

class TestCounter(AppBase):
    def setUp(self):
        super().setUp()
        self.teller = self.office.get_staff("EMP003")
        self.office.open_till(self.teller, "INR", 20_000_000)          # 2 lakh rupees in the drawer

    def bal(self, number):
        return self.bank.get_account(self.cid).balance if number == self.acc.number else \
            next(a.balance for a in self.bank.accounts_of(self.other) if a.number == number)

    def test_deposit_and_withdraw_move_the_drawer(self):
        before = self.bal(self.acc.number)
        kind, op_id = self.office.counter(self.teller, self.cid, "deposit", {"account": self.acc.number, "amount": "5000",
                                                                            "depositor": "Test User"})
        self.assertEqual(kind, "done")
        self.assertEqual(self.bal(self.acc.number), before + 500_000)
        op = self.office.counter_op(op_id)
        self.assertTrue(op["receipt_no"].startswith("CR"))
        self.office.counter(self.teller, self.cid, "withdraw", {"account": self.acc.number, "amount": "2000", "id_checked": "1",
                                                               "id_type": "Passport", "id_number": "Z1234567", "signature": "1"})
        self.assertEqual(self.bal(self.acc.number), before + 300_000)
        t = self.office.till("EMP003", "INR")
        self.assertEqual(t["expected"], 20_000_000 + 500_000 - 200_000)

    def test_withdraw_needs_id_and_signature(self):
        with self.assertRaises(BankError):
            self.office.counter(self.teller, self.cid, "withdraw", {"account": self.acc.number, "amount": "100"})
        with self.assertRaises(BankError):
            self.office.counter(self.teller, self.cid, "withdraw", {"account": self.acc.number, "amount": "100", "id_checked": "1",
                                                                   "id_type": "Passport", "id_number": "Z1234567"})

    def test_no_cash_without_open_drawer_and_not_more_than_drawer(self):
        five = self.office.get_staff("EMP005")
        with self.assertRaises(BankError):
            self.office.counter(five, self.cid, "deposit", {"account": self.acc.number, "amount": "100", "depositor": "Test User"})
        self.office.open_till(five, "INR", 10_000)                      # only 100 rupees
        with self.assertRaises(BankError):
            self.office.counter(five, self.cid, "withdraw", {"account": self.acc.number, "amount": "500", "id_checked": "1",
                                                            "id_type": "Passport", "id_number": "Z1234567", "signature": "1"})

    def test_transfer_shows_name_and_moves_money(self):
        before = self.bal(self.other_acc.number)
        kind, op_id = self.office.counter(self.teller, self.cid, "transfer", {"account": self.acc.number, "to_account": self.other_acc.number,
                                                                             "amount": "1500"})
        self.assertEqual(self.bal(self.other_acc.number), before + 150_000)
        self.assertIn("Asha Rao", self.office.counter_op(op_id)["details"])
        with self.assertRaises(BankError):
            self.office.counter(self.teller, self.cid, "transfer", {"account": self.acc.number, "to_account": "999999999999",
                                                                   "amount": "10"})

    def test_external_payment_at_the_counter(self):
        before = self.bal(self.acc.number)
        kind, op_id = self.office.counter(self.teller, self.cid, "external", {
            "account": self.acc.number, "rail": "IMPS", "name": "Ravi Kumar", "account_": "", "ifsc": "NOVA0001234",
            "amount": "1000", "remarks": "rent"} | {"account": self.acc.number})
        self.assertEqual(kind, "done")
        self.assertLess(self.bal(self.acc.number), before)

    def test_large_amount_waits_for_a_second_person(self):
        kind, pid = self.office.counter(self.teller, self.cid, "deposit", {"account": self.acc.number, "amount": "60000",
                                                                          "depositor": "Test User"})
        self.assertEqual(kind, "waiting")
        before = self.bal(self.acc.number)
        with self.assertRaises(BankError):
            self.office.decide_entry(self.teller, pid, True)            # not the same person
        self.office.decide_entry(self.admin, pid, True)
        self.assertEqual(self.bal(self.acc.number), before + 6_000_000)
        ops = self.office.counter_ops(cid=self.cid)
        self.assertEqual(ops[0]["approved_by"], "EMP001")

    def test_own_limit_sends_to_second_person(self):
        self.office.set_staff_access(self.admin, "EMP003", {"job_role": "teller", "max_amount": "1000"})
        teller = self.office.get_staff("EMP003")
        kind, _ = self.office.counter(teller, self.cid, "deposit", {"account": self.acc.number, "amount": "2000",
                                                                   "depositor": "Test User"})
        self.assertEqual(kind, "waiting")

    def test_fd_and_rd_at_the_counter(self):
        self.office.counter(self.teller, self.cid, "fd", {"account": self.acc.number, "amount": "10000", "months": "12"})
        self.assertEqual(len(self.bank.fixed_deposits(self.cid)), 1)
        self.office.counter(self.teller, self.cid, "rd", {"account": self.acc.number, "amount": "1000", "months": "12"})
        self.assertEqual(len(self.bank.recurring_deposits(self.cid)), 1)

    def test_close_drawer_with_difference_and_accept(self):
        t = self.office.till("EMP003", "INR")
        with self.assertRaises(BankError):
            self.office.close_till(self.teller, t["id"], t["expected"] - 10_000)          # no note
        diff = self.office.close_till(self.teller, t["id"], t["expected"] - 10_000, "short by 100, recounted")
        self.assertEqual(diff, -10_000)
        with self.assertRaises(BankError):
            self.office.accept_till(self.teller, t["id"], "ok")                         # not your own
        self.office.accept_till(self.admin, t["id"], "paid from petty cash")
        self.assertEqual(self.office.till("EMP003", "INR")["status"], "Accepted")

    def test_web_counter_flow(self):
        self.staff("EMP003")
        r = self.post(f"/admin/counter/{self.cid}/deposit", account=self.acc.number, amount="250", depositor="Test User")
        self.assertIn("/admin/receipt/", r.headers["Location"])
        page = self.c.get(r.headers["Location"]).get_data(as_text=True)
        self.assertIn("Counter receipt", page)
        self.assertIn("Test User", page)
        self.assertEqual(self.c.get(f"/admin/counter?q={self.cid}").status_code, 302)
        self.assertEqual(self.c.get(f"/admin/counter?q={self.acc.number}").status_code, 302)
        self.assertEqual(self.c.get("/admin/roles").status_code, 403)

    def test_loan_officer_cannot_take_cash(self):
        self.staff("EMP004")
        r = self.post(f"/admin/counter/{self.cid}/deposit", account=self.acc.number, amount="250", depositor="Test User")
        self.assertEqual(r.status_code, 403)


# =========================================================================== name enquiry

class TestNameEnquiry(AppBase):
    def test_pybank_account(self):
        out = self.bank.name_enquiry("PYBANK", {"account": self.other_acc.number})
        self.assertEqual(out["name"], "Asha Rao")
        self.assertEqual(out["bank"], "PyBank")
        with self.assertRaises(BankError):
            self.bank.name_enquiry("PYBANK", {"account": "000000000001"})

    def test_other_rails_give_stable_names(self):
        a = self.bank.name_enquiry("IMPS", {"account": "123456789012", "ifsc": "NOVA0001234"})
        b = self.bank.name_enquiry("NEFT", {"account": "123456789012", "ifsc": "NOVA0001234"})
        self.assertEqual(a["name"], b["name"])
        self.assertTrue(a["name"])
        self.assertTrue(self.bank.name_enquiry("UPI", {"upi": "priya@okbank"})["name"])
        sw = self.bank.name_enquiry("SWIFT", {"account": "DE89370400440532013000", "bic": "RHMUDEFF"})
        self.assertEqual(sw["country"], "DE")
        sepa = self.bank.name_enquiry("SEPA", {"account": "FR7630006000011234567890189"})
        self.assertEqual(sepa["currency"], "EUR")

    def test_errors(self):
        for rail, data in (("IMPS", {"account": "123456789012", "ifsc": "BAD"}), ("IMPS", {"account": "12345678", "ifsc": "NOVA0001234"}),
                           ("IMPS", {"account": "123456780000", "ifsc": "NOVA0001234"}), ("UPI", {"upi": "failme@okbank"}),
                           ("SEPA", {"account": "FR7630006000011234567890180"}), ("XYZ", {})):
            with self.assertRaises(BankError, msg=(rail, data)):
                self.bank.name_enquiry(rail, data)

    def test_pybank_ifsc_goes_inside(self):
        out = self.bank.name_enquiry("IMPS", {"account": self.other_acc.number, "ifsc": "PYBK0001024"})
        self.assertEqual(out["name"], "Asha Rao")

    def test_confirmation_of_payee(self):
        self.assertEqual(compare_names("Asha Rao", "Asha Rao"), "match")
        self.assertEqual(compare_names("Rao Asha", "Asha Rao"), "close")
        self.assertEqual(compare_names("A Rao", "Asha Rao"), "close")
        self.assertEqual(compare_names("Mohan Lal", "Asha Rao"), "no")
        out = self.bank.name_enquiry("PYBANK", {"account": self.other_acc.number, "name": "Mohan Lal"})
        self.assertEqual(out["check"], "no")

    def test_web_lookup(self):
        self.customer()
        d = self.c.get(f"/netbanking/name-enquiry?rail=PYBANK&account={self.other_acc.number}").get_json()
        self.assertTrue(d["ok"])
        self.assertEqual(d["name"], "Asha Rao")
        self.assertNotIn("customer_id", d)
        d = self.c.get(f"/netbanking/name-enquiry?rail=PYBANK&account={self.acc.number}").get_json()
        self.assertTrue(d.get("own"))
        page = self.c.get("/netbanking/transfer").get_data(as_text=True)
        self.assertIn("data-ne=", page)
        self.assertIn("ne-box", self.c.get("/netbanking/transfer/other-bank").get_data(as_text=True))

    def test_lookup_rate_limit(self):
        self.customer()
        codes = [self.c.get(f"/netbanking/name-enquiry?rail=PYBANK&account={self.other_acc.number}").status_code for _ in range(25)]
        self.assertIn(429, codes)


# =========================================================================== smart search and assistant

class TestSmartSearch(unittest.TestCase):
    DOCS = [{"title": "Withdrawal at an ATM", "text": "Take cash with your card", "url": "/a", "kind": "Help"},
            {"title": "Fixed deposits", "text": "Rates for 1 to 5 years", "url": "/b", "kind": "Page"},
            {"title": "Money transfer", "text": "Send money to any bank", "url": "/c", "kind": "Help"},
            {"title": "Rahul Verma", "text": "PB10000001 502100000001", "url": "/d", "kind": "Customer"}]

    def titles(self, q):
        return [d["title"] for d in smartsearch.search(q, self.DOCS)["results"]]

    def test_typo_is_corrected(self):
        res = smartsearch.search("withdrawl", self.DOCS)
        self.assertEqual(res["results"][0]["title"], "Withdrawal at an ATM")
        self.assertTrue(res["did_you_mean"])
        self.assertEqual(self.titles("Rahul Varma")[0], "Rahul Verma")

    def test_synonyms_in_several_languages(self):
        for q in ("cash out", "снять деньги", "retrait", "取款"):
            self.assertEqual(self.titles(q)[0], "Withdrawal at an ATM", q)
        self.assertEqual(self.titles("FD")[0], "Fixed deposits")
        self.assertEqual(self.titles("virement")[0], "Money transfer")

    def test_numbers_find_records(self):
        self.assertEqual(self.titles("502100000001")[0], "Rahul Verma")

    def test_suggest(self):
        s = smartsearch.suggest("fix", self.DOCS)
        self.assertEqual(s[0]["text"], "Fixed deposits")
        self.assertEqual(smartsearch.suggest("a", self.DOCS), [])

    def test_empty_and_nothing(self):
        self.assertEqual(smartsearch.search("", self.DOCS)["results"], [])
        self.assertEqual(smartsearch.search("zzzzqqq", self.DOCS)["results"], [])


class TestSearchPages(AppBase):
    def test_public_search_and_suggest(self):
        page = self.c.get("/search?q=fixd+deposit").get_data(as_text=True)
        self.assertIn("result(s)", page)
        self.assertIsInstance(self.c.get("/search/suggest?q=fi").get_json(), list)

    def test_netbanking_search_only_own_data(self):
        self.bank.transfer(self.other, self.acc.number, 12345, PIN, "from asha")
        self.customer()
        page = self.c.get("/netbanking/search?q=withdrawl").get_data(as_text=True)
        self.assertIn("result(s)", page)
        self.assertNotIn(self.other_acc.number, self.c.get("/netbanking/search?q=asha").get_data(as_text=True).replace(
            f"XX{self.other_acc.number[-4:]}", ""))

    def test_office_search_respects_abilities(self):
        self.staff("EMP002")                                            # website editor: no customers
        page = self.c.get("/admin/search?q=Test+User").get_data(as_text=True)
        self.assertNotIn(self.cid, page)
        self.staff("EMP005")
        self.assertIn(self.cid, self.c.get("/admin/search?q=Tset+User").get_data(as_text=True))


class TestAssistant(AppBase):
    def ask(self, url, q):
        r = self.c.post(url, json={"q": q}, headers={"X-CSRF-Token": "test-token"})
        self.assertEqual(r.status_code, 200)
        return r.get_json()

    def test_helpers(self):
        self.assertEqual(parse_amount_words("5 lakh"), 500000)
        self.assertEqual(parse_amount_words("2k"), 2000)
        start, end, label = parse_period("last month", today=date(2026, 3, 15))
        self.assertEqual((start, end), (date(2026, 2, 1), date(2026, 2, 28)))

    def test_public_questions(self):
        self.assertIn("%", self.ask("/assistant", "fd rate")["text"])
        self.assertIn("EMI", self.ask("/assistant", "EMI for 5 lakh at 9% for 5 years")["text"])
        self.assertEqual(self.c.post("/assistant", json={"q": "hi"}).status_code, 400)       # no CSRF header

    def test_customer_balance_and_spending(self):
        self.customer()
        d = self.ask("/assistant", "what is my balance")
        self.assertTrue(any(f"XX{self.acc.number[-4:]}" in i for i in d["items"]))
        self.assertIn("spent", self.ask("/assistant", "how much did I spend this month")["text"].lower())
        self.assertIsInstance(d["links"][0], dict)

    def test_staff_lists_follow_abilities(self):
        self.staff("EMP002")
        self.assertEqual(self.ask("/admin/assistant", "pending loans")["kind"], "denied")
        self.staff("EMP001", "Admin@1234")
        self.assertNotEqual(self.ask("/admin/assistant", "pending loans")["kind"], "denied")


# =========================================================================== RD and goals

class TestDeposits(BankBase):
    def test_rd_value_math(self):
        d = date(2026, 1, 1)
        months = [date(2026, m, 1) for m in range(1, 13)]
        v = rd_value(100_000, 7.0, months, date(2027, 1, 1))
        self.assertGreater(v, 1_200_000)
        self.assertLess(v, 1_260_000)
        self.assertEqual(rd_value(100_000, 7.0, [d], d), 100_000)

    def test_open_collect_and_close_rd(self):
        before = self.bank.get_account(self.cid).balance
        self.bank.open_rd(self.cid, self.acc.number, 100_000, 6, PIN)
        self.assertEqual(self.bank.get_account(self.cid).balance, before - 100_000)
        rd = self.bank.recurring_deposits(self.cid)[0]
        self.assertEqual(rd["paid_count"], 1)
        self.bank.collect_rd_instalments(date.fromisoformat(rd["next_due"]))
        self.assertEqual(self.bank.recurring_deposits(self.cid)[0]["paid_count"], 2)
        self.bank.close_rd(self.cid, rd["id"], PIN)
        self.assertEqual(self.bank.recurring_deposits(self.cid)[0]["status"], "Closed")
        self.assertGreaterEqual(self.bank.get_account(self.cid).balance, before - 1)

    def test_rd_rules(self):
        with self.assertRaises(BankError):
            self.bank.open_rd(self.cid, self.acc.number, 100_000, 7, PIN)
        with self.assertRaises(BankError):
            self.bank.open_rd(self.cid, self.acc.number, 100, 12, PIN)
        with self.assertRaises(BankError):
            self.bank.open_rd(self.cid, self.acc.number, 100_000, 12, "0000")

    def test_missed_instalments_stop_the_rd(self):
        self.bank.open_rd(self.cid, self.acc.number, 100_000, 12, PIN)
        rd = self.bank.recurring_deposits(self.cid)[0]
        with self.bank._connect() as conn:
            conn.execute("UPDATE accounts SET balance = 0 WHERE number = ?", (self.acc.number,))
        due = date.fromisoformat(rd["next_due"])
        for k in range(3):
            self.bank.collect_rd_instalments(due + timedelta(days=31 * k))
        self.assertEqual(self.bank.recurring_deposits(self.cid)[0]["status"], "Stopped")

    def test_goal(self):
        gid = self.bank.create_goal(self.cid, self.acc.number, "Laptop", 6_000_000, (date.today() + timedelta(days=200)).isoformat())
        before = self.bank.get_account(self.cid).balance
        self.bank.move_goal(self.cid, gid, 1_000_000, True, PIN)
        g = self.bank.goals(self.cid)[0]
        self.assertEqual(g["saved"], 1_000_000)
        self.assertEqual(g["percent"], 16)
        self.assertEqual(self.bank.get_account(self.cid).balance, before - 1_000_000)
        with self.assertRaises(BankError):
            self.bank.move_goal(self.cid, gid, 2_000_000, False, PIN)            # more than in the goal
        self.bank.close_goal(self.cid, gid, PIN)
        self.assertEqual(self.bank.get_account(self.cid).balance, before)
        self.assertEqual(self.bank.goals(self.cid), [])

    def test_goal_date_must_be_future(self):
        with self.assertRaises(BankError):
            self.bank.create_goal(self.cid, self.acc.number, "Trip", 100_000, "2000-01-01")


class TestSavingsPage(AppBase):
    def test_quote_open_and_goal_by_web(self):
        self.customer()
        page = self.c.post("/netbanking/savings", data={"csrf_token": "test-token", "action": "rd_quote",
                                                        "instalment": "1000", "months": "12"}).get_data(as_text=True)
        self.assertIn("You pay", page)
        self.post("/netbanking/savings", action="rd_open", instalment="1000", months="12", pin=PIN)
        self.assertEqual(len(self.bank.recurring_deposits(self.cid)), 1)
        self.post("/netbanking/savings", action="goal_new", name="Fees", target="20000",
                  target_date=(date.today() + timedelta(days=90)).isoformat())
        self.assertIn("Fees", self.c.get("/netbanking/savings").get_data(as_text=True))


# =========================================================================== screening and risk

class TestScreening(BankBase):
    def test_name_score(self):
        self.assertGreater(name_score("Kofi Mensah", "Kofi Mensa"), 0.85)
        self.assertLess(name_score("Asha Rao", "Kofi Mensa"), 0.5)

    def test_new_customer_with_listed_name_is_held(self):
        from test_v4 import PERSON
        cid, _ = self.bank.register(dict(PERSON, full_name="Kofi Mensah", email="kofi@mailbox.in", mobile="9844444444"))
        hits = self.bank.screening_hits("Open", cid)
        self.assertEqual(len(hits), 1)
        admin = {"staff_id": "EMP001", "full_name": "Admin"}
        with self.assertRaises(BankError):
            self.bank.decide_hit(admin, hits[0]["id"], "Confirmed", "")
        self.bank.decide_hit(admin, hits[0]["id"], "Confirmed", "same date of birth on passport")
        self.assertTrue(all(a.status == "Frozen" for a in self.bank.accounts_of(cid)))

    def test_false_positive_keeps_accounts(self):
        from test_v4 import PERSON
        cid, _ = self.bank.register(dict(PERSON, full_name="Kofi Mensah", email="kofi2@mailbox.in", mobile="9855555555"))
        h = self.bank.screening_hits("Open", cid)[0]
        self.bank.decide_hit({"staff_id": "EMP001"}, h["id"], "False positive", "different date of birth")
        self.assertTrue(all(a.status != "Frozen" for a in self.bank.accounts_of(cid)))
        self.assertEqual(self.bank.screen_customer(cid) and len(self.bank.screening_hits("Open", cid)), 0)

    def test_risk_rating(self):
        self.assertEqual(self.bank.get_customer(self.cid).risk_rating, "Low")
        self.bank.set_risk({"staff_id": "EMP001"}, self.cid, "auto", True, "")
        self.assertIn(self.bank.get_customer(self.cid).risk_rating, ("Medium", "High"))
        self.bank.set_risk({"staff_id": "EMP001"}, self.cid, "High", False, "checked by compliance")
        self.assertEqual(self.bank.get_customer(self.cid).risk_rating, "High")
        self.bank.update_risk(self.cid)                                      # manual rating stays
        self.assertEqual(self.bank.get_customer(self.cid).risk_rating, "High")
        with self.assertRaises(BankError):
            self.bank.set_risk({"staff_id": "EMP001"}, self.cid, "Low", False, "")


# =========================================================================== open banking

class TestOpenBanking(AppBase):
    def test_full_oauth_flow_with_pkce(self):
        client_id, secret = self.bank.register_ob_app(self.admin, "Budget Buddy", "Demo Apps Ltd",
                                                      "https://budget.example/callback")
        verifier = secrets.token_urlsafe(40)
        q = {"response_type": "code", "client_id": client_id, "redirect_uri": "https://budget.example/callback",
             "scope": "accounts transactions", "state": "xyz", "code_challenge": pkce_challenge(verifier),
             "code_challenge_method": "S256"}
        r = self.c.get("/oauth/authorize", query_string=q)
        self.assertIn("/login", r.headers["Location"])                     # sign in first
        self.customer()
        page = self.c.get("/oauth/authorize", query_string=q).get_data(as_text=True)
        self.assertIn("Budget Buddy", page)
        r = self.c.post("/oauth/authorize", query_string=q, data={"csrf_token": "test-token", "decision": "allow"})
        back = urlparse(r.headers["Location"])
        self.assertEqual(back.netloc, "budget.example")
        args = parse_qs(back.query)
        self.assertEqual(args["state"], ["xyz"])
        code = args["code"][0]
        bad = self.app.test_client().post("/oauth/token", data={"grant_type": "authorization_code", "code": code,
                                                                "client_id": client_id, "client_secret": secret,
                                                                "redirect_uri": q["redirect_uri"], "code_verifier": "wrong"})
        self.assertEqual(bad.status_code, 400)
        tok = self.app.test_client().post("/oauth/token", data={"grant_type": "authorization_code", "code": code,
                                                                "client_id": client_id, "client_secret": secret,
                                                                "redirect_uri": q["redirect_uri"], "code_verifier": verifier})
        self.assertEqual(tok.status_code, 200, tok.get_data(as_text=True))
        token = tok.get_json()["access_token"]
        api = self.app.test_client()
        h = {"Authorization": "Bearer " + token}
        accs = api.get("/openbanking/v1/accounts", headers=h).get_json()["accounts"]
        self.assertEqual(accs[0]["number"], self.acc.number)
        self.assertEqual(api.get(f"/openbanking/v1/accounts/{self.acc.number}/transactions", headers=h).status_code, 200)
        self.assertEqual(api.get(f"/openbanking/v1/accounts/{self.other_acc.number}/transactions", headers=h).status_code, 404)
        self.assertEqual(api.get("/openbanking/v1/me", headers=h).status_code, 403)          # not allowed
        # the code works only once
        again = api.post("/oauth/token", data={"grant_type": "authorization_code", "code": code, "client_id": client_id,
                                               "client_secret": secret, "redirect_uri": q["redirect_uri"],
                                               "code_verifier": verifier})
        self.assertEqual(again.status_code, 400)
        # the customer withdraws -> the token stops working
        consent = self.bank.consents(self.cid)[0]
        self.post("/netbanking/connected-apps", id=str(consent["id"]))
        self.assertEqual(api.get("/openbanking/v1/accounts", headers=h).status_code, 401)

    def test_bad_requests(self):
        client_id, _ = self.bank.register_ob_app(self.admin, "App", "", "https://app.example/cb")
        for q in ({"client_id": "nope"}, {"client_id": client_id, "redirect_uri": "https://evil.example/cb",
                                          "response_type": "code", "scope": "accounts"},
                  {"client_id": client_id, "redirect_uri": "https://app.example/cb", "response_type": "code", "scope": "money"}):
            self.assertEqual(self.c.get("/oauth/authorize", query_string=q).status_code, 400)
        with self.assertRaises(BankError):
            self.bank.register_ob_app(self.admin, "App", "", "http://plain.example/cb")

    def test_refuse_and_stop_app(self):
        client_id, secret = self.bank.register_ob_app(self.admin, "App", "", "https://app.example/cb")
        q = {"response_type": "code", "client_id": client_id, "redirect_uri": "https://app.example/cb", "scope": "accounts"}
        self.customer()
        r = self.c.post("/oauth/authorize", query_string=q, data={"csrf_token": "test-token", "decision": "deny"})
        self.assertIn("error=access_denied", r.headers["Location"])
        self.bank.set_ob_app_active(self.admin, client_id, False)
        self.assertEqual(self.c.get("/oauth/authorize", query_string=q).status_code, 400)


# =========================================================================== data manager

class TestDataManager(AppBase):
    def setUp(self):
        super().setUp()
        from datamanager import DataManager
        self.dm = DataManager(self.bank, self.office)

    def test_edit_customer_but_never_money(self):
        self.dm.save(self.admin, "customers", self.cid, {"city": "Mullana", "balance": "999"})
        self.assertEqual(self.bank.get_customer(self.cid).city, "Mullana")
        before = self.bank.get_account(self.cid).balance
        self.dm.save(self.admin, "accounts", self.acc.number, {"balance": "1", "status": "Active"})
        self.assertEqual(self.bank.get_account(self.cid).balance, before)
        with self.assertRaises(BankError):
            self.dm.save(self.admin, "customers", self.cid, {"email": "not-an-email"})
        with self.assertRaises(BankError):
            self.dm.save(self.admin, "accounts", self.acc.number, {"status": "Rich"})

    def test_secrets_are_hidden(self):
        self.assertNotIn("password_hash", self.dm.columns("customers"))
        self.assertNotIn("pin_hash", self.dm.columns("customers"))
        self.assertNotIn("password_hash", self.dm.columns("staff"))

    def test_add_and_delete_and_audit(self):
        key = self.dm.add(self.admin, "faqs", {"question": "Is PyBank real?", "answer": "No, it is a demo.",
                                               "category": "General", "position": "9", "active": "1"})
        self.assertEqual(self.dm.row("faqs", key)["question"], "Is PyBank real?")
        self.dm.delete(self.admin, "faqs", key)
        with self.assertRaises(BankError):
            self.dm.row("faqs", key)
        with self.assertRaises(BankError):
            self.dm.delete(self.admin, "customers", self.cid)
        with self.bank._connect() as conn:
            actions = [r[0] for r in conn.execute("SELECT action FROM audit_log WHERE action LIKE 'Data manager%'")]
        self.assertIn("Data manager: record added", actions)
        self.assertIn("Data manager: record deleted", actions)

    def test_only_with_ability(self):
        self.staff("EMP003")
        self.assertEqual(self.c.get("/admin/data").status_code, 403)
        self.staff("EMP001", "Admin@1234")
        r = self.post(f"/admin/data/customers/{self.cid}", city="Panchkula")
        self.assertEqual(r.status_code, 302)
        self.assertEqual(self.bank.get_customer(self.cid).city, "Panchkula")


if __name__ == "__main__":
    unittest.main()

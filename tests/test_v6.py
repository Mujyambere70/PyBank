"""Tests for version 6: abilities fully managed by the administrator (own abilities, page rules, tick
boxes for one person, the ability matrix, temporary abilities, copy and reset), the branch vault with
two signatures and note counting, cheque deposits with clearing, nominees, the credit card, payments
held by the sanctions check, sanctions list import, the chat assistant that remembers and does things
after a confirmation, the data manager v2 (every table, sort, pages, CSV out and in, delete many), the
administrator's own details and activity."""

import io
import os
import sys
import unittest
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_v4 import PIN, AppBase  # noqa: E402

import roles  # noqa: E402
from bank import BankError  # noqa: E402
from cheques import next_working_day  # noqa: E402
from vault import count_notes  # noqa: E402


class V6Base(AppBase):
    def as_admin(self):
        self.staff("EMP001", "Admin@1234")

    def chat(self, q, url="/chat"):
        r = self.c.post(url, json={"q": q}, headers={"X-CSRF-Token": "test-token"})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        return r.get_json()

    def chat_confirm(self, msg_id, url="/chat/confirm", **form):
        return self.c.post(url, json=dict(form, id=msg_id), headers={"X-CSRF-Token": "test-token"})


# =========================================================================== abilities

class TestAbilities(V6Base):
    def test_own_ability_with_page_rule_locks_and_opens_a_page(self):
        teller = self.office.get_staff("EMP003")
        self.assertTrue(roles.can(teller, None))
        code = self.office.create_ability(self.admin, "See loan list", "Loans", "", ["/admin/loans"])
        self.assertTrue(code.startswith("x."))
        self.assertEqual(roles.ability_for("/admin/loans"), code)
        self.staff("EMP004")                                       # the loan officer lost the page ...
        self.assertEqual(self.c.get("/admin/loans").status_code, 403)
        self.office.give_ability(self.admin, "EMP004", code)       # ... and gets it back with the own ability
        self.assertEqual(self.c.get("/admin/loans").status_code, 200)
        self.office.delete_ability(self.admin, code)               # deleting it gives the page its old ability
        self.assertNotEqual(roles.ability_for("/admin/loans"), code)
        self.assertNotIn(code, self.office.get_staff("EMP004")["_abilities"])

    def test_protected_pages_and_bad_patterns(self):
        for pattern in ("/admin/abilities", "/admin/staff/EMP003", "/admin", "loans", "/admin/../x"):
            with self.assertRaises(BankError):
                self.office.add_rule(self.admin, pattern, "audit.view")
        with self.assertRaises(BankError):
            self.office.add_rule(self.admin, "/admin/loans", "no.such.ability")

    def test_post_only_rule(self):
        self.office.add_rule(self.admin, "/admin/requests", "audit.view", "POST")
        self.assertEqual(roles.ability_for("/admin/requests", "POST"), "audit.view")
        self.assertNotEqual(roles.ability_for("/admin/requests", "GET"), "audit.view")

    def test_tick_boxes_give_exactly_the_ticked_abilities(self):
        base = self.office.role_base("teller")
        want = (base - {"counter.withdraw"}) | {"loans.decide"}
        self.office.set_person_abilities(self.admin, "EMP003", want)
        s = self.office.get_staff("EMP003")
        self.assertEqual(s["_abilities"], want)
        self.assertIn("loans.decide", s["extra_abilities"])
        self.assertIn("counter.withdraw", s["denied_abilities"])
        self.office.reset_to_role(self.admin, "EMP003")
        self.assertEqual(self.office.get_staff("EMP003")["_abilities"], base)

    def test_ticks_through_the_web_page(self):
        self.as_admin()
        r = self.post("/admin/staff/EMP003/abilities", action="ticks", mode="ticks", job_role="teller",
                      abilities=["counter.deposit", "audit.view"], max_amount="5000")
        self.assertEqual(r.status_code, 302)
        s = self.office.get_staff("EMP003")
        self.assertEqual(s["_abilities"], {"counter.deposit", "audit.view"})
        self.assertEqual(s["max_amount"], 500_000)
        page = self.c.get("/admin/staff/EMP003/access").get_data(as_text=True)
        self.assertIn('name="abilities" value="audit.view" checked', page)
        self.assertIn("Abilities changed", page)                    # history on the page

    def test_nobody_changes_his_own_abilities(self):
        with self.assertRaises(BankError):
            self.office.set_person_abilities(self.admin, "EMP001", set())
        with self.assertRaises(BankError):
            self.office.grant_temporary(self.admin, "EMP001", "audit.view", date.today().isoformat(), "test")

    def test_temporary_ability_ends_by_itself(self):
        with self.assertRaises(BankError):
            self.office.grant_temporary(self.admin, "EMP003", "audit.view", date.today().isoformat(), "")
        with self.assertRaises(BankError):
            self.office.grant_temporary(self.admin, "EMP003", "audit.view",
                                        (date.today() + timedelta(days=400)).isoformat(), "too long")
        gid = self.office.grant_temporary(self.admin, "EMP003", "audit.view", date.today().isoformat(), "cover")
        self.assertTrue(roles.can(self.office.get_staff("EMP003"), "audit.view"))
        with self.bank._connect() as conn:                          # pretend the last day was yesterday
            conn.execute("UPDATE ability_grants SET until = ? WHERE id = ?",
                         ((date.today() - timedelta(days=1)).isoformat(), gid))
        self.assertFalse(roles.can(self.office.get_staff("EMP003"), "audit.view"))
        gid = self.office.grant_temporary(self.admin, "EMP003", "audit.view", date.today().isoformat(), "again")
        self.office.revoke_grant(self.admin, gid)
        self.assertFalse(roles.can(self.office.get_staff("EMP003"), "audit.view"))

    def test_temporary_ability_is_not_made_permanent_by_a_save(self):
        self.office.grant_temporary(self.admin, "EMP003", "audit.view", date.today().isoformat(), "cover")
        s = self.office.get_staff("EMP003")
        self.office.set_person_abilities(self.admin, "EMP003", s["_abilities"] - self.office.active_grants("EMP003"))
        self.assertNotIn("audit.view", self.office.get_staff("EMP003")["extra_abilities"])

    def test_copy_abilities_from_a_colleague(self):
        self.office.give_ability(self.admin, "EMP004", "screening")
        self.office.copy_abilities(self.admin, "EMP004", "EMP005")
        five = self.office.get_staff("EMP005")
        self.assertEqual(five["job_role"], "loan_officer")
        self.assertEqual(five["_abilities"], self.office.get_staff("EMP004")["_abilities"])
        with self.assertRaises(BankError):
            self.office.copy_abilities(self.admin, "EMP001", "EMP005")      # nothing to copy from an admin

    def test_matrix_saves_only_the_changed_people_and_keeps_other_groups(self):
        before4 = self.office.get_staff("EMP004")["_abilities"]
        group = roles.ability_groups()[0]
        codes = [c for c, _ in group[1]]
        three = self.office.get_staff("EMP003")["_abilities"]
        form = {"rows": ["EMP003", "EMP004"]}
        for sid, have in (("EMP003", three), ("EMP004", before4)):
            for c in have:
                form[f"m:{sid}:{c}"] = "1"
        form.pop(f"m:EMP003:{codes[0]}", None)                       # take the first ability of the group from EMP003
        form["m:EMP003:" + codes[0]] = None

        class F(dict):
            def getlist(self, k):
                return self.get(k, [])
        n = self.office.save_matrix(self.admin, F({k: v for k, v in form.items() if v is not None}))
        self.assertEqual(n, 1 if codes[0] in three else 0)
        self.assertEqual(self.office.get_staff("EMP004")["_abilities"], before4)

    def test_pages_for_the_administrator(self):
        self.office.create_ability(self.admin, "Night shift", "", "")
        self.as_admin()
        for url in ("/admin/abilities", "/admin/abilities?who=till.own", "/admin/abilities/matrix",
                    "/admin/abilities/matrix?group=Money", "/admin/staff", "/admin/staff/EMP003/access",
                    "/admin/profile/activity"):
            r = self.c.get(url)
            self.assertEqual(r.status_code, 200, url)
        self.assertIn("Night shift", self.c.get("/admin/abilities").get_data(as_text=True))
        self.assertIn("Abilities &amp; details", self.c.get("/admin/staff").get_data(as_text=True))
        r = self.post("/admin/abilities", action="create", label="Read reports", group="Reports",
                      pages="/admin/health", roles="officer")
        self.assertEqual(r.status_code, 302)
        code = next(c for c, label in roles.ABILITIES.items() if label == "Read reports")
        self.assertIn(code, self.office.job_roles()["officer"]["abilities"])
        self.assertEqual(roles.ability_for("/admin/health"), code)

    def test_abilities_page_is_locked_for_staff(self):
        self.staff("EMP003")
        for url in ("/admin/abilities", "/admin/abilities/matrix", "/admin/vault"):
            self.assertEqual(self.c.get(url).status_code, 403, url)

    def test_own_abilities_survive_a_restart(self):
        code = self.office.create_ability(self.admin, "Keep me", "", "", ["/admin/notices"])
        roles.load_custom({}, [])                                     # another worker that has not read them yet
        self.assertNotIn(code, roles.ABILITIES)
        self.office._abilities_version = None                         # a worker that just started
        self.office.refresh_abilities()
        self.assertIn(code, roles.ABILITIES)
        self.assertEqual(roles.ability_for("/admin/notices"), code)

    def test_every_new_admin_page_needs_an_ability(self):
        for path, method in (("/admin/vault", "GET"), ("/admin/cheques", "POST"), ("/admin/credit-cards", "GET"),
                             ("/admin/payment-holds", "GET"), ("/admin/assistant/train", "GET"),
                             ("/admin/abilities", "GET"), ("/admin/abilities/matrix", "POST"),
                             ("/admin/staff/EMP003/abilities", "POST"), ("/admin/data/faqs/export.csv", "GET"),
                             ("/admin/screening/import", "POST"), ("/admin/till/vault", "POST")):
            self.assertIsNotNone(roles.ability_for(path, method), path)


# =========================================================================== vault and cash drawers

class TestVault(V6Base):
    def test_opening_a_drawer_takes_from_the_vault_and_closing_gives_back(self):
        start = next(v["balance"] for v in self.office.vault() if v["currency"] == "INR")
        teller = self.office.get_staff("EMP003")
        self.office.open_till(teller, "INR", 1_00_000 * 100)
        tid = self.office.till("EMP003", "INR")["id"]
        self.assertEqual(next(v["balance"] for v in self.office.vault() if v["currency"] == "INR"), start - 1_00_000 * 100)
        notes, total = count_notes("INR", {"n_500": "150", "n_2000": "12", "n_100": "10"})
        self.assertEqual(total, (75_000 + 24_000 + 1_000) * 100)
        diff = self.office.close_till(teller, tid, total, "", notes)
        self.assertEqual(diff, 0)
        self.assertEqual(next(v["balance"] for v in self.office.vault() if v["currency"] == "INR"), start)
        self.assertIn("500", self.office.till("EMP003", "INR")["notes"])

    def test_note_count_rejects_words(self):
        with self.assertRaises(BankError):
            count_notes("INR", {"n_500": "ten"})

    def test_top_up_needs_a_second_person(self):
        teller = self.office.get_staff("EMP003")
        self.office.open_till(teller, "INR", 10_000 * 100)
        mid = self.office.request_topup(teller, "INR", 5_000 * 100)
        with self.assertRaises(BankError):
            self.office.decide_vault_move(teller, mid, True)        # not the same person
        self.office.decide_vault_move(self.admin, mid, True)
        self.assertEqual(self.office.till("EMP003", "INR")["expected"], 15_000 * 100)
        rid = self.office.request_return(teller, "INR", 3_000 * 100)
        self.office.decide_vault_move(self.admin, rid, True)
        self.assertEqual(self.office.till("EMP003", "INR")["expected"], 12_000 * 100)
        with self.assertRaises(BankError):
            self.office.request_return(teller, "INR", 99_000 * 100)  # more than the drawer has

    def test_cash_van_and_web_pages(self):
        before = next(v["balance"] for v in self.office.vault() if v["currency"] == "USD")
        with self.assertRaises(BankError):
            self.office.head_office_move(self.admin, "USD", 100, True, "")     # needs the seal number
        mid = self.office.head_office_move(self.admin, "USD", 1_000 * 100, True, "Seal 77")
        self.office.decide_vault_move(self.office.get_staff("EMP005"), mid, True)
        self.assertEqual(next(v["balance"] for v in self.office.vault() if v["currency"] == "USD"), before + 100_000)
        self.as_admin()
        self.assertEqual(self.c.get("/admin/vault").status_code, 200)
        self.post("/admin/till", action="open", currency="INR", opening="1000")
        r = self.post("/admin/till", action="close", id=str(self.office.till("EMP001", "INR")["id"]), n_500="2")
        self.assertEqual(r.status_code, 302)
        self.assertEqual(self.office.till("EMP001", "INR")["status"], "Closed")


# =========================================================================== cheques and nominees

class TestCheques(V6Base):
    def deposit(self, no="123456", amount=5_000_00, written=None):
        return self.bank.deposit_cheque(self.cid, self.acc.number, amount, no, "Sunrise Bank", "Kavita Malhotra",
                                        (written or date.today()).isoformat(), "EMP003")

    def test_rules_of_a_cheque(self):
        for args in (("12345",), ("123456", 0)):
            with self.assertRaises(BankError):
                self.deposit(*args)
        with self.assertRaises(BankError):
            self.deposit(written=date.today() + timedelta(days=3))       # post-dated
        with self.assertRaises(BankError):
            self.deposit(written=date.today() - timedelta(days=100))     # out of date
        self.deposit()
        with self.assertRaises(BankError):
            self.deposit()                                               # the same cheque twice

    def test_clearing_credits_next_working_day_and_bounces_000(self):
        before = self.bank.get_account(self.cid).balance
        q = self.deposit()
        self.assertEqual(self.bank.get_account(self.cid).balance, before)  # not yet
        self.assertEqual(self.bank.uncleared_total(self.acc.number), 5_000_00)
        self.assertEqual(self.bank.clear_cheques(), 0)                    # not today
        self.assertEqual(self.bank.clear_cheques(date.fromisoformat(q["clear_on"])), 1)
        self.assertEqual(self.bank.get_account(self.cid).balance, before + 5_000_00)
        self.deposit("777000", 1_000_00)
        self.bank.clear_cheques(date.today() + timedelta(days=3))
        bounced = self.bank.cheques(self.cid, "Returned")[0]
        self.assertEqual(bounced["reason"], "Funds insufficient")
        self.assertEqual(self.bank.get_account(self.cid).balance, before + 5_000_00 - 200_00)   # return charge

    def test_no_clearing_on_sunday(self):
        saturday = date(2026, 11, 28)
        self.assertEqual(next_working_day(saturday), date(2026, 11, 30))

    def test_cheque_at_the_counter_and_pages(self):
        teller = self.office.get_staff("EMP003")
        kind, op_id = self.office.counter(teller, self.cid, "cheque", {
            "account": self.acc.number, "amount": "2500", "cheque_no": "555111", "drawee_bank": "Metro City Bank",
            "drawer_name": "Rohan Verma", "cheque_date": date.today().isoformat()})
        self.assertEqual(kind, "done")
        self.assertIn("555111", self.office.counter_op(op_id)["details"])
        self.as_admin()
        self.assertEqual(self.c.get("/admin/cheques").status_code, 200)
        qid = self.bank.cheques(self.cid)[0]["id"]
        self.post("/admin/cheques", action="return", id=str(qid), reason="Signature differs")
        self.assertEqual(self.bank.cheques(self.cid)[0]["status"], "Returned")
        c2 = self.app.test_client()
        self.customer(c2)
        page = c2.get("/netbanking/cheques").get_data(as_text=True)
        self.assertIn("555111", page)
        self.assertIn("Signature differs", page)

    def test_nominees(self):
        with self.assertRaises(BankError):
            self.bank.save_nominees(self.cid, [{"name": "Meera Rao", "relation": "Sister", "share": 60}])
        with self.assertRaises(BankError):
            self.bank.save_nominees(self.cid, [{"name": f"Person {i} Rao", "relation": "Friend", "share": 25}
                                               for i in range(4)])
        with self.assertRaises(BankError):
            self.bank.save_nominees(self.cid, [{"name": "Meera", "relation": "Sister", "share": 100}])
        self.bank.save_nominees(self.cid, [{"name": "Meera Rao", "relation": "Sister", "share": 70},
                                           {"name": "Ravi Rao", "relation": "Father", "share": "30%"}])
        self.assertEqual([n["share"] for n in self.bank.nominees(self.cid)], [70, 30])
        self.customer()
        r = self.post("/netbanking/nominees", pin="0000", name_0="Meera Rao", relation_0="Sister", share_0="100")
        self.assertIn("PIN", r.get_data(as_text=True))                     # wrong PIN - nothing saved
        self.post("/netbanking/nominees", pin=PIN, name_0="Meera Rao", relation_0="Sister", share_0="100")
        self.assertEqual(len(self.bank.nominees(self.cid)), 1)


# =========================================================================== credit card

class TestCreditCard(V6Base):
    def card(self, limit=50_000_00):
        cid_ = self.bank.apply_credit_card(self.cid, 40_000_00, limit)
        self.bank.decide_credit_card(self.admin, cid_, True, limit)
        return self.bank.credit_card(self.cid)

    def test_apply_rules(self):
        with self.assertRaises(BankError):
            self.bank.apply_credit_card(self.cid, 5_000_00)                  # income too low
        self.bank.apply_credit_card(self.cid, 40_000_00)
        with self.assertRaises(BankError):
            self.bank.apply_credit_card(self.cid, 40_000_00)                 # one at a time
        self.assertEqual(len(self.bank.credit_card_applications()), 1)

    def test_purchase_limit_statement_and_payment(self):
        k = self.card()
        self.assertEqual(self.bank.cc_purchase(k["id"], 60_000_00, "Big shop"), "DECLINED: credit limit reached")
        self.assertEqual(self.bank.cc_purchase(k["id"], 10_000_00, "Shop"), "APPROVED")
        bill_day = date.today().replace(day=5) + timedelta(days=40)
        bill_day = bill_day.replace(day=5)
        self.bank.make_cc_statements(bill_day)
        st = self.bank.cc_statements(k["id"])[0]
        self.assertEqual(st["total"], 10_000_00)
        self.assertEqual(st["minimum"], 500_00)
        before = self.bank.get_account(self.cid).balance
        self.bank.pay_credit_card(self.cid, "minimum", 0, PIN)
        self.assertEqual(self.bank.get_account(self.cid).balance, before - 500_00)
        self.bank.pay_credit_card(self.cid, "full", 0, PIN)
        self.assertEqual(self.bank.credit_card(self.cid)["outstanding"], 0)
        with self.assertRaises(BankError):
            self.bank.pay_credit_card(self.cid, "full", 0, PIN)              # nothing left

    def test_blocked_card_declines(self):
        k = self.card()
        self.bank.block_credit_card(self.cid, True)
        self.assertTrue(self.bank.cc_purchase(k["id"], 100, "x").startswith("DECLINED"))

    def test_pages(self):
        self.customer()
        self.assertEqual(self.c.get("/netbanking/credit-card").status_code, 200)
        self.post("/netbanking/credit-card", action="apply", income="50000")
        self.as_admin()
        page = self.c.get("/admin/credit-cards").get_data(as_text=True)
        self.assertIn("Test User", page)
        k = self.bank.credit_card(self.cid)
        self.post("/admin/credit-cards", action="approve", id=str(k["id"]), limit="40000")
        self.assertEqual(self.bank.credit_card(self.cid)["status"], "Active")
        self.assertEqual(self.bank.credit_card(self.cid)["limit_amount"], 40_000_00)


# =========================================================================== held payments and the list

class TestHeldPayments(V6Base):
    def send(self, name):
        return self.bank.send_external(self.cid, "NEFT", {"name": name, "account": "55019922000", "ifsc": "NOVA0001207"},
                                       2_000_00, PIN)

    def test_payment_to_a_listed_name_is_held_and_can_be_stopped(self):
        before = self.bank.get_account(self.cid).balance
        ref = self.send("Viktor Grauberg")
        hold = self.bank.payment_holds()[0]
        self.assertEqual(hold["ext_ref"], ref)
        self.bank.decide_payment_hold(self.admin, hold["id"], False, "Same person")
        self.assertEqual(self.bank.get_account(self.cid).balance, before)
        self.assertEqual(self.bank.payment_holds("Stopped")[0]["ext_ref"], ref)

    def test_release_sends_the_payment(self):
        self.send("Viktor Grauberg")
        hold = self.bank.payment_holds()[0]
        self.bank.decide_payment_hold(self.admin, hold["id"], True, "Other person, born 1990")
        with self.bank._connect() as conn:
            status = conn.execute("SELECT status FROM external_transfers WHERE ref = ?", (hold["ext_ref"],)).fetchone()[0]
        self.assertNotEqual(status, "Held")

    def test_ordinary_name_is_not_held(self):
        self.send("Kavita Malhotra")
        self.assertEqual(self.bank.payment_holds(), [])

    def test_csv_import(self):
        n, added = self.bank.import_sanctions_csv(self.admin, "name,aliases,country,dob,program\n"
                                                              "Zed Q Example,,XX,,Test\nZed Q Example,,XX,,Test\n")
        self.assertEqual((n, added), (2, 1))
        self.assertTrue(self.bank.screen_name("Zed Example"))
        self.as_admin()
        r = self.c.post("/admin/screening/import", data={"csrf_token": "test-token",
                                                         "file": (io.BytesIO(b"name,program\nOmar Testcase,T\n"), "l.csv")})
        self.assertEqual(r.status_code, 302)
        self.assertTrue(any(e["name"] == "Omar Testcase" for e in self.bank.sanctions()))
        self.assertEqual(self.c.get("/admin/payment-holds").status_code, 200)
        self.assertEqual(self.c.get("/admin/screening/template.csv").status_code, 200)


# =========================================================================== chat assistant

class TestChat(V6Base):
    def test_public_chat_remembers_and_clears(self):
        self.assertIn("Hello", self.chat("hello")["text"])
        r = self.chat("what is the FD rate?")
        self.assertTrue(r["text"])
        hist = self.c.get("/chat/history").get_json()
        self.assertEqual([m["role"] for m in hist], ["user", "bot", "user", "bot"])
        self.c.post("/chat/feedback", json={"id": r["id"], "value": -1}, headers={"X-CSRF-Token": "test-token"})
        self.assertEqual(self.c.get("/chat/history").get_json()[-1]["feedback"], -1)
        self.c.post("/chat/clear", headers={"X-CSRF-Token": "test-token"})
        self.assertEqual(self.c.get("/chat/history").get_json(), [])

    def test_two_visitors_do_not_see_each_other(self):
        self.chat("hello")
        other = self.app.test_client()
        self.assertEqual(other.get("/chat/history").get_json(), [])

    def test_customer_sends_money_after_pin(self):
        self.customer()
        before = self.bank.get_account(self.other).balance
        r = self.chat(f"send 1500 to {self.other_acc.number}")
        self.assertEqual(r["confirm"]["action"], "transfer")
        self.assertTrue(r["confirm"]["pin"])
        bad = self.chat_confirm(r["id"], pin="0000")
        self.assertEqual(bad.status_code, 400)
        self.assertEqual(self.bank.get_account(self.other).balance, before)
        ok = self.chat_confirm(r["id"], pin=PIN)
        self.assertEqual(ok.status_code, 200)
        self.assertIn("Done", ok.get_json()["text"])
        self.assertEqual(self.bank.get_account(self.other).balance, before + 1_500_00)
        again = self.chat_confirm(r["id"], pin=PIN)                         # the same button twice
        self.assertEqual(again.status_code, 400)
        self.assertEqual(self.bank.get_account(self.other).balance, before + 1_500_00)

    def test_cancel_and_yes(self):
        self.customer()
        r = self.chat("block my card")
        if r.get("confirm") is None:                                         # account without a debit card
            return
        self.assertIn("All right", self.chat("no")["text"])
        self.chat("block my card")
        done = self.chat("yes")
        self.assertIn("blocked", done["text"])
        self.assertEqual(self.bank.card(self.cid)["status"], "Blocked")

    def test_confirm_of_another_person_is_refused(self):
        self.customer()
        r = self.chat("I need a cheque book")
        other = self.app.test_client()
        self.csrf(other)
        res = other.post("/chat/confirm", json={"id": r["id"]}, headers={"X-CSRF-Token": "test-token"})
        self.assertEqual(res.status_code, 403)
        ok = self.chat_confirm(r["id"])
        self.assertEqual(ok.status_code, 200)
        self.assertEqual(self.bank.service_requests(self.cid)[0]["type"], "Cheque book")

    def test_follow_up_question(self):
        self.customer()
        self.chat("how much did I spend on food this month?")
        r = self.chat("and last month?")
        self.assertNotEqual(r.get("kind"), "unknown")

    def test_trained_answer_comes_first(self):
        from chatbot import ChatBot
        ChatBot(self.bank, self.office).save_answer(self.admin, {"question": "Do you have lockers?",
                                                                 "keywords": "locker, safe deposit", "answer": "Yes, 40 lockers."})
        self.assertIn("40 lockers", self.chat("is there a safe deposit locker?")["text"])

    def test_staff_chat_respects_abilities(self):
        self.staff("EMP002")                                                # web editor: no account status ability
        r = self.chat(f"freeze {self.cid}", "/admin/chat")
        if r.get("confirm"):
            res = self.chat_confirm(r["id"], "/admin/chat/confirm", reason="test")
            self.assertEqual(res.status_code, 400)
        self.assertEqual(self.bank.get_customer(self.cid).status, "Active")
        self.assertEqual(self.c.get("/admin/chat/history").status_code, 200)

    def test_admin_freezes_from_the_chat(self):
        self.as_admin()
        r = self.chat(f"freeze {self.cid}", "/admin/chat")
        self.assertEqual(r["confirm"]["action"], "freeze")
        res = self.chat_confirm(r["id"], "/admin/chat/confirm", reason="Customer asked by phone")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(self.bank.get_account(self.cid).status, "Frozen")

    def test_training_page(self):
        self.chat("qwzx plorb")
        self.as_admin()
        page = self.c.get("/admin/assistant/train").get_data(as_text=True)
        self.assertIn("qwzx plorb", page)
        self.post("/admin/assistant/train", question="Branch timing?", answer="10 to 4.", keywords="hours")
        self.assertIn("Branch timing?", self.c.get("/admin/assistant/train").get_data(as_text=True))

    def test_old_assistant_address_still_answers(self):
        r = self.c.post("/assistant", json={"q": "FD rates"}, headers={"X-CSRF-Token": "test-token"})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.get_json()["text"])


# =========================================================================== data manager v2

class TestDataManager(V6Base):
    def test_read_only_tables_sort_and_pages(self):
        self.as_admin()
        page = self.c.get("/admin/data").get_data(as_text=True)
        self.assertIn("Transactions", page)
        r = self.c.get("/admin/data/transactions?sort=amount&desc=1")
        self.assertEqual(r.status_code, 200)
        self.assertNotIn("Add a record", r.get_data(as_text=True))
        self.assertEqual(self.post("/admin/data/transactions", amount="1").status_code, 405)
        self.assertEqual(self.c.get("/admin/data/sessions").status_code, 404)      # secrets are never shown
        body = self.c.get("/admin/data/customers").get_data(as_text=True)
        self.assertNotIn("password_hash", body)
        self.assertNotIn("pin_hash", body)

    def test_csv_round_trip_and_delete_many(self):
        self.as_admin()
        for i in range(3):
            self.post("/admin/data/faqs", question=f"Question {i}?", answer=f"Answer {i}.", lang="en", position=str(i),
                      active="1")
        csv_text = self.c.get("/admin/data/faqs/export.csv").get_data(as_text=True).lstrip("﻿")
        self.assertIn("Question 1?", csv_text)
        changed = csv_text.replace("Answer 1.", "Better answer 1.")
        r = self.c.post("/admin/data/faqs/import", data={"csrf_token": "test-token",
                                                         "file": (io.BytesIO(changed.encode()), "faqs.csv")})
        self.assertEqual(r.status_code, 302)
        faqs = {f["question"]: f for f in self.bank.faqs(active_only=False)}
        self.assertEqual(faqs["Question 1?"]["answer"], "Better answer 1.")
        keys = [str(f["id"]) for q, f in faqs.items() if q.startswith("Question")]
        self.post("/admin/data/faqs/delete", keys=keys[:2])
        left = [f for f in self.bank.faqs(active_only=False) if f["question"].startswith("Question")]
        self.assertEqual(len(left), 1)

    def test_new_editable_tables(self):
        self.as_admin()
        for t in ("site_texts", "site_pages", "products", "banners", "job_roles", "custom_abilities", "nominees",
                  "bot_answers"):
            self.assertEqual(self.c.get(f"/admin/data/{t}").status_code, 200, t)


# =========================================================================== own details

class TestOwnDetails(V6Base):
    def test_admin_changes_his_own_branch_and_sees_his_activity(self):
        self.as_admin()
        self.post("/admin/profile", form="details", full_name="Main Admin", email="admin@test.example", phone="",
                  designation="Manager", branch="Mullana Branch")
        self.assertEqual(self.office.get_staff("EMP001")["branch"], "Mullana Branch")
        self.assertIn("Own details changed", self.c.get("/admin/profile/activity").get_data(as_text=True))

    def test_staff_cannot_change_own_branch(self):
        self.staff("EMP003")
        self.post("/admin/profile", form="details", full_name="Cash Teller", email="emp003@test.example", phone="",
                  designation="teller", branch="Elsewhere")
        self.assertNotEqual(self.office.get_staff("EMP003").get("branch"), "Elsewhere")


if __name__ == "__main__":
    unittest.main()

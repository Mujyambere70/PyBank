"""
cards.py - the customer controls his own debit card (version 4).

Every main account has a debit card (only the last 4 digits are kept - PyBank never stores a full
card number). In Net Banking -> My Card the customer can:

    * block the card for a while (lost in the house?) and unblock it again;
    * switch online (internet) payments, payments abroad and contactless payments on or off;
    * set his own daily limits for ATM cash and for shop (POS) payments, up to the bank's maximum;
    * report the card lost or stolen: it is blocked for ever and a new card with a new number is ordered
      (the replacement fee from the list of charges is taken).

`card_payment()` is the sandbox "card network": it answers a payment request with APPROVED or a reason
for DECLINED, using exactly these controls, the limits and the balance. Bank staff use it in the
back office to test a card (there is no real card network).
"""

import random
from datetime import date

import world
from core import BankError, now
from world import format_money

CARD_SCHEMA = """
CREATE TABLE IF NOT EXISTS cards (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    account_number TEXT NOT NULL,
    customer_id    TEXT NOT NULL,
    last4          TEXT NOT NULL,
    status         TEXT NOT NULL DEFAULT 'Active',
    online         INTEGER NOT NULL DEFAULT 1,
    abroad         INTEGER NOT NULL DEFAULT 0,
    contactless    INTEGER NOT NULL DEFAULT 1,
    atm_limit      INTEGER NOT NULL DEFAULT 0,
    pos_limit      INTEGER NOT NULL DEFAULT 0,
    expires        TEXT NOT NULL,
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS card_events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    card_id    INTEGER NOT NULL,
    at         TEXT NOT NULL,
    channel    TEXT NOT NULL,
    merchant   TEXT NOT NULL,
    country    TEXT NOT NULL,
    amount     INTEGER NOT NULL,
    result     TEXT NOT NULL,
    tx_ref     TEXT NOT NULL DEFAULT ''
);
"""
CHANNELS = {"pos": "Shop (POS)", "online": "Online", "atm": "ATM cash", "contactless": "Contactless (tap)"}
CARD_STATUSES = ["Active", "Blocked", "Lost", "Replaced"]
REPLACEMENT_FEE = {"INR": 150, "RUB": 300, "USD": 5, "EUR": 5, "GBP": 5, "CNY": 30, "AED": 20, "RWF": 5_000}


def max_atm(currency):
    return world.policy(currency, "withdraw")


def max_pos(currency):
    return world.policy(currency, "transfer")


class CardsMixin:

    def card(self, cid):
        """The debit card of the customer's main account (made on first use from accounts.card_last4)."""
        main = self.get_account(cid)
        with self._connect() as conn:
            r = conn.execute("SELECT * FROM cards WHERE account_number = ? AND status IN ('Active', 'Blocked') "
                             "ORDER BY id DESC LIMIT 1", (main.number,)).fetchone()
            if r is None:
                if not main.card_last4:
                    return None
                opened = main.opened_at[:10]
                expires = f"{int(opened[:4]) + 5}-{opened[5:7]}"
                conn.execute("INSERT INTO cards (account_number, customer_id, last4, atm_limit, pos_limit, expires, "
                             "created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                             (main.number, cid, main.card_last4, max_atm(main.currency) // 2,
                              max_pos(main.currency) // 2, expires, now()))
                r = conn.execute("SELECT * FROM cards WHERE account_number = ? ORDER BY id DESC LIMIT 1",
                                 (main.number,)).fetchone()
        card = dict(r)
        card["currency"] = main.currency
        card["max_atm"], card["max_pos"] = max_atm(main.currency), max_pos(main.currency)
        return card

    def set_card_controls(self, cid, form):
        card = self.card(cid)
        if card is None:
            raise BankError("This account has no debit card.")
        cur = card["currency"]
        try:
            atm = int(round(float(str(form.get("atm_limit", "0")).replace(",", "")) * 100))
            pos = int(round(float(str(form.get("pos_limit", "0")).replace(",", "")) * 100))
        except ValueError:
            raise BankError("Please enter the limits as numbers.")
        if not 0 <= atm <= card["max_atm"]:
            raise BankError(f"The ATM limit can be from 0 to {format_money(card['max_atm'], cur)} a day.")
        if not 0 <= pos <= card["max_pos"]:
            raise BankError(f"The shop and online limit can be from 0 to {format_money(card['max_pos'], cur)} a day.")
        with self._connect() as conn:
            conn.execute("UPDATE cards SET online = ?, abroad = ?, contactless = ?, atm_limit = ?, pos_limit = ?, "
                         "updated_at = ? WHERE id = ?",
                         (int(bool(form.get("online"))), int(bool(form.get("abroad"))),
                          int(bool(form.get("contactless"))), atm, pos, now(), card["id"]))
        return self.card(cid)

    def block_card(self, cid, block=True):
        card = self.card(cid)
        if card is None:
            raise BankError("This account has no debit card.")
        with self._connect() as conn:
            conn.execute("UPDATE cards SET status = ?, updated_at = ? WHERE id = ?",
                         ("Blocked" if block else "Active", now(), card["id"]))
            self._alert(conn, cid, "Debit card blocked" if block else "Debit card unblocked",
                        f"Your debit card XX{card['last4']} was {'blocked' if block else 'unblocked'} in Net Banking. "
                        f"Not you? Call the fraud line at once.")

    def report_card_lost(self, cid, pin):
        """Block the card for ever and order a new one (with a new number). Returns the new card."""
        self._check_pin(cid, pin)
        card = self.card(cid)
        if card is None:
            raise BankError("This account has no debit card.")
        main = self.get_account(cid)
        fee = REPLACEMENT_FEE.get(main.currency, 0) * 100
        today = date.today()
        with self._connect() as conn:
            conn.execute("UPDATE cards SET status = 'Lost', updated_at = ? WHERE id = ?", (now(), card["id"]))
            last4 = f"{random.randint(0, 9999):04d}"
            while last4 == card["last4"]:
                last4 = f"{random.randint(0, 9999):04d}"
            conn.execute("UPDATE accounts SET card_last4 = ? WHERE number = ?", (last4, main.number))
            conn.execute("INSERT INTO cards (account_number, customer_id, last4, online, abroad, contactless, atm_limit, "
                         "pos_limit, expires, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                         (main.number, cid, last4, card["online"], card["abroad"], card["contactless"],
                          card["atm_limit"], card["pos_limit"], f"{today.year + 5}-{today.month:02d}", now()))
            acc = self._load(conn, main.number)
            if fee and acc.balance - self._reserved(conn, acc.number) >= fee:
                self._save(conn, acc.debit(fee, "Charges", f"REPLACEMENT DEBIT CARD XX{last4}"))
            self._alert(conn, cid, "New debit card ordered",
                        f"Your card XX{card['last4']} is blocked for ever. A new card XX{last4} is on its way to your "
                        f"address.")
        return self.card(cid)

    def card_events(self, cid, limit=20):
        card = self.card(cid)
        if not card:
            return []
        with self._connect() as conn:
            return [dict(r) for r in conn.execute(
                "SELECT e.* FROM card_events e JOIN cards c ON c.id = e.card_id WHERE c.account_number = ? "
                "ORDER BY e.id DESC LIMIT ?", (self.get_account(cid).number, limit))]

    # ----- the sandbox card network -----

    def card_payment(self, last4, account_number, amount, channel, merchant, country="IN", when=None):
        """Ask the bank to approve a card payment. Returns (result, transaction or None).
        result is 'APPROVED' or 'DECLINED: <reason>'."""
        if channel not in CHANNELS:
            raise BankError("Unknown payment channel.")
        merchant = " ".join(str(merchant or "").split())[:40] or "Test shop"
        country = (country or "IN").upper()
        with self._connect() as conn:
            r = conn.execute("SELECT * FROM cards WHERE account_number = ? AND last4 = ? ORDER BY id DESC LIMIT 1",
                             (account_number, last4)).fetchone()
            if r is None:
                raise BankError("Card not found.")
            card = dict(r)
            acc = self._load(conn, account_number, must_be_active=False)
            home = conn.execute("SELECT country FROM customers WHERE customer_id = ?", (acc.customer_id,)).fetchone()[0]
            today = date.today().isoformat()
            used = conn.execute("SELECT COALESCE(SUM(amount), 0) FROM card_events WHERE card_id = ? AND at >= ? "
                                "AND result = 'APPROVED' AND channel " + ("= 'atm'" if channel == "atm" else "!= 'atm'"),
                                (card["id"], today)).fetchone()[0]
            limit = card["atm_limit"] if channel == "atm" else card["pos_limit"]
            reason = None
            if card["status"] != "Active":
                reason = f"card is {card['status'].lower()}"
            elif card["expires"] < today[:7]:
                reason = "card has expired"
            elif acc.status != "Active":
                reason = "account is not active"
            elif channel == "online" and not card["online"]:
                reason = "online payments are switched off"
            elif channel == "contactless" and not card["contactless"]:
                reason = "contactless payments are switched off"
            elif country != home and not card["abroad"]:
                reason = "payments abroad are switched off"
            elif used + amount > limit:
                reason = f"daily {'ATM' if channel == 'atm' else 'card'} limit reached"
            elif amount > acc.balance - self._reserved(conn, acc.number):
                reason = "not enough money"
            t = None
            if reason is None:
                category = "Withdrawal" if channel == "atm" else "Card payment"
                t = acc.debit(amount, category, f"CARD XX{last4}/{CHANNELS[channel].upper()}/{merchant}/{country}", when)
                self._save(conn, t)
            result = "APPROVED" if reason is None else f"DECLINED: {reason}"
            conn.execute("INSERT INTO card_events (card_id, at, channel, merchant, country, amount, result, tx_ref) "
                         "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                         (card["id"], when or now(), channel, merchant, country, amount,
                          "APPROVED" if reason is None else reason, t.ref if t else ""))
            if reason and reason not in ("not enough money",):
                self._alert(conn, acc.customer_id, f"Card payment declined: {reason}",
                            f"A {CHANNELS[channel].lower()} payment of {format_money(amount, acc.currency, 'en')} at "
                            f"{merchant} ({country}) with card XX{last4} was declined: {reason}. "
                            f"If it was you, you can change the card controls in Net Banking.", only_in_app=True)
        return result, t

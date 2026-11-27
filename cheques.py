"""
cheques.py - cheque deposits with clearing, and nominees (version 6).

Cheques. A customer brings a cheque written by somebody else to the counter. The teller types the
cheque number, the bank it is drawn on, the name of the writer (drawer) and the date written on it.
The money is NOT credited at once: the cheque goes to clearing and the amount is credited on the next
working day, when the other bank pays. In this sandbox a cheque whose number ends in 000 "bounces"
(the writer has no money): it is returned with a reason and the bank's return charge is taken.
Staff with the ability "cheques.clear" can also clear or return a cheque by hand.

    a cheque is valid for 3 months from its date; a post-dated cheque is not accepted

Nominees. A customer names up to three people who receive the money if he dies, with a share
for each (the shares add up to 100 %). Staff see them on the customer page.
"""

import random
import re
from datetime import date, timedelta

from core import BankError, now
from world import format_money

CHEQUE_SCHEMA = """
CREATE TABLE IF NOT EXISTS cheques (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    ref            TEXT NOT NULL UNIQUE,
    customer_id    TEXT NOT NULL,
    account_number TEXT NOT NULL,
    amount         INTEGER NOT NULL,
    currency       TEXT NOT NULL,
    cheque_no      TEXT NOT NULL,
    drawee_bank    TEXT NOT NULL,
    drawer_name    TEXT NOT NULL,
    cheque_date    TEXT NOT NULL,
    status         TEXT NOT NULL DEFAULT 'In clearing',
    clear_on       TEXT NOT NULL,
    reason         TEXT NOT NULL DEFAULT '',
    deposited_by   TEXT NOT NULL DEFAULT '',
    decided_by     TEXT NOT NULL DEFAULT '',
    decided_at     TEXT NOT NULL DEFAULT '',
    tx_ref         TEXT NOT NULL DEFAULT '',
    created_at     TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS nominees (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    customer_id TEXT NOT NULL,
    name        TEXT NOT NULL,
    relation    TEXT NOT NULL,
    dob         TEXT NOT NULL DEFAULT '',
    share       INTEGER NOT NULL DEFAULT 100,
    address     TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL
);
"""
CHEQUE_STATUSES = ["In clearing", "Cleared", "Returned"]
RETURN_REASONS = ["Funds insufficient", "Signature differs", "Cheque out of date", "Amount in words and figures differ",
                  "Payment stopped by the drawer", "Account closed"]
RELATIONS = ["Spouse", "Son", "Daughter", "Father", "Mother", "Brother", "Sister", "Other relative", "Friend"]
MAX_NOMINEES = 3
CHEQUE_VALID_DAYS = 90


def next_working_day(d):
    d = d + timedelta(days=1)
    while d.weekday() == 6:                   # no clearing on Sunday
        d += timedelta(days=1)
    return d


class ChequesMixin:
    """Mixed into Bank."""

    # ===== cheques =====

    def deposit_cheque(self, cid, account_number, amount, cheque_no, drawee_bank, drawer_name, cheque_date,
                       staff_id="", today=None):
        today = today or date.today()
        acc = self.account_of(cid, account_number)
        cheque_no = (cheque_no or "").strip()
        drawee_bank = " ".join((drawee_bank or "").split())[:60]
        drawer_name = " ".join((drawer_name or "").split())[:60]
        if not re.fullmatch(r"\d{6}", cheque_no):
            raise BankError("The cheque number has 6 digits (bottom left of the cheque).")
        if len(drawee_bank) < 3:
            raise BankError("Please write the bank the cheque is drawn on.")
        if len(drawer_name.split()) < 2:
            raise BankError("Please write the full name of the person who wrote the cheque.")
        if amount <= 0:
            raise BankError("Please enter the amount of the cheque.")
        try:
            written = date.fromisoformat((cheque_date or "").strip())
        except ValueError:
            raise BankError("Please write the date on the cheque (YYYY-MM-DD).")
        if written > today:
            raise BankError("This cheque is post-dated: it can be deposited from " + written.isoformat() + ".")
        if (today - written).days > CHEQUE_VALID_DAYS:
            raise BankError("This cheque is out of date (older than 3 months).")
        clear_on = next_working_day(today)
        with self._connect() as conn:
            if conn.execute("SELECT 1 FROM cheques WHERE cheque_no = ? AND LOWER(drawee_bank) = ? AND status <> 'Returned'",
                            (cheque_no, drawee_bank.lower())).fetchone():
                raise BankError("This cheque was already deposited.")
            ref = self._unique(conn, "SELECT 1 FROM cheques WHERE ref = ?",
                               lambda: f"CQ{today:%y%m%d}{random.randint(0, 99_999):05d}")
            conn.execute("INSERT INTO cheques (ref, customer_id, account_number, amount, currency, cheque_no, drawee_bank, "
                         "drawer_name, cheque_date, clear_on, deposited_by, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, "
                         "?, ?, ?)", (ref, cid, acc.number, amount, acc.currency, cheque_no, drawee_bank, drawer_name,
                                      written.isoformat(), clear_on.isoformat(), staff_id, now()))
            self._alert(conn, cid, "Cheque received for clearing",
                        f"We received cheque {cheque_no} of {format_money(amount, acc.currency, 'en')} from {drawer_name}. "
                        f"It will be credited after clearing, on {clear_on:%d %b %Y}.", only_in_app=True)
        return {"ref": ref, "cheque_no": cheque_no, "drawer_name": drawer_name, "drawee_bank": drawee_bank,
                "clear_on": clear_on.isoformat(), "amount": amount}

    def cheques(self, cid=None, status="", limit=100):
        sql = ("SELECT q.*, c.full_name FROM cheques q LEFT JOIN customers c ON c.customer_id = q.customer_id "
               "WHERE 1 = 1")
        args = []
        if cid:
            sql += " AND q.customer_id = ?"
            args.append(cid)
        if status:
            sql += " AND q.status = ?"
            args.append(status)
        with self._connect() as conn:
            return [dict(r) for r in conn.execute(sql + f" ORDER BY q.id DESC LIMIT {int(limit)}", args)]

    def uncleared_total(self, number):
        with self._connect() as conn:
            r = conn.execute("SELECT COALESCE(SUM(amount), 0) FROM cheques WHERE account_number = ? "
                             "AND status = 'In clearing'", (number,)).fetchone()
        return r[0]

    def decide_cheque(self, cheque_id, clear, by="system", reason=""):
        """Clear (credit the money) or return (charge the return fee) one cheque."""
        with self._connect() as conn:
            q = conn.execute("SELECT * FROM cheques WHERE id = ?", (cheque_id,)).fetchone()
            if q is None or q["status"] != "In clearing":
                raise BankError("This cheque is not in clearing.")
            acc = self._load(conn, q["account_number"], must_be_active=False)
            if clear:
                t = acc.credit(q["amount"], "Deposit", f"CHEQUE CLEARED/{q['cheque_no']}/{q['drawer_name'].upper()}"[:100])
                self._save(conn, t)
                conn.execute("UPDATE cheques SET status = 'Cleared', decided_by = ?, decided_at = ?, tx_ref = ? WHERE id = ?",
                             (by, now(), t.ref, cheque_id))
                self._alert(conn, q["customer_id"], "Cheque cleared",
                            f"Cheque {q['cheque_no']} from {q['drawer_name']} cleared: "
                            f"{format_money(q['amount'], q['currency'], 'en')} was credited.", only_in_app=True)
                return "Cleared"
            reason = reason if reason in RETURN_REASONS else "Funds insufficient"
            fee = int(float(self.settings().get("cheque_return_fee", "200")) * 100)
            ref = ""
            if fee and acc.balance >= fee:
                t = acc.debit(fee, "Charges", f"CHEQUE RETURN CHARGE/{q['cheque_no']}/{reason.upper()}"[:100])
                self._save(conn, t)
                ref = t.ref
            conn.execute("UPDATE cheques SET status = 'Returned', reason = ?, decided_by = ?, decided_at = ?, tx_ref = ? "
                         "WHERE id = ?", (reason, by, now(), ref, cheque_id))
            self._alert(conn, q["customer_id"], "Cheque returned",
                        f"Cheque {q['cheque_no']} from {q['drawer_name']} was returned unpaid: {reason}."
                        + (f" A return charge of {format_money(fee, q['currency'], 'en')} was taken." if ref else ""))
            return "Returned"

    def clear_cheques(self, today=None):
        """Daily: cheques whose clearing day has come are paid (sandbox: a number ending in 000 bounces)."""
        today = (today or date.today()).isoformat()
        with self._connect() as conn:
            due = [dict(r) for r in conn.execute("SELECT id, cheque_no FROM cheques WHERE status = 'In clearing' "
                                                 "AND clear_on <= ?", (today,))]
        done = 0
        for q in due:
            try:
                self.decide_cheque(q["id"], not q["cheque_no"].endswith("000"), "clearing")
                done += 1
            except BankError:
                pass
        return done

    # ===== nominees =====

    def nominees(self, cid):
        with self._connect() as conn:
            return [dict(r) for r in conn.execute("SELECT * FROM nominees WHERE customer_id = ? ORDER BY share DESC, id",
                                                  (cid,))]

    def save_nominees(self, cid, rows):
        """rows: [{"name", "relation", "dob", "share", "address"}] - replaces the list; shares must add up to 100."""
        clean = []
        for r in rows:
            name = " ".join(str(r.get("name") or "").split())[:60]
            if not name:
                continue
            if len(name.split()) < 2:
                raise BankError("Please write the nominee's full name.")
            relation = r.get("relation") or ""
            if relation not in RELATIONS:
                raise BankError("Please choose how the nominee is related to you.")
            dob = (r.get("dob") or "").strip()
            if dob:
                try:
                    if date.fromisoformat(dob) >= date.today():
                        raise ValueError
                except ValueError:
                    raise BankError("Please give the nominee's date of birth (YYYY-MM-DD).")
            try:
                share = int(str(r.get("share") or "0").strip().rstrip("%"))
            except ValueError:
                raise BankError("The share must be a whole number of per cent.")
            if not 1 <= share <= 100:
                raise BankError("Each share is between 1 and 100 %.")
            clean.append((name, relation, dob, share, " ".join(str(r.get("address") or "").split())[:200]))
        if len(clean) > MAX_NOMINEES:
            raise BankError(f"You can name at most {MAX_NOMINEES} nominees.")
        if clean and sum(c[3] for c in clean) != 100:
            raise BankError("The shares of the nominees must add up to 100 %.")
        with self._connect() as conn:
            conn.execute("DELETE FROM nominees WHERE customer_id = ?", (cid,))
            for name, relation, dob, share, address in clean:
                conn.execute("INSERT INTO nominees (customer_id, name, relation, dob, share, address, created_at) "
                             "VALUES (?, ?, ?, ?, ?, ?, ?)", (cid, name, relation, dob, share, address, now()))
            self._alert(conn, cid, "Nominees updated",
                        "The nominees of your account were changed: " + (", ".join(f"{c[0]} ({c[3]}%)" for c in clean)
                                                                          or "none") + ". Not you? Call the bank at once.")
        return len(clean)

"""
screening.py - sanctions screening and a risk rating for every customer (version 5).

Sanctions screening: banks must not serve people on sanctions lists. PyBank keeps its own list
(table `sanctions_list`; the demo list has only MADE-UP names) that compliance staff can change.
Every new customer, every change of name and - once a day - every customer is compared with it.
Names are compared without accents, case and word order, and with small typos allowed
(similarity of the letters >= 0.85 = "possible match"). A possible match is not an accusation: a
compliance officer looks at it and marks it "False positive" or "Confirmed" (a confirmed match
freezes the account).

Risk rating (Low / Medium / High) - points from what the bank knows:
    country with higher risk (setting "high_risk_countries")     +3
    politically exposed person (PEP, ticked by staff)            +3
    open suspicious-activity flags                               +2 each (max 4)
    possible sanctions match not yet cleared                     +5
    cash in the last 90 days above 10 x the AML amount          +2
    account younger than 90 days                                 +1
  0-2 Low, 3-4 Medium, 5+ High. A compliance officer can set the rating himself, with a reason.
"""

from datetime import date, timedelta
from difflib import SequenceMatcher

from compliance import aml_amount
from core import BankError, now
from nameenquiry import plain

SCREEN_SCHEMA = """
CREATE TABLE IF NOT EXISTS sanctions_list (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    name      TEXT NOT NULL,
    aliases   TEXT NOT NULL DEFAULT '',
    country   TEXT NOT NULL DEFAULT '',
    dob       TEXT NOT NULL DEFAULT '',
    program   TEXT NOT NULL DEFAULT '',
    active    INTEGER NOT NULL DEFAULT 1,
    added_by  TEXT NOT NULL DEFAULT '',
    added_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS screening_hits (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    customer_id TEXT NOT NULL,
    entry_id    INTEGER NOT NULL,
    score       REAL NOT NULL,
    status      TEXT NOT NULL DEFAULT 'Open',
    note        TEXT NOT NULL DEFAULT '',
    checked_by  TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL,
    checked_at  TEXT NOT NULL DEFAULT ''
);
"""
CUSTOMER_COLUMNS = [("risk_rating", "TEXT NOT NULL DEFAULT 'Low'"), ("risk_points", "INTEGER NOT NULL DEFAULT 0"),
                    ("risk_reasons", "TEXT NOT NULL DEFAULT ''"), ("risk_manual", "INTEGER NOT NULL DEFAULT 0"),
                    ("pep", "INTEGER NOT NULL DEFAULT 0")]
HIT_STATUSES = ["Open", "False positive", "Confirmed"]
RATINGS = ["Low", "Medium", "High"]
MATCH_SCORE = 0.85
DEMO_LIST = [  # made-up names only - no real person
    ("Viktor Grauberg", "Victor Grauberg; V. Grauberg", "XX", "1971-03-14", "Demo list - fraud ring"),
    ("Selim Ravanor", "Salim Ravanor", "XX", "1965-11-02", "Demo list - arms trade"),
    ("Marta Oskolenko", "", "XX", "1980-07-21", "Demo list - money laundering"),
    ("Kofi Mensa", "", "XX", "1950-01-01", "Demo list - same-name test entry"),
    ("Northwind Shell Trading", "Northwind Trading LLC", "XX", "", "Demo list - company"),
]


def name_score(a, b):
    """0..1 - how alike two names are (no accents, no case, any word order, small typos)."""
    pa, pb = plain(a), plain(b)
    if not pa or not pb:
        return 0.0
    if pa == pb:
        return 1.0
    sa, sb = " ".join(sorted(pa.split())), " ".join(sorted(pb.split()))
    return max(SequenceMatcher(None, pa, pb).ratio(), SequenceMatcher(None, sa, sb).ratio())


class ScreeningMixin:
    """Mixed into Bank."""

    @staticmethod
    def _audit(conn, by, action, target="", details=""):
        conn.execute("INSERT INTO audit_log (staff_id, action, target, details, at) VALUES (?, ?, ?, ?, ?)",
                     (by["staff_id"], action, target, details[:300], now()))

    def ensure_sanctions_list(self):
        with self._connect() as conn:
            if conn.execute("SELECT COUNT(*) FROM sanctions_list").fetchone()[0]:
                return
            for name, aliases, country, dob, program in DEMO_LIST:
                conn.execute("INSERT INTO sanctions_list (name, aliases, country, dob, program, added_by, added_at) "
                             "VALUES (?, ?, ?, ?, ?, 'system', ?)", (name, aliases, country, dob, program, now()))

    def sanctions(self, q=""):
        with self._connect() as conn:
            rows = [dict(r) for r in conn.execute("SELECT * FROM sanctions_list ORDER BY active DESC, name")]
        if q:
            rows = [r for r in rows if name_score(q, r["name"]) >= 0.6 or plain(q) in plain(r["name"] + " " + r["aliases"])]
        return rows

    def save_sanction(self, by, form, entry_id=None):
        name = " ".join((form.get("name") or "").split())[:100]
        if len(name) < 3:
            raise BankError("Please write the name on the list.")
        vals = (name, " ".join((form.get("aliases") or "").split())[:300], (form.get("country") or "").upper()[:2],
                (form.get("dob") or "")[:10], " ".join((form.get("program") or "").split())[:100],
                1 if form.get("active", "1") == "1" else 0)
        with self._connect() as conn:
            if entry_id:
                conn.execute("UPDATE sanctions_list SET name = ?, aliases = ?, country = ?, dob = ?, program = ?, active = ? "
                             "WHERE id = ?", (*vals, entry_id))
            else:
                entry_id = conn.execute("INSERT INTO sanctions_list (name, aliases, country, dob, program, active, "
                                        "added_by, added_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                                        (*vals, by["staff_id"], now())).lastrowid
            self._audit(conn, by, "Sanctions list changed", str(entry_id), name)
        return entry_id

    def screen_name(self, name, dob=""):
        """[(entry, score)] for the list entries that look like this name."""
        out = []
        for e in self.sanctions():
            if not e["active"]:
                continue
            score = max([name_score(name, e["name"])] +
                        [name_score(name, a) for a in e["aliases"].split(";") if a.strip()])
            if dob and e["dob"] and dob == e["dob"]:
                score = min(1.0, score + 0.05)
            if score >= MATCH_SCORE:
                out.append((e, round(score, 3)))
        return sorted(out, key=lambda x: -x[1])

    def screen_customer(self, cid):
        """Compare one customer with the list; new possible matches become hits. Returns the hits."""
        c = self.get_customer(cid)
        found = self.screen_name(c.full_name, c.dob)
        with self._connect() as conn:
            for e, score in found:
                if not conn.execute("SELECT 1 FROM screening_hits WHERE customer_id = ? AND entry_id = ?",
                                    (cid, e["id"])).fetchone():
                    conn.execute("INSERT INTO screening_hits (customer_id, entry_id, score, created_at) VALUES (?, ?, ?, ?)",
                                 (cid, e["id"], score, now()))
        self.update_risk(cid)
        return found

    def screen_all(self):
        with self._connect() as conn:
            ids = [r[0] for r in conn.execute("SELECT customer_id FROM customers")]
        return sum(len(self.screen_customer(cid)) for cid in ids)

    def screening_hits(self, status="Open", cid=None):
        sql = ("SELECT h.*, c.full_name, c.dob, c.country, s.name AS list_name, s.program, s.dob AS list_dob "
               "FROM screening_hits h JOIN customers c ON c.customer_id = h.customer_id "
               "JOIN sanctions_list s ON s.id = h.entry_id WHERE 1 = 1")
        args = []
        if status:
            sql += " AND h.status = ?"
            args.append(status)
        if cid:
            sql += " AND h.customer_id = ?"
            args.append(cid)
        with self._connect() as conn:
            return [dict(r) for r in conn.execute(sql + " ORDER BY h.id DESC", args)]

    def decide_hit(self, by, hit_id, status, note):
        note = " ".join((note or "").split())[:300]
        if status not in ("False positive", "Confirmed"):
            raise BankError("Choose False positive or Confirmed.")
        if len(note) < 5:
            raise BankError("Please write why (at least 5 characters).")
        with self._connect() as conn:
            h = conn.execute("SELECT * FROM screening_hits WHERE id = ?", (hit_id,)).fetchone()
            if h is None:
                raise BankError("Match not found.")
            conn.execute("UPDATE screening_hits SET status = ?, note = ?, checked_by = ?, checked_at = ? WHERE id = ?",
                         (status, note, by["staff_id"], now(), hit_id))
            if status == "Confirmed":
                conn.execute("UPDATE accounts SET status = 'Frozen' WHERE customer_id = ? AND status = 'Active'",
                             (h["customer_id"],))
            self._audit(conn, by, f"Sanctions match: {status}", h["customer_id"], note)
        self.update_risk(h["customer_id"])

    # ===== risk rating =====

    def high_risk_countries(self):
        return {c.strip().upper() for c in self.settings().get("high_risk_countries", "").split(",") if c.strip()}

    def risk_points(self, cid):
        c = self.get_customer(cid)
        points, why = 0, []
        if c.country in self.high_risk_countries():
            points += 3
            why.append("country with higher risk")
        with self._connect() as conn:
            row = conn.execute("SELECT pep, created_at FROM customers WHERE customer_id = ?", (cid,)).fetchone()
            if row["pep"]:
                points += 3
                why.append("politically exposed person")
            flags = conn.execute("SELECT COUNT(*) FROM aml_flags WHERE customer_id = ? AND status = 'Open'",
                                 (cid,)).fetchone()[0]
            if flags:
                points += min(4, 2 * flags)
                why.append(f"{flags} open suspicious-activity flag(s)")
            if conn.execute("SELECT 1 FROM screening_hits WHERE customer_id = ? AND status IN ('Open', 'Confirmed')",
                            (cid,)).fetchone():
                points += 5
                why.append("possible sanctions match")
            since = (date.today() - timedelta(days=90)).isoformat()
            cash = 0
            for acc in conn.execute("SELECT number, currency FROM accounts WHERE customer_id = ?", (cid,)).fetchall():
                s = conn.execute("SELECT COALESCE(SUM(amount), 0) FROM transactions WHERE account_number = ? AND "
                                 "category IN ('Deposit', 'Withdrawal') AND description LIKE '%CASH%' AND created_at >= ?",
                                 (acc[0], since)).fetchone()[0]
                if s >= 10 * aml_amount(acc[1]):
                    cash = 1
            if cash:
                points += 2
                why.append("much cash in 90 days")
            if row["created_at"] >= since:
                points += 1
                why.append("new customer")
        rating = "High" if points >= 5 else "Medium" if points >= 3 else "Low"
        return rating, points, why

    def update_risk(self, cid):
        rating, points, why = self.risk_points(cid)
        with self._connect() as conn:
            conn.execute("UPDATE customers SET risk_points = ?, risk_reasons = ? WHERE customer_id = ?",
                         (points, "; ".join(why), cid))
            conn.execute("UPDATE customers SET risk_rating = ? WHERE customer_id = ? AND risk_manual = 0", (rating, cid))
        return rating

    def update_all_risks(self):
        with self._connect() as conn:
            ids = [r[0] for r in conn.execute("SELECT customer_id FROM customers")]
        return {cid: self.update_risk(cid) for cid in ids}

    def set_risk(self, by, cid, rating, pep, reason):
        reason = " ".join((reason or "").split())[:200]
        if rating not in RATINGS + ["auto"]:
            raise BankError("Choose a rating.")
        if rating != "auto" and len(reason) < 5:
            raise BankError("Please write why you set the rating yourself.")
        with self._connect() as conn:
            conn.execute("UPDATE customers SET pep = ?, risk_manual = ? WHERE customer_id = ?",
                         (1 if pep else 0, 0 if rating == "auto" else 1, cid))
            if rating != "auto":
                conn.execute("UPDATE customers SET risk_rating = ? WHERE customer_id = ?", (rating, cid))
            self._audit(conn, by, "Risk rating set", cid, f"{rating}, PEP {'yes' if pep else 'no'} - {reason}")
        self.update_risk(cid)

    def risk_summary(self):
        with self._connect() as conn:
            return {r[0]: r[1] for r in conn.execute("SELECT risk_rating, COUNT(*) FROM customers GROUP BY risk_rating")}

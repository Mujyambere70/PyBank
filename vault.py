"""
vault.py - the branch vault and the notes in every cash drawer (version 6; "future scope" of version 5).

Version 5 counted the cash in each teller's drawer, but where the cash came from was only typed in.
Now the branch has a vault with a balance in every currency:

  * opening a drawer takes the opening cash out of the vault;
  * a teller who runs short asks the vault for more cash (top-up), and a teller with too much cash
    sends some back (return) - both wait until a second person with the ability "vault.manage"
    confirms that the cash was handed over (two signatures);
  * closing a drawer puts the counted cash back into the vault;
  * cash that comes from or goes to the head office (the cash van) is recorded by one vault keeper
    and confirmed by a second one;
  * the teller counts his drawer note by note (2000, 500, 200 ... for rupees); PyBank adds it up and
    keeps the breakdown with the drawer.
"""


import world
from core import BankError, now
from world import format_money

VAULT_SCHEMA = """
CREATE TABLE IF NOT EXISTS vault (
    currency   TEXT PRIMARY KEY,
    balance    INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS vault_moves (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    kind         TEXT NOT NULL,
    currency     TEXT NOT NULL,
    amount       INTEGER NOT NULL,
    staff_id     TEXT NOT NULL DEFAULT '',
    status       TEXT NOT NULL DEFAULT 'Done',
    requested_by TEXT NOT NULL,
    approved_by  TEXT NOT NULL DEFAULT '',
    note         TEXT NOT NULL DEFAULT '',
    created_at   TEXT NOT NULL,
    decided_at   TEXT NOT NULL DEFAULT ''
);
"""
TILL_COLUMNS = [("vault_in", "INTEGER NOT NULL DEFAULT 0"), ("vault_out", "INTEGER NOT NULL DEFAULT 0"),
                ("notes_count", "TEXT NOT NULL DEFAULT ''")]
# notes and coins of each currency (in whole units), biggest first
DENOMINATIONS = {"INR": [2000, 500, 200, 100, 50, 20, 10, 5, 2, 1], "RUB": [5000, 2000, 1000, 500, 200, 100, 50, 10, 5, 2, 1],
                 "USD": [100, 50, 20, 10, 5, 2, 1], "EUR": [500, 200, 100, 50, 20, 10, 5, 2, 1],
                 "GBP": [50, 20, 10, 5, 2, 1], "CNY": [100, 50, 20, 10, 5, 1], "AED": [1000, 500, 200, 100, 50, 20, 10, 5, 1],
                 "RWF": [5000, 2000, 1000, 500, 100, 50, 20, 10, 5, 1]}
START_VAULT = {"INR": 1_00_00_000, "USD": 50_000, "EUR": 30_000, "GBP": 20_000, "RUB": 20_00_000, "CNY": 2_00_000,
               "AED": 1_00_000, "RWF": 50_00_000}        # whole units of the demo branch vault
MOVE_KINDS = {"opening": "Opening of a drawer", "topup": "Top-up of a drawer", "return": "Cash back from a drawer",
              "closing": "Closing of a drawer", "in": "Cash from head office", "out": "Cash to head office"}
MOVE_STATUSES = ["Waiting", "Done", "Rejected"]


def count_notes(currency, form):
    """{'2000': 3, ...} from form fields n_<value>, and the total in minor units."""
    counts, total = {}, 0
    for value in DENOMINATIONS.get(currency, [1]):
        raw = str(form.get(f"n_{value}", "") or "").strip()
        if not raw:
            continue
        if not raw.isdigit():
            raise BankError("Please write how many notes or coins of each kind (whole numbers).")
        n = int(raw)
        if n:
            counts[str(value)] = n
            total += n * value * 100
    return counts, total


class VaultMixin:
    """Mixed into the back-office class (Office), next to TellerMixin."""

    def ensure_vault(self):
        with self._connect() as conn:
            have = {r[0] for r in conn.execute("SELECT currency FROM vault")}
            for cur in world.CURRENCIES:
                if cur not in have:
                    conn.execute("INSERT INTO vault (currency, balance, updated_at) VALUES (?, ?, ?)",
                                 (cur, START_VAULT.get(cur, 10_000) * 100, now()))

    def vault(self):
        with self._connect() as conn:
            rows = {r["currency"]: dict(r) for r in conn.execute("SELECT * FROM vault")}
            out = conn.execute("SELECT currency, COALESCE(SUM(opening + cash_in - cash_out + vault_in - vault_out), 0) "
                               "FROM tills WHERE status = 'Open' GROUP BY currency").fetchall()
        drawers = {r[0]: r[1] for r in out}
        return [dict(rows[c], in_drawers=drawers.get(c, 0)) for c in world.CURRENCIES if c in rows]

    def vault_moves(self, status="", limit=100):
        sql = ("SELECT m.*, s.full_name FROM vault_moves m LEFT JOIN staff s ON s.staff_id = m.staff_id WHERE 1 = 1")
        args = []
        if status:
            sql += " AND m.status = ?"
            args.append(status)
        with self._connect() as conn:
            rows = [dict(r) for r in conn.execute(sql + f" ORDER BY m.id DESC LIMIT {int(limit)}", args)]
        for r in rows:
            r["label"] = MOVE_KINDS.get(r["kind"], r["kind"])
        return rows

    def _vault_change(self, conn, currency, delta, kind, by, staff_id="", note="", status="Done", approved_by=""):
        row = conn.execute("SELECT balance FROM vault WHERE currency = ?", (currency,)).fetchone()
        if row is None:
            conn.execute("INSERT INTO vault (currency, balance, updated_at) VALUES (?, 0, ?)", (currency, now()))
            row = (0,)
        if status == "Done":
            if row[0] + delta < 0:
                raise BankError(f"The vault has only {format_money(row[0], currency)}.")
            conn.execute("UPDATE vault SET balance = balance + ?, updated_at = ? WHERE currency = ?",
                         (delta, now(), currency))
        return conn.execute("INSERT INTO vault_moves (kind, currency, amount, staff_id, status, requested_by, "
                            "approved_by, note, created_at, decided_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                            (kind, currency, abs(delta), staff_id, status, by["staff_id"], approved_by, note[:200],
                             now(), now() if status == "Done" else "")).lastrowid

    def _open_drawer_row(self, conn, staff_id, currency):
        from datetime import date
        r = conn.execute("SELECT * FROM tills WHERE staff_id = ? AND day = ? AND currency = ?",
                         (staff_id, date.today().isoformat(), currency)).fetchone()
        if r is None or r["status"] != "Open":
            raise BankError(f"Please open your {currency} cash drawer first (Counter > My cash drawer).")
        return self._till_row(r)

    def request_topup(self, me, currency, amount, note=""):
        if amount <= 0:
            raise BankError("Please enter an amount.")
        with self._connect() as conn:
            self._open_drawer_row(conn, me["staff_id"], currency)
            mid = self._vault_change(conn, currency, -amount, "topup", me, me["staff_id"], note, status="Waiting")
            self.audit(conn, me, "Vault top-up asked", currency, format_money(amount, currency))
        return mid

    def request_return(self, me, currency, amount, note=""):
        if amount <= 0:
            raise BankError("Please enter an amount.")
        with self._connect() as conn:
            t = self._open_drawer_row(conn, me["staff_id"], currency)
            if amount > t["expected"]:
                raise BankError(f"Your drawer has only {format_money(t['expected'], currency)}.")
            mid = self._vault_change(conn, currency, amount, "return", me, me["staff_id"], note, status="Waiting")
            self.audit(conn, me, "Cash back to the vault asked", currency, format_money(amount, currency))
        return mid

    def head_office_move(self, by, currency, amount, incoming, note=""):
        """Cash van: recorded by one vault keeper, confirmed by another."""
        if amount <= 0:
            raise BankError("Please enter an amount.")
        if len(" ".join((note or "").split())) < 3:
            raise BankError("Please write the seal or slip number of the cash van.")
        with self._connect() as conn:
            if not incoming:
                bal = conn.execute("SELECT balance FROM vault WHERE currency = ?", (currency,)).fetchone()
                if bal is None or bal[0] < amount:
                    raise BankError("The vault does not have this much cash.")
            mid = self._vault_change(conn, currency, amount if incoming else -amount, "in" if incoming else "out", by,
                                     note=note, status="Waiting")
            self.audit(conn, by, "Head-office cash recorded", currency, f"{'in' if incoming else 'out'} "
                       f"{format_money(amount, currency)} - {note}")
        return mid

    def decide_vault_move(self, by, move_id, approve, note=""):
        with self._connect() as conn:
            m = conn.execute("SELECT * FROM vault_moves WHERE id = ?", (move_id,)).fetchone()
            if m is None or m["status"] != "Waiting":
                raise BankError("This vault move is not waiting.")
            if by["staff_id"] in (m["requested_by"], m["staff_id"]):
                raise BankError("A second person must confirm a vault move (two signatures).")
            if not approve:
                conn.execute("UPDATE vault_moves SET status = 'Rejected', approved_by = ?, decided_at = ?, note = ? "
                             "WHERE id = ?", (by["staff_id"], now(), (m["note"] + " | " + note)[:200], move_id))
                self.audit(conn, by, "Vault move rejected", str(move_id), note)
                return
            delta = {"topup": -1, "out": -1}.get(m["kind"], 1) * m["amount"]
            bal = conn.execute("SELECT balance FROM vault WHERE currency = ?", (m["currency"],)).fetchone()[0]
            if bal + delta < 0:
                raise BankError(f"The vault has only {format_money(bal, m['currency'])}.")
            if m["kind"] in ("topup", "return"):
                t = self._open_drawer_row(conn, m["staff_id"], m["currency"])
                if m["kind"] == "return" and m["amount"] > t["expected"]:
                    raise BankError("The drawer does not have this much cash any more.")
                col = "vault_in" if m["kind"] == "topup" else "vault_out"
                conn.execute(f"UPDATE tills SET {col} = {col} + ? WHERE id = ?", (m["amount"], t["id"]))
            conn.execute("UPDATE vault SET balance = balance + ?, updated_at = ? WHERE currency = ?",
                         (delta, now(), m["currency"]))
            conn.execute("UPDATE vault_moves SET status = 'Done', approved_by = ?, decided_at = ? WHERE id = ?",
                         (by["staff_id"], now(), move_id))
            self.audit(conn, by, "Vault move confirmed", str(move_id),
                       f"{MOVE_KINDS[m['kind']]} {format_money(m['amount'], m['currency'])}")

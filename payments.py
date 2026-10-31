"""
payments.py - money that leaves PyBank (mixin class used by Bank), in a SANDBOX:

    IMPS   instant, 24x7, India, rupee accounts, up to ₹5,00,000 per payment
    UPI    instant, to a UPI ID like name@bank, rupee accounts, up to ₹1,00,000 per payment
    NEFT   India, rupee accounts, settled in half-hourly batches
    SWIFT  any country, any currency, settled after about one hour in this sandbox

No real payment network is called. The other bank is simulated:
    * a beneficiary account number ending in 0000, or a UPI ID starting with "fail",
      is treated as a closed account: the payment is RETURNED and the money comes back;
    * every other payment is completed and gets a reference number (UTR / RRN / UETR).

Exchange rates (for transfers between accounts in different currencies) are downloaded
once a day from the free open.er-api.com service, or typed in by the admin.
"""

import json
import random
import re
import urllib.request
import uuid
from datetime import datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal

import world
from core import BankError, now
from world import format_money

FX_URL = "https://open.er-api.com/v6/latest/USD"

RAILS = {
    "IMPS": {"currencies": {"INR"}, "max": 5_00_000 * 100, "speed": "instant"},
    "UPI": {"currencies": {"INR"}, "max": 1_00_000 * 100, "speed": "instant"},
    "NEFT": {"currencies": {"INR"}, "max": None, "speed": "half-hourly batch"},
    "SWIFT": {"currencies": None, "max": None, "speed": "about 1 hour (sandbox)"},
}
IFSC_RE = re.compile(r"^[A-Z]{4}0[A-Z0-9]{6}$")
BIC_RE = re.compile(r"^[A-Z]{4}[A-Z]{2}[A-Z0-9]{2}([A-Z0-9]{3})?$")
UPI_RE = re.compile(r"^[a-zA-Z0-9._-]{2,64}@[a-zA-Z]{2,32}$")
ACCOUNT_RE = re.compile(r"^[A-Z0-9]{6,34}$")
# first four letters of an IFSC code -> bank name; the sandbox knows a few made-up banks
KNOWN_BANKS = {"NOVA": "Nova Cooperative Bank", "SUNR": "Sunrise Bank", "KISN": "Kisan Gramin Bank",
               "METR": "Metro City Bank"}


def fetch_rates(timeout=15):
    """Units of each supported currency for 1 US dollar, from open.er-api.com (free, no key)."""
    with urllib.request.urlopen(FX_URL, timeout=timeout) as resp:
        data = json.loads(resp.read().decode())
    if data.get("result") != "success":
        raise RuntimeError("exchange-rate service did not answer")
    return {c: float(data["rates"][c]) for c in world.CURRENCIES if c in data.get("rates", {})}


def iban_ok(value):
    """ISO 13616 check: move the first 4 characters to the end, letters -> numbers, the result mod 97 must be 1."""
    if not re.fullmatch(r"[A-Z]{2}\d{2}[A-Z0-9]{10,30}", value):
        return False
    digits = "".join(str(int(ch, 36)) for ch in value[4:] + value[:4])
    return int(digits) % 97 == 1


def next_neft_batch(t):
    """NEFT settles in batches every half hour: 10:07 -> 10:30, 10:30 -> 11:00."""
    base = t.replace(second=0, microsecond=0)
    minutes = 30 - base.minute % 30
    return base + timedelta(minutes=minutes)


class PaymentsMixin:

    # ===== exchange rates =====

    def fx_rates(self):
        with self._connect() as conn:
            return {r["currency"]: dict(r) for r in conn.execute("SELECT * FROM fx_rates ORDER BY currency")}

    def save_fx_rates(self, rates, source):
        rates = {c: float(v) for c, v in rates.items() if c in world.CURRENCIES and float(v) > 0}
        rates["USD"] = 1.0
        with self._connect() as conn:
            for cur, per_usd in rates.items():
                conn.execute("INSERT INTO fx_rates (currency, per_usd, updated_at, source) VALUES (?, ?, ?, ?) "
                             "ON CONFLICT(currency) DO UPDATE SET per_usd = excluded.per_usd, "
                             "updated_at = excluded.updated_at, source = excluded.source",
                             (cur, per_usd, now(), source))
        return len(rates)

    def refresh_fx_rates(self):
        """Download today's rates (called once a day and by the admin)."""
        fetch = self.fx_fetch or fetch_rates
        return self.save_fx_rates(fetch(), "open.er-api.com")

    def convert(self, amount, src, dst, conn=None):
        """Amount in `src` -> (amount in `dst`, rate). Raises BankError when a rate is missing."""
        if src == dst:
            return amount, 1.0
        rows = conn.execute("SELECT currency, per_usd FROM fx_rates").fetchall() if conn else None
        rates = {r[0]: r[1] for r in rows} if rows is not None else {c: r["per_usd"] for c, r in self.fx_rates().items()}
        if src not in rates or dst not in rates:
            raise BankError(f"Transfers between {src} and {dst} accounts are not possible right now: "
                            f"the bank has no exchange rate. Please try again later.")
        rate = rates[dst] / rates[src]
        out = int((Decimal(amount) * Decimal(str(rate))).to_integral_value(ROUND_HALF_UP))
        if out <= 0:
            raise BankError("The amount is too small to convert.")
        return out, rate

    def quote_internal(self, src_currency, to_number, amount):
        """For a transfer inside PyBank to an account in another currency: what the receiver gets."""
        with self._connect() as conn:
            r = conn.execute("SELECT currency FROM accounts WHERE number = ?", (str(to_number).strip(),)).fetchone()
        if r is None or r[0] == src_currency:
            return None
        received, rate = self.convert(amount, src_currency, r[0])
        return {"currency": r[0], "amount": received, "rate": rate}

    # ===== payments to other banks =====

    def send_external(self, cid, rail, data, amount, pin):
        """Pay to another bank. The money leaves the account at once; the payment then settles
        (or is returned) according to the rail. Returns the PyBank reference."""
        rail = (rail or "").upper()
        if rail not in RAILS:
            raise BankError("Please choose how to send the money (IMPS, NEFT, UPI or SWIFT).")
        self._check_pin(cid, pin)
        acc = self.get_account(cid)
        info = RAILS[rail]
        if info["currencies"] and acc.currency not in info["currencies"]:
            raise BankError(f"{rail} works only from rupee (INR) accounts. Use SWIFT to send money abroad.")
        if info["max"] and amount > info["max"]:
            raise BankError(f"The {rail} limit is {format_money(info['max'], 'INR')} per payment.")
        name = " ".join(str(data.get("name", "")).split())[:60]
        remarks = " ".join(str(data.get("remarks", "")).split())[:60]
        if rail != "UPI" and (len(name) < 3 or len(name.split()) < 2):
            raise BankError("Please enter the beneficiary's full name as it is in the bank.")
        code, bank_name, country = "", "", "IN"
        if rail == "UPI":
            account = str(data.get("upi", "")).strip().lower()
            if not UPI_RE.match(account):
                raise BankError("Please enter a valid UPI ID, for example name@okbank.")
            name = name or account.split("@")[0]
            bank_name = "UPI"
        else:
            account = re.sub(r"[\s-]", "", str(data.get("account", ""))).upper()
            if rail == "SWIFT":
                code = re.sub(r"\s", "", str(data.get("bic", ""))).upper()
                if not BIC_RE.match(code):
                    raise BankError("Please enter a valid SWIFT/BIC code (8 or 11 characters).")
                country = code[4:6]
                if country not in world.DIAL_CODES:
                    raise BankError("The country in this SWIFT/BIC code is not known.")
                if account[:2].isalpha() and not iban_ok(account):
                    raise BankError("This IBAN is not valid. Please check it.")
                if not ACCOUNT_RE.match(account):
                    raise BankError("Please enter the account number or IBAN of the beneficiary.")
                if code.startswith("PYBK"):
                    raise BankError("This is a PyBank account. Please use a transfer within PyBank.")
                bank_name = " ".join(str(data.get("bank_name", "")).split())[:60] or code
            else:
                code = str(data.get("ifsc", "")).strip().upper()
                if not IFSC_RE.match(code):
                    raise BankError("Please enter a valid 11-character IFSC code, e.g. NOVA0001234.")
                if code.startswith("PYBK"):
                    raise BankError("This is a PyBank account. Please use a transfer within PyBank.")
                if not (account.isdigit() and 9 <= len(account) <= 18):
                    raise BankError("Account number must be 9 to 18 digits.")
                bank_name = KNOWN_BANKS.get(code[:4], f"Bank {code[:4]}")
        start = datetime.now()
        settle = (start if info["speed"] == "instant" else next_neft_batch(start) if rail == "NEFT"
                  else start + timedelta(hours=1))
        with self._connect() as conn:
            limit = self.limit_for("transfer", acc.currency)
            if self._today_total(conn, acc.number, "Transfer") + amount > limit:
                raise BankError(f"Daily transfer limit is {format_money(limit, acc.currency)}.")
            src = self._load(conn, acc.number)
            self._check_available(conn, src, amount)
            ref = self._unique(conn, "SELECT 1 FROM external_transfers WHERE ref = ?",
                               lambda: f"EX{start:%y%m%d}{random.randint(0, 999_999):06d}")
            shown = account if rail == "UPI" else f"A/c XX{account[-4:]}"
            note = f" - {remarks}" if remarks else ""
            t = src.debit(amount, "Transfer", f"{rail}/To {name}/{bank_name}/{shown}{note}")
            self._save(conn, t)
            conn.execute("INSERT INTO external_transfers (ref, customer_id, account_number, rail, beneficiary_name, "
                         "beneficiary_account, bank_code, bank_name, country, currency, amount, remarks, created_at, "
                         "settle_after, tx_ref) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                         (ref, cid, acc.number, rail, name, account, code, bank_name, country, acc.currency, amount,
                          remarks, start.strftime("%Y-%m-%d %H:%M:%S"), settle.strftime("%Y-%m-%d %H:%M:%S"), t.ref))
        self.process_external()                       # instant payments settle now
        return ref

    def process_external(self, at=None):
        """Settle (or return) every payment whose time has come. Safe to call often and from many workers."""
        at = (at or datetime.now()).strftime("%Y-%m-%d %H:%M:%S")
        with self._connect() as conn:
            due = [dict(r) for r in conn.execute("SELECT * FROM external_transfers WHERE status = 'Processing' "
                                                 "AND settle_after <= ?", (at,))]
        done = 0
        for p in due:
            fail = p["beneficiary_account"].endswith("0000") or p["beneficiary_account"].startswith("fail")
            with self._connect() as conn:
                new_status = "Returned" if fail else "Completed"
                if conn.execute("UPDATE external_transfers SET status = ?, settled_at = ? WHERE id = ? "
                                "AND status = 'Processing'", (new_status, now(), p["id"])).rowcount == 0:
                    continue                          # another worker settled it
                if fail:
                    reason = "Beneficiary account is closed (sandbox test account)"
                    acc = self._load(conn, p["account_number"], must_be_active=False)
                    back = acc.credit(p["amount"], "Refund", f"{p['rail']} RETURN/{p['ref']}/{reason.upper()}")
                    self._save(conn, back)
                    conn.execute("UPDATE external_transfers SET reason = ?, return_ref = ? WHERE id = ?",
                                 (reason, back.ref, p["id"]))
                    self._alert(conn, p["customer_id"], f"{p['rail']} payment {p['ref']} returned",
                                f"Your {p['rail']} payment of {format_money(p['amount'], p['currency'], 'en')} to "
                                f"{p['beneficiary_name']} was returned by the other bank ({reason}). The money is back "
                                f"in your account.")
                else:
                    utr = (str(uuid.uuid4()) if p["rail"] == "SWIFT" else
                           f"PYBKN{p['created_at'][2:4]}{p['created_at'][5:7]}{p['created_at'][8:10]}"
                           f"{random.randint(0, 99_999_999):08d}" if p["rail"] == "NEFT" else
                           f"{random.randint(10 ** 11, 10 ** 12 - 1)}")
                    conn.execute("UPDATE external_transfers SET utr = ? WHERE id = ?", (utr, p["id"]))
                    if p["rail"] in ("NEFT", "SWIFT"):     # instant payments were already announced by the debit alert
                        self._alert(conn, p["customer_id"], f"{p['rail']} payment {p['ref']} completed",
                                    f"Your {p['rail']} payment of {format_money(p['amount'], p['currency'], 'en')} to "
                                    f"{p['beneficiary_name']} reached the other bank. Reference {utr}.",
                                    only_in_app=True)
                done += 1
        return done

    def external_transfers(self, cid=None, status=None, limit=100):
        sql = "SELECT * FROM external_transfers WHERE 1 = 1"
        args = []
        if cid:
            sql += " AND customer_id = ?"
            args.append(cid)
        if status:
            sql += " AND status = ?"
            args.append(status)
        sql += f" ORDER BY id DESC LIMIT {int(limit)}"
        with self._connect() as conn:
            return [dict(r) for r in conn.execute(sql, args)]

    def external_transfer(self, cid, ref):
        with self._connect() as conn:
            r = conn.execute("SELECT * FROM external_transfers WHERE customer_id = ? AND ref = ?", (cid, ref)).fetchone()
        if r is None:
            raise BankError("Payment not found.")
        return dict(r)

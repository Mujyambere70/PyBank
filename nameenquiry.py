"""
nameenquiry.py - the receiver's name appears by itself while a transfer is typed (version 5).

Real payment systems answer the question "whose account is this?" before the money is sent:
India's IMPS/UPI show the account holder's name, Nigeria's NIP has a "name enquiry", the United
Kingdom checks the typed name ("Confirmation of Payee") and the EU does the same since 2025
("Verification of Payee"). PyBank does both:

  * a PyBank account number    -> the holder's name from the database (+ branch, currency)
  * IFSC + account (IMPS/NEFT) -> the bank and branch of the IFSC code, the holder's name
  * UPI ID                     -> the name registered for the UPI ID
  * SWIFT/BIC or IBAN          -> the bank and its country, the holder's name

Other banks are a SANDBOX like the rest of the payments: the "directory" of the other banks is
made up, but always gives the same answer for the same account (the name is chosen from name
lists of the account's country with a hash of the account number). The rules of the sandbox stay
the same as in version 3: an account ending in 0000, or a UPI ID starting with "fail", is closed.

Because a name enquiry tells who owns an account, it needs a signed-in customer or worker and is
limited to a few lookups a minute (rate limit "name_enquiry").
"""

import hashlib
import re
import unicodedata
from difflib import SequenceMatcher

import world
from core import BankError
from payments import BIC_RE, IFSC_RE, KNOWN_BANKS, UPI_RE, iban_ok

# made-up banks of the sandbox, by SWIFT/BIC (first 4 letters) and by the bank code inside an IBAN
SANDBOX_BICS = {"NOVA": "Nova Cooperative Bank", "SUNR": "Sunrise Bank", "METR": "Metro City Bank",
                "KISN": "Kisan Gramin Bank", "ALPN": "Alpine Savings Bank", "BALT": "Baltic Trade Bank",
                "LUMI": "Lumière Banque", "SOLR": "Banco Solar", "VOLG": "Volga Commercial Bank",
                "NILE": "Nile Union Bank", "GREA": "Great Lakes Bank", "JADE": "Jade River Bank",
                "DUNE": "Dune Gulf Bank", "MAPL": "Maple North Bank", "THAM": "Thames Merchant Bank",
                "RHIN": "Rhine Volksbank", "TULP": "Tulip Bank"}
BRANCH_PLACES = {"IN": ["Main Branch, Mumbai", "Connaught Place, New Delhi", "MG Road, Bengaluru", "Park Street, Kolkata",
                        "Anna Salai, Chennai", "Sector 17, Chandigarh", "Civil Lines, Ambala"]}
NAMES = {
    "IN": (["Aarav", "Vihaan", "Ishaan", "Kavya", "Ananya", "Diya", "Rohan", "Sneha", "Arjun", "Meera", "Karan", "Pooja"],
           ["Sharma", "Patel", "Iyer", "Reddy", "Gupta", "Singh", "Nair", "Das", "Mehta", "Rao", "Kapoor", "Joshi"]),
    "RU": (["Ivan", "Olga", "Dmitry", "Elena", "Sergey", "Natalia", "Alexei", "Irina", "Pavel", "Maria"],
           ["Ivanov", "Petrova", "Smirnov", "Kuznetsova", "Popov", "Volkova", "Sokolov", "Morozova"]),
    "FR": (["Louis", "Camille", "Hugo", "Chloé", "Lucas", "Manon", "Jules", "Léa"],
           ["Martin", "Bernard", "Dubois", "Moreau", "Laurent", "Lefebvre", "Girard", "Roux"]),
    "ES": (["Javier", "Lucía", "Carlos", "Sofía", "Pablo", "Elena", "Diego", "Carmen"],
           ["García", "Fernández", "López", "Martínez", "Sánchez", "Romero", "Navarro", "Torres"]),
    "DE": (["Lukas", "Anna", "Jonas", "Lea", "Felix", "Mia", "Paul", "Hannah"],
           ["Müller", "Schmidt", "Schneider", "Fischer", "Weber", "Wagner", "Becker", "Hoffmann"]),
    "AE": (["Omar", "Fatima", "Ahmed", "Mariam", "Yousef", "Aisha", "Khalid", "Noura"],
           ["Al Mansouri", "Al Hashimi", "Haddad", "Khalil", "Nasser", "Saleh", "Rahman", "Aziz"]),
    "CN": (["Wei", "Fang", "Jun", "Li", "Hao", "Xiu", "Ming", "Yan"],
           ["Wang", "Li", "Zhang", "Liu", "Chen", "Yang", "Zhao", "Huang"]),
    "RW": (["Eric", "Aline", "Jean Paul", "Claudine", "Patrick", "Divine", "Emmanuel", "Grace"],
           ["Mugisha", "Uwase", "Habimana", "Ingabire", "Niyonzima", "Mukamana", "Nshimiyimana", "Iradukunda"]),
    "GB": (["Oliver", "Amelia", "George", "Isla", "Harry", "Ava", "Jack", "Emily"],
           ["Smith", "Jones", "Taylor", "Brown", "Williams", "Wilson", "Johnson", "Davies"]),
}
NAME_REGION = {"IN": "IN", "RU": "RU", "BY": "RU", "KZ": "RU", "FR": "FR", "BE": "FR", "LU": "FR", "MC": "FR",
               "ES": "ES", "MX": "ES", "AR": "ES", "CO": "ES", "PE": "ES", "CL": "ES", "PT": "ES", "IT": "ES",
               "DE": "DE", "AT": "DE", "CH": "DE", "NL": "DE", "PL": "DE", "AE": "AE", "SA": "AE", "EG": "AE",
               "QA": "AE", "KW": "AE", "OM": "AE", "JO": "AE", "MA": "AE", "CN": "CN", "HK": "CN", "SG": "CN",
               "TW": "CN", "RW": "RW", "UG": "RW", "KE": "RW", "TZ": "RW", "BI": "RW", "CD": "RW", "GH": "RW",
               "NG": "RW", "GB": "GB", "IE": "GB", "US": "GB", "CA": "GB", "AU": "GB", "NZ": "GB"}


def _pick(seed, items):
    return items[int(hashlib.sha256(seed.encode()).hexdigest(), 16) % len(items)]


def sandbox_name(bank_code, account, country):
    """The holder's name in the sandbox directory: always the same for the same account."""
    first, last = NAMES[NAME_REGION.get((country or "IN").upper(), "GB")]
    seed = f"{bank_code}|{account}"
    return f"{_pick(seed + '/f', first)} {_pick(seed + '/l', last)}"


def plain(text):
    """Lower case, no accents, only letters and spaces - for comparing names."""
    t = unicodedata.normalize("NFKD", text or "")
    t = "".join(ch for ch in t if not unicodedata.combining(ch)).lower()
    return " ".join(re.sub(r"[^a-zЀ-ӿ؀-ۿ一-鿿 ]+", " ", t).split())


def compare_names(typed, real):
    """Confirmation of payee: "match", "close" (a small typo, initials, other order) or "no"."""
    a, b = plain(typed), plain(real)
    if not a:
        return ""
    if a == b:
        return "match"
    wa, wb = a.split(), b.split()
    if sorted(wa) == sorted(wb):
        return "close"
    if len(wa) == len(wb) and all(x == y or (len(x) == 1 and y.startswith(x)) or (len(y) == 1 and x.startswith(y))
                                  for x, y in zip(wa, wb)):
        return "close"
    if SequenceMatcher(None, a, b).ratio() >= 0.82:
        return "close"
    return "no"


class NameEnquiryMixin:
    """Mixed into Bank."""

    def name_enquiry_internal(self, number, full=True):
        number = re.sub(r"\s", "", str(number or ""))
        if not re.fullmatch(r"\d{9,18}", number):
            raise BankError("Please type the full account number.")
        with self._connect() as conn:
            r = conn.execute("SELECT a.number, a.currency, a.branch, a.ifsc, a.status, c.full_name, c.customer_id "
                             "FROM accounts a JOIN customers c USING (customer_id) WHERE a.number = ?",
                             (number,)).fetchone()
        if r is None:
            raise BankError("No PyBank account has this number.")
        if r["status"] in ("Closed", "Pending"):
            raise BankError("This account cannot receive money (it is closed or not approved yet).")
        return {"ok": True, "name": r["full_name"] if full else self.short_name(r["full_name"]), "bank": "PyBank",
                "branch": r["branch"], "code": r["ifsc"], "currency": r["currency"], "status": r["status"],
                "network": "PyBank", "customer_id": r["customer_id"]}

    @staticmethod
    def short_name(name):
        parts = name.split()
        return " ".join(parts[:-1] + [parts[-1][0] + "."]) if len(parts) > 1 else name

    def name_enquiry(self, rail, data):
        """Answer for the transfer forms (customer and counter). Raises BankError with a clear text."""
        rail = (rail or "").upper()
        get = lambda k: str(data.get(k) or "").strip()                       # noqa: E731
        if rail in ("", "PYBANK", "INTERNAL"):
            out = self.name_enquiry_internal(get("account"))
            out.pop("customer_id", None)
        elif rail in ("IMPS", "NEFT"):
            ifsc, account = get("ifsc").upper(), re.sub(r"[\s-]", "", get("account"))
            if not IFSC_RE.match(ifsc):
                raise BankError("Type the 11-character IFSC code first, e.g. NOVA0001234.")
            if ifsc.startswith("PYBK"):
                return self.name_enquiry("PYBANK", {"account": account})
            if not (account.isdigit() and 9 <= len(account) <= 18):
                raise BankError("Account number must be 9 to 18 digits.")
            if account.endswith("0000"):
                raise BankError("The other bank says this account is closed.")
            bank = KNOWN_BANKS.get(ifsc[:4], f"Bank {ifsc[:4]}")
            out = {"name": sandbox_name(ifsc[:4], account, "IN"), "bank": bank,
                   "branch": _pick(ifsc, BRANCH_PLACES["IN"]), "code": ifsc, "currency": "INR", "network": rail}
        elif rail == "UPI":
            upi = get("upi").lower()
            if not UPI_RE.match(upi):
                raise BankError("Please enter a valid UPI ID, for example name@okbank.")
            if upi.startswith("fail"):
                raise BankError("No account is linked to this UPI ID.")
            out = {"name": sandbox_name("UPI", upi, "IN"), "bank": f"UPI handle @{upi.split('@')[1]}",
                   "branch": "", "code": upi, "currency": "INR", "network": "UPI"}
        elif rail in ("SWIFT", "SEPA"):
            account = re.sub(r"[\s-]", "", get("account") or get("iban")).upper()
            bic = re.sub(r"\s", "", get("bic")).upper()
            country = ""
            if account[:2].isalpha():
                if not iban_ok(account):
                    raise BankError("This IBAN is not valid. Please check it.")
                country = account[:2]
            if bic:
                if not BIC_RE.match(bic):
                    raise BankError("Please enter a valid SWIFT/BIC code (8 or 11 characters).")
                country = country or bic[4:6]
            if not country:
                raise BankError("Type the SWIFT/BIC code or an IBAN first.")
            if len(account) < 6:
                raise BankError("Type the account number or IBAN of the beneficiary.")
            if account.endswith("0000"):
                raise BankError("The other bank says this account is closed.")
            code = bic[:4] if bic else account[4:8]
            bank = SANDBOX_BICS.get(code[:4]) or (f"Bank {code}" if code else "Bank")
            out = {"name": sandbox_name(code, account, country), "bank": bank,
                   "branch": world.country_name(country, "en") if hasattr(world, "country_name") else country,
                   "code": bic or account[:8], "currency": "EUR" if rail == "SEPA" else "", "network": rail,
                   "country": country}
        else:
            raise BankError("Unknown payment type.")
        out["ok"] = True
        typed = get("name")
        if typed:
            out["check"] = compare_names(typed, out["name"])
        return out

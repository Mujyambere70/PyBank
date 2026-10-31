"""
world.py - what makes PyBank an international bank:
languages of the interface, countries, account currencies, money and date formats,
phone numbers and postal codes of every country.

Money is still kept as an integer number of the smallest unit (1/100 of the currency:
paise, kopecks, cents ...). Only the way it is shown depends on the language.
Country names in every language come from the Unicode CLDR data (Babel library).
"""

import re
from contextvars import ContextVar
from datetime import datetime
from decimal import Decimal

from babel import Locale
from babel.dates import format_date, format_datetime
from babel.numbers import format_currency, get_territory_currencies

# ---------- interface languages (the six official languages of the United Nations) ----------
LANGUAGES = {
    "en": "English",
    "ru": "Русский",
    "fr": "Français",
    "es": "Español",
    "ar": "العربية",
    "zh": "中文",
}
RTL_LANGUAGES = {"ar"}                       # written right to left
BABEL_LOCALE = {"en": "en", "ru": "ru", "fr": "fr", "es": "es", "ar": "ar", "zh": "zh_Hans_CN"}

# ---------- currencies the bank opens accounts in ----------
# Amounts are the bank's own product rules for each currency (in whole units, not exchange rates).
CURRENCIES = {
    #       min opening deposit      daily limits             fixed deposit  loan amount           one payment
    "INR": dict(savings=500, current=5_000, withdraw=50_000, transfer=200_000, fd_min=1_000,
                loan_min=10_000, loan_max=1_000_000, max_tx=10_000_000),
    "RUB": dict(savings=1_000, current=10_000, withdraw=200_000, transfer=1_000_000, fd_min=10_000,
                loan_min=50_000, loan_max=5_000_000, max_tx=50_000_000),
    "USD": dict(savings=10, current=100, withdraw=2_000, transfer=10_000, fd_min=100,
                loan_min=500, loan_max=50_000, max_tx=1_000_000),
    "EUR": dict(savings=10, current=100, withdraw=2_000, transfer=10_000, fd_min=100,
                loan_min=500, loan_max=50_000, max_tx=1_000_000),
    "GBP": dict(savings=10, current=100, withdraw=2_000, transfer=10_000, fd_min=100,
                loan_min=500, loan_max=50_000, max_tx=1_000_000),
    "CNY": dict(savings=100, current=1_000, withdraw=20_000, transfer=100_000, fd_min=1_000,
                loan_min=5_000, loan_max=500_000, max_tx=10_000_000),
    "AED": dict(savings=50, current=500, withdraw=10_000, transfer=50_000, fd_min=500,
                loan_min=2_000, loan_max=200_000, max_tx=5_000_000),
    "RWF": dict(savings=5_000, current=50_000, withdraw=2_000_000, transfer=10_000_000, fd_min=50_000,
                loan_min=500_000, loan_max=50_000_000, max_tx=1_000_000_000),
}
DEFAULT_CURRENCY = "INR"


def policy(currency, key):
    """A product rule in the smallest unit, e.g. policy('RUB', 'withdraw') -> 20000000 (200 000 ₽)."""
    return CURRENCIES.get(currency, CURRENCIES[DEFAULT_CURRENCY])[key] * 100


def currency_name(code, lang="en"):
    try:
        return Locale.parse(BABEL_LOCALE.get(lang, "en")).currencies.get(code, code)
    except Exception:
        return code


# ---------- branches ----------
BRANCHES = {
    "IN": {"name": "Ambala Cantt Main Branch", "ifsc": "PYBK0001024", "swift": "PYBKINBBAMB",
           "address": "12 Mall Road, Ambala Cantt, Haryana 133001, India"},
    "RU": {"name": "Krasnoyarsk International Branch", "ifsc": "PYBK0007001", "swift": "PYBKRUMMKRS",
           "address": "79 Svobodny Prospekt, Krasnoyarsk 660041, Russia"},
}


def branch_for(country, currency):
    return BRANCHES["RU"] if country == "RU" or currency == "RUB" else BRANCHES["IN"]


# ---------- countries ----------
DIAL_CODES = {'AF': '93', 'AL': '355', 'DZ': '213', 'AS': '1684', 'AD': '376', 'AO': '244', 'AI': '1264', 'AG': '1268', 'AR': '54', 'AM': '374', 'AW': '297', 'AC': '247', 'AU': '61', 'AT': '43', 'AZ': '994', 'BS': '1242', 'BH': '973', 'BD': '880', 'BB': '1246', 'BY': '375', 'BE': '32', 'BZ': '501', 'BJ': '229', 'BM': '1441', 'BT': '975', 'BO': '591', 'BA': '387', 'BW': '267', 'BR': '55', 'IO': '246', 'VG': '1284', 'BN': '673', 'BG': '359', 'BF': '226', 'BI': '257', 'KH': '855', 'CM': '237', 'CA': '1', 'CV': '238', 'BQ': '599', 'KY': '1345', 'CF': '236', 'TD': '235', 'CL': '56', 'CN': '86', 'CX': '61', 'CC': '61', 'CO': '57', 'KM': '269', 'CG': '242', 'CK': '682', 'CR': '506', 'HR': '385', 'CU': '53', 'CW': '599', 'CY': '357', 'CZ': '420', 'CI': '225', 'CD': '243', 'DK': '45', 'DJ': '253', 'DM': '1767', 'DO': '1809', 'EC': '593', 'EG': '20', 'SV': '503', 'GQ': '240', 'ER': '291', 'EE': '372', 'SZ': '268', 'ET': '251', 'FK': '500', 'FO': '298', 'FJ': '679', 'FI': '358', 'FR': '33', 'GF': '594', 'PF': '689', 'GA': '241', 'GM': '220', 'GE': '995', 'DE': '49', 'GH': '233', 'GI': '350', 'GR': '30', 'GL': '299', 'GD': '1473', 'GP': '590', 'GU': '1671', 'GT': '502', 'GG': '44', 'GN': '224', 'GW': '245', 'GY': '592', 'HT': '509', 'HN': '504', 'HK': '852', 'HU': '36', 'IS': '354', 'IN': '91', 'ID': '62', 'IR': '98', 'IQ': '964', 'IE': '353', 'IM': '44', 'IL': '972', 'IT': '39', 'JM': '1876', 'JP': '81', 'JE': '44', 'JO': '962', 'KZ': '7', 'KE': '254', 'KI': '686', 'XK': '383', 'KW': '965', 'KG': '996', 'LA': '856', 'LV': '371', 'LB': '961', 'LS': '266', 'LR': '231', 'LY': '218', 'LI': '423', 'LT': '370', 'LU': '352', 'MO': '853', 'MG': '261', 'MW': '265', 'MY': '60', 'MV': '960', 'ML': '223', 'MT': '356', 'MH': '692', 'MQ': '596', 'MR': '222', 'MU': '230', 'YT': '262', 'MX': '52', 'FM': '691', 'MD': '373', 'MC': '377', 'MN': '976', 'ME': '382', 'MS': '1664', 'MA': '212', 'MZ': '258', 'MM': '95', 'NA': '264', 'NR': '674', 'NP': '977', 'NL': '31', 'NC': '687', 'NZ': '64', 'NI': '505', 'NE': '227', 'NG': '234', 'NU': '683', 'NF': '672', 'KP': '850', 'MK': '389', 'MP': '1670', 'NO': '47', 'OM': '968', 'PK': '92', 'PW': '680', 'PS': '970', 'PA': '507', 'PG': '675', 'PY': '595', 'PE': '51', 'PH': '63', 'PN': '64', 'PL': '48', 'PT': '351', 'PR': '1787', 'QA': '974', 'RO': '40', 'RU': '7', 'RW': '250', 'RE': '262', 'WS': '685', 'SM': '378', 'SA': '966', 'SN': '221', 'RS': '381', 'SC': '248', 'SL': '232', 'SG': '65', 'SX': '1721', 'SK': '421', 'SI': '386', 'SB': '677', 'SO': '252', 'ZA': '27', 'KR': '82', 'SS': '211', 'ES': '34', 'LK': '94', 'BL': '590', 'SH': '290', 'KN': '1869', 'LC': '1758', 'MF': '590', 'PM': '508', 'VC': '1784', 'SD': '249', 'SR': '597', 'SJ': '47', 'SE': '46', 'CH': '41', 'SY': '963', 'ST': '239', 'TW': '886', 'TJ': '992', 'TZ': '255', 'TH': '66', 'TL': '670', 'TG': '228', 'TK': '690', 'TO': '676', 'TT': '1868', 'TA': '290', 'TN': '216', 'TM': '993', 'TC': '1649', 'TV': '688', 'TR': '90', 'VI': '1340', 'UG': '256', 'UA': '380', 'AE': '971', 'GB': '44', 'US': '1', 'UY': '598', 'UZ': '998', 'VU': '678', 'VA': '39', 'VE': '58', 'VN': '84', 'WF': '681', 'EH': '212', 'YE': '967', 'ZM': '260', 'ZW': '263', 'AX': '358'}


def countries(lang="en"):
    """[(code, name)] of every country, sorted by the name in the chosen language."""
    loc = Locale.parse(BABEL_LOCALE.get(lang, "en"))
    names = loc.territories
    rows = [(c, names.get(c, c)) for c in DIAL_CODES]
    return sorted(rows, key=lambda r: r[1])


def country_name(code, lang="en"):
    try:
        return Locale.parse(BABEL_LOCALE.get(lang, "en")).territories.get(code, code)
    except Exception:
        return code


def default_currency(country, lang="en"):
    """Russian interface -> rubles. Otherwise the currency of the country if the bank has it, else US dollars."""
    if lang == "ru":
        return "RUB"
    try:
        local = get_territory_currencies(country)
    except Exception:
        local = []
    for code in local:
        if code in CURRENCIES:
            return code
    return "USD"


# ---------- phone numbers and postal codes ----------
POSTCODE_RULES = {
    "IN": (r"[1-9]\d{5}", "6 digits"),
    "RU": (r"\d{6}", "6 digits"),
    "US": (r"\d{5}(-\d{4})?", "5 digits"),
    "CN": (r"\d{6}", "6 digits"),
    "FR": (r"\d{5}", "5 digits"),
    "DE": (r"\d{5}", "5 digits"),
    "ES": (r"\d{5}", "5 digits"),
}


def clean_postcode(country, value):
    value = " ".join(str(value or "").upper().split())
    rule = POSTCODE_RULES.get(country)
    if rule:
        return value if re.fullmatch(rule[0], value) else None
    return value if re.fullmatch(r"[A-Z0-9][A-Z0-9 -]{1,9}", value) else None


def clean_mobile(country, value):
    """Return the number as it is stored, or None if it is not valid.
    India keeps the 10-digit form (as before); every other country is stored as +<code><number>."""
    digits = re.sub(r"[\s().-]", "", str(value or ""))
    dial = DIAL_CODES.get(country, "")
    if digits.startswith("+"):
        digits = digits[1:]
        if dial and digits.startswith(dial):
            digits = digits[len(dial):]
        elif dial:
            return None
    if not digits.isdigit():
        return None
    if country == "IN":
        digits = digits[-10:] if len(digits) == 12 and digits.startswith("91") else digits
        return digits if re.fullmatch(r"[6-9]\d{9}", digits) else None
    digits = digits.lstrip("0") if country not in ("IT",) else digits
    if country == "RU" and not re.fullmatch(r"9\d{9}", digits):
        return None
    if not 6 <= len(digits) <= 14 or len(dial + digits) > 15:
        return None
    return f"+{dial}{digits}"


# ---------- language and currency of the current page ----------
_lang = ContextVar("lang", default="en")
_country = ContextVar("country", default="IN")
_currency = ContextVar("currency", default=DEFAULT_CURRENCY)


def set_context(lang=None, country=None, currency=None):
    if lang:
        _lang.set(lang if lang in LANGUAGES else "en")
    if country:
        _country.set(country)
    if currency:
        _currency.set(currency)


def current_lang():
    return _lang.get()


def current_currency():
    return _currency.get()


def babel_locale(lang=None, country=None):
    lang = lang or _lang.get()
    country = country or _country.get()
    if lang == "en" and country == "IN":
        return "en_IN"                       # ₹1,00,000.00 (lakh grouping)
    return BABEL_LOCALE.get(lang, "en")


def format_money(amount, currency=None, lang=None):
    """Amount in the smallest unit -> text in the style of the language:
    150050 INR (English) -> '₹1,500.50'; 100000 RUB (Russian) -> '1 000,00 ₽'."""
    currency = currency or _currency.get()
    loc = babel_locale(lang)
    try:
        return format_currency(Decimal(int(amount)) / 100, currency, locale=loc)
    except Exception:
        return f"{int(amount) / 100:,.2f} {currency}"


def format_when(value, with_time=True, lang=None):
    """'2026-10-31 01:48:00' -> '31 Oct 2026, 01:48 AM' (English) or '31 окт. 2026 г., 01:48' (Russian)."""
    lang = lang or _lang.get()
    d = value if isinstance(value, datetime) else datetime.strptime(str(value)[:19], "%Y-%m-%d %H:%M:%S")
    if lang == "en":
        return d.strftime("%d %b %Y, %I:%M %p" if with_time else "%d %b %Y")
    loc = babel_locale(lang)
    return format_datetime(d, "d MMM y, HH:mm", locale=loc) if with_time else format_date(d, "medium", locale=loc)

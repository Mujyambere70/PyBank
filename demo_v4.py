"""
demo_v4.py - demo content for the version 4 pages (runs once, also on an existing online database).

All people are made up and use example.com / pybank.demo addresses, so nothing is ever emailed or texted.
Exchange rates are never invented: the currency exchange of the demo is made only when real rates were
downloaded (open.er-api.com) or typed by the admin.
"""

from datetime import date, datetime, timedelta

from core import BankError, now
from mrz import check_digit

DEMO_PIN = "2580"
STAFF_PASSWORD = "Staff@1234"
NEW_STAFF = [  # staff id, name, email, phone, designation, job role
    ("EMP003", "Ravi Kumar", "ravi.kumar@pybank.demo", "+91 98120 45672", "Cashier", "teller"),
    ("EMP004", "Meera Joshi", "meera.joshi@pybank.demo", "+91 98120 45673", "Loan Officer", "loan_officer"),
    ("EMP005", "Sergey Volkov", "sergey.volkov@pybank.demo", "+7 391 000 7005", "Compliance Officer", "compliance"),
    ("EMP006", "Aisha Khan", "aisha.khan@pybank.demo", "+91 98120 45676", "Website Editor", "content_editor"),
    ("EMP007", "Vikram Singh", "vikram.singh@pybank.demo", "+91 98120 45677", "Deputy Branch Manager", "branch_manager"),
]

# translations of a few website items (the editor can translate everything in Back office -> Website)
TR_TEXTS = {
    "tagline": {"ru": "Банк — это просто", "fr": "La banque en toute simplicité", "es": "La banca, sencilla",
                "ar": "الخدمات المصرفية ببساطة", "zh": "银行，就这么简单"},
    "contact_intro": {"ru": "Мы рады помочь 24 часа в сутки.", "fr": "Nous sommes là pour vous aider, 24 h sur 24.",
                      "es": "Estamos aquí para ayudarle las 24 horas.", "ar": "نحن هنا لمساعدتك على مدار الساعة.",
                      "zh": "我们全天24小时为您服务。"},
}
TR_BANNER = {
    "ru": ("Цифровой счёт без бумаг", "Откройте сберегательный счёт за 5 минут",
           "До 3,5 % годовых, бесплатная дебетовая карта и интернет-банк 24/7 — переводы, выписки и многое другое.",
           "Открыть счёт"),
    "fr": ("Compte numérique sans papier", "Ouvrez un compte d'épargne en 5 minutes",
           "Jusqu'à 3,5 % par an, une carte de débit gratuite et la banque en ligne 24 h/24 — virements, relevés et plus.",
           "Ouvrir un compte"),
    "es": ("Cuenta digital sin papeles", "Abra una cuenta de ahorro en 5 minutos",
           "Hasta un 3,5 % anual, tarjeta de débito gratis y banca en línea 24/7: transferencias, extractos y más.",
           "Abrir cuenta"),
    "ar": ("حساب رقمي بلا أوراق", "افتح حساب توفير في 5 دقائق",
           "فائدة تصل إلى 3.5٪ سنويًا وبطاقة خصم مجانية وخدمات مصرفية عبر الإنترنت على مدار الساعة.",
           "افتح حسابًا الآن"),
    "zh": ("无纸化数字账户", "5分钟开立储蓄账户", "年利率最高3.5%，免费借记卡，网上银行全天候服务——转账、对账单等。", "立即开户"),
}
TR_FAQ = {  # first FAQ (How do I open an account?)
    "ru": ("Как открыть счёт?", "Нажмите «Открыть счёт», заполните личные и контактные данные, выберите сберегательный "
           "или текущий счёт, внесите первый взнос и задайте пароль и 4-значный PIN-код операций.", "Счёт"),
    "fr": ("Comment ouvrir un compte ?", "Cliquez sur « Ouvrir un compte », remplissez vos coordonnées, choisissez "
           "épargne ou courant, faites le dépôt d'ouverture et choisissez votre mot de passe et votre code PIN à 4 "
           "chiffres.", "Compte"),
    "es": ("¿Cómo abro una cuenta?", "Pulse «Abrir cuenta», complete sus datos personales y de contacto, elija ahorro "
           "o corriente, haga el depósito inicial y elija su contraseña y su PIN de 4 cifras.", "Cuenta"),
    "ar": ("كيف أفتح حسابًا؟", "اضغط «فتح حساب»، واملأ بياناتك الشخصية وبيانات الاتصال، واختر حساب توفير أو حسابًا "
           "جاريًا، وقم بالإيداع الأول، ثم اختر كلمة المرور ورمز PIN من 4 أرقام.", "الحساب"),
    "zh": ("如何开户？", "点击“开户”，填写个人和联系信息，选择储蓄或活期账户，存入开户金额，并设置登录密码和4位交易密码。", "账户"),
}


def td3(surname, given, number, nationality, birth, sex, expiry):
    """Make the two MRZ lines of a passport with correct check digits (for the demo applicant)."""
    names = (surname.upper().replace(" ", "<") + "<<" + given.upper().replace(" ", "<"))[:39].ljust(39, "<")
    l1 = "P<" + nationality + names
    num = number.ljust(9, "<")
    b, e = birth.strftime("%y%m%d"), expiry.strftime("%y%m%d")
    optional = "<" * 14
    body = num + check_digit(num) + nationality + b + check_digit(b) + sex + e + check_digit(e) + optional + "<"
    final = check_digit(body[0:10] + body[13:20] + body[21:43])
    return l1 + "\n" + body + final


def _ago(days, hour=11, minute=20):
    return (datetime.now() - timedelta(days=days)).replace(hour=hour, minute=minute, second=0).strftime("%Y-%m-%d %H:%M:%S")


def seed_v4(bank, office):
    """Add the demo content of version 4 once."""
    with bank._connect() as conn:
        if conn.execute("SELECT 1 FROM settings WHERE key = 'demo_v4'").fetchone():
            return False
        conn.execute("INSERT INTO settings (key, value) VALUES ('demo_v4', ?)", (now(),))
    admin = office.get_staff("EMP001")
    if admin is None or bank.is_empty():
        return False
    # ---- staff with job roles
    for sid, name, email, phone, designation, job in NEW_STAFF:
        if office.get_staff(sid) is None:
            office.create_staff(admin, name, email, phone, designation, "staff", STAFF_PASSWORD, staff_id=sid,
                                when=_ago(120))
            office.set_job_role(admin, sid, job)
    editor = office.get_staff("EMP006")
    neha = office.get_staff("EMP002")

    # ---- website: translations, a second banner, a changed text with history
    for key, langs in TR_TEXTS.items():
        for lang, value in langs.items():
            bank.save_translation(editor, "text", key, lang, {"value": value})
    banner = bank.content_items("banner")[0]["id"]
    for lang, (e, t, x, b) in TR_BANNER.items():
        bank.save_translation(editor, "banner", banner, lang, {"eyebrow": e, "title": t, "text": x, "button": b})
    faq = bank.content_items("faq")[0]["id"]
    for lang, (q, a, c) in TR_FAQ.items():
        bank.save_translation(editor, "faq", faq, lang, {"question": q, "answer": a, "category": c})
    bank.save_item(editor, "banner", {"eyebrow": "New in version 4", "title": "Accounts in 8 currencies",
                                      "text": "Keep rupees, rubles, dollars and euros and move money between them.",
                                      "button": "See the rates", "link": "/rates#exchange", "image": "static:app.jpg",
                                      "starts": date.today().isoformat(),
                                      "ends": (date.today() + timedelta(days=60)).isoformat(), "active": "1"})
    bank.save_item(editor, "banner", {"title": "Diwali offer: FD at 7.25%", "text": "This offer has ended.",
                                      "starts": (date.today() - timedelta(days=40)).isoformat(),
                                      "ends": (date.today() - timedelta(days=20)).isoformat(), "active": "1"})
    bank.save_text(editor, "office_hours", "Mon-Sat, 9:30 AM - 5:00 PM")
    bank.save_text(editor, "office_hours", "Mon-Sat, 9:30 AM - 4:30 PM")         # changed back: 2 versions in the history

    rahul, priya, arjun, anna, amani = "PB10000001", "PB10000002", "PB10000003", "PB10000004", "PB10000005"
    try:
        bank.refresh_fx_rates()                         # real rates (open.er-api.com); without internet: skipped
    except Exception:
        pass
    have_rates = bool(bank.fx_rates())

    # ---- Rahul: a USD account, card controls and card activity, bills, autopay, budgets, an app
    usd = bank.open_currency_account(rahul, "USD", DEMO_PIN)
    if have_rates:
        bank.exchange(rahul, bank.get_account(rahul).number, usd.number, 20_000 * 100, DEMO_PIN)
    card = bank.card(rahul)
    bank.set_card_controls(rahul, {"online": "1", "abroad": "", "contactless": "1",
                                   "atm_limit": "20000", "pos_limit": "50000"})
    main = bank.get_account(rahul)
    bank.card_payment(card["last4"], main.number, 1_249 * 100, "online", "ShopKart Online", "IN", _ago(5, 20))
    bank.card_payment(card["last4"], main.number, 460 * 100, "contactless", "Chai Point Express", "IN", _ago(2, 9))
    bank.card_payment(card["last4"], main.number, 3_000 * 100, "pos", "Duty Free Dubai", "AE", _ago(1, 22))  # declined
    electricity = bank.billers(currency="INR")[0]
    bill = bank.fetch_bill(electricity["id"], "1004567891")
    bank.pay_bill(rahul, electricity["id"], "1004567891", bill["amount"], DEMO_PIN)
    bank.create_instruction(rahul, {"kind": "transfer", "payee": bank.get_account(priya).number, "name": "Pocket money",
                                    "amount": "2000", "frequency": "monthly",
                                    "start": (date.today() + timedelta(days=5)).isoformat()}, DEMO_PIN)
    bank.create_instruction(rahul, {"kind": "bill", "biller": str(electricity["id"]), "consumer_no": "1004567891",
                                    "name": "Electricity", "amount": "1500", "frequency": "monthly",
                                    "start": (date.today() + timedelta(days=12)).isoformat()}, DEMO_PIN)
    bank.set_budget(rahul, main.number, "Shopping", 6_000 * 100)
    bank.set_budget(rahul, main.number, "Bill payment", 5_000 * 100)
    bank.create_token(rahul, "PyBank app (Android)")

    # ---- Anna: a euro account and a SEPA payment; the SWIFT + SEPA payments go out in an ISO 20022 file
    eur = bank.open_currency_account(anna, "EUR", DEMO_PIN)
    if have_rates:
        bank.exchange(anna, bank.get_account(anna).number, eur.number, 30_000 * 100, DEMO_PIN)
        if bank.available_balance(eur.number) >= 150 * 100:
            bank.send_external(anna, "SEPA", {"name": "Claire Martin", "account": "FR7630006000011234567890189",
                                              "bic": "AGRIFRPP", "remarks": "Flat deposit Paris"}, 150 * 100, DEMO_PIN,
                               from_number=eur.number)
    if bank.waiting_for_file():
        fid = bank.create_pain001("EMP005")
        bank.apply_pain002(bank.test_pain002(fid), "EMP005")

    # ---- suspicious activity: a large cash deposit (flag), and an earlier flag that was cleared
    ref = bank.request_cash(arjun, "Deposit", 12_00_000 * 100, DEMO_PIN, "Grain sale - cash")
    rid = [r for r in bank.cash_requests(arjun) if r["ref"] == ref][0]["id"]
    office.decide_cash(neha, rid, True)
    flags = bank.aml_flags("Open")
    if flags:
        with bank._connect() as conn:
            conn.execute("INSERT INTO aml_flags (customer_id, account_number, rule, details, amount, tx_ref, day, status, "
                         "created_at, reviewed_by, reviewed_at, note) VALUES (?, ?, 'PASS_THROUGH', ?, ?, '', ?, 'Cleared', "
                         "?, 'EMP005', ?, ?)",
                         (priya, bank.get_account(priya).number, "₹62,000.00 came in and ₹60,000.00 went out within 24 hours.",
                          60_000 * 100, _ago(20)[:10], _ago(20), _ago(19),
                          "Tuition fee received from parents and paid to the college the same day - normal."))

    # ---- four-eyes rule: a large manual entry waits for a second person
    office.manual_entry(neha, arjun, "CR", 75_000 * 100, "Cash counted again: shortage of 12 Nov returned")

    # ---- a dormant account (opened 3 years ago, last used 26 months ago)
    opened = _ago(3 * 365, 12)
    try:
        cid, acc = bank.register({"full_name": "Kofi Mensah", "dob": "1979-03-14", "gender": "Male",
                                  "email": "kofi.mensah@example.com", "mobile": "+233 20 000 1979",
                                  "address": "14 Ring Road", "city": "Accra", "state": "", "pincode": "00233",
                                  "occupation": "Self-employed", "country": "GH", "lang": "en", "account_type": "Savings",
                                  "currency": "USD", "initial_deposit": "50", "password": "Demo@1234", "pin": DEMO_PIN},
                                 when=opened, customer_id="PB10000007")
        bank.post(acc.number, "CR", "Deposit", "CASH DEPOSIT/BRANCH", 1_200 * 100, _ago(26 * 30, 12))
        bank.mark_dormant()
    except BankError:
        pass

    # ---- the applicant from Spain typed her passport MRZ; a bank officer checked it too
    lucia = td3("Fernandez", "Lucia", "PAB123456", "ESP", date(1998, 6, 21), "F", date(2031, 5, 30))
    try:
        bank.check_mrz("PB10000006", lucia, "customer (online form)")
    except BankError:
        pass
    _rebuild_running_balances(bank)
    return True


def _rebuild_running_balances(bank):
    """Demo only: some demo card payments are dated a few days back, so the 'balance after' of every row is
    worked out again in date order (from the real balance backwards). The balances themselves do not change."""
    with bank._connect() as conn:
        for acc in conn.execute("SELECT number, balance FROM accounts").fetchall():
            running = acc["balance"]
            rows = conn.execute("SELECT id, type, amount, balance_after FROM transactions WHERE account_number = ? "
                                "ORDER BY created_at DESC, id DESC", (acc["number"],)).fetchall()
            for r in rows:
                if r["balance_after"] != running:
                    conn.execute("UPDATE transactions SET balance_after = ? WHERE id = ?", (running, r["id"]))
                running -= r["amount"] if r["type"] == "CR" else -r["amount"]

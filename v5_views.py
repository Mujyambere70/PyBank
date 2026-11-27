"""
v5_views.py - the web pages of version 5.

Back office   Counter (cash deposit / withdrawal, transfers, other banks, exchange, deposits, EMI)
              with counter receipts, My cash drawer and All cash drawers, Staff & roles (roles in the
              database, abilities and amount limit of each person), sanctions screening and risk,
              open-banking partner apps, the data manager, smart search and the assistant, the
              worker's own details and photo.
Net Banking   name enquiry while a transfer is typed, smart search, "Ask PyBank", recurring deposits
              and savings goals, connected apps (open-banking consents).
Public        smart search, "Ask PyBank", OAuth 2.0 consent (/oauth/authorize, /oauth/token) and the
              read-only open-banking API (/openbanking/v1/...).
"""

import base64
import io
from datetime import date
from urllib.parse import urlencode

from flask import Response, abort, flash, jsonify, redirect, render_template, request, session, url_for

import roles
import smartsearch
import world
from assistant import Assistant
from bank import FD_TENURES, BankError, parse_amount
from compliance import checker_amount
from datamanager import PER_PAGE, DataManager
from deposits import RD_TENURES
from openbanking import SCOPES
from screening import HIT_STATUSES
from teller import ID_DOCUMENTS, OPS
from vault import count_notes
from world import format_money

PHOTO_MAX = 2 * 1024 * 1024


def small_photo(data):
    """Any picture -> a 160 x 160 JPEG as base64 (None when it is not a picture)."""
    try:
        from PIL import Image, ImageOps
        img = Image.open(io.BytesIO(data))
        img = ImageOps.exif_transpose(img).convert("RGB")
        img = ImageOps.fit(img, (160, 160))
        out = io.BytesIO()
        img.save(out, "JPEG", quality=82)
        return base64.b64encode(out.getvalue()).decode()
    except Exception:
        return None


def register(app, bank, office, h):
    login_required, current = h["login_required"], h["current"]
    staff_required, office_page, client_ip = h["staff_required"], h["office_page"], h["client_ip"]
    helper = Assistant(bank)
    data_manager = DataManager(bank, office)

    # ---------- template helpers ----------

    def live_roles():
        return office.job_roles()

    def tr_answer(a):
        """The assistant's answer in the language of the page (texts the catalog knows)."""
        import i18n
        a["text"] = "\n".join(i18n.translate_text(line) for line in a["text"].split("\n"))
        a["links"] = [dict(link, title=i18n.translate_text(link["title"])) for link in a["links"]]
        a["items"] = [i18n.translate_text(i) if isinstance(i, str) else i for i in a["items"]]
        return a

    app.jinja_env.globals.update(ABILITY_GROUPS=roles.ABILITY_GROUPS, COUNTER_OPS=OPS, ID_DOCUMENTS=ID_DOCUMENTS,
                                 live_roles=live_roles, SCOPES=SCOPES)

    @app.context_processor
    def v5_globals():
        if request.path.startswith("/admin") and "staff_id" in session:
            r = office.job_roles()
            return {"ROLES": r, "JOB_ROLES": list(r)}
        return {}

    # =====================================================================
    # Search documents (what each kind of user may find)
    # =====================================================================

    def plain_markup(text):
        """The page body without the mini-markup signs (##, **, - lists, [text](link))."""
        import re
        text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text or "")
        text = re.sub(r"(?m)^\s*(#{1,3}|-)\s+", "", text)
        return " ".join(text.replace("**", "").split())

    def public_docs():
        lang = world.current_lang()
        docs = [
            {"title": "Open an account online", "url": url_for("register"), "kind": "Help",
             "keywords": "open account new account register sign up apply savings current", "concepts": ["open account"]},
            {"title": "Net Banking login", "url": url_for("login"), "kind": "Help",
             "keywords": "login sign in net banking password customer id", "concepts": ["password"]},
            {"title": "Interest rates and exchange rates", "url": url_for("rates"), "kind": "Help",
             "keywords": "interest rate fd rd savings loan exchange currency charges", "concepts": ["interest", "exchange"]},
            {"title": "Branches, ATMs and contact", "url": url_for("contact"), "kind": "Help",
             "keywords": "branch atm address phone contact email hours map", "concepts": ["branch", "contact"]},
            {"title": "Questions and answers (FAQ)", "url": url_for("faq"), "kind": "Help", "keywords": "faq questions help"},
            {"title": "About PyBank", "url": url_for("about"), "kind": "Help", "keywords": "about bank who history developer"},
            {"title": "For developers (API, open banking)", "url": url_for("developers"), "kind": "Help",
             "keywords": "api developers open banking oauth app json", "concepts": ["open banking"]},
        ]
        for f in bank.faqs(lang):
            docs.append({"title": f["question"], "text": f["answer"], "url": url_for("faq") + f"#faq-{f['id']}",
                         "kind": "Question", "keywords": f["category"]})
        for p in bank.products(lang):
            docs.append({"title": p["title"], "text": f"{p.get('text', '')} {p.get('stat', '')}",
                         "url": url_for("index") + "#products", "kind": "Product", "keywords": "product account"})
        for b in bank.branches(lang):
            docs.append({"title": b["name"], "text": f"{b.get('address', '')} {b.get('city', '')} {b.get('hours', '')}",
                         "url": url_for("contact"), "kind": "Branch", "keywords": "branch atm " + b.get("kind", ""),
                         "concepts": ["branch"]})
        for pg in bank.site_pages(lang):
            docs.append({"title": pg["title"], "text": plain_markup(pg.get("body", ""))[:2000], "url": url_for("site_page", slug=pg["slug"]),
                         "kind": "Page", "keywords": pg["slug"]})
        return docs

    NB_HELP = [
        ("dashboard", "Dashboard", "home overview balance summary"),
        ("my_accounts", "My accounts and currency exchange", "accounts currency exchange convert open usd eur balance"),
        ("account_details", "Account details", "account number ifsc branch card login history"),
        ("transfer", "Send money to a PyBank account", "transfer send pay money payee account number"),
        ("other_bank", "Pay another bank or abroad (IMPS, NEFT, UPI, SWIFT, SEPA)", "transfer other bank imps neft upi swift sepa abroad iban"),
        ("bills", "Pay bills and standing instructions", "bill electricity mobile recharge autopay standing instruction"),
        ("deposit", "Ask to deposit cash", "deposit cash in pay in"),
        ("withdraw", "Ask to withdraw cash", "withdraw cash out"),
        ("statement", "Statement and transaction history", "statement history transactions download csv pdf"),
        ("insights", "Insights and budgets", "spending budget categories food shopping"),
        ("estatements", "Monthly e-statements (PDF)", "e-statement monthly pdf"),
        ("payees", "Beneficiaries (saved payees)", "payee beneficiary add delete rename"),
        ("fixed_deposits", "Fixed deposits", "fixed deposit fd open close interest"),
        ("savings_plans", "Recurring deposits and savings goals", "recurring deposit rd monthly saving goal pot target"),
        ("loans", "Loans and EMI", "loan emi apply personal vehicle education pay close"),
        ("my_card", "My card: block, limits, online and abroad", "card block lost stolen limit atm contactless online abroad"),
        ("alerts", "Alerts", "alerts notifications sms email"),
        ("service_requests", "Service requests and complaints", "cheque book request complaint nominee"),
        ("profile", "My profile", "profile address email mobile change name"),
        ("security", "Security: password, PIN, two-factor login, devices", "password pin change two-factor 2fa device sign out"),
        ("connected_apps", "Connected apps (open banking)", "open banking connected apps consent third party revoke"),
    ]

    def netbanking_docs(cid):
        docs = [{"title": t, "url": url_for(ep), "kind": "Help", "keywords": k} for ep, t, k in NB_HELP]
        cur = bank.get_account(cid).currency
        for acc in bank.accounts_of(cid):
            for t in bank.transactions(acc.number, limit=400):
                docs.append({"title": f"{t.description[:70]}", "kind": "Transaction",
                             "text": f"{t.category} {t.ref} {t.created_at[:10]} {t.amount // 100} "
                                     f"{format_money(t.amount, acc.currency)}",
                             "keywords": t.category, "amount": t.amount, "ref": t.ref,
                             "url": url_for("receipt", ref=t.ref), "date": t.created_at[:10],
                             "money": ("+" if t.type == "CR" else "−") + format_money(t.amount, acc.currency)})
        for p in bank.beneficiaries(cid):
            docs.append({"title": f"{p.get('nickname') or p.get('name')}", "kind": "Payee",
                         "text": f"{p.get('name', '')} {p.get('account_number', '')}", "url": url_for("payees"),
                         "keywords": "payee beneficiary"})
        for f in bank.fixed_deposits(cid):
            docs.append({"title": f"Fixed deposit {f['fd_number']}", "kind": "Deposit", "url": url_for("fixed_deposits"),
                         "text": f"{format_money(f['principal'], cur)} {f['rate']}% {f['status']}", "keywords": "fd fixed deposit"})
        for r in bank.recurring_deposits(cid):
            docs.append({"title": f"Recurring deposit {r['rd_number']}", "kind": "Deposit", "url": url_for("savings_plans"),
                         "text": f"{format_money(r['instalment'], r['currency'])} {r['status']}", "keywords": "rd recurring"})
        for g in bank.goals(cid):
            docs.append({"title": f"Goal: {g['name']}", "kind": "Goal", "url": url_for("savings_plans"),
                         "text": format_money(g["saved"], g["currency"]), "keywords": "goal"})
        return docs

    OFFICE_HELP = [
        ("office_dashboard", "Dashboard", "home overview numbers", None),
        ("office_counter", "Counter: cash and payments for a customer", "counter teller cash deposit withdrawal transfer customer", "customers.view"),
        ("office_till", "My cash drawer", "till cash drawer open close count", "till.own"),
        ("office_tills", "All cash drawers", "tills drawers difference supervisor", "till.supervise"),
        ("office_customers", "Customers", "customers search kyc", "customers.view"),
        ("office_customer_new", "Open an account at the branch", "open account new customer", "customers.edit"),
        ("office_transactions", "Transactions", "transactions search reverse", "transactions.view"),
        ("office_applications", "New accounts and changes of data", "applications kyc approve new account", "applications"),
        ("office_approvals", "Cash approvals", "cash approvals deposit withdrawal request", "cash.approve"),
        ("office_checker", "Second approvals (four-eyes)", "second approval checker four eyes", "checker"),
        ("office_loans", "Loan applications", "loans approve reject", "loans.decide"),
        ("office_payments", "Payments to other banks", "payments other bank imps neft swift", "payments.view"),
        ("office_requests", "Service requests", "service requests complaints disputes", "requests.answer"),
        ("office_compliance", "Suspicious activity", "aml suspicious flags", "compliance"),
        ("office_screening", "Sanctions screening and risk", "sanctions screening risk rating pep", "screening"),
        ("office_content", "Website content", "website pages faq products branches banners", "content.manage"),
        ("office_staff", "Staff and their abilities", "staff people abilities limits", "staff.manage"),
        ("office_roles", "Job roles", "roles abilities permissions", "roles.manage"),
        ("office_settings", "Bank settings", "settings limits interest rates exchange", "settings.manage"),
        ("office_openbanking", "Open banking partner apps", "open banking apps consents oauth", "openbanking.manage"),
        ("office_data", "Data manager", "data manager tables records edit", "data.manage"),
        ("office_backups", "Backups", "backup restore", "backups"),
        ("office_audit", "Audit log", "audit log who did what", "audit.view"),
        ("office_profile", "My profile", "my profile password photo details two-factor", None),
    ]

    def office_docs(me):
        can = lambda a: roles.can(me, a)                                   # noqa: E731
        docs = [{"title": t, "url": url_for(ep), "kind": "Page", "keywords": k}
                for ep, t, k, a in OFFICE_HELP if a is None or can(a)]
        with bank._connect() as conn:
            if can("customers.view"):
                for r in conn.execute("SELECT c.customer_id, c.full_name, c.email, c.mobile, c.country, c.status, "
                                      "(SELECT GROUP_CONCAT(number, ' ') FROM accounts a WHERE a.customer_id = c.customer_id) "
                                      "AS numbers FROM customers c" if not conn.is_postgres else
                                      "SELECT c.customer_id, c.full_name, c.email, c.mobile, c.country, c.status, "
                                      "(SELECT STRING_AGG(number, ' ') FROM accounts a WHERE a.customer_id = c.customer_id) "
                                      "AS numbers FROM customers c").fetchall():
                    docs.append({"title": f"{r['full_name']} ({r['customer_id']})", "kind": "Customer",
                                 "text": f"{r['customer_id']} {r['email']} {r['mobile']} {r['numbers'] or ''} {r['status']}",
                                 "keywords": f"customer {r['country']}", "url": url_for("office_customer", cid=r["customer_id"])})
            if can("staff.manage"):
                for r in conn.execute("SELECT staff_id, full_name, designation FROM staff").fetchall():
                    docs.append({"title": f"{r['full_name']} ({r['staff_id']})", "kind": "Staff",
                                 "text": r["designation"], "url": url_for("office_staff_access", sid=r["staff_id"])})
        return docs

    def office_tx_docs(me, q):
        """Transactions are many: look them up by reference or amount only when the query has one."""
        if not roles.can(me, "transactions.view"):
            return []
        refs = [w.upper() for w in q.split() if len(w) >= 8 and w[:2].isalpha() and w[2:].isdigit()]
        if not refs:
            return []
        with bank._connect() as conn:
            rows = conn.execute("SELECT t.*, a.customer_id, a.currency FROM transactions t JOIN accounts a "
                                "ON a.number = t.account_number WHERE t.ref = ? LIMIT 10", (refs[0],)).fetchall()
        return [{"title": f"{r['ref']} · {r['description'][:50]}", "kind": "Transaction",
                 "text": f"{r['customer_id']} {format_money(r['amount'], r['currency'])}", "keywords": r["ref"].lower(),
                 "url": url_for("office_customer", cid=r["customer_id"])} for r in rows]

    # =====================================================================
    # Public: search, assistant, open banking
    # =====================================================================

    h.setdefault("docs", {}).update(public_docs=public_docs, netbanking_docs=netbanking_docs, office_docs=office_docs,
                                    tr_answer=tr_answer)                       # used by the v6 chat assistant

    @app.route("/search")
    def site_search():
        q = request.args.get("q", "")[:120]
        res = smartsearch.search(q, public_docs()) if q.strip() else None
        return render_template("public/search.html", q=q, res=res)

    @app.route("/search/suggest")
    def site_suggest():
        return jsonify(smartsearch.suggest(request.args.get("q", "")[:60], public_docs()))

    @app.route("/assistant", methods=["POST"])
    def site_assistant():
        if not bank.rate_ok("assistant", client_ip()):
            return jsonify({"text": "Too many questions. Please wait a minute.", "links": [], "items": []}), 429
        text = (request.get_json(silent=True) or {}).get("q", "") or request.form.get("q", "")
        if "cid" in session:
            return jsonify(tr_answer(helper.customer(session["cid"], text[:300], netbanking_docs(session["cid"]),
                                                     current()[1])))
        return jsonify(tr_answer(helper.public(text[:300], public_docs())))

    @app.route("/oauth/authorize", methods=["GET", "POST"])
    def oauth_authorize():
        args = request.values
        try:
            ob_app, scopes = bank.check_authorize(args)
        except BankError as e:
            return render_template("public/error.html", code=400, title="This app cannot connect",
                                   message=str(e)), 400
        if "cid" not in session:
            session["after_login"] = request.full_path
            flash("Please log in to Net Banking to answer the app's request.", "info")
            return redirect(url_for("login"))
        back = lambda **kw: redirect(ob_app["redirect_uri"] + ("&" if "?" in ob_app["redirect_uri"] else "?")   # noqa: E731
                                     + urlencode({**kw, **({"state": args["state"]} if args.get("state") else {})}))
        if request.method == "POST":
            if request.form.get("decision") != "allow":
                return back(error="access_denied")
            customer = bank.get_customer(session["cid"])
            if customer.status != "Active":
                flash("Your account is not active.", "error")
                return redirect(url_for("dashboard"))
            code = bank.give_consent(session["cid"], ob_app, scopes, args.get("code_challenge", ""))
            return back(code=code)
        customer, account = current()
        return render_template("netbanking/consent.html", customer=customer, account=account, app_=ob_app,
                               scopes=scopes, args=args)

    @app.route("/oauth/token", methods=["POST"])
    def oauth_token():
        try:
            return jsonify(bank.token_for_code(request.form))
        except BankError as e:
            return jsonify({"error": str(e)}), 400

    def ob_consent(scope):
        auth = request.headers.get("Authorization", "")
        consent = bank.consent_for_token(auth[7:]) if auth.startswith("Bearer ") else None
        if consent is None:
            abort(Response('{"error": "invalid_token"}', 401, mimetype="application/json"))
        if scope not in consent["scope_list"]:
            abort(Response('{"error": "insufficient_scope"}', 403, mimetype="application/json"))
        if not bank.rate_ok("api", "ob" + str(consent["id"])):
            abort(Response('{"error": "too_many_requests"}', 429, mimetype="application/json"))
        return consent

    @app.route("/openbanking/v1/accounts")
    def ob_accounts():
        c = ob_consent("accounts")
        return jsonify({"accounts": [{"number": a.number, "type": a.type, "currency": a.currency, "balance": a.balance,
                                      "balance_text": format_money(a.balance, a.currency), "status": a.status}
                                     for a in bank.accounts_of(c["customer_id"]) if a.status != "Closed"]})

    @app.route("/openbanking/v1/accounts/<number>/transactions")
    def ob_transactions(number):
        c = ob_consent("transactions")
        try:
            acc = bank.account_of(c["customer_id"], number)
        except BankError:
            abort(404)
        since = date.today().replace(year=date.today().year - 1).isoformat()
        rows = bank.transactions(acc.number, start=since, limit=min(int(request.args.get("limit", 100) or 100), 500))
        return jsonify({"account": acc.number, "currency": acc.currency,
                        "transactions": [{"ref": t.ref, "date": t.created_at, "type": t.type, "category": t.category,
                                          "description": t.description, "amount": t.amount} for t in rows]})

    @app.route("/openbanking/v1/me")
    def ob_me():
        c = ob_consent("profile")
        cu = bank.get_customer(c["customer_id"])
        return jsonify({"customer_id": cu.customer_id, "name": cu.full_name, "email": cu.email, "mobile": cu.mobile,
                        "country": cu.country})

    # =====================================================================
    # Net Banking
    # =====================================================================

    @app.route("/netbanking/name-enquiry")
    @login_required
    def nb_name_enquiry():
        if bank.settings().get("name_enquiry", "1") != "1":
            return jsonify({"ok": False, "error": "Name lookup is switched off."})
        if not bank.rate_ok("name_enquiry", session["cid"]):
            return jsonify({"ok": False, "error": "Too many lookups. Please wait a minute."}), 429
        try:
            out = bank.name_enquiry(request.args.get("rail", ""), request.args)
            mine = {a.number for a in bank.accounts_of(session["cid"])}
            if request.args.get("account", "").replace(" ", "") in mine and request.args.get("rail", "").upper() in ("", "PYBANK"):
                out["own"] = True
            return jsonify(out)
        except BankError as e:
            return jsonify({"ok": False, "error": str(e)})

    @app.route("/netbanking/search")
    @login_required
    def nb_search():
        customer, account = current()
        q = request.args.get("q", "")[:120]
        res = smartsearch.search(q, netbanking_docs(customer.customer_id)) if q.strip() else None
        return render_template("netbanking/search.html", customer=customer, account=account, q=q, res=res)

    @app.route("/netbanking/suggest")
    @login_required
    def nb_suggest():
        return jsonify(smartsearch.suggest(request.args.get("q", "")[:60], netbanking_docs(session["cid"])))

    @app.route("/netbanking/assistant")
    @login_required
    def nb_assistant():
        customer, account = current()
        return render_template("netbanking/assistant.html", customer=customer, account=account)

    @app.route("/netbanking/savings", methods=["GET", "POST"])
    @login_required
    def savings_plans():
        customer, account = current()
        cid, f = customer.customer_id, request.form
        quote = None
        if request.method == "POST":
            action = f.get("action")
            try:
                if action in ("rd_quote", "rd_open"):
                    acc = bank.account_of(cid, f.get("account") or account.number)
                    amount = parse_amount(f.get("instalment", ""), acc.currency)
                    months = int(f.get("months") or 0)
                    if action == "rd_open":
                        rd = bank.open_rd(cid, acc.number, amount, months, f.get("pin", ""))
                        flash(f"Recurring deposit {rd} is open. The first instalment was paid.", "success")
                        return redirect(url_for("savings_plans"))
                    if months not in RD_TENURES:
                        raise BankError("Please choose how many months (6, 12, 24 or 36).")
                    quote = {**bank.rd_quote(cid, amount, months), "currency": acc.currency, "months": months,
                             "instalment": amount}
                elif action == "rd_close":
                    t = bank.close_rd(cid, int(f.get("id") or 0), f.get("pin", ""))
                    flash(f"Recurring deposit closed. {format_money(t.amount, bank.account_of(cid, t.account_number).currency)} "
                          f"was paid into your account.", "success")
                    return redirect(url_for("savings_plans"))
                elif action == "goal_new":
                    acc = bank.account_of(cid, f.get("account") or account.number)
                    bank.create_goal(cid, acc.number, f.get("name"), parse_amount(f.get("target", ""), acc.currency),
                                     f.get("target_date"))
                    flash("Goal created. Move money into it whenever you like.", "success")
                    return redirect(url_for("savings_plans"))
                elif action in ("goal_in", "goal_out"):
                    g = next((x for x in bank.goals(cid) if str(x["id"]) == f.get("id")), None)
                    if g is None:
                        raise BankError("Goal not found.")
                    bank.move_goal(cid, g["id"], parse_amount(f.get("amount", ""), g["currency"]), action == "goal_in",
                                   f.get("pin", ""))
                    flash("Money moved.", "success")
                    return redirect(url_for("savings_plans"))
                elif action == "goal_close":
                    bank.close_goal(cid, int(f.get("id") or 0), f.get("pin", ""))
                    flash("Goal closed; the money is back in your account.", "success")
                    return redirect(url_for("savings_plans"))
            except BankError as e:
                flash(str(e), "error")
        return render_template("netbanking/savings.html", customer=customer, account=account,
                               rds=bank.recurring_deposits(cid), goals=bank.goals(cid), RD_TENURES=RD_TENURES,
                               rd_rates={m: bank.rd_rate(cid, m) for m in RD_TENURES}, quote=quote, form=f,
                               accounts=[a for a in bank.accounts_of(cid) if a.status == "Active"],
                               min_rd=bank.rd_min(account.currency), today=date.today())

    @app.route("/netbanking/connected-apps", methods=["GET", "POST"])
    @login_required
    def connected_apps():
        customer, account = current()
        if request.method == "POST":
            try:
                bank.revoke_consent(customer.customer_id, int(request.form.get("id") or 0))
                flash("The app can no longer read your account.", "success")
            except BankError as e:
                flash(str(e), "error")
            return redirect(url_for("connected_apps"))
        return render_template("netbanking/connected_apps.html", customer=customer, account=account,
                               rows=bank.consents(customer.customer_id))

    # =====================================================================
    # Back office
    # =====================================================================

    @app.route("/admin/search")
    @staff_required()
    def office_search():
        me, q = request.staff, request.args.get("q", "")[:120]
        res = smartsearch.search(q, office_docs(me) + office_tx_docs(me, q)) if q.strip() else None
        return office_page("search.html", q=q, res=res)

    @app.route("/admin/suggest")
    @staff_required()
    def office_suggest():
        return jsonify(smartsearch.suggest(request.args.get("q", "")[:60], office_docs(request.staff)))

    @app.route("/admin/assistant", methods=["GET", "POST"])
    @staff_required()
    def office_assistant():
        if request.method == "POST":
            text = (request.get_json(silent=True) or {}).get("q", "") or request.form.get("q", "")
            me = request.staff
            return jsonify(tr_answer(helper.staff(office, me, text[:300], office_docs(me), lambda a: roles.can(me, a))))
        return office_page("assistant.html")

    @app.route("/admin/name-enquiry")
    @staff_required()
    def office_name_enquiry():
        try:
            return jsonify(bank.name_enquiry(request.args.get("rail", ""), request.args))
        except BankError as e:
            return jsonify({"ok": False, "error": str(e)})

    # ----- the counter

    @app.route("/admin/counter")
    @staff_required()
    def office_counter():
        q = request.args.get("q", "").strip()[:80]
        res = None
        if q:
            if q.upper().startswith("PB") and len(q) == 10:
                try:
                    bank.get_customer(q.upper())
                    return redirect(url_for("office_counter_customer", cid=q.upper()))
                except BankError:
                    pass
            if q.replace(" ", "").isdigit():
                try:
                    info = bank.name_enquiry_internal(q)
                    return redirect(url_for("office_counter_customer", cid=info["customer_id"]))
                except BankError:
                    pass
            res = smartsearch.search(q, [d for d in office_docs(request.staff) if d["kind"] == "Customer"], limit=15)
        me = request.staff
        return office_page("counter.html", q=q, res=res, tills=office.my_tills(me),
                           today_ops=office.counter_ops(staff_id=me["staff_id"], day=date.today().isoformat(), limit=20))

    @app.route("/admin/counter/<cid>")
    @staff_required()
    def office_counter_customer(cid):
        try:
            c, accounts = office.counter_customer(cid)
        except BankError:
            abort(404)
        me = request.staff
        return office_page("counter_customer.html", c=c, accounts=accounts, loans=[l for l in bank.loans(cid) if l["status"] == "Active"],
                           ops=office.counter_ops(cid=cid, limit=10), tills={t["currency"]: t for t in office.my_tills(me)},
                           RAILS=__import__("payments").RAILS, FD_TENURES=FD_TENURES, RD_TENURES=RD_TENURES,
                           limits={a.currency: checker_amount(a.currency) for a in accounts},
                           own_limit=me.get("max_amount") or 0, docs=bank.documents(cid))

    @app.route("/admin/counter/<cid>/<op>", methods=["POST"])
    @staff_required()
    def office_counter_op(cid, op):
        if op not in OPS:
            abort(404)
        try:
            kind, ident = office.counter(request.staff, cid, op, request.form)
            if kind == "waiting":
                flash("This amount needs a second person: it was sent to Second approvals. "
                      "The customer waits until it is approved.", "info")
                return redirect(url_for("office_counter_customer", cid=cid))
            flash(f"{OPS[op][0]} done.", "success")
            return redirect(url_for("office_receipt", op_id=ident))
        except BankError as e:
            flash(str(e), "error")
        return redirect(url_for("office_counter_customer", cid=cid) + f"#op-{op}")

    @app.route("/admin/receipt/<int:op_id>")
    @staff_required()
    def office_receipt(op_id):
        try:
            op = office.counter_op(op_id)
        except BankError:
            abort(404)
        return office_page("counter_receipt.html", op=op, c=bank.get_customer(op["customer_id"]))

    # ----- cash drawers

    @app.route("/admin/till", methods=["GET", "POST"])
    @staff_required()
    def office_till():
        me = request.staff
        if request.method == "POST":
            f = request.form
            try:
                if f.get("action") == "open":
                    cur = f.get("currency", "INR")
                    office.open_till(me, cur, parse_amount(f.get("opening", "0") or "0", cur) if (f.get("opening") or "0") not in ("0", "") else 0)
                    flash(f"Your {cur} cash drawer is open.", "success")
                elif f.get("action") == "close":
                    t = next((x for x in office.my_tills(me) if str(x["id"]) == f.get("id")), None)
                    if t is None:
                        raise BankError("Cash drawer not found.")
                    notes, by_notes = count_notes(t["currency"], f)           # v6: counted note by note
                    if notes:
                        counted = by_notes
                    else:
                        counted = (parse_amount(f.get("counted", "0") or "0", t["currency"])
                                   if (f.get("counted") or "0") not in ("0", "") else 0)
                    diff = office.close_till(me, t["id"], counted, f.get("note", ""), notes or None)
                    flash("Cash drawer closed - the cash matches." if diff == 0 else
                          f"Cash drawer closed with a difference of {format_money(diff, t['currency'])}. "
                          f"A supervisor must accept it.", "success" if diff == 0 else "info")
            except BankError as e:
                flash(str(e), "error")
            return redirect(url_for("office_till"))
        days = request.args.get("day") or date.today().isoformat()
        return office_page("till.html", tills=office.my_tills(me, days), day=days,
                           ops=office.counter_ops(staff_id=me["staff_id"], day=days, limit=100))

    @app.route("/admin/tills", methods=["GET", "POST"])
    @staff_required()
    def office_tills():
        if request.method == "POST":
            try:
                office.accept_till(request.staff, int(request.form.get("id") or 0), request.form.get("note", ""))
                flash("Difference accepted.", "success")
            except BankError as e:
                flash(str(e), "error")
            return redirect(url_for("office_tills"))
        day = request.args.get("day", "")
        status = request.args.get("status", "")
        return office_page("tills.html", rows=office.all_tills(day or None, status), day=day, status=status)

    # ----- staff, roles and abilities

    @app.route("/admin/roles", methods=["GET", "POST"])
    @staff_required()
    def office_roles():
        if request.method == "POST":
            try:
                code = office.save_role(request.staff, "", request.form.get("label"), request.form.getlist("abilities"))
                flash("Role created. Give it to staff members in Staff & roles.", "success")
                return redirect(url_for("office_roles") + f"#role-{code}")
            except BankError as e:
                flash(str(e), "error")
        return office_page("roles.html", job_roles=office.job_roles())

    @app.route("/admin/roles/<code>", methods=["POST"])
    @staff_required()
    def office_role_action(code):
        f = request.form
        try:
            if f.get("action") == "save":
                office.save_role(request.staff, code, f.get("label"), f.getlist("abilities"))
                flash("Role saved. It works from the next click of its people.", "success")
            elif f.get("action") == "reset":
                office.reset_role(request.staff, code)
                flash("Role reset to its starting abilities.", "success")
            elif f.get("action") == "delete":
                office.delete_role(request.staff, code)
                flash("Role deleted.", "success")
            else:
                abort(404)
        except BankError as e:
            flash(str(e), "error")
        return redirect(url_for("office_roles") + f"#role-{code}")

    @app.route("/admin/staff/<sid>/access", methods=["GET", "POST"])
    @staff_required()
    def office_staff_access(sid):
        target = office.get_staff(sid)
        if target is None:
            abort(404)
        if request.method == "POST":
            try:
                if request.form.get("form") == "details":
                    office.update_staff_details(request.staff, sid, request.form)
                    flash("Details saved.", "success")
                else:
                    office.set_staff_access(request.staff, sid, request.form)
                    flash("Abilities saved. They work from the next click of this person.", "success")
            except BankError as e:
                flash(str(e), "error")
            return redirect(url_for("office_staff_access", sid=sid))
        r = office.job_roles()
        base = r.get(target.get("job_role") or "officer", {"abilities": set()})["abilities"]
        grants = office.grants(sid)                                            # v6
        return office_page("staff_access.html", s=target, base=base, extra=roles.split_codes(target.get("extra_abilities")),
                           denied=roles.split_codes(target.get("denied_abilities")), job_roles=r, grants=grants,
                           grant_codes={g["ability"] for g in grants if not g["expired"]},
                           colleagues=[p for p in office.staff_list() if p["staff_id"] != sid and p.get("role") != "admin"
                                       and p.get("active")], history=office.ability_history(sid, 30))

    @app.route("/admin/staff-photo/<sid>.jpg")
    @staff_required()
    def staff_photo(sid):
        s = office.get_staff(sid)
        if not s or not s.get("photo"):
            abort(404)
        return Response(base64.b64decode(s["photo"]), mimetype="image/jpeg",
                        headers={"Cache-Control": "private, max-age=300"})

    @app.route("/admin/profile/photo", methods=["POST"])
    @staff_required()
    def office_profile_photo():
        me = request.staff
        if request.form.get("remove") == "1":
            office.set_own_photo(me, "")
            flash("Photo removed.", "success")
        else:
            f = request.files.get("photo")
            data = f.read(PHOTO_MAX + 1) if f else b""
            photo = small_photo(data) if data and len(data) <= PHOTO_MAX else None
            if not photo:
                flash("Please choose a picture (JPG, PNG or WEBP, at most 2 MB).", "error")
            else:
                office.set_own_photo(me, photo)
                flash("Photo saved.", "success")
        return redirect(url_for("office_profile"))

    # ----- sanctions screening and risk

    @app.route("/admin/screening", methods=["GET", "POST"])
    @staff_required()
    def office_screening():
        me, f = request.staff, request.form
        if request.method == "POST":
            try:
                action = f.get("action")
                if action == "decide":
                    bank.decide_hit(me, int(f.get("id") or 0), f.get("status"), f.get("note"))
                    flash("Decision saved.", "success")
                elif action == "save":
                    bank.save_sanction(me, f, int(f.get("id")) if (f.get("id") or "").isdigit() else None)
                    flash("List entry saved.", "success")
                elif action == "run":
                    n = bank.screen_all()
                    flash(f"All customers were compared with the list: {n} possible match(es).", "success")
                elif action == "check":
                    found = bank.screen_name(f.get("name", ""), f.get("dob", ""))
                    flash(("Possible match: " + "; ".join(f"{e['name']} ({int(s * 100)}%)" for e, s in found))
                          if found else "No match on the list.", "info" if found else "success")
            except BankError as e:
                flash(str(e), "error")
            return redirect(url_for("office_screening"))
        status = request.args.get("status", "Open")
        return office_page("screening.html", hits=bank.screening_hits(status), status=status, entries=bank.sanctions(),
                           summary=bank.risk_summary(), HIT_STATUSES=HIT_STATUSES,
                           high=[dict(c) for c in office.customers() if c.get("risk_rating") == "High"])

    @app.route("/admin/customers/<cid>/risk", methods=["POST"])
    @staff_required()
    def office_customer_risk(cid):
        try:
            bank.get_customer(cid)
            bank.set_risk(request.staff, cid, request.form.get("rating"), request.form.get("pep") == "1",
                          request.form.get("reason"))
            flash("Risk rating saved.", "success")
        except BankError as e:
            flash(str(e), "error")
        return redirect(url_for("office_customer", cid=cid) + "#risk")

    @app.route("/admin/customers/<cid>/screen", methods=["POST"])
    @staff_required()
    def office_customer_screen(cid):
        try:
            found = bank.screen_customer(cid)
            flash(f"Possible matches: {len(found)}." if found else "No match on the sanctions list.",
                  "info" if found else "success")
        except BankError as e:
            flash(str(e), "error")
        return redirect(url_for("office_customer", cid=cid) + "#risk")

    # ----- open banking

    @app.route("/admin/openbanking", methods=["GET", "POST"])
    @staff_required()
    def office_openbanking():
        secret = None
        if request.method == "POST":
            f = request.form
            try:
                if f.get("action") == "register":
                    client_id, sec = bank.register_ob_app(request.staff, f.get("name"), f.get("company"), f.get("redirect_uri"))
                    secret = {"client_id": client_id, "secret": sec}
                    flash("App registered. Copy the secret now - it is shown only once.", "success")
                else:
                    bank.set_ob_app_active(request.staff, f.get("client_id"), f.get("action") == "start")
                    flash("App updated.", "success")
                    return redirect(url_for("office_openbanking"))
            except BankError as e:
                flash(str(e), "error")
        return office_page("openbanking.html", apps=bank.ob_apps(), consents=bank.consents()[:100], secret=secret)

    # ----- data manager

    @app.route("/admin/data")
    @staff_required()
    def office_data():
        return office_page("data.html", tables=data_manager.tables(), others=data_manager.read_only_tables())

    @app.route("/admin/data/<table>", methods=["GET", "POST"])
    @staff_required()
    def office_data_table(table):
        info = data_manager.info(table)
        if info is None:
            abort(404)
        if request.method == "POST":
            if info["readonly"]:
                abort(405)
            try:
                key = data_manager.add(request.staff, table, request.form)
                flash("Record added.", "success")
                return redirect(url_for("office_data_row", table=table, key=key))
            except BankError as e:
                flash(str(e), "error")
        q = request.args.get("q", "")[:80]
        sort, desc = request.args.get("sort", ""), request.args.get("desc") == "1"
        page = max(1, int(request.args.get("page") or 1)) if (request.args.get("page") or "1").isdigit() else 1
        rows, total = data_manager.page(table, q, sort, desc, page)
        cols = data_manager.columns(table)
        return office_page("data_table.html", t=table, info=info, q=q, rows=rows, cols=cols, total=total, page=page,
                           pages=max(1, -(-total // PER_PAGE)), sort=sort if sort in cols else "", desc=desc,
                           keycol=data_manager._key_col(table))

    @app.route("/admin/data/<table>/<path:key>", methods=["GET", "POST"])
    @staff_required()
    def office_data_row(table, key):
        info = data_manager.info(table)
        if info is None:
            abort(404)
        if request.method == "POST":
            if info["readonly"]:
                abort(405)
            try:
                if request.form.get("action") == "delete":
                    data_manager.delete(request.staff, table, key)
                    flash("Record deleted.", "success")
                    return redirect(url_for("office_data_table", table=table))
                data_manager.save(request.staff, table, key, request.form)
                flash("Record saved.", "success")
            except BankError as e:
                flash(str(e), "error")
            return redirect(url_for("office_data_row", table=table, key=key))
        try:
            row = data_manager.row(table, key)
        except BankError:
            abort(404)
        return office_page("data_row.html", t=table, info=info, key=key, row=row,
                           cols=data_manager.columns(table))

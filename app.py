"""
app.py - Flask web application (routes) for PyBank, the Python-Based Banking System.

Run:  python app.py   then open http://127.0.0.1:5000
Demo login:  Customer ID PB10000001, password Demo@1234, transaction PIN 2580

Database: SQLite file bank.db on your computer, or PostgreSQL when DATABASE_URL is set
(needed online - free hosts like Render delete local files at every restart).
Languages: English, Russian, French, Spanish, Arabic and Chinese (translations/*.json).
"""

import csv
import io
import json
import os
import secrets
import time
from dataclasses import asdict
from datetime import date, datetime, timedelta
from functools import wraps

from flask import (Flask, Response, abort, flash, jsonify, redirect, render_template, request,
                   send_from_directory, session, url_for)

import demo_data
import i18n
import otp
import world
from admin import NOTICE_LEVELS, REQUEST_STATUSES, InternationalOffice
from bank import (ACCOUNT_TYPES, BRANCH, FD_TENURES, LOAN_PRODUCTS, LOAN_TENURES, REQUEST_TYPES, Bank, BankError,
                  parse_amount)
from onboarding import DOC_KINDS, FIELD_LABELS
from payments import RAILS
from world import format_money, policy

SESSION_IDLE_MINUTES = 10
STAFF_IDLE_MINUTES = 30

STATES = ["Andhra Pradesh", "Bihar", "Chandigarh", "Delhi", "Gujarat", "Haryana", "Himachal Pradesh",
          "Jammu and Kashmir", "Karnataka", "Kerala", "Madhya Pradesh", "Maharashtra", "Odisha",
          "Punjab", "Rajasthan", "Tamil Nadu", "Telangana", "Uttar Pradesh", "Uttarakhand", "West Bengal"]
OCCUPATIONS = ["Salaried", "Self-employed", "Business", "Student", "Retired", "Homemaker", "Other"]


def create_app(db_path=None, seed=True, notifier=None, driver=None, housekeeping=True, fx_fetch=None):
    app = Flask(__name__)
    app.config["SESSION_COOKIE_HTTPONLY"] = True
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
    app.config["MAX_CONTENT_LENGTH"] = 20 * 1024 * 1024          # backup uploads

    target = db_path or os.environ.get("DATABASE_URL") or os.path.join(app.root_path, "bank.db")
    bank = Bank(target, notifier=notifier, driver=driver, fx_fetch=fx_fetch)
    office = InternationalOffice(bank)
    # the key that signs login cookies must stay the same after a restart and for every worker
    app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY") or bank.secret_key()
    if seed:
        demo_data.seed(bank, office)
    app.bank, app.office = bank, office
    app.config["HOUSEKEEPING"] = housekeeping
    app.config["LAST_HOUSEKEEPING"] = None
    app.config["LAST_PAYMENTS_RUN"] = 0.0

    # ---------- template helpers ----------

    app.jinja_env.filters["money"] = lambda value, currency=None: format_money(value or 0, currency)

    @app.template_filter("nicedate")
    def nicedate(value, with_time=True):
        return world.format_when(value, with_time)

    @app.template_filter("t")
    def translate_filter(text):
        """Translate text that is put inside JavaScript or attributes the page translator does not see."""
        return i18n.translate_text(text)

    @app.context_processor
    def globals_():
        today = date.today()
        try:
            max_dob = today.replace(year=today.year - 18)
        except ValueError:                      # 29 February
            max_dob = today.replace(year=today.year - 18, day=28)
        settings = bank.settings()
        cur = world.current_currency()
        lang = world.current_lang()
        return {"BRANCH": BRANCH, "BRANCHES": world.BRANCHES, "ACCOUNT_TYPES": ACCOUNT_TYPES, "today": today,
                "max_dob": max_dob.isoformat(), "now_hour": datetime.now().hour,
                "DAILY_WITHDRAW_LIMIT": bank.limit_for("withdraw", cur),
                "DAILY_TRANSFER_LIMIT": bank.limit_for("transfer", cur), "CURRENCY": cur,
                "SETTINGS": settings, "FD_RATES": bank.fd_rates(), "NOTICES": bank.active_notices(),
                "LANG": lang, "LANGUAGES": world.LANGUAGES, "RTL": lang in world.RTL_LANGUAGES,
                "CURRENCIES": world.CURRENCIES, "policy": policy, "country_name": world.country_name,
                "currency_name": world.currency_name}

    # ---------- CSRF protection: every form carries a secret token ----------

    def csrf_token():
        if "csrf_token" not in session:
            session["csrf_token"] = secrets.token_hex(16)
        return session["csrf_token"]

    app.jinja_env.globals["csrf_token"] = csrf_token

    @app.before_request
    def language_and_currency():
        """Language of the page: chosen by the user (session), else the browser's language, else English.
        Currency: the currency of the logged-in customer's account."""
        lang = session.get("lang")
        if lang not in world.LANGUAGES:
            lang = request.accept_languages.best_match(list(world.LANGUAGES)) or "en"
        if request.path.startswith("/admin"):
            lang = "en"                               # the back office is in English for all staff
        world.set_context(lang=lang, country="IN", currency=world.DEFAULT_CURRENCY)
        if "cid" in session and request.endpoint != "static":
            try:
                c, acc = bank.get_customer(session["cid"]), bank.get_account(session["cid"])
                world.set_context(country=c.country, currency=acc.currency)
            except BankError:
                session.pop("cid", None)

    @app.before_request
    def settle_payments():
        """Payments to other banks (NEFT batches, SWIFT) are settled at most once a minute."""
        if request.endpoint == "static" or time.time() - app.config["LAST_PAYMENTS_RUN"] < 60:
            return
        app.config["LAST_PAYMENTS_RUN"] = time.time()
        try:
            bank.process_external()
        except Exception as e:                        # never block the website because of this
            app.logger.warning("payment processing failed: %s", e)

    @app.before_request
    def housekeeping():
        """Once a day: pay savings interest after a quarter ends and make the automatic backup."""
        if not app.config["HOUSEKEEPING"] or request.endpoint == "static":
            return
        today = date.today()
        if app.config["LAST_HOUSEKEEPING"] == today:
            return
        app.config["LAST_HOUSEKEEPING"] = today
        try:
            bank.daily_tasks(today)
        except Exception as e:                        # never block the website because of this
            app.logger.warning("daily tasks failed: %s", e)

    @app.before_request
    def security_checks():
        if request.method == "POST":
            sent = request.form.get("csrf_token", "") or request.headers.get("X-CSRF-Token", "")
            if not sent or not secrets.compare_digest(sent, session.get("csrf_token", "")):
                abort(400)
        # log out automatically after 10 minutes without activity
        if "cid" in session:
            if time.time() - session.get("last_active", 0) > SESSION_IDLE_MINUTES * 60:
                session.pop("cid", None)
                flash("Your session expired for your security. Please log in again.", "info")
                return redirect(url_for("login"))
            session["last_active"] = time.time()
        if "staff_id" in session:
            if time.time() - session.get("staff_active", 0) > STAFF_IDLE_MINUTES * 60:
                session.pop("staff_id", None)
                flash("Back-office session expired. Please sign in again.", "info")
                return redirect(url_for("staff_login"))
            session["staff_active"] = time.time()

    @app.after_request
    def no_cache(resp):
        # pages with account data must not be stored by the browser
        if request.path.startswith("/netbanking"):
            resp.headers["Cache-Control"] = "no-store"
        resp.headers["X-Frame-Options"] = "DENY"
        lang = world.current_lang()
        if lang != "en" and resp.mimetype == "text/html" and not resp.direct_passthrough:
            resp.set_data(i18n.translate_html(resp.get_data(as_text=True), lang))
        return resp

    @app.route("/lang/<code>")
    def set_language(code):
        if code in world.LANGUAGES:
            session["lang"] = code
            if "cid" in session:
                bank.set_language(session["cid"], code)        # alerts and statements follow the choice
        nxt = request.args.get("next", "")
        return redirect(nxt if nxt.startswith("/") and not nxt.startswith("//") else url_for("index"))

    # ---------- the phone app (Progressive Web App) ----------

    @app.route("/manifest.webmanifest")
    def manifest():
        return send_from_directory(os.path.join(app.root_path, "static"), "manifest.webmanifest",
                                   mimetype="application/manifest+json")

    @app.route("/sw.js")
    def service_worker():
        resp = send_from_directory(os.path.join(app.root_path, "static"), "sw.js", mimetype="text/javascript")
        resp.headers["Cache-Control"] = "no-cache"
        return resp

    def login_required(view):
        @wraps(view)
        def wrapper(*args, **kwargs):
            if "cid" not in session:
                flash("Please log in to continue.", "info")
                return redirect(url_for("login"))
            return view(*args, **kwargs)
        return wrapper

    def current():
        cid = session["cid"]
        return bank.get_customer(cid), bank.get_account(cid)

    # =====================================================================
    # Public website
    # =====================================================================

    @app.route("/")
    def index():
        return render_template("public/index.html")

    @app.route("/about")
    def about():
        return render_template("public/about.html")

    @app.route("/rates")
    def rates():
        return render_template("public/rates.html")

    @app.route("/contact")
    def contact():
        return render_template("public/contact.html")

    @app.route("/faq")
    def faq():
        return render_template("public/faq.html")

    @app.route("/open-account", methods=["GET", "POST"])
    def register():
        if bank.settings()["online_account_opening"] != "1":
            flash("Online account opening is paused. Please visit your nearest PyBank branch.", "info")
            return redirect(url_for("index"))
        lang = world.current_lang()
        form = request.form if request.method == "POST" else {"country": "RU" if lang == "ru" else "IN"}
        page = lambda: render_template("public/register.html", form=form, states=STATES,       # noqa: E731
                                       occupations=OCCUPATIONS, countries=world.countries(lang),
                                       dial=world.DIAL_CODES)
        if request.method == "POST":
            try:
                if form.get("password") != form.get("password2"):
                    raise BankError("The two passwords do not match.")
                if form.get("pin") != form.get("pin2"):
                    raise BankError("The two transaction PINs do not match.")
                if form.get("terms") != "yes":
                    raise BankError("Please accept the terms and conditions.")
                doc = request.files.get("id_doc")
                docs = [("ID proof", doc.filename, doc.read())] if doc and doc.filename else []
                cid, account = bank.register({**form.to_dict(), "lang": lang}, online=True, documents=docs)
            except BankError as e:
                flash(str(e), "error")
                return page(), 400
            return render_template("public/registered.html", cid=cid, account=account,
                                   customer=bank.get_customer(cid))
        return page()

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if "cid" in session:
            return redirect(url_for("dashboard"))
        if request.method == "POST":
            try:
                c = bank.authenticate(request.form.get("login", ""), request.form.get("password", ""),
                                      request.remote_addr or "unknown")
            except BankError as e:
                flash(str(e), "error")
                return render_template("public/login.html", login=request.form.get("login", "")), 401
            keep = {k: session[k] for k in ("csrf_token", "staff_id", "staff_active") if k in session}
            lang = session.get("lang")
            session.clear()                    # new session after login
            session.update(**keep)
            if lang in world.LANGUAGES:        # the language picked before login is remembered
                session["lang"] = lang
                bank.set_language(c.customer_id, lang)
            elif c.lang in world.LANGUAGES and c.lang != "en":
                session["lang"] = c.lang
            if c.totp_enabled:                 # second step: code from the authenticator app
                session.update(pending_cid=c.customer_id, pending_at=time.time(), pending_tries=0,
                               pending_previous=c.last_login)
                return redirect(url_for("login_verify"))
            session.update(cid=c.customer_id, last_active=time.time(), previous_login=c.last_login)
            return redirect(url_for("dashboard"))
        return render_template("public/login.html", login="")

    @app.route("/login/verify", methods=["GET", "POST"])
    def login_verify():
        cid = session.get("pending_cid")
        if not cid or time.time() - session.get("pending_at", 0) > 300:
            session.pop("pending_cid", None)
            flash("Please log in again.", "info")
            return redirect(url_for("login"))
        if request.method == "POST":
            try:
                bank.verify_2fa(cid, request.form.get("code", ""))
            except BankError as e:
                session["pending_tries"] = session.get("pending_tries", 0) + 1
                if session["pending_tries"] >= 5:
                    session.pop("pending_cid", None)
                    flash("Too many wrong codes. Please log in again.", "error")
                    return redirect(url_for("login"))
                flash(str(e), "error")
                return render_template("public/verify.html", who="customer"), 401
            previous = session.get("pending_previous", "")
            for k in ("pending_cid", "pending_at", "pending_tries", "pending_previous"):
                session.pop(k, None)
            session.update(cid=cid, last_active=time.time(), previous_login=previous)
            return redirect(url_for("dashboard"))
        return render_template("public/verify.html", who="customer")

    @app.route("/logout")
    def logout():
        for k in ("cid", "last_active", "previous_login", "transfer_quote"):
            session.pop(k, None)
        flash("You have been logged out securely. Thank you for banking with PyBank.", "success")
        return redirect(url_for("login"))

    # =====================================================================
    # Net banking (after login)
    # =====================================================================

    @app.route("/netbanking")
    @login_required
    def dashboard():
        customer, account = current()
        return render_template(
            "netbanking/dashboard.html", customer=customer, account=account,
            recent=bank.transactions(account.number, limit=6),
            summary=bank.month_summary(account.number),
            chart=bank.monthly_chart(account.number),
            previous_login=session.get("previous_login"),
            fds=[f for f in bank.fixed_deposits(customer.customer_id) if f["status"] == "Active"],
            requests_open=[r for r in bank.service_requests(customer.customer_id) if r["status"] in ("Open", "In progress")],
            pending_cash=[r for r in bank.cash_requests(customer.customer_id, 10) if r["status"] == "Pending"],
            loans_open=[ln for ln in bank.loans(customer.customer_id) if ln["status"] in ("Active", "Pending")],
            rub_view=rub_equivalent(account))

    def rub_equivalent(account):
        """Russian interface + an account in another currency: show the balance in rubles too (approximate)."""
        if world.current_lang() != "ru" or account.currency == "RUB":
            return None
        try:
            return bank.convert(account.balance, account.currency, "RUB")[0]
        except BankError:
            return None

    @app.route("/netbanking/account")
    @login_required
    def account_details():
        customer, account = current()
        return render_template("netbanking/account.html", customer=customer, account=account,
                               logins=bank.login_history(customer.customer_id, 8))

    def money_form(kind):
        """Shared code for deposit and withdraw pages. The customer sends a request; staff approve it."""
        customer, account = current()
        form = request.form if request.method == "POST" else {}
        status = 200
        if request.method == "POST":
            try:
                amount = parse_amount(form.get("amount", ""))
                ref = bank.request_cash(customer.customer_id, "Deposit" if kind == "deposit" else "Withdrawal",
                                        amount, form.get("pin", ""), form.get("note", ""))
                flash(f"Request {ref} for {format_money(amount, account.currency)} was sent to the branch. The money moves as soon as "
                      f"a bank officer approves it - you will get an alert.", "success")
                return redirect(url_for(kind))
            except BankError as e:
                flash(str(e), "error")
                status = 400
        return render_template("netbanking/money.html", kind=kind, is_dep=kind == "deposit", customer=customer,
                               account=account, form=form, available=bank.available_balance(account.number),
                               reqs=bank.cash_requests(customer.customer_id)), status

    @app.route("/netbanking/cash-requests/<int:rid>/cancel", methods=["POST"])
    @login_required
    def cancel_cash(rid):
        try:
            bank.cancel_cash_request(session["cid"], rid)
            flash("Request cancelled.", "success")
        except BankError as e:
            flash(str(e), "error")
        return redirect(request.form.get("next") if (request.form.get("next") or "").startswith("/netbanking")
                        else url_for("dashboard"))

    @app.route("/netbanking/deposit", methods=["GET", "POST"])
    @login_required
    def deposit():
        return money_form("deposit")

    @app.route("/netbanking/withdraw", methods=["GET", "POST"])
    @login_required
    def withdraw():
        return money_form("withdraw")

    @app.route("/netbanking/transfer", methods=["GET", "POST"])
    @login_required
    def transfer():
        """Step 1: enter details. Step 2: check beneficiary name, enter PIN. Then receipt."""
        customer, account = current()
        form = request.form if request.method == "POST" else {}
        step = form.get("step", "details")
        if request.method == "POST":
            try:
                to = form.get("to_account", "").strip()
                if to != form.get("to_account2", to).strip():
                    raise BankError("The two account numbers do not match.")
                if not (to.isdigit() and len(to) == 12):
                    raise BankError("Account number must be 12 digits.")
                if to == account.number:
                    raise BankError("You cannot transfer money to your own account.")
                amount = parse_amount(form.get("amount", ""), account.currency)
                if amount > account.balance:
                    raise BankError(f"Insufficient balance. Available balance is "
                                    f"{format_money(account.balance, account.currency)}.")
                beneficiary = bank.find_beneficiary(to)
                quote = bank.quote_internal(account.currency, to, amount)
                if step == "confirm":
                    t = bank.transfer(customer.customer_id, to, amount, form.get("pin", ""), form.get("remarks", ""))
                    flash("Funds transferred successfully.", "success")
                    return redirect(url_for("receipt", ref=t.ref))
                return render_template("netbanking/transfer_confirm.html", customer=customer,
                                       account=account, form=form, amount=amount, beneficiary=beneficiary, quote=quote)
            except BankError as e:
                flash(str(e), "error")
                if step == "confirm" and "PIN" in str(e):
                    amount = parse_amount(form["amount"], account.currency)
                    return render_template("netbanking/transfer_confirm.html", customer=customer,
                                           account=account, form=form, amount=amount,
                                           beneficiary=bank.find_beneficiary(form["to_account"]),
                                           quote=bank.quote_internal(account.currency, form["to_account"], amount)), 400
                return render_template("netbanking/transfer.html", customer=customer, account=account, form=form,
                                       payees=bank.beneficiaries(customer.customer_id)), 400
        if request.args.get("to"):
            form = {"to_account": request.args["to"], "to_account2": request.args["to"]}
        return render_template("netbanking/transfer.html", customer=customer, account=account, form=form,
                               payees=bank.beneficiaries(customer.customer_id))

    @app.route("/netbanking/receipt/<ref>")
    @login_required
    def receipt(ref):
        customer, account = current()
        try:
            t = bank.get_transaction(account.number, ref)
        except BankError:
            abort(404)
        return render_template("netbanking/receipt.html", customer=customer, account=account, t=t)

    def statement_filters():
        today = date.today()
        start = request.args.get("from") or (today - timedelta(days=30)).isoformat()
        end = request.args.get("to") or today.isoformat()
        kind = request.args.get("type", "")
        try:
            if date.fromisoformat(start) > date.fromisoformat(end):
                start, end = end, start
        except ValueError:
            start, end = (today - timedelta(days=30)).isoformat(), today.isoformat()
        return start, end, kind

    @app.route("/netbanking/statement")
    @login_required
    def statement():
        customer, account = current()
        start, end, kind = statement_filters()
        rows = bank.transactions(account.number, start, end, kind)
        return render_template("netbanking/statement.html", customer=customer, account=account,
                               rows=rows, start=start, end=end, kind=kind, notes=bank.tx_notes(customer.customer_id),
                               total_cr=sum(t.amount for t in rows if t.is_credit),
                               total_dr=sum(t.amount for t in rows if not t.is_credit))

    @app.route("/netbanking/statement.csv")
    @login_required
    def statement_csv():
        customer, account = current()
        start, end, kind = statement_filters()
        out = io.StringIO()
        w = csv.writer(out)
        w.writerow(["PyBank account statement"])
        w.writerow(["Customer", customer.full_name, "Account", account.number, "IFSC", account.ifsc])
        w.writerow(["Period", start, end])
        w.writerow([])
        cur = account.currency
        w.writerow(["Date", "Reference", "Description", f"Debit ({cur})", f"Credit ({cur})", f"Balance ({cur})"])
        for t in reversed(bank.transactions(account.number, start, end, kind)):
            amt = f"{t.amount / 100:.2f}"
            w.writerow([t.created_at, t.ref, t.description, "" if t.is_credit else amt,
                        amt if t.is_credit else "", f"{t.balance_after / 100:.2f}"])
        name = f"PyBank_statement_{account.number[-4:]}_{start}_to_{end}.csv"
        return Response("﻿" + out.getvalue(), mimetype="text/csv",
                        headers={"Content-Disposition": f"attachment; filename={name}"})

    @app.route("/netbanking/statement.pdf")
    @login_required
    def statement_pdf():
        customer, account = current()
        month = request.args.get("month") or date.today().strftime("%Y-%m")
        try:
            year, mon = (int(x) for x in month.split("-"))
            blob = bank.statement_pdf(customer.customer_id, year, mon, world.current_lang())
        except (ValueError, BankError):
            flash("Please choose a valid month.", "error")
            return redirect(url_for("statement"))
        return Response(blob, mimetype="application/pdf", headers={
            "Content-Disposition": f"inline; filename=PyBank_statement_{account.number[-4:]}_{year}-{mon:02d}.pdf"})

    @app.route("/netbanking/e-statements", methods=["GET", "POST"])
    @login_required
    def estatements():
        customer, account = current()
        if request.method == "POST":
            bank.set_estatement(customer.customer_id, request.form.get("on") == "1")
            flash("E-statement setting saved.", "success")
            return redirect(url_for("estatements"))
        months = []
        d = date.today().replace(day=1)
        opened = account.opened_at[:7]
        for _ in range(12):
            if d.strftime("%Y-%m") < opened:
                break
            months.append(d.strftime("%Y-%m"))
            d = date.fromordinal(d.toordinal() - 1).replace(day=1)
        return render_template("netbanking/estatements.html", customer=customer, account=account,
                               rows=bank.estatements(customer.customer_id), months=months)

    @app.route("/netbanking/e-statements/<int:sid>.pdf")
    @login_required
    def estatement_file(sid):
        try:
            month, blob = bank.estatement_blob(session["cid"], sid)
        except BankError:
            abort(404)
        return Response(blob, mimetype="application/pdf",
                        headers={"Content-Disposition": f"inline; filename=PyBank_statement_{month}.pdf"})

    @app.route("/netbanking/profile", methods=["GET", "POST"])
    @login_required
    def profile():
        """Personal data. Changes are sent to the bank and applied after a staff member approves them."""
        customer, account = current()
        lang = world.current_lang()
        form = asdict(customer)
        status = 200
        if request.method == "POST":
            form = request.form
            try:
                doc = request.files.get("proof")
                docs = [(form.get("proof_kind", "ID proof"), doc.filename, doc.read())] if doc and doc.filename else []
                bank.request_profile_change(customer.customer_id, form, docs)
                flash("Your change was sent to the bank. Your details change as soon as a bank officer approves it.",
                      "success")
                return redirect(url_for("profile"))
            except BankError as e:
                flash(str(e), "error")
                status = 400
        return render_template("netbanking/profile.html", customer=customer, account=account, states=STATES,
                               occupations=OCCUPATIONS, form=form, countries=world.countries(lang),
                               changes=bank.profile_changes(customer.customer_id), labels=FIELD_LABELS,
                               DOC_KINDS=DOC_KINDS, docs=bank.documents(customer.customer_id)), status

    @app.route("/netbanking/profile/changes/<int:pid>/cancel", methods=["POST"])
    @login_required
    def cancel_profile_change(pid):
        try:
            bank.cancel_profile_change(session["cid"], pid)
            flash("Request cancelled.", "success")
        except BankError as e:
            flash(str(e), "error")
        return redirect(url_for("profile"))

    @app.route("/netbanking/documents/<int:doc_id>")
    @login_required
    def my_document(doc_id):
        try:
            d = bank.document(doc_id, session["cid"])
        except BankError:
            abort(404)
        return Response(d["data"], mimetype=d["mime"], headers={"Content-Disposition": f"inline; filename={d['filename']}"})

    # ----- payments to other banks (sandbox)
    @app.route("/netbanking/transfer/other-bank", methods=["GET", "POST"])
    @login_required
    def other_bank():
        customer, account = current()
        form = request.form if request.method == "POST" else {"rail": "SWIFT" if account.currency != "INR" else "IMPS"}
        if request.method == "POST":
            step = form.get("step", "details")
            try:
                amount = parse_amount(form.get("amount", ""), account.currency)
                if step == "confirm":
                    ref = bank.send_external(customer.customer_id, form.get("rail"), form, amount, form.get("pin", ""))
                    flash("Payment sent.", "success")
                    return redirect(url_for("payment_detail", ref=ref))
                if amount > bank.available_balance(account.number):
                    raise BankError(f"Insufficient balance. Available balance is "
                                    f"{format_money(bank.available_balance(account.number), account.currency)}.")
                if form.get("rail") not in RAILS:
                    raise BankError("Please choose how to send the money (IMPS, NEFT, UPI or SWIFT).")
                return render_template("netbanking/other_bank_confirm.html", customer=customer, account=account,
                                       form=form, amount=amount, rail=RAILS[form["rail"]])
            except BankError as e:
                flash(str(e), "error")
                return render_template("netbanking/other_bank.html", customer=customer, account=account, form=form,
                                       RAILS=RAILS, countries=world.countries(world.current_lang()),
                                       payments=bank.external_transfers(customer.customer_id, limit=10)), 400
        return render_template("netbanking/other_bank.html", customer=customer, account=account, form=form, RAILS=RAILS,
                               countries=world.countries(world.current_lang()),
                               payments=bank.external_transfers(customer.customer_id, limit=10))

    @app.route("/netbanking/payments/<ref>")
    @login_required
    def payment_detail(ref):
        customer, account = current()
        try:
            p = bank.external_transfer(customer.customer_id, ref)
        except BankError:
            abort(404)
        return render_template("netbanking/payment.html", customer=customer, account=account, p=p, rail=RAILS[p["rail"]])

    @app.route("/netbanking/security", methods=["GET", "POST"])
    @login_required
    def security():
        customer, account = current()
        if request.method == "POST":
            f = request.form
            try:
                if f.get("action") == "2fa_start":
                    session["new_totp"] = otp.new_secret()
                    return redirect(url_for("security") + "#two-factor")
                if f.get("action") == "2fa_confirm":
                    bank.enable_2fa(customer.customer_id, session.get("new_totp", ""), f.get("code", ""))
                    session.pop("new_totp", None)
                    flash("Two-factor login is ON. From now on you also need the code from your app to log in.", "success")
                    return redirect(url_for("security"))
                if f.get("action") == "2fa_disable":
                    bank.disable_2fa(customer.customer_id, f.get("password", ""), f.get("code", ""))
                    flash("Two-factor login is OFF.", "success")
                    return redirect(url_for("security"))
                if f.get("action") == "password":
                    if f.get("new_password") != f.get("new_password2"):
                        raise BankError("The two new passwords do not match.")
                    bank.change_password(customer.customer_id, f.get("old_password", ""), f.get("new_password", ""))
                    flash("Your password was changed.", "success")
                else:
                    if f.get("new_pin") != f.get("new_pin2"):
                        raise BankError("The two new PINs do not match.")
                    bank.change_pin(customer.customer_id, f.get("password", ""), f.get("new_pin", ""))
                    flash("Your transaction PIN was changed.", "success")
            except BankError as e:
                flash(str(e), "error")
                return security_page(customer, account), 400
            return redirect(url_for("security"))
        return security_page(customer, account)

    def security_page(customer, account):
        secret = session.get("new_totp") if not customer.totp_enabled else None
        return render_template("netbanking/security.html", customer=customer, account=account, new_secret=secret,
                               new_secret_grouped=otp.grouped(secret) if secret else "",
                               otp_uri=otp.uri(secret, customer.customer_id) if secret else "")

    # =====================================================================
    # Loans, alerts
    # =====================================================================

    @app.route("/netbanking/loans", methods=["GET", "POST"])
    @login_required
    def loans():
        customer, account = current()
        form = request.form if request.method == "POST" else {}
        status = 200
        if request.method == "POST":
            try:
                docs = []
                for field, kind in (("id_doc", "ID proof"), ("income_doc", "Income proof")):
                    f = request.files.get(field)
                    if f and f.filename:
                        docs.append((kind, f.filename, f.read()))
                number = bank.apply_loan(customer.customer_id, form.get("product"),
                                         parse_amount(form.get("amount", ""), account.currency),
                                         form.get("months"), form.get("purpose"), form.get("pin", ""), documents=docs)
                flash(f"Loan application {number} was sent to the branch. You will get an alert when it is decided.", "success")
                return redirect(url_for("loans"))
            except BankError as e:
                flash(str(e), "error")
                status = 400
        rates = {p: bank.loan_rate(p) for p in LOAN_PRODUCTS}
        return render_template("netbanking/loans.html", customer=customer, account=account, loans=bank.loans(customer.customer_id),
                               form=form, PRODUCTS=LOAN_PRODUCTS, TENURES=LOAN_TENURES, RATES=rates,
                               LOAN_MIN=policy(account.currency, "loan_min"), LOAN_MAX=policy(account.currency, "loan_max"),
                               score=bank.credit_report(customer.customer_id), late_fee=bank.settings()["late_fee_percent"]), status

    @app.route("/netbanking/loans/<int:loan_id>")
    @login_required
    def loan_detail(loan_id):
        customer, account = current()
        try:
            ln = bank.get_loan(customer.customer_id, loan_id)
        except BankError:
            abort(404)
        return render_template("netbanking/loan.html", customer=customer, account=account, ln=ln,
                               available=bank.available_balance(account.number),
                               late_fee=bank.settings()["late_fee_percent"])

    @app.route("/netbanking/loans/<int:loan_id>/<action>", methods=["POST"])
    @login_required
    def loan_action(loan_id, action):
        cid = session["cid"]
        try:
            if action == "pay":
                t = bank.pay_emi(cid, loan_id, request.form.get("pin", ""))
                flash(f"EMI of {format_money(t.amount, t.currency)} paid.", "success")
                return redirect(url_for("receipt", ref=t.ref))
            if action == "close":
                t = bank.close_loan(cid, loan_id, request.form.get("pin", ""))
                flash(f"Loan closed. {format_money(t.amount, t.currency)} was paid.", "success")
                return redirect(url_for("receipt", ref=t.ref))
            if action == "cancel":
                bank.cancel_loan_application(cid, loan_id)
                flash("Loan application cancelled.", "success")
                return redirect(url_for("loans"))
            abort(404)
        except BankError as e:
            flash(str(e), "error")
        return redirect(url_for("loan_detail", loan_id=loan_id))

    @app.route("/netbanking/alerts", methods=["GET", "POST"])
    @login_required
    def alerts():
        customer, account = current()
        if request.method == "POST":
            bank.set_alert_prefs(customer.customer_id, request.form.get("email"), request.form.get("sms"))
            flash("Alert settings saved.", "success")
            return redirect(url_for("alerts"))
        return render_template("netbanking/alerts.html", customer=customer, account=account,
                               rows=bank.alerts(customer.customer_id), providers=bank.notifier.status(),
                               devices=bank.push_subscriptions(customer.customer_id))

    @app.route("/netbanking/push/key")
    @login_required
    def push_key():
        return jsonify({"key": bank.vapid_keys()["public"]})

    @app.route("/netbanking/push/subscribe", methods=["POST"])
    @login_required
    def push_subscribe():
        try:
            bank.add_push_subscription(session["cid"], request.get_json(force=True, silent=True) or {},
                                       request.user_agent.string[:80])
        except BankError as e:
            return jsonify({"ok": False, "error": i18n.translate_text(str(e))}), 400
        return jsonify({"ok": True})

    @app.route("/netbanking/push/<action>", methods=["POST"])
    @login_required
    def push_action(action):
        try:
            if action == "remove":
                bank.remove_push_subscription(session["cid"], int(request.form.get("id", "0") or 0))
                flash("This device will not get notifications any more.", "success")
            elif action == "test":
                bank.test_push(session["cid"])
                flash("Test notification sent. It should appear on your phone in a few seconds.", "success")
            else:
                abort(404)
        except BankError as e:
            flash(str(e), "error")
        return redirect(url_for("alerts"))

    # =====================================================================
    # Customer: things I create and manage myself
    # =====================================================================

    @app.route("/netbanking/note/<ref>", methods=["POST"])
    @login_required
    def tx_note(ref):
        try:
            bank.set_tx_note(session["cid"], ref, request.form.get("note", ""))
            flash("Note saved." if request.form.get("note", "").strip() else "Note removed.", "success")
        except BankError:
            abort(404)
        return redirect(request.form.get("next") if (request.form.get("next") or "").startswith("/netbanking")
                        else url_for("statement"))

    @app.route("/netbanking/beneficiaries", methods=["GET", "POST"])
    @login_required
    def payees():
        customer, account = current()
        if request.method == "POST":
            try:
                bank.add_beneficiary(customer.customer_id, request.form.get("account_number"), request.form.get("nickname"))
                flash("Beneficiary added. You can now send money with one click.", "success")
                return redirect(url_for("payees"))
            except BankError as e:
                flash(str(e), "error")
        return render_template("netbanking/payees.html", customer=customer, account=account,
                               payees=bank.beneficiaries(customer.customer_id), form=request.form)

    @app.route("/netbanking/beneficiaries/<int:bid>/<action>", methods=["POST"])
    @login_required
    def payee_action(bid, action):
        try:
            if action == "rename":
                bank.rename_beneficiary(session["cid"], bid, request.form.get("nickname"))
                flash("Nickname changed.", "success")
            elif action == "delete":
                bank.delete_beneficiary(session["cid"], bid)
                flash("Beneficiary deleted.", "success")
            else:
                abort(404)
        except BankError as e:
            flash(str(e), "error")
        return redirect(url_for("payees"))

    @app.route("/netbanking/fixed-deposits", methods=["GET", "POST"])
    @login_required
    def fixed_deposits():
        customer, account = current()
        if request.method == "POST":
            try:
                fd_no = bank.open_fd(customer.customer_id, parse_amount(request.form.get("amount", ""), account.currency),
                                     request.form.get("months", ""), request.form.get("pin", ""))
                flash(f"Fixed deposit {fd_no} opened successfully.", "success")
                return redirect(url_for("fixed_deposits"))
            except BankError as e:
                flash(str(e), "error")
                customer, account = current()
        return render_template("netbanking/fixed_deposits.html", customer=customer, account=account,
                               fds=bank.fixed_deposits(customer.customer_id), FD_TENURES=FD_TENURES,
                               FD_MIN=policy(account.currency, "fd_min"), form=request.form)

    @app.route("/netbanking/fixed-deposits/<int:fd_id>/close", methods=["POST"])
    @login_required
    def close_fd(fd_id):
        try:
            t = bank.close_fd(session["cid"], fd_id, request.form.get("pin", ""))
            flash(f"Fixed deposit closed. {format_money(t.amount, t.currency)} was credited to your account.", "success")
            return redirect(url_for("receipt", ref=t.ref))
        except BankError as e:
            flash(str(e), "error")
        return redirect(url_for("fixed_deposits"))

    @app.route("/netbanking/requests", methods=["GET", "POST"])
    @login_required
    def service_requests():
        customer, account = current()
        if request.method == "POST":
            try:
                rid = bank.create_request(customer.customer_id, request.form.get("type"), request.form.get("details"))
                flash(f"Request #{rid} sent to the branch. We will reply here.", "success")
                return redirect(url_for("service_requests"))
            except BankError as e:
                flash(str(e), "error")
        return render_template("netbanking/requests.html", customer=customer, account=account,
                               requests=bank.service_requests(customer.customer_id), REQUEST_TYPES=REQUEST_TYPES,
                               form=request.form)

    @app.route("/netbanking/requests/<int:rid>/<action>", methods=["POST"])
    @login_required
    def request_action(rid, action):
        try:
            if action == "edit":
                bank.update_request(session["cid"], rid, request.form.get("type"), request.form.get("details"))
                flash("Request updated.", "success")
            elif action == "delete":
                bank.delete_request(session["cid"], rid)
                flash("Request cancelled and deleted.", "success")
            else:
                abort(404)
        except BankError as e:
            flash(str(e), "error")
        return redirect(url_for("service_requests"))

    # =====================================================================
    # Back office (bank staff and admin)
    # =====================================================================

    def staff_required(role=None):
        def deco(view):
            @wraps(view)
            def wrapper(*args, **kwargs):
                me = office.get_staff(session.get("staff_id", "")) if "staff_id" in session else None
                if not me or not me["active"]:
                    session.pop("staff_id", None)
                    flash("Please sign in to the back office.", "info")
                    return redirect(url_for("staff_login"))
                if role == "admin" and me["role"] != "admin":
                    abort(403)
                request.staff = me
                return view(*args, **kwargs)
            return wrapper
        return deco

    def office_page(template, **kw):
        return render_template(f"admin/{template}", me=request.staff, counts=office.overview(), **kw)

    # ----- account applications and changes of personal data
    @app.route("/admin/applications")
    @staff_required()
    def office_applications():
        return office_page("applications.html", rows=bank.pending_customers(),
                           changes=bank.profile_changes(status="Pending"), labels=FIELD_LABELS)

    @app.route("/admin/applications/<cid>/<action>", methods=["POST"])
    @staff_required()
    def office_application_action(cid, action):
        if action not in ("approve", "reject"):
            abort(404)
        try:
            row = office.decide_application(request.staff, cid, action == "approve", request.form.get("reason", ""))
            flash(f"Account of {row['full_name']} opened." if action == "approve"
                  else f"Application of {row['full_name']} rejected. The applicant was told why.", "success")
        except BankError as e:
            flash(str(e), "error")
        return redirect(url_for("office_applications"))

    @app.route("/admin/profile-changes/<int:pid>/<action>", methods=["POST"])
    @staff_required()
    def office_profile_change_action(pid, action):
        if action not in ("approve", "reject"):
            abort(404)
        try:
            office.decide_profile(request.staff, pid, action == "approve", request.form.get("reason", ""))
            flash("Change approved - the customer's details are updated." if action == "approve"
                  else "Change rejected. The customer was told why.", "success")
        except BankError as e:
            flash(str(e), "error")
        return redirect(url_for("office_applications"))

    @app.route("/admin/documents/<int:doc_id>")
    @staff_required()
    def office_document(doc_id):
        try:
            d = bank.document(doc_id)
        except BankError:
            abort(404)
        return Response(d["data"], mimetype=d["mime"], headers={"Content-Disposition": f"inline; filename={d['filename']}"})

    @app.route("/admin/payments")
    @staff_required()
    def office_payments():
        status = request.args.get("status", "")
        return office_page("payments.html", rows=bank.external_transfers(status=status, limit=300), status=status)

    @app.route("/admin/login", methods=["GET", "POST"])
    def staff_login():
        if request.method == "POST":
            try:
                me = office.authenticate_staff(request.form.get("login"), request.form.get("password"))
            except BankError as e:
                flash(str(e), "error")
                return render_template("admin/login.html", login=request.form.get("login", "")), 401
            if me.get("totp_enabled"):
                session.update(pending_staff=me["staff_id"], pending_staff_at=time.time(), pending_staff_tries=0)
                return redirect(url_for("staff_verify"))
            session["staff_id"], session["staff_active"] = me["staff_id"], time.time()
            return redirect(url_for("office_dashboard"))
        return render_template("admin/login.html", login="")

    @app.route("/admin/login/verify", methods=["GET", "POST"])
    def staff_verify():
        sid = session.get("pending_staff")
        if not sid or time.time() - session.get("pending_staff_at", 0) > 300:
            session.pop("pending_staff", None)
            return redirect(url_for("staff_login"))
        if request.method == "POST":
            try:
                office.verify_staff_2fa(sid, request.form.get("code", ""))
            except BankError as e:
                session["pending_staff_tries"] = session.get("pending_staff_tries", 0) + 1
                if session["pending_staff_tries"] >= 5:
                    session.pop("pending_staff", None)
                    flash("Too many wrong codes. Please sign in again.", "error")
                    return redirect(url_for("staff_login"))
                flash(str(e), "error")
                return render_template("public/verify.html", who="staff"), 401
            for k in ("pending_staff", "pending_staff_at", "pending_staff_tries"):
                session.pop(k, None)
            session["staff_id"], session["staff_active"] = sid, time.time()
            return redirect(url_for("office_dashboard"))
        return render_template("public/verify.html", who="staff")

    @app.route("/admin/logout")
    def staff_logout():
        session.pop("staff_id", None)
        flash("You have signed out of the back office.", "success")
        return redirect(url_for("staff_login"))

    @app.route("/admin")
    @staff_required()
    def office_dashboard():
        return office_page("dashboard.html", recent=office.recent_transactions(limit=8),
                           requests=office.requests()[:6], audit=office.audit_log(8))

    # ----- customers
    @app.route("/admin/customers")
    @staff_required()
    def office_customers():
        return office_page("customers.html", rows=office.customers(request.args.get("q", "").strip(),
                                                                     request.args.get("status", "")), args=request.args)

    @app.route("/admin/customers/new", methods=["GET", "POST"])
    @staff_required()
    def office_customer_new():
        form = request.form if request.method == "POST" else {}
        if request.method == "POST":
            try:
                doc = request.files.get("id_doc")
                docs = [("ID proof", doc.filename, doc.read())] if doc and doc.filename else []
                cid, _ = bank.register(form.to_dict(), documents=docs)
                with bank._connect() as conn:
                    office.audit(conn, request.staff, "Customer account opened", cid, form.get("full_name", ""))
                flash(f"Account opened. Customer ID {cid}. Give the customer the password and PIN you entered.", "success")
                return redirect(url_for("office_customer", cid=cid))
            except BankError as e:
                flash(str(e), "error")
        return office_page("customer_new.html", form=form or {"country": "IN"}, states=STATES, occupations=OCCUPATIONS,
                           countries=world.countries("en"))

    @app.route("/admin/customers/<cid>")
    @staff_required()
    def office_customer(cid):
        try:
            customer, account = bank.get_customer(cid), bank.get_account(cid)
        except BankError:
            abort(404)
        world.set_context(currency=account.currency)          # amounts on this page are in this customer's currency
        with bank._connect() as conn:
            rows = [dict(r) for r in conn.execute("SELECT * FROM transactions WHERE account_number = ? "
                                                  "ORDER BY created_at DESC, id DESC LIMIT 40", (account.number,))]
        return office_page("customer.html", c=customer, account=account, tx=rows, cash=bank.cash_requests(cid, 10),
                           docs=bank.documents(cid), changes=bank.profile_changes(cid), labels=FIELD_LABELS,
                           payments=bank.external_transfers(cid, limit=10), countries=world.countries("en"),
                           score=bank.credit_report(cid) if customer.status == "Active" else None,
                           loans=bank.loans(cid), logins=bank.login_history(cid, 6), fds=bank.fixed_deposits(cid),
                           payees=bank.beneficiaries(cid), reqs=bank.service_requests(cid),
                           states=STATES, occupations=OCCUPATIONS, locked=customer.locked_until > datetime.now().strftime("%Y-%m-%d %H:%M:%S"))

    @app.route("/admin/customers/<cid>/<action>", methods=["POST"])
    @staff_required()
    def office_customer_action(cid, action):
        me, f = request.staff, request.form
        try:
            bank.get_customer(cid)
        except BankError:
            abort(404)
        try:
            if action == "edit":
                office.update_customer(me, cid, f)
                flash("Customer details saved.", "success")
            elif action == "unlock":
                office.unlock_customer(me, cid)
                flash("Login unlocked.", "success")
            elif action == "reset2fa":
                office.reset_customer_2fa(me, cid)
                flash("Two-factor login turned off. The customer can log in with the password and set it up again.", "success")
            elif action == "password":
                office.reset_customer_password(me, cid, f.get("password", ""))
                flash("New password set. Tell the customer to change it after login.", "success")
            elif action == "pin":
                office.reset_customer_pin(me, cid, f.get("pin", ""))
                flash("New transaction PIN set.", "success")
            elif action in ("freeze", "activate"):
                office.set_account_status(me, cid, "Frozen" if action == "freeze" else "Active", f.get("reason", ""))
                flash("Account frozen - no money can move." if action == "freeze" else "Account is active again.", "success")
            elif action == "type":
                office.change_account_type(me, cid, f.get("type"))
                flash("Account type changed.", "success")
            elif action == "entry":
                cur = bank.get_account(cid).currency
                t = office.manual_entry(me, cid, f.get("kind"), parse_amount(f.get("amount", ""), cur), f.get("description"))
                flash(f"Entry posted (ref {t.ref}). New balance {format_money(t.balance_after, cur)}.", "success")
            elif action == "delete":
                if me["role"] != "admin":
                    abort(403)
                office.delete_customer(me, cid)
                flash(f"Customer {cid} and all their records were deleted.", "success")
                return redirect(url_for("office_customers"))
            else:
                abort(404)
        except BankError as e:
            flash(str(e), "error")
        return redirect(url_for("office_customer", cid=cid))

    # ----- transactions
    @app.route("/admin/transactions")
    @staff_required()
    def office_transactions():
        a = request.args
        return office_page("transactions.html", rows=office.recent_transactions(
            a.get("q", "").strip(), a.get("type", ""), a.get("category", ""), a.get("from", ""), a.get("to", ""), 300),
            categories=office.categories(), args=a)

    @app.route("/admin/transactions/<int:tx_id>/reverse", methods=["POST"])
    @staff_required()
    def office_reverse(tx_id):
        try:
            t = office.reverse_transaction(request.staff, tx_id, request.form.get("reason"))
            flash(f"Transaction reversed (new ref {t.ref}).", "success")
        except BankError as e:
            flash(str(e), "error")
        return redirect(request.referrer or url_for("office_transactions"))

    # ----- service requests
    @app.route("/admin/requests")
    @staff_required()
    def office_requests():
        return office_page("requests.html", rows=office.requests(request.args.get("status", "")),
                           STATUSES=REQUEST_STATUSES, args=request.args)

    @app.route("/admin/requests/<int:rid>/<action>", methods=["POST"])
    @staff_required()
    def office_request_action(rid, action):
        try:
            if action == "answer":
                office.answer_request(request.staff, rid, request.form.get("status"), request.form.get("reply"))
                flash("Reply saved - the customer can see it in Net Banking.", "success")
            elif action == "delete":
                office.delete_request(request.staff, rid)
                flash("Request deleted.", "success")
            else:
                abort(404)
        except BankError as e:
            flash(str(e), "error")
        return redirect(url_for("office_requests", status=request.args.get("status", "")))

    # ----- notices (shown on the website and in Net Banking)
    @app.route("/admin/notices", methods=["GET", "POST"])
    @staff_required("admin")
    def office_notices():
        if request.method == "POST":
            f = request.form
            try:
                office.save_notice(request.staff, f.get("title"), f.get("body"), f.get("level"), f.get("active"),
                                   int(f["id"]) if f.get("id", "").isdigit() else None)
                flash("Notice saved.", "success")
                return redirect(url_for("office_notices"))
            except BankError as e:
                flash(str(e), "error")
        return office_page("notices.html", rows=office.notices(), LEVELS=NOTICE_LEVELS)

    @app.route("/admin/notices/<int:nid>/delete", methods=["POST"])
    @staff_required("admin")
    def office_notice_delete(nid):
        office.delete_notice(request.staff, nid)
        flash("Notice deleted.", "success")
        return redirect(url_for("office_notices"))

    # ----- staff (admin only)
    @app.route("/admin/staff", methods=["GET", "POST"])
    @staff_required("admin")
    def office_staff():
        if request.method == "POST":
            f = request.form
            try:
                sid = office.create_staff(request.staff, f.get("full_name"), f.get("email"), f.get("phone"),
                                          f.get("designation"), f.get("role"), f.get("password"))
                flash(f"Staff member added with ID {sid}.", "success")
                return redirect(url_for("office_staff"))
            except BankError as e:
                flash(str(e), "error")
        return office_page("staff.html", rows=office.staff_list())

    @app.route("/admin/staff/<sid>/<action>", methods=["POST"])
    @staff_required("admin")
    def office_staff_action(sid, action):
        me, f = request.staff, request.form
        if not office.get_staff(sid):
            abort(404)
        try:
            if action == "edit":
                office.update_staff(me, sid, f.get("full_name"), f.get("email"), f.get("phone"),
                                    f.get("designation"), f.get("role"))
                flash("Staff details saved.", "success")
            elif action == "password":
                office.reset_staff_password(me, sid, f.get("password", ""))
                flash("Password reset.", "success")
            elif action == "toggle":
                office.toggle_staff(me, sid)
                flash("Staff account updated.", "success")
            elif action == "delete":
                office.delete_staff(me, sid)
                flash("Staff member deleted.", "success")
            elif action == "reset2fa":
                office.reset_staff_2fa(me, sid)
                flash("Two-factor login of this staff member was turned off.", "success")
            else:
                abort(404)
        except BankError as e:
            flash(str(e), "error")
        return redirect(url_for("office_staff"))

    # ----- settings, database, audit (admin only)
    @app.route("/admin/settings", methods=["GET", "POST"])
    @staff_required("admin")
    def office_settings():
        if request.method == "POST":
            try:
                office.save_settings(request.staff, request.form)
                flash("Bank settings saved. New limits and FD rates apply immediately.", "success")
                return redirect(url_for("office_settings"))
            except BankError as e:
                flash(str(e), "error")
        return office_page("settings.html", s=bank.settings(), FD_TENURES=FD_TENURES, PRODUCTS=LOAN_PRODUCTS,
                           runs=bank.interest_runs(), due=bank.due_quarters(), providers=bank.notifier.status(),
                           database=bank.db.describe(), secret_from_env=bool(os.environ.get("SECRET_KEY")),
                           fx=bank.fx_rates(), limits={c: (bank.limit_for("withdraw", c), bank.limit_for("transfer", c))
                                                       for c in world.CURRENCIES})

    @app.route("/admin/settings/international", methods=["POST"])
    @staff_required("admin")
    def office_settings_international():
        f = request.form
        try:
            if f.get("form") == "fx_download":
                n = office.refresh_fx(request.staff)
                flash(f"Exchange rates downloaded for {n} currencies.", "success")
            elif f.get("form") == "fx":
                n = office.save_fx(request.staff, f)
                flash(f"Exchange rates saved ({n} currencies).", "success")
            else:
                office.save_currency_limits(request.staff, f)
                flash("Daily limits saved.", "success")
        except BankError as e:
            flash(str(e), "error")
        except Exception as e:                                # no internet, service down ...
            flash(f"Could not download exchange rates ({str(e)[:80]}). You can type them by hand.", "error")
        return redirect(url_for("office_settings") + "#international")

    @app.route("/admin/interest/run", methods=["POST"])
    @staff_required("admin")
    def office_interest_run():
        results = office.run_interest(request.staff)
        if results:
            for r in results:
                totals = ", ".join(format_money(v, c) for c, v in r["totals"].items()) or format_money(0)
                flash(f"Interest for {r['quarter']}: {totals} credited to {r['accounts']} savings accounts.", "success")
        else:
            flash("No quarter is waiting for interest. It is paid automatically after each quarter ends.", "info")
        return redirect(url_for("office_settings"))

    # ----- deposit / withdrawal approvals
    @app.route("/admin/approvals")
    @staff_required()
    def office_approvals():
        status = request.args.get("status", "Pending")
        return office_page("approvals.html", rows=office.cash_requests(status), status=status)

    @app.route("/admin/approvals/<int:rid>/<action>", methods=["POST"])
    @staff_required()
    def office_approval_action(rid, action):
        if action not in ("approve", "reject"):
            abort(404)
        try:
            r, t = office.decide_cash(request.staff, rid, action == "approve", request.form.get("reason", ""))
            flash(f"{r['kind']} {r['ref']} approved - {format_money(r['amount'], t.currency)} moved (ref {t.ref})." if t
                  else f"{r['kind']} {r['ref']} rejected. The customer was told why.", "success")
        except BankError as e:
            flash(str(e), "error")
        return redirect(request.form.get("next") if (request.form.get("next") or "").startswith("/admin")
                        else url_for("office_approvals"))

    # ----- loans
    @app.route("/admin/loans")
    @staff_required()
    def office_loans():
        status = request.args.get("status", "")
        return office_page("loans.html", rows=office.loans(status), status=status)

    @app.route("/admin/loans/<int:loan_id>/<action>", methods=["POST"])
    @staff_required()
    def office_loan_action(loan_id, action):
        if action not in ("approve", "reject"):
            abort(404)
        try:
            t = office.decide_loan(request.staff, loan_id, action == "approve", request.form.get("reason", ""))
            flash(f"Loan approved - {format_money(t.amount, t.currency)} paid into the customer's account (ref {t.ref})." if t
                  else "Loan application rejected. The customer was told why.", "success")
        except BankError as e:
            flash(str(e), "error")
        return redirect(url_for("office_loans", status=request.args.get("status", "")))

    # ----- backups (admin only)
    @app.route("/admin/backups", methods=["GET", "POST"])
    @staff_required("admin")
    def office_backups():
        if request.method == "POST":
            bid = office.create_backup(request.staff)
            flash(f"Backup #{bid} created. Download it and keep it safe on your computer.", "success")
            return redirect(url_for("office_backups"))
        return office_page("backups.html", rows=bank.backups(), database=bank.db.describe(),
                           backup_email=os.environ.get("BACKUP_EMAIL", ""), providers=bank.notifier.status())

    @app.route("/admin/backups/<int:bid>/download")
    @staff_required("admin")
    def office_backup_download(bid):
        try:
            blob = bank.backup_blob(bid)
        except BankError:
            abort(404)
        return Response(blob, mimetype="application/gzip",
                        headers={"Content-Disposition": f"attachment; filename=pybank_backup_{bid}.json.gz"})

    @app.route("/admin/backups/now.json.gz")
    @staff_required("admin")
    def office_backup_now():
        blob = bank.pack(bank.export_data())
        with bank._connect() as conn:
            office.audit(conn, request.staff, "Backup downloaded", "", f"{len(blob):,} bytes")
        return Response(blob, mimetype="application/gzip", headers={
            "Content-Disposition": f"attachment; filename=pybank_backup_{datetime.now():%Y%m%d_%H%M}.json.gz"})

    @app.route("/admin/backups/restore", methods=["POST"])
    @staff_required("admin")
    def office_restore():
        f = request.files.get("file")
        if request.form.get("confirm") != "RESTORE":
            flash('Type RESTORE in capital letters to confirm.', "error")
        elif not f or not f.filename:
            flash("Choose a backup file (.json.gz).", "error")
        else:
            try:
                counts = office.restore(request.staff, f.read(), request.form.get("password"))
                flash(f"Database restored: {counts.get('customers', 0)} customers, {counts.get('transactions', 0)} "
                      f"transactions. A copy of the old data was saved first.", "success")
            except BankError as e:
                flash(str(e), "error")
        return redirect(url_for("office_backups"))

    @app.route("/admin/database")
    @staff_required("admin")
    def office_database():
        name = request.args.get("table", "customers")
        try:
            cols, rows, total = office.table(name)
        except BankError:
            abort(404)
        return office_page("database.html", table=name, cols=cols, rows=rows, total=total,
                           tables=office.table_counts())

    @app.route("/admin/database/<name>.csv")
    @staff_required("admin")
    def office_database_csv(name):
        try:
            cols, rows, _ = office.table(name, limit=1_000_000)
        except BankError:
            abort(404)
        out = io.StringIO()
        w = csv.writer(out)
        w.writerow(cols)
        w.writerows(rows)
        return Response("\ufeff" + out.getvalue(), mimetype="text/csv",
                        headers={"Content-Disposition": f"attachment; filename=pybank_{name}_{date.today()}.csv"})

    @app.route("/admin/audit")
    @staff_required("admin")
    def office_audit():
        return office_page("audit.html", rows=office.audit_log(400))

    # ----- my own staff profile
    @app.route("/admin/profile", methods=["GET", "POST"])
    @staff_required()
    def office_profile():
        me, f = request.staff, request.form
        if request.method == "POST":
            try:
                if f.get("form") == "2fa_start":
                    session["new_staff_totp"] = otp.new_secret()
                    return redirect(url_for("office_profile") + "#two-factor")
                if f.get("form") == "2fa_confirm":
                    office.enable_staff_2fa(me, session.get("new_staff_totp", ""), f.get("code", ""))
                    session.pop("new_staff_totp", None)
                    flash("Two-factor login is ON for your staff account.", "success")
                    return redirect(url_for("office_profile"))
                if f.get("form") == "2fa_disable":
                    office.disable_staff_2fa(me, f.get("password"), f.get("code"))
                    flash("Two-factor login is OFF.", "success")
                    return redirect(url_for("office_profile"))
                if f.get("form") == "password":
                    if f.get("new") != f.get("new2"):
                        raise BankError("The two new passwords do not match.")
                    office.change_own_password(me, f.get("old"), f.get("new"))
                    flash("Your password was changed.", "success")
                else:
                    office.update_staff(me, me["staff_id"], f.get("full_name"), f.get("email"), f.get("phone"),
                                        f.get("designation"))
                    flash("Your details were saved.", "success")
                return redirect(url_for("office_profile"))
            except BankError as e:
                flash(str(e), "error")
        secret = session.get("new_staff_totp") if not me.get("totp_enabled") else None
        return office_page("profile.html", new_secret=secret, new_secret_grouped=otp.grouped(secret) if secret else "",
                           otp_uri=otp.uri(secret, me["staff_id"], "PyBank Back Office") if secret else "")

    # ---------- errors ----------

    @app.errorhandler(404)
    def not_found(e):
        return render_template("public/error.html", code=404, title="Page not found",
                               message="The page you are looking for does not exist."), 404

    @app.errorhandler(403)
    def forbidden(e):
        return render_template("public/error.html", code=403, title="Access denied",
                               message="Only the bank administrator can open this page."), 403

    @app.errorhandler(400)
    def bad_request(e):
        return render_template("public/error.html", code=400, title="Session expired",
                               message="This form has expired. Please go back, reload the page and try again."), 400

    return app


app = create_app()

if __name__ == "__main__":
    app.run(debug=os.environ.get("FLASK_DEBUG") == "1")

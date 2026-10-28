"""
app.py - Flask web application (routes) for PyBank, the Python-Based Banking System.

Run:  python app.py   then open http://127.0.0.1:5000
Demo login:  Customer ID PB10000001, password Demo@1234, transaction PIN 2580
"""

import csv
import io
import os
import secrets
import time
from dataclasses import asdict
from datetime import date, datetime, timedelta
from functools import wraps

from flask import (Flask, Response, abort, flash, redirect, render_template, request,
                   session, url_for)

import demo_data
from admin import NOTICE_LEVELS, REQUEST_STATUSES, BankAdmin
from bank import (ACCOUNT_TYPES, BRANCH, FD_MIN, FD_TENURES, REQUEST_TYPES, Bank, BankError, format_money,
                  parse_amount)

SESSION_IDLE_MINUTES = 10
STAFF_IDLE_MINUTES = 30

STATES = ["Andhra Pradesh", "Bihar", "Chandigarh", "Delhi", "Gujarat", "Haryana", "Himachal Pradesh",
          "Jammu and Kashmir", "Karnataka", "Kerala", "Madhya Pradesh", "Maharashtra", "Odisha",
          "Punjab", "Rajasthan", "Tamil Nadu", "Telangana", "Uttar Pradesh", "Uttarakhand", "West Bengal"]
OCCUPATIONS = ["Salaried", "Self-employed", "Business", "Student", "Retired", "Homemaker", "Other"]


def create_app(db_path=None, seed=True):
    app = Flask(__name__)
    app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY") or secrets.token_hex(32)
    app.config["SESSION_COOKIE_HTTPONLY"] = True
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"

    bank = Bank(db_path or os.path.join(app.root_path, "bank.db"))
    office = BankAdmin(bank)
    if seed:
        demo_data.seed(bank, office)
    app.bank, app.office = bank, office

    # ---------- template helpers ----------

    app.jinja_env.filters["money"] = format_money

    @app.template_filter("nicedate")
    def nicedate(value, with_time=True):
        d = datetime.strptime(value, "%Y-%m-%d %H:%M:%S")
        return d.strftime("%d %b %Y, %I:%M %p" if with_time else "%d %b %Y")

    @app.context_processor
    def globals_():
        today = date.today()
        try:
            max_dob = today.replace(year=today.year - 18)
        except ValueError:                      # 29 February
            max_dob = today.replace(year=today.year - 18, day=28)
        settings = bank.settings()
        return {"BRANCH": BRANCH, "ACCOUNT_TYPES": ACCOUNT_TYPES, "today": today,
                "max_dob": max_dob.isoformat(), "now_hour": datetime.now().hour,
                "DAILY_WITHDRAW_LIMIT": int(settings["daily_withdraw_limit"]),
                "DAILY_TRANSFER_LIMIT": int(settings["daily_transfer_limit"]),
                "SETTINGS": settings, "FD_RATES": bank.fd_rates(), "NOTICES": bank.active_notices()}

    # ---------- CSRF protection: every form carries a secret token ----------

    def csrf_token():
        if "csrf_token" not in session:
            session["csrf_token"] = secrets.token_hex(16)
        return session["csrf_token"]

    app.jinja_env.globals["csrf_token"] = csrf_token

    @app.before_request
    def security_checks():
        if request.method == "POST":
            sent = request.form.get("csrf_token", "")
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
        form = request.form if request.method == "POST" else {}
        if request.method == "POST":
            try:
                if form.get("password") != form.get("password2"):
                    raise BankError("The two passwords do not match.")
                if form.get("pin") != form.get("pin2"):
                    raise BankError("The two transaction PINs do not match.")
                if form.get("terms") != "yes":
                    raise BankError("Please accept the terms and conditions.")
                cid, account = bank.register(form.to_dict())
            except BankError as e:
                flash(str(e), "error")
                return render_template("public/register.html", form=form,
                                       states=STATES, occupations=OCCUPATIONS), 400
            return render_template("public/registered.html", cid=cid, account=account,
                                   customer=bank.get_customer(cid))
        return render_template("public/register.html", form=form, states=STATES, occupations=OCCUPATIONS)

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
            session.clear()                    # new session after login
            session.update(cid=c.customer_id, last_active=time.time(), previous_login=c.last_login, **keep)
            return redirect(url_for("dashboard"))
        return render_template("public/login.html", login="")

    @app.route("/logout")
    def logout():
        for k in ("cid", "last_active", "previous_login"):
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
            requests_open=[r for r in bank.service_requests(customer.customer_id) if r["status"] in ("Open", "In progress")])

    @app.route("/netbanking/account")
    @login_required
    def account_details():
        customer, account = current()
        return render_template("netbanking/account.html", customer=customer, account=account,
                               logins=bank.login_history(customer.customer_id, 8))

    def money_form(kind):
        """Shared code for deposit and withdraw pages."""
        customer, account = current()
        form = request.form if request.method == "POST" else {}
        if request.method == "POST":
            try:
                amount = parse_amount(form.get("amount", ""))
                action = bank.deposit if kind == "deposit" else bank.withdraw
                t = action(customer.customer_id, amount, form.get("pin", ""), form.get("note", "").strip()[:40])
            except BankError as e:
                flash(str(e), "error")
                return render_template("netbanking/money.html", kind=kind, is_dep=kind == "deposit",
                                       customer=customer, account=account, form=form), 400
            flash("Transaction successful.", "success")
            return redirect(url_for("receipt", ref=t.ref))
        return render_template("netbanking/money.html", kind=kind, is_dep=kind == "deposit",
                               customer=customer, account=account, form=form)

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
                amount = parse_amount(form.get("amount", ""))
                if amount > account.balance:
                    raise BankError(f"Insufficient balance. Available balance is {format_money(account.balance)}.")
                beneficiary = bank.find_beneficiary(to)
                if step == "confirm":
                    t = bank.transfer(customer.customer_id, to, amount, form.get("pin", ""), form.get("remarks", ""))
                    flash("Funds transferred successfully.", "success")
                    return redirect(url_for("receipt", ref=t.ref))
                return render_template("netbanking/transfer_confirm.html", customer=customer,
                                       account=account, form=form, amount=amount, beneficiary=beneficiary)
            except BankError as e:
                flash(str(e), "error")
                if step == "confirm" and "PIN" in str(e):
                    return render_template("netbanking/transfer_confirm.html", customer=customer,
                                           account=account, form=form, amount=parse_amount(form["amount"]),
                                           beneficiary=bank.find_beneficiary(form["to_account"])), 400
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
        w.writerow(["Date", "Reference", "Description", "Debit (INR)", "Credit (INR)", "Balance (INR)"])
        for t in reversed(bank.transactions(account.number, start, end, kind)):
            amt = f"{t.amount / 100:.2f}"
            w.writerow([t.created_at, t.ref, t.description, "" if t.is_credit else amt,
                        amt if t.is_credit else "", f"{t.balance_after / 100:.2f}"])
        name = f"PyBank_statement_{account.number[-4:]}_{start}_to_{end}.csv"
        return Response("﻿" + out.getvalue(), mimetype="text/csv",
                        headers={"Content-Disposition": f"attachment; filename={name}"})

    @app.route("/netbanking/profile", methods=["GET", "POST"])
    @login_required
    def profile():
        customer, account = current()
        if request.method == "POST":
            f = request.form
            try:
                bank.update_contact(customer.customer_id, f.get("email", ""), f.get("mobile", ""),
                                    f.get("address", ""), f.get("city", ""), f.get("state", ""),
                                    f.get("pincode", ""))
            except BankError as e:
                flash(str(e), "error")
                return render_template("netbanking/profile.html", customer=customer, account=account,
                                       states=STATES, form=f), 400
            flash("Your contact details were updated.", "success")
            return redirect(url_for("profile"))
        return render_template("netbanking/profile.html", customer=customer, account=account,
                               states=STATES, form=asdict(customer))

    @app.route("/netbanking/security", methods=["GET", "POST"])
    @login_required
    def security():
        customer, account = current()
        if request.method == "POST":
            f = request.form
            try:
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
                return render_template("netbanking/security.html", customer=customer, account=account), 400
            return redirect(url_for("security"))
        return render_template("netbanking/security.html", customer=customer, account=account)

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
                fd_no = bank.open_fd(customer.customer_id, parse_amount(request.form.get("amount", "")),
                                     request.form.get("months", ""), request.form.get("pin", ""))
                flash(f"Fixed deposit {fd_no} opened successfully.", "success")
                return redirect(url_for("fixed_deposits"))
            except BankError as e:
                flash(str(e), "error")
                customer, account = current()
        return render_template("netbanking/fixed_deposits.html", customer=customer, account=account,
                               fds=bank.fixed_deposits(customer.customer_id), FD_TENURES=FD_TENURES,
                               FD_MIN=FD_MIN, form=request.form)

    @app.route("/netbanking/fixed-deposits/<int:fd_id>/close", methods=["POST"])
    @login_required
    def close_fd(fd_id):
        try:
            t = bank.close_fd(session["cid"], fd_id, request.form.get("pin", ""))
            flash(f"Fixed deposit closed. {format_money(t.amount)} was credited to your account.", "success")
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

    @app.route("/admin/login", methods=["GET", "POST"])
    def staff_login():
        if request.method == "POST":
            try:
                me = office.authenticate_staff(request.form.get("login"), request.form.get("password"))
            except BankError as e:
                flash(str(e), "error")
                return render_template("admin/login.html", login=request.form.get("login", "")), 401
            session["staff_id"], session["staff_active"] = me["staff_id"], time.time()
            return redirect(url_for("office_dashboard"))
        return render_template("admin/login.html", login="")

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
                cid, _ = bank.register(form.to_dict())
                with bank._connect() as conn:
                    office.audit(conn, request.staff, "Customer account opened", cid, form.get("full_name", ""))
                flash(f"Account opened. Customer ID {cid}. Give the customer the password and PIN you entered.", "success")
                return redirect(url_for("office_customer", cid=cid))
            except BankError as e:
                flash(str(e), "error")
        return office_page("customer_new.html", form=form, states=STATES, occupations=OCCUPATIONS)

    @app.route("/admin/customers/<cid>")
    @staff_required()
    def office_customer(cid):
        try:
            customer, account = bank.get_customer(cid), bank.get_account(cid)
        except BankError:
            abort(404)
        with bank._connect() as conn:
            rows = [dict(r) for r in conn.execute("SELECT * FROM transactions WHERE account_number = ? "
                                                  "ORDER BY created_at DESC, id DESC LIMIT 40", (account.number,))]
        return office_page("customer.html", c=customer, account=account, tx=rows,
                           logins=bank.login_history(cid, 6), fds=bank.fixed_deposits(cid),
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
                t = office.manual_entry(me, cid, f.get("kind"), parse_amount(f.get("amount", "")), f.get("description"))
                flash(f"Entry posted (ref {t.ref}). New balance {format_money(t.balance_after)}.", "success")
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
        return office_page("settings.html", s=bank.settings(), FD_TENURES=FD_TENURES)

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
        return office_page("profile.html")

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

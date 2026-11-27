"""
v4_views.py - the web pages of version 4 and the back-office class with the new staff work.

Public website   /page/<slug>, /media/<id>, /status, /health, /developers (+ the old pages now read the
                 database: home, about, rates, contact, faq)
Net Banking      My Accounts (several currencies, exchange), My Card (controls), Bills & Autopay
                 (bill payments, standing instructions), Insights (spending, budgets), devices and app
                 tokens, "download my data", interest certificate, report a problem with a transaction
Back office      Website (texts, pages, FAQ, products, branches, banners, charges, billers, pictures,
                 translations, history), ISO 20022 files, MRZ check, compliance flags, dormant accounts,
                 second approvals (four-eyes rule), system health, job roles and the staff member's language
"""

import io
from datetime import date, timedelta, datetime

from flask import Response, abort, flash, jsonify, redirect, render_template, request, session, url_for

import admin as admin_module
import roles
import sitecontent
import world
from admin import InternationalOffice
from autopay import BILLER_CATEGORIES, FREQUENCIES
from bank import REQUEST_TYPES, BankError, parse_amount
from cards import CHANNELS
from compliance import FLAG_STATUSES, RULES, CheckerOffice, aml_amount, checker_amount
from core import now
from devices import mask_ip
from insights import NOT_SPENDING, SPENDING
from iso20022 import OK_CODES, REJECT_REASONS
from privacy import financial_year
from staffperm import StaffPermMixin
from teller import TellerMixin
from abilities import AbilityAdminMixin          # v6
from vault import VaultMixin                     # v6
from wallets import MAX_EXTRA
from world import format_money

V4_TABLES = ["site_texts", "site_pages", "faqs", "products", "branches", "banners", "charges", "media",
             "content_tr", "content_history", "fx_history", "cards", "card_events", "iso_files", "mrz_checks",
             "billers", "bill_payments", "standing_instructions", "budgets", "aml_flags", "pending_entries",
             "sessions", "api_tokens", "rate_blocks", "error_log"]
for _t in V4_TABLES:
    if _t not in admin_module.TABLES:
        admin_module.TABLES.append(_t)
admin_module.SECRET_COLUMNS.update({"token_hash", "xml"})
if "Transaction dispute" not in REQUEST_TYPES:
    REQUEST_TYPES.append("Transaction dispute")
if "Activate dormant account" not in REQUEST_TYPES:
    REQUEST_TYPES.append("Activate dormant account")

CONTENT_KINDS = {"faq": "FAQ", "product": "Products", "branch": "Branches & ATMs", "banner": "Banners",
                 "charge": "Service charges"}


class Office(VaultMixin, AbilityAdminMixin, TellerMixin, StaffPermMixin, CheckerOffice, InternationalOffice):
    """The back office of version 4: everything of version 3 + the four-eyes rule, job roles, languages."""

    def overview(self):
        o = super().overview()
        with self._connect() as conn:
            q = lambda sql, *a: conn.execute(sql, a).fetchone()[0]          # noqa: E731
            o["fd_total"] = [(r[0], r[1]) for r in conn.execute(
                "SELECT a.currency, SUM(f.principal) FROM fixed_deposits f JOIN accounts a ON a.customer_id = f.customer_id "
                "AND a.opened_at = (SELECT MIN(opened_at) FROM accounts x WHERE x.customer_id = f.customer_id) "
                "WHERE f.status = 'Active' GROUP BY a.currency ORDER BY a.currency") if r[1]]
            o["aml_open"] = q("SELECT COUNT(*) FROM aml_flags WHERE status = 'Open'")
            o["checker_waiting"] = q("SELECT COUNT(*) FROM pending_entries WHERE status = 'Waiting'")
            o["dormant"] = q("SELECT COUNT(*) FROM accounts WHERE status = 'Dormant'")
            o["till_differences"] = q("SELECT COUNT(*) FROM tills WHERE status = 'Difference'")              # v5
            o["screening_open"] = q("SELECT COUNT(*) FROM screening_hits WHERE status = 'Open'")            # v5
            o["counter_today"] = q("SELECT COUNT(*) FROM counter_ops WHERE created_at >= ?", date.today().isoformat())
            o["iso_waiting"] = q("SELECT COUNT(*) FROM external_transfers WHERE status = 'Processing' "
                                 "AND rail IN ('SWIFT', 'SEPA') AND iso_file = ''")
            o["extra_accounts"] = q("SELECT COUNT(*) FROM accounts a WHERE a.opened_at > (SELECT MIN(opened_at) "
                                    "FROM accounts x WHERE x.customer_id = a.customer_id)")
            o["vault_waiting"] = q("SELECT COUNT(*) FROM vault_moves WHERE status = 'Waiting'")              # v6
            o["cheques_clearing"] = q("SELECT COUNT(*) FROM cheques WHERE status = 'In clearing'")
            o["cc_applied"] = q("SELECT COUNT(*) FROM credit_cards WHERE status = 'Applied'")
            o["holds"] = q("SELECT COUNT(*) FROM payment_holds WHERE status = 'Held'")
            o["bot_unknown"] = q("SELECT COUNT(*) FROM chat_messages WHERE role = 'bot' AND (kind = 'unknown' OR feedback < 0) "
                                 "AND created_at >= ?", (date.today() - timedelta(days=7)).isoformat())
        return o

    def set_staff_lang(self, me, lang):
        if lang not in world.LANGUAGES:
            raise BankError("Unknown language.")
        with self._connect() as conn:
            conn.execute("UPDATE staff SET lang = ? WHERE staff_id = ?", (lang, me["staff_id"]))

    def save_settings_v4(self, by, form):
        values = {}
        try:
            for key, low, high in (("savings_rate_high", 0, 30), ("fd_senior_extra", 0, 2), ("fx_margin", 0, 5)):
                v = float(form.get(key, ""))
                if not low <= v <= high:
                    raise ValueError
                values[key] = f"{v:.2f}"
        except ValueError:
            raise BankError("The higher savings rate must be 0-30 %, the senior extra 0-2 % and the exchange "
                            "margin 0-5 %.")
        mode = form.get("payments_mode")
        if mode not in ("sandbox", "iso_files"):
            raise BankError("Please choose how SWIFT and SEPA payments are finished.")
        values["payments_mode"] = mode
        with self._connect() as conn:
            for k, v in values.items():
                conn.execute("INSERT INTO settings (key, value) VALUES (?, ?) "
                             "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (k, v))
            self.audit(conn, by, "Bank settings changed (v4)", "", ", ".join(f"{k}={v}" for k, v in values.items()))

    def card_test(self, by, cid, form):
        acc = self.bank.get_account(cid)
        card = self.bank.card(cid)
        if not card:
            raise BankError("This account has no debit card.")
        amount = parse_amount(form.get("amount", ""), acc.currency)
        result, t = self.bank.card_payment(card["last4"], acc.number, amount, form.get("channel"),
                                           form.get("merchant"), form.get("country") or "IN")
        with self._connect() as conn:
            self.audit(conn, by, "Card test payment", cid, f"{form.get('channel')} {format_money(amount, acc.currency)}: {result}")
        return result, t


def register(app, bank, office, h):
    login_required, current, current_main = h["login_required"], h["current"], h["current_main"]
    staff_required, office_page, client_ip = h["staff_required"], h["office_page"], h["client_ip"]

    # ---------- template helpers: the website texts come from the database ----------

    @app.context_processor
    def site_globals():
        lang = world.current_lang()
        if request.path.startswith(("/static", "/api/")):
            return {}
        return {"SITE": bank.site_texts(lang), "FOOTER_PAGES": bank.site_pages(lang, footer_only=True),
                "picture_url": bank.picture_url, "MAX_EXTRA": MAX_EXTRA}

    app.jinja_env.globals.update(site_banners=lambda: bank.banners(world.current_lang()),
                                 site_products=lambda: bank.products(world.current_lang()),
                                 my_accounts=lambda: bank.accounts_of(session["cid"]) if "cid" in session else [],
                                 osm_link=lambda b: (f"https://www.openstreetmap.org/?mlat={b['lat']}&mlon={b['lng']}"
                                                     f"#map=17/{b['lat']}/{b['lng']}") if b.get("lat") is not None else "")

    def staff_me():
        return office.get_staff(session["staff_id"]) if "staff_id" in session else None

    # =====================================================================
    # Public website
    # =====================================================================

    def public_page(slug, template="public/site_page.html", **kw):
        preview = request.args.get("preview") == "1" and roles.can(staff_me(), "content.manage")
        try:
            page = bank.site_page(slug, world.current_lang(), include_hidden=preview)
        except BankError:
            abort(404)
        return render_template(template, page=page, preview=preview, **kw)

    app.view_functions["about"] = lambda: public_page("about", "public/about.html")

    @app.route("/page/<slug>")
    def site_page(slug):
        if slug == "about":
            return redirect(url_for("about"))
        return public_page(slug)

    def faq_view():
        q = request.args.get("q", "").strip()[:80]
        rows = bank.faqs(world.current_lang(), q=q)
        cats = []
        for r in rows:
            if r["category"] not in cats:
                cats.append(r["category"])
        return render_template("public/faq.html", rows=rows, cats=cats, q=q)
    app.view_functions["faq"] = faq_view

    def contact_view():
        country = request.args.get("country", "").upper()
        all_branches = bank.branches(world.current_lang())
        countries = sorted({b["country"] for b in all_branches})
        rows = [b for b in all_branches if not country or b["country"] == country]
        return render_template("public/contact.html", branches=rows, countries=countries, country=country)
    app.view_functions["contact"] = contact_view

    def rates_view():
        base = world.current_currency()
        return render_template("public/rates.html", charges=bank.charges(world.current_lang()),
                               fx=bank.rate_table(base), fx_base=base, margin=bank.fx_margin(),
                               slab=world.policy(base, "slab"))
    app.view_functions["rates"] = rates_view

    @app.route("/media/<int:media_id>")
    def media(media_id):
        try:
            m, blob = bank.media_blob(media_id)
        except BankError:
            abort(404)
        resp = Response(blob, mimetype=m["mime"])
        resp.headers["Cache-Control"] = "public, max-age=86400"
        return resp

    @app.route("/health")
    def health():
        h_ = bank.health()
        return jsonify(h_), (200 if h_["status"] != "down" else 503)

    @app.route("/status")
    def status_page():
        return render_template("public/status.html", h=bank.health())

    @app.route("/developers")
    def developers():
        import api
        return render_template("public/developers.html", spec=api.openapi())

    # =====================================================================
    # Net Banking
    # =====================================================================

    @app.route("/netbanking/switch", methods=["POST"])
    @login_required
    def switch_account():
        try:
            acc = bank.account_of(session["cid"], request.form.get("account"))
            session["acc"] = acc.number
        except BankError as e:
            flash(str(e), "error")
        nxt = request.form.get("next", "")
        return redirect(nxt if nxt.startswith("/netbanking") else url_for("dashboard"))

    @app.route("/netbanking/accounts", methods=["GET", "POST"])
    @login_required
    def my_accounts():
        customer, account = current()
        cid = customer.customer_id
        quote, form, status = None, request.form if request.method == "POST" else {}, 200
        if request.method == "POST":
            action = form.get("action")
            try:
                if action == "open":
                    acc = bank.open_currency_account(cid, form.get("currency"), form.get("pin", ""))
                    flash(f"Your {acc.currency} account {acc.number} is open.", "success")
                    return redirect(url_for("my_accounts"))
                if action == "close":
                    bank.close_currency_account(cid, form.get("account"), form.get("pin", ""))
                    session.pop("acc", None)
                    flash("The account is closed.", "success")
                    return redirect(url_for("my_accounts"))
                if action in ("quote", "exchange"):
                    src = bank.account_of(cid, form.get("from"))
                    dst = bank.account_of(cid, form.get("to"))
                    amount = parse_amount(form.get("amount", ""), src.currency)
                    if src.number == dst.number:
                        raise BankError("Please choose two different accounts.")
                    if action == "exchange":
                        t, q = bank.exchange(cid, src.number, dst.number, amount, form.get("pin", ""))
                        flash(f"{format_money(amount, src.currency)} moved; {format_money(q['amount'], dst.currency)} "
                              f"arrived in your {dst.currency} account.", "success")
                        return redirect(url_for("receipt", ref=t.ref))
                    if amount > bank.available_balance(src.number):
                        raise BankError(f"Insufficient balance. Available balance is "
                                        f"{format_money(bank.available_balance(src.number), src.currency)}.")
                    quote = {**bank.quote(src.currency, dst.currency, amount), "src_acc": src, "dst_acc": dst,
                             "amount_in": amount}
            except BankError as e:
                flash(str(e), "error")
                status = 400
        accounts = bank.accounts_of(cid)
        have = {a.currency for a in accounts if a.status != "Closed"}
        return render_template("netbanking/accounts.html", customer=customer, account=account, accounts=accounts,
                               can_open=[c for c in world.CURRENCIES if c not in have], quote=quote, form=form,
                               total=bank.total_in(cid, accounts[0].currency), main=accounts[0]), status

    @app.route("/netbanking/card", methods=["GET", "POST"])
    @login_required
    def my_card():
        customer, account = current_main()
        cid = customer.customer_id
        if request.method == "POST":
            action = request.form.get("action")
            try:
                if action == "controls":
                    bank.set_card_controls(cid, request.form)
                    flash("Card controls saved. They work at once.", "success")
                elif action in ("block", "unblock"):
                    bank.block_card(cid, action == "block")
                    flash("Card blocked. No payment will be accepted until you unblock it." if action == "block"
                          else "Card unblocked.", "success")
                elif action == "lost":
                    c = bank.report_card_lost(cid, request.form.get("pin", ""))
                    flash(f"Your old card is blocked for ever. A new card ending {c['last4']} is ordered.", "success")
                else:
                    abort(400)
            except BankError as e:
                flash(str(e), "error")
            return redirect(url_for("my_card"))
        return render_template("netbanking/card.html", customer=customer, account=account, card=bank.card(cid),
                               events=bank.card_events(cid), CHANNELS=CHANNELS)

    @app.route("/netbanking/bills", methods=["GET", "POST"])
    @login_required
    def bills():
        customer, account = current()
        cid = customer.customer_id
        form = request.form if request.method == "POST" else request.args
        bill, status = None, 200
        if request.method == "POST":
            action = form.get("action")
            try:
                if action == "fetch":
                    bill = bank.fetch_bill(int(form.get("biller") or 0), form.get("consumer_no"))
                elif action == "pay":
                    amount = parse_amount(form.get("amount", ""), account.currency)
                    ref, t = bank.pay_bill(cid, int(form.get("biller") or 0), form.get("consumer_no"), amount,
                                           form.get("pin", ""), from_number=account.number)
                    flash(f"Bill paid (payment {ref}).", "success")
                    return redirect(url_for("receipt", ref=t.ref))
                elif action == "instruction":
                    bank.create_instruction(cid, {**form.to_dict(), "account": account.number}, form.get("pin", ""))
                    flash("Standing instruction created. The bank pays on the due dates and sends you an alert.", "success")
                    return redirect(url_for("bills") + "#autopay")
                elif action in ("pause", "resume", "cancel"):
                    bank.set_instruction_status(cid, int(form.get("id") or 0), action)
                    flash({"pause": "Standing instruction paused.", "resume": "Standing instruction active again.",
                           "cancel": "Standing instruction cancelled."}[action], "success")
                    return redirect(url_for("bills") + "#autopay")
                else:
                    abort(400)
            except (BankError, ValueError) as e:
                flash(str(e) if isinstance(e, BankError) else "Please choose a biller.", "error")
                status = 400
        return render_template("netbanking/bills.html", customer=customer, account=account, form=form, bill=bill,
                               billers=bank.billers(currency=account.currency), all_billers=bank.billers(),
                               payments=bank.bill_payments(cid), instructions=bank.instructions(cid),
                               payees=bank.beneficiaries(cid), FREQUENCIES=FREQUENCIES,
                               today=date.today()), status

    @app.route("/netbanking/insights", methods=["GET", "POST"])
    @login_required
    def insights():
        customer, account = current()
        if request.method == "POST":
            try:
                raw = request.form.get("amount", "").strip()
                amount = parse_amount(raw, account.currency) if raw not in ("", "0") else 0
                bank.set_budget(customer.customer_id, account.number, request.form.get("category"), amount)
                flash("Budget saved." if amount else "Budget removed.", "success")
            except BankError as e:
                flash(str(e), "error")
            return redirect(url_for("insights"))
        return render_template("netbanking/insights.html", customer=customer, account=account,
                               data=bank.insights(account.number), budgets=bank.budgets(customer.customer_id, account.number),
                               CATEGORIES=bank.spending_categories(account.number))

    @app.route("/netbanking/security/devices", methods=["POST"])
    @login_required
    def devices_action():
        cid, action = session["cid"], request.form.get("action")
        if action == "end":
            sid = request.form.get("id", "")
            if sid == session.get("sid"):
                return redirect(url_for("logout"))
            bank.end_session(cid, sid)
            flash("That device is signed out.", "success")
        elif action == "end_others":
            n = bank.end_other_sessions(cid, session.get("sid", ""))
            flash(f"{n} other device(s) signed out.", "success")
        elif action == "revoke":
            bank.revoke_token(cid, int(request.form.get("id") or 0))
            flash("The app is signed out.", "success")
        else:
            abort(400)
        return redirect(url_for("security") + "#devices")

    @app.route("/netbanking/my-data.json")
    @login_required
    def my_data():
        data = bank.export_my_data(session["cid"])
        return Response(data, mimetype="application/json", headers={
            "Content-Disposition": f"attachment; filename=PyBank_my_data_{date.today().isoformat()}.json"})

    @app.route("/netbanking/interest-certificate.pdf")
    @login_required
    def interest_certificate():
        customer = bank.get_customer(session["cid"])
        try:
            year = int(request.args.get("year", ""))
            label, blob = bank.interest_certificate(customer.customer_id, year, world.current_lang())
        except (ValueError, BankError):
            flash("Please choose a valid year.", "error")
            return redirect(url_for("estatements"))
        return Response(blob, mimetype="application/pdf", headers={
            "Content-Disposition": f"inline; filename=PyBank_interest_certificate_{label.replace(' ', '_')}.pdf"})

    @app.route("/netbanking/dispute/<ref>", methods=["POST"])
    @login_required
    def dispute(ref):
        cid = session["cid"]
        found = None
        for acc in bank.accounts_of(cid):
            try:
                found = bank.get_transaction(acc.number, ref)
                break
            except BankError:
                continue
        if found is None:
            abort(404)
        text = " ".join(request.form.get("details", "").split())
        try:
            rid = bank.create_request(cid, "Transaction dispute",
                                      f"Ref {ref} ({found.created_at[:10]}, {format_money(found.amount, found.currency)}): "
                                      f"{text}")
            flash(f"Your report #{rid} was sent to the bank. A bank officer will reply in Service Requests.", "success")
        except BankError as e:
            flash(str(e), "error")
        return redirect(url_for("receipt", ref=ref))

    # =====================================================================
    # Back office
    # =====================================================================

    @app.route("/admin/lang/<code>", methods=["POST"])
    @staff_required()
    def office_lang(code):
        try:
            office.set_staff_lang(request.staff, code)
            session["staff_lang"] = code
        except BankError as e:
            flash(str(e), "error")
        nxt = request.form.get("next", "")
        return redirect(nxt if nxt.startswith("/admin") else url_for("office_dashboard"))

    # ----- website content -----

    @app.route("/admin/content")
    @staff_required()
    def office_content():
        status = bank.translation_status()
        return office_page("content.html", texts=bank.content_items("text"), groups=sitecontent.TEXT_GROUPS,
                           labels={k: v[1] for k, v in sitecontent.DEFAULT_TEXTS.items()},
                           pages=bank.content_items("page"), status=status, KINDS=CONTENT_KINDS,
                           item_counts={k: len(bank.content_items(k)) for k in CONTENT_KINDS},
                           billers=bank.billers(active_only=False), media=bank.media_list(),
                           history=bank.history("text", request.args.get("key", "care_phone"), 10),
                           history_key=request.args.get("key", "care_phone"))

    @app.route("/admin/content/texts", methods=["POST"])
    @staff_required()
    def office_content_texts():
        changed = 0
        try:
            for key in sitecontent.DEFAULT_TEXTS:
                if key in request.form:
                    changed += bank.save_text(request.staff, key, request.form.get(key))
            flash(f"{changed} text(s) changed. The website shows them now." if changed else "Nothing was changed.",
                  "success")
        except BankError as e:
            flash(str(e), "error")
        return redirect(url_for("office_content"))

    @app.route("/admin/content/pages/new", methods=["GET", "POST"])
    @staff_required()
    def office_page_new():
        if request.method == "POST":
            try:
                slug = bank.save_page(request.staff, request.form.get("slug"), request.form, new=True)
                flash("Page created.", "success")
                return redirect(url_for("office_page_edit", slug=slug))
            except BankError as e:
                flash(str(e), "error")
        return office_page("content_page.html", page=None, form=request.form, history=[])

    @app.route("/admin/content/pages/<slug>", methods=["GET", "POST"])
    @staff_required()
    def office_page_edit(slug):
        try:
            page = bank.content_item("page", slug)
        except BankError:
            abort(404)
        if request.method == "POST":
            try:
                if request.form.get("action") == "delete":
                    bank.delete_page(request.staff, slug)
                    flash("Page deleted (its text is kept in the history).", "success")
                    return redirect(url_for("office_content"))
                bank.save_page(request.staff, slug, request.form)
                flash("Page saved. The old text is kept in the history below.", "success")
                return redirect(url_for("office_page_edit", slug=slug))
            except BankError as e:
                flash(str(e), "error")
        return office_page("content_page.html", page=page, form=request.form or page, history=bank.history("page", slug),
                           preview_html=sitecontent.render_body((request.form or page).get("body", "")))

    @app.route("/admin/content/history/<int:hid>/restore", methods=["POST"])
    @staff_required()
    def office_history_restore(hid):
        try:
            kind, ref = bank.restore_version(request.staff, hid)
            flash("The older version is back. The text before it went into the history.", "success")
            if kind == "page":
                return redirect(url_for("office_page_edit", slug=ref))
            return redirect(url_for("office_content", key=ref) + "#history")
        except BankError as e:
            flash(str(e), "error")
        return redirect(url_for("office_content"))

    @app.route("/admin/content/items/<kind>", methods=["GET", "POST"])
    @staff_required()
    def office_items(kind):
        if kind not in CONTENT_KINDS:
            abort(404)
        if request.method == "POST":
            try:
                bank.save_item(request.staff, kind, request.form)
                flash("Added. It shows on the website now (if it is switched on).", "success")
                return redirect(url_for("office_items", kind=kind))
            except BankError as e:
                flash(str(e), "error")
        return office_page("content_items.html", kind=kind, title=CONTENT_KINDS[kind], rows=bank.content_items(kind),
                           fields=sitecontent.KINDS[kind][2], item=None, form=request.form,
                           status=bank.translation_status(), pictures=bank.picture_choices(),
                           BRANCH_KINDS=sitecontent.BRANCH_KINDS, countries=world.countries("en"))

    @app.route("/admin/content/items/<kind>/<int:item_id>", methods=["GET", "POST"])
    @staff_required()
    def office_item(kind, item_id):
        if kind not in CONTENT_KINDS:
            abort(404)
        try:
            item = bank.content_item(kind, item_id)
        except BankError:
            abort(404)
        if request.method == "POST":
            try:
                if request.form.get("action") == "delete":
                    bank.delete_item(request.staff, kind, item_id)
                    flash("Deleted (a copy is kept in the history).", "success")
                else:
                    bank.save_item(request.staff, kind, request.form, item_id)
                    flash("Saved. The website shows the change now.", "success")
                return redirect(url_for("office_items", kind=kind))
            except BankError as e:
                flash(str(e), "error")
        return office_page("content_items.html", kind=kind, title=CONTENT_KINDS[kind], rows=[], item=item,
                           fields=sitecontent.KINDS[kind][2], form=request.form or item, status={},
                           pictures=bank.picture_choices(), BRANCH_KINDS=sitecontent.BRANCH_KINDS,
                           countries=world.countries("en"), history=bank.history(kind, item_id, 10))

    @app.route("/admin/content/translate/<kind>/<path:ref>", methods=["GET", "POST"])
    @staff_required()
    def office_translate(kind, ref):
        if kind not in sitecontent.KINDS:
            abort(404)
        lang = request.values.get("lang", "ru")
        if lang not in world.LANGUAGES or lang == "en":
            lang = "ru"
        try:
            item = bank.content_item(kind, ref)
        except BankError:
            abort(404)
        if request.method == "POST":
            try:
                bank.save_translation(request.staff, kind, ref, lang, request.form)
                flash("Translation saved. Visitors who read in this language see it now.", "success")
                return redirect(url_for("office_translate", kind=kind, ref=ref, lang=lang))
            except BankError as e:
                flash(str(e), "error")
        fields = sitecontent.KINDS[kind][2]
        return office_page("content_translate.html", kind=kind, ref=ref, item=item, lang=lang,
                           fields=[(f, fields[f]) for f in sitecontent.KINDS[kind][3]],
                           tr=bank.content_translation(kind, ref, lang),
                           done=bank.translation_status().get((kind, str(ref)), set()))

    @app.route("/admin/media", methods=["GET", "POST"])
    @staff_required()
    def office_media():
        if request.method == "POST":
            try:
                if request.form.get("action") == "delete":
                    bank.delete_media(request.staff, int(request.form.get("id") or 0))
                    flash("Picture deleted.", "success")
                else:
                    f = request.files.get("file")
                    mid = bank.add_media(request.staff, f.filename if f else "", f.read() if f else b"",
                                         request.form.get("alt", ""))
                    flash(f"Picture uploaded. Choose \"media:{mid}\" as the picture of a product or banner.", "success")
            except BankError as e:
                flash(str(e), "error")
            return redirect(url_for("office_media"))
        return office_page("media.html", rows=bank.media_list())

    @app.route("/admin/content/billers", methods=["GET", "POST"])
    @staff_required()
    def office_billers():
        edit = None
        if request.args.get("id", "").isdigit():
            try:
                edit = bank.biller(int(request.args["id"]))
            except BankError:
                abort(404)
        if request.method == "POST":
            try:
                bank.save_biller(request.staff, request.form,
                                 int(request.form["id"]) if request.form.get("id", "").isdigit() else None)
                flash("Biller saved.", "success")
                return redirect(url_for("office_billers"))
            except BankError as e:
                flash(str(e), "error")
        return office_page("billers.html", rows=bank.billers(active_only=False), edit=edit, form=request.form or edit or {},
                           CATEGORIES=BILLER_CATEGORIES, countries=world.countries("en"))

    # ----- ISO 20022 payment files -----

    @app.route("/admin/payments/iso", methods=["GET", "POST"])
    @staff_required()
    def office_iso():
        if request.method == "POST":
            action = request.form.get("action")
            try:
                if action == "create":
                    fid = bank.create_pain001(request.staff["staff_id"])
                    flash(f"Payment file #{fid} (pain.001) made. Download it and send it to the payment partner.", "success")
                elif action == "upload":
                    f = request.files.get("file")
                    text = f.read().decode("utf-8", "replace") if f and f.filename else request.form.get("xml", "")
                    done = bank.apply_pain002(text, request.staff["staff_id"])
                    flash(f"Status report read: {done['completed']} completed, {done['returned']} returned "
                          f"(money back to the customer), {done['already']} already finished, {done['unknown']} unknown.",
                          "success")
                else:
                    abort(400)
            except BankError as e:
                flash(str(e), "error")
            return redirect(url_for("office_iso"))
        return office_page("iso.html", files=bank.iso_files(), waiting=bank.waiting_for_file(),
                           mode=bank.payments_mode(), OK_CODES=OK_CODES, REJECT_REASONS=REJECT_REASONS)

    @app.route("/admin/payments/iso/<int:fid>.xml")
    @staff_required()
    def office_iso_file(fid):
        try:
            f = bank.iso_file(fid)
        except BankError:
            abort(404)
        return Response(f["xml"], mimetype="application/xml",
                        headers={"Content-Disposition": f"attachment; filename={f['msg_id']}_{f['kind']}.xml"})

    @app.route("/admin/payments/iso/<int:fid>/test-answer.xml")
    @staff_required()
    def office_iso_answer(fid):
        try:
            xml = bank.test_pain002(fid, request.staff["staff_id"])
        except BankError:
            abort(404)
        return Response(xml, mimetype="application/xml",
                        headers={"Content-Disposition": f"attachment; filename=test_pain002_{fid}.xml"})

    # ----- MRZ check of a document -----

    @app.route("/admin/mrz/<cid>", methods=["POST"])
    @staff_required()
    def office_mrz(cid):
        if not bank.rate_ok("mrz", request.staff["staff_id"]):
            abort(429)
        try:
            r = bank.check_mrz(cid, request.form.get("mrz", ""), request.staff["staff_id"])
            if r["ok"]:
                flash(f"MRZ is correct: {r['kind']} of {r['nationality']}, {r['surname']} {r['given']}, born "
                      f"{r['birth']}, valid until {r['expiry']}. Now compare the photo with the person.", "success")
            else:
                flash("MRZ check found problems: " + "; ".join(r["problems"]) + ".", "error")
        except BankError as e:
            flash(str(e), "error")
        return redirect(request.form.get("next") if (request.form.get("next") or "").startswith("/admin")
                        else url_for("office_applications"))

    # ----- compliance -----

    @app.route("/admin/compliance")
    @staff_required()
    def office_compliance():
        status = request.args.get("status", "Open")
        status = status if status in FLAG_STATUSES or status == "" else "Open"
        return office_page("compliance.html", rows=bank.aml_flags(status), status=status, RULES=RULES,
                           STATUSES=FLAG_STATUSES, examples={c: aml_amount(c) for c in world.CURRENCIES})

    @app.route("/admin/compliance/<int:flag_id>", methods=["GET", "POST"])
    @staff_required()
    def office_flag(flag_id):
        try:
            f = bank.aml_flag(flag_id)
        except BankError:
            abort(404)
        if request.method == "POST":
            try:
                bank.review_flag(request.staff, flag_id, request.form.get("action"), request.form.get("note"))
                flash("Flag cleared." if request.form.get("action") == "clear"
                      else "Flag reported. The report text is below (sandbox - nothing was sent).", "success")
                return redirect(url_for("office_flag", flag_id=flag_id))
            except BankError as e:
                flash(str(e), "error")
        with bank._connect() as conn:
            recent = [dict(r) for r in conn.execute(
                "SELECT * FROM transactions WHERE account_number = ? ORDER BY created_at DESC, id DESC LIMIT 15",
                (f["account_number"],))]
        return office_page("flag.html", f=f, recent=recent, RULES=RULES)

    @app.route("/admin/dormant", methods=["GET", "POST"])
    @staff_required()
    def office_dormant():
        if request.method == "POST":
            try:
                bank.activate_dormant(request.staff, request.form.get("number"), request.form.get("note"))
                flash("Account activated. The customer got an alert.", "success")
            except BankError as e:
                flash(str(e), "error")
            return redirect(url_for("office_dormant"))
        return office_page("dormant.html", rows=bank.dormant_accounts(), soon=bank.dormant_candidates())

    @app.route("/admin/checker", methods=["GET", "POST"])
    @staff_required()
    def office_checker():
        if request.method == "POST":
            try:
                t = office.decide_entry(request.staff, int(request.form.get("id") or 0),
                                        request.form.get("action") == "approve", request.form.get("reason", ""))
                ref = (t["receipt_no"] if isinstance(t, dict) else t.ref) if t else ""
                flash(f"Approved and posted (ref {ref})." if t else "Entry rejected. Nothing was posted.", "success")
            except BankError as e:
                flash(str(e), "error")
            return redirect(url_for("office_checker"))
        return office_page("checker.html", rows=bank.pending_entries(), log=office.entries_log(),
                           limits={c: checker_amount(c) for c in world.CURRENCIES})

    @app.route("/admin/health")
    @staff_required()
    def office_health():
        return office_page("health.html", h=bank.health(), blocks=bank.rate_report(), errors=bank.errors(),
                           LIMITS=__import__("devices").LIMITS)

    # ----- settings (v4 part), staff roles, card test -----

    @app.route("/admin/settings/v4", methods=["POST"])
    @staff_required()
    def office_settings_v4():
        try:
            office.save_settings_v4(request.staff, request.form)
            flash("Settings saved.", "success")
        except BankError as e:
            flash(str(e), "error")
        return redirect(url_for("office_settings") + "#v4")

    @app.route("/admin/fx/history")
    @staff_required()
    def office_fx_history():
        return office_page("fx_history.html", days=bank.fx_history_days(30))

    @app.route("/admin/staff/<sid>/job", methods=["POST"])
    @staff_required()
    def office_staff_job(sid):
        try:
            office.set_job_role(request.staff, sid, request.form.get("job_role"))
            flash("Role changed. It works from the next click of that person.", "success")
        except BankError as e:
            flash(str(e), "error")
        return redirect(url_for("office_staff"))

    @app.route("/admin/customers/<cid>/card-test", methods=["POST"])
    @staff_required()
    def office_card_test(cid):
        try:
            result, t = office.card_test(request.staff, cid, request.form)
            flash(f"Sandbox card network answered: {result}" + (f" (ref {t.ref})" if t else ""),
                  "success" if result == "APPROVED" else "info")
        except BankError as e:
            flash(str(e), "error")
        return redirect(url_for("office_customer", cid=cid) + "#card")

    app.jinja_env.globals.update(mask_ip=mask_ip, ROLES=roles.ROLES, JOB_ROLES=roles.JOB_ROLES,
                                 ABILITIES=roles.ABILITIES, abilities_of=roles.abilities_of, role_label=roles.job_label,
                                 mrz_checks=bank.mrz_checks, customer_accounts=bank.accounts_of,
                                 customer_card=lambda cid: bank.card(cid), card_channels=CHANNELS,
                                 customer_flags=lambda cid: [f for f in bank.aml_flags("") if f["customer_id"] == cid][:5],
                                 financial_year=financial_year, aml_rules=RULES)

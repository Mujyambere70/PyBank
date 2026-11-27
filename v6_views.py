"""
v6_views.py - the web pages of version 6.

Back office   Abilities (every ability of the bank, own abilities, which ability opens which page),
              the ability matrix (people x abilities, tick and save), the person's abilities page
              (tick boxes, temporary abilities, copy from a colleague, back to the role, history),
              the branch vault, cheques in clearing, credit-card applications, held payments,
              sanctions list import, training of the assistant, the chat assistant that can do
              things, and the worker's own activity.
Net Banking   credit card (apply, pay, block, statements), nominees, cheques, the chat assistant.
Public        the chat assistant (/chat ...), which keeps the conversation and can answer follow-ups.
"""

import re
import secrets
from datetime import date, timedelta

from flask import Response, abort, flash, jsonify, redirect, render_template, request, session, url_for

import roles
import world
from bank import BankError, parse_amount
from chatbot import ChatBot, who_can
from cheques import MAX_NOMINEES, RELATIONS, RETURN_REASONS
from creditcard import LIMIT_MAX, LIMIT_MIN
from datamanager import DATA_TABLES_EDIT, DataManager
from screening import CSV_COLUMNS, HOLD_STATUSES
from vault import DENOMINATIONS, MOVE_KINDS
from world import format_money

CSV_MAX = 2 * 1024 * 1024


def register(app, bank, office, h):
    login_required, current = h["login_required"], h["current"]
    staff_required, office_page, client_ip = h["staff_required"], h["office_page"], h["client_ip"]
    bot = ChatBot(bank, office)
    data_manager = DataManager(bank, office)
    docs = h.setdefault("docs", {})                        # search documents of v5 (filled by v5_views.register)

    app.jinja_env.globals.update(DENOMINATIONS=DENOMINATIONS, MOVE_KINDS=MOVE_KINDS, RETURN_REASONS=RETURN_REASONS,
                                 RELATIONS=RELATIONS, MAX_NOMINEES=MAX_NOMINEES)

    @app.context_processor
    def v6_globals():
        # the groups are read on every page: own abilities made by the administrator appear at once
        return {"ABILITY_GROUPS": roles.ability_groups(), "ABILITIES": roles.ABILITIES}

    # =====================================================================
    # Chat assistant (public, Net Banking, back office)
    # =====================================================================

    def faq_docs():
        return [{"title": f["question"], "url": url_for("faq") + f"#faq-{f['id']}", "kind": "FAQ",
                 "keywords": f["question"], "answer": f["answer"], "concepts": []}
                for f in bank.faqs(world.current_lang())]

    def chat_who():
        if "cid" in session:
            return "customer:" + session["cid"]
        if not session.get("chat_id"):
            session["chat_id"] = secrets.token_hex(8)
        return "public:" + session["chat_id"]

    def chat_docs():
        if "cid" in session and "netbanking_docs" in docs:
            return docs["netbanking_docs"](session["cid"])
        return docs["public_docs"]() if "public_docs" in docs else []

    def tr(r):
        return docs["tr_answer"](r) if "tr_answer" in docs else r

    def body():
        return request.get_json(silent=True) or request.form

    @app.route("/chat", methods=["POST"])
    def chat_ask():
        if not bank.rate_ok("assistant", client_ip()):
            return jsonify({"text": "Too many questions. Please wait a minute.", "links": [], "items": []}), 429
        customer, account = current() if "cid" in session else (None, None)
        r = bot.ask(chat_who(), body().get("q", ""), chat_docs(), customer=customer, account=account,
                    faq_docs=faq_docs())
        return jsonify(tr(r))

    @app.route("/chat/confirm", methods=["POST"])
    def chat_confirm():
        if "cid" not in session:
            return jsonify({"text": "Please sign in to Net Banking first.", "kind": "error", "links": [], "items": []}), 403
        customer, _ = current()
        try:
            return jsonify(tr(bot.confirm(chat_who(), int(body().get("id") or 0), body(), customer=customer)))
        except BankError as e:
            return jsonify({"text": str(e), "kind": "error", "links": [], "items": []}), 400

    @app.route("/chat/history")
    def chat_history():
        return jsonify(bot.history(chat_who()))

    @app.route("/chat/feedback", methods=["POST"])
    def chat_feedback():
        bot.feedback(chat_who(), int(body().get("id") or 0), int(body().get("value") or 0))
        return jsonify({"ok": True})

    @app.route("/chat/clear", methods=["POST"])
    def chat_clear():
        bot.clear(chat_who())
        return jsonify({"ok": True})

    def staff_who():
        return "staff:" + request.staff["staff_id"]

    @app.route("/admin/chat", methods=["POST"])
    @staff_required()
    def office_chat():
        me = request.staff
        r = bot.ask(staff_who(), body().get("q", ""), docs["office_docs"](me) if "office_docs" in docs else [],
                    staff=me, can=who_can(me))
        return jsonify(tr(r))

    @app.route("/admin/chat/confirm", methods=["POST"])
    @staff_required()
    def office_chat_confirm():
        me = request.staff
        try:
            return jsonify(tr(bot.confirm(staff_who(), int(body().get("id") or 0), body(), staff=me, can=who_can(me))))
        except BankError as e:
            return jsonify({"text": str(e), "kind": "error", "links": [], "items": []}), 400

    @app.route("/admin/chat/history")
    @staff_required()
    def office_chat_history():
        return jsonify(bot.history(staff_who()))

    @app.route("/admin/chat/feedback", methods=["POST"])
    @staff_required()
    def office_chat_feedback():
        bot.feedback(staff_who(), int(body().get("id") or 0), int(body().get("value") or 0))
        return jsonify({"ok": True})

    @app.route("/admin/chat/clear", methods=["POST"])
    @staff_required()
    def office_chat_clear():
        bot.clear(staff_who())
        return jsonify({"ok": True})

    @app.route("/admin/assistant/train", methods=["GET", "POST"])
    @staff_required()
    def office_assistant_train():
        f = request.form
        if request.method == "POST":
            try:
                if f.get("action") == "delete":
                    bot.delete_answer(request.staff, int(f.get("id") or 0))
                    flash("Answer deleted.", "success")
                else:
                    bot.save_answer(request.staff, f, int(f.get("id")) if (f.get("id") or "").isdigit() else None)
                    flash("Answer saved. The assistant uses it from the next question.", "success")
            except BankError as e:
                flash(str(e), "error")
            return redirect(url_for("office_assistant_train"))
        return office_page("assistant_train.html", answers=bot.trained_answers(), unanswered=bot.unanswered(),
                           stats=bot.stats(), ask=request.args.get("q", ""))

    # =====================================================================
    # Abilities
    # =====================================================================

    def admin_pages():
        """Every back-office page with the ability that opens it now (custom rules first)."""
        out, seen = [], set()
        for rule in app.url_map.iter_rules():
            path = rule.rule
            if not path.startswith("/admin") or path in ("/admin", "/admin/login", "/admin/logout"):
                continue
            pattern = re.sub(r"<[^>]+>", "*", path)
            for method in (["GET", "POST"] if "POST" in rule.methods and "GET" in rule.methods else
                           ["POST"] if "POST" in rule.methods else ["GET"]):
                if (pattern, method) in seen:
                    continue
                seen.add((pattern, method))
                code = roles.ability_for(pattern, method)
                out.append({"pattern": pattern, "method": method, "ability": code,
                            "label": "everyone signed in" if code is None else roles.ABILITIES.get(code, code)})
        return sorted(out, key=lambda p: (p["pattern"], p["method"]))

    @app.route("/admin/abilities", methods=["GET", "POST"])
    @staff_required()
    def office_abilities():
        me, f = request.staff, request.form
        if request.method == "POST":
            action = f.get("action")
            try:
                if action == "create":
                    code = office.create_ability(me, f.get("label"), f.get("group"), f.get("description"),
                                                 [p for p in (f.get("pages") or "").splitlines() if p.strip()])
                    if f.get("roles"):
                        for job in f.getlist("roles"):
                            r = office.job_roles().get(job)
                            if r:
                                office.save_role(me, job, r["label"], list(r["abilities"] | {code}))
                    flash(f"Ability “{roles.ABILITIES[code]}” created. Tick it for people or roles.", "success")
                    return redirect(url_for("office_abilities") + f"#ab-{code}")
                if action == "update":
                    office.update_ability(me, f.get("code"), f.get("label"), f.get("group"), f.get("description"))
                    flash("Ability saved.", "success")
                elif action == "delete":
                    office.delete_ability(me, f.get("code"))
                    flash("Ability deleted - it was taken away from every role and person.", "success")
                elif action == "rule":
                    office.add_rule(me, f.get("pattern"), f.get("ability"), f.get("method"), f.get("note"))
                    flash("Page rule saved. It works from the next click.", "success")
                    return redirect(url_for("office_abilities") + "#pages")
                elif action == "unrule":
                    office.delete_rule(me, int(f.get("id") or 0))
                    flash("Page rule removed: the page uses the bank's own ability again.", "success")
                    return redirect(url_for("office_abilities") + "#pages")
                else:
                    abort(400)
            except BankError as e:
                flash(str(e), "error")
            return redirect(url_for("office_abilities"))
        people = office.staff_list()
        counts = {c: sum(1 for s in people if s.get("active") and c in (s.get("_abilities") or set())) for c in roles.ABILITIES}
        q = request.args.get("q", "").strip().lower()
        return office_page("abilities.html", custom=office.custom_abilities(), rules=office.rules(), pages=admin_pages(),
                           held=counts, people=len([s for s in people if s.get("active")]), q=q,
                           job_roles=office.job_roles(), who=request.args.get("who", ""),
                           who_rows=office.who_has(request.args.get("who")) if request.args.get("who") in roles.ABILITIES else [])

    @app.route("/admin/abilities/matrix", methods=["GET", "POST"])
    @staff_required()
    def office_ability_matrix():
        if request.method == "POST":
            try:
                n = office.save_matrix(request.staff, request.form)
                flash(f"Saved. Abilities of {n} person(s) changed." if n else "Nothing was changed.", "success")
            except BankError as e:
                flash(str(e), "error")
            return redirect(url_for("office_ability_matrix", group=request.form.get("group", "")))
        groups = roles.ability_groups()
        group = request.args.get("group") or groups[0][0]
        items = next((i for g, i in groups if g == group), groups[0][1])
        people = [s for s in office.staff_list() if s.get("active")]
        return office_page("abilities_matrix.html", groups=groups, group=group, items=items, people=people,
                           grants={s["staff_id"]: office.active_grants(s["staff_id"]) for s in people})

    @app.route("/admin/staff/<sid>/abilities", methods=["POST"])
    @staff_required()
    def office_staff_abilities(sid):
        me, f = request.staff, request.form
        action = f.get("action")
        try:
            if action == "ticks":
                office.set_staff_access(me, sid, f)                     # the form carries mode=ticks
                flash("Abilities saved. They work from the next click of this person.", "success")
            elif action == "give":
                office.give_ability(me, sid, f.get("code"))
                flash(f"Given: {roles.ABILITIES.get(f.get('code'), '')}.", "success")
            elif action == "take":
                office.take_ability(me, sid, f.get("code"))
                flash(f"Taken away: {roles.ABILITIES.get(f.get('code'), '')}.", "success")
            elif action == "grant":
                office.grant_temporary(me, sid, f.get("code"), f.get("until"), f.get("reason"))
                flash("Temporary ability given. It stops by itself after the last day.", "success")
            elif action == "revoke":
                office.revoke_grant(me, int(f.get("id") or 0))
                flash("Temporary ability ended.", "success")
            elif action == "copy":
                office.copy_abilities(me, f.get("from"), sid)
                flash("Abilities copied.", "success")
            elif action == "reset":
                office.reset_to_role(me, sid)
                flash("Abilities are now exactly those of the job role.", "success")
            else:
                abort(400)
        except BankError as e:
            flash(str(e), "error")
        back = f.get("back") or ""
        if back.startswith("/admin/"):
            return redirect(back)
        return redirect(url_for("office_staff_access", sid=sid) + "#abilities")

    # =====================================================================
    # Vault, cheques, credit cards, held payments, sanctions import
    # =====================================================================

    @app.route("/admin/vault", methods=["GET", "POST"])
    @staff_required()
    def office_vault():
        me, f = request.staff, request.form
        if request.method == "POST":
            try:
                if f.get("action") in ("approve", "reject"):
                    office.decide_vault_move(me, int(f.get("id") or 0), f.get("action") == "approve", f.get("note", ""))
                    flash("Vault move confirmed." if f.get("action") == "approve" else "Vault move rejected.", "success")
                elif f.get("action") in ("in", "out"):
                    cur = f.get("currency", "INR")
                    office.head_office_move(me, cur, parse_amount(f.get("amount", ""), cur), f.get("action") == "in",
                                            f.get("note", ""))
                    flash("Recorded. A second vault keeper must confirm it.", "success")
                else:
                    abort(400)
            except BankError as e:
                flash(str(e), "error")
            return redirect(url_for("office_vault"))
        return office_page("vault.html", vault=office.vault(), waiting=office.vault_moves("Waiting"),
                           moves=office.vault_moves(limit=60))

    @app.route("/admin/till/vault", methods=["POST"])
    @staff_required()
    def office_till_vault():
        me, f = request.staff, request.form
        cur = f.get("currency", "INR")
        try:
            amount = parse_amount(f.get("amount", ""), cur)
            if f.get("action") == "return":
                office.request_return(me, cur, amount, f.get("note", ""))
            else:
                office.request_topup(me, cur, amount, f.get("note", ""))
            flash("Asked. The cash moves when a vault keeper confirms it.", "success")
        except BankError as e:
            flash(str(e), "error")
        return redirect(url_for("office_till"))

    @app.route("/admin/cheques", methods=["GET", "POST"])
    @staff_required()
    def office_cheques():
        f = request.form
        if request.method == "POST":
            try:
                if f.get("action") == "run":
                    n = bank.clear_cheques()
                    flash(f"Clearing run: {n} cheque(s) decided.", "success")
                else:
                    res = bank.decide_cheque(int(f.get("id") or 0), f.get("action") == "clear",
                                             request.staff["staff_id"], f.get("reason", ""))
                    flash(f"Cheque {res.lower()}.", "success")
            except BankError as e:
                flash(str(e), "error")
            return redirect(url_for("office_cheques", status=request.args.get("status", "In clearing")))
        status = request.args.get("status", "In clearing")
        return office_page("cheques.html", rows=bank.cheques(status=status if status != "all" else ""), status=status)

    @app.route("/admin/credit-cards", methods=["GET", "POST"])
    @staff_required()
    def office_credit_cards():
        f = request.form
        if request.method == "POST":
            try:
                if f.get("action") == "limit":
                    bank.set_credit_limit(request.staff, int(f.get("id") or 0), parse_amount(f.get("limit", ""), "INR"))
                    flash("Credit limit changed.", "success")
                else:
                    bank.decide_credit_card(request.staff, int(f.get("id") or 0), f.get("action") == "approve",
                                            parse_amount(f.get("limit") or "0", "INR") if f.get("limit") else 0,
                                            f.get("reason", ""))
                    flash("Decision saved; the customer was told.", "success")
            except BankError as e:
                flash(str(e), "error")
            return redirect(url_for("office_credit_cards", status=request.args.get("status", "Applied")))
        status = request.args.get("status", "Applied")
        rows = bank.credit_card_applications(status if status != "all" else "")
        for k in rows:
            if k["status"] == "Applied":
                k["suggested"], k["score"] = bank.suggested_limit(k["customer_id"], k["income"])
            else:
                k["suggested"] = k["limit_amount"]
        return office_page("credit_cards.html", rows=rows,
                           status=status, LIMIT_MIN=LIMIT_MIN, LIMIT_MAX=LIMIT_MAX)

    @app.route("/admin/payment-holds", methods=["GET", "POST"])
    @staff_required()
    def office_payment_holds():
        f = request.form
        if request.method == "POST":
            try:
                bank.decide_payment_hold(request.staff, int(f.get("id") or 0), f.get("action") == "release",
                                           f.get("note", ""))
                flash("Payment released: it goes to the other bank now." if f.get("action") == "release"
                      else "Payment stopped: the money is back in the customer's account.", "success")
            except BankError as e:
                flash(str(e), "error")
            return redirect(url_for("office_payment_holds"))
        status = request.args.get("status", "Held")
        return office_page("payment_holds.html", rows=bank.payment_holds(status if status != "all" else ""),
                           status=status, STATUSES=HOLD_STATUSES)

    @app.route("/admin/screening/import", methods=["POST"])
    @staff_required()
    def office_screening_import():
        up = request.files.get("file")
        try:
            if up is None or not up.filename:
                raise BankError("Please choose a CSV file.")
            raw = up.read(CSV_MAX + 1)
            if len(raw) > CSV_MAX:
                raise BankError("The file is too big (2 MB at most).")
            rows, added = bank.import_sanctions_csv(request.staff, raw.decode("utf-8-sig", "replace"),
                                                      request.form.get("replace") == "1")
            flash(f"{rows} line(s) read, {added} name(s) added. Run “Screen everybody” to check the customers again.",
                  "success")
        except BankError as e:
            flash(str(e), "error")
        return redirect(url_for("office_screening"))

    @app.route("/admin/screening/template.csv")
    @staff_required()
    def office_screening_template():
        return Response(",".join(CSV_COLUMNS) + "\nIvan Example Person,Ivan E. Person;I. Person,Testland,1970-01-01,DEMO\n",
                        mimetype="text/csv", headers={"Content-Disposition": "attachment; filename=sanctions_template.csv"})

    # =====================================================================
    # Data manager v2 (sort, pages, CSV, delete many, every table to read)
    # =====================================================================

    @app.route("/admin/data/<table>/export.csv")
    @staff_required()
    def office_data_export(table):
        if data_manager.info(table) is None:
            abort(404)
        with bank._connect() as conn:
            office.audit(conn, request.staff, "Data exported", table, request.args.get("q", "")[:80])
        return Response(data_manager.to_csv(table, request.args.get("q", "")[:80]), mimetype="text/csv",
                        headers={"Content-Disposition": f"attachment; filename={table}_{date.today()}.csv"})

    @app.route("/admin/data/<table>/import", methods=["POST"])
    @staff_required()
    def office_data_import(table):
        if table not in DATA_TABLES_EDIT:
            abort(404)
        up = request.files.get("file")
        try:
            if up is None or not up.filename:
                raise BankError("Please choose a CSV file.")
            raw = up.read(CSV_MAX + 1)
            if len(raw) > CSV_MAX:
                raise BankError("The file is too big (2 MB at most).")
            changed, added, errors = data_manager.import_csv(request.staff, table, raw.decode("utf-8-sig", "replace"))
            flash(f"Import: {changed} record(s) changed, {added} added." + (" Problems: " + "; ".join(errors[:5]) if errors else ""),
                  "info" if errors else "success")
        except BankError as e:
            flash(str(e), "error")
        return redirect(url_for("office_data_table", table=table))

    @app.route("/admin/data/<table>/delete", methods=["POST"])
    @staff_required()
    def office_data_delete_many(table):
        info = DATA_TABLES_EDIT.get(table)
        if info is None or not info["delete"]:
            abort(404)
        keys = request.form.getlist("keys")
        try:
            if not keys:
                raise BankError("Tick the records to delete first.")
            n = data_manager.delete_many(request.staff, table, keys[:200])
            flash(f"{n} record(s) deleted.", "success")
        except BankError as e:
            flash(str(e), "error")
        return redirect(url_for("office_data_table", table=table))

    # =====================================================================
    # The worker's own activity
    # =====================================================================

    @app.route("/admin/profile/activity")
    @staff_required()
    def office_profile_activity():
        me = request.staff
        with bank._connect() as conn:
            rows = [dict(r) for r in conn.execute("SELECT * FROM audit_log WHERE staff_id = ? ORDER BY id DESC LIMIT 200",
                                                  (me["staff_id"],))]
            about_me = [dict(r) for r in conn.execute("SELECT * FROM audit_log WHERE target = ? AND staff_id <> ? "
                                                      "ORDER BY id DESC LIMIT 50", (me["staff_id"], me["staff_id"]))]
        return office_page("profile_activity.html", rows=rows, about_me=about_me,
                           grants=office.grants(me["staff_id"]), mine=sorted(me.get("_abilities") or []))

    # =====================================================================
    # Net Banking: credit card, nominees, cheques
    # =====================================================================

    @app.route("/netbanking/credit-card", methods=["GET", "POST"])
    @login_required
    def nb_credit_card():
        customer, account = current()
        cid, f = customer.customer_id, request.form
        if request.method == "POST":
            action = f.get("action")
            try:
                if action == "apply":
                    bank.apply_credit_card(cid, parse_amount(f.get("income", ""), "INR"),
                                           parse_amount(f.get("requested") or "0", "INR") if f.get("requested") else 0)
                    flash("Application sent. The bank decides within two working days.", "success")
                elif action == "pay":
                    how = f.get("how", "full")
                    t = bank.pay_credit_card(cid, how, parse_amount(f.get("amount") or "0", "INR") if how == "other" else 0,
                                             f.get("pin", ""), f.get("from") or None)
                    flash(f"Paid {format_money(t.amount, 'INR')} to your credit card.", "success")
                elif action in ("block", "unblock"):
                    if action == "unblock":
                        bank._check_pin(cid, f.get("pin", ""))
                    bank.block_credit_card(cid, action == "block")
                    flash("Credit card blocked." if action == "block" else "Credit card works again.", "success")
                elif action == "try":
                    k = bank.credit_card(cid)
                    if k is None:
                        raise BankError("You have no credit card.")
                    res = bank.cc_purchase(k["id"], parse_amount(f.get("amount", ""), "INR"), f.get("merchant") or "Test shop")
                    flash("Sandbox purchase: " + res, "success" if res == "APPROVED" else "error")
                else:
                    abort(400)
            except BankError as e:
                flash(str(e), "error")
            return redirect(url_for("nb_credit_card"))
        k = bank.credit_card(cid)
        return render_template("netbanking/credit_card.html", customer=customer, account=account, k=k,
                               tx=bank.cc_transactions(k["id"]) if k else [], sts=bank.cc_statements(k["id"]) if k else [],
                               accounts=[a for a in bank.accounts_of(cid) if a.status == "Active" and a.currency == "INR"],
                               LIMIT_MIN=LIMIT_MIN, LIMIT_MAX=LIMIT_MAX, cc_rate=bank.settings().get("cc_rate", "3.5"))

    @app.route("/netbanking/nominees", methods=["GET", "POST"])
    @login_required
    def nb_nominees():
        customer, account = current()
        cid = customer.customer_id
        if request.method == "POST":
            f = request.form
            rows = [{k: f.get(f"{k}_{i}", "") for k in ("name", "relation", "dob", "share", "address")}
                    for i in range(MAX_NOMINEES)]
            try:
                bank._check_pin(cid, f.get("pin", ""))
                n = bank.save_nominees(cid, rows)
                flash(f"Nominees saved ({n}).", "success")
                return redirect(url_for("nb_nominees"))
            except BankError as e:
                flash(str(e), "error")
        return render_template("netbanking/nominees.html", customer=customer, account=account, rows=bank.nominees(cid))

    @app.route("/netbanking/cheques")
    @login_required
    def nb_cheques():
        customer, account = current()
        return render_template("netbanking/cheques.html", customer=customer, account=account,
                               rows=bank.cheques(customer.customer_id))

    # template helpers that need the bank
    def customer_extras(cid):
        """Nominees, cheques, credit card and held payments for the customer page of the back office."""
        return {"nominees": bank.nominees(cid), "cheques": bank.cheques(cid, limit=10), "card": bank.credit_card(cid),
                "holds": [h for h in bank.payment_holds("") if h["customer_id"] == cid][:10]}

    app.jinja_env.globals.update(customer_extras=customer_extras, uncleared_total=bank.uncleared_total,
                                 vault_currencies=lambda: [v["currency"] for v in office.vault()],
                                 days_from_today=lambda n: (date.today() + timedelta(days=n)).isoformat())

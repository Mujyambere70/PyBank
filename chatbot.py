"""
chatbot.py - "Ask PyBank" as a real chat assistant (version 6).

Version 5 answered one question at a time. Version 6 turns it into a conversation:

  * it remembers the conversation (table chat_messages) - the chat comes back after a new page or a
    new sign-in, and it understands follow-ups: "how much did I spend on food last month?" ...
    "and this month?" ... "what about shopping?";
  * it DOES things, always after the person confirms (and with the transaction PIN where money moves):
      customers  send money to a payee or an account, block or unblock the debit card, open a fixed
                 deposit, put money into a savings goal, pay the credit card bill, ask for a cheque
                 book or another service, open a statement for a month
      staff      open the cash drawer, freeze or activate a customer, show a customer's summary,
                 list the work waiting - each only with the needed ability
  * small talk (hello, thanks, who are you, what can you do) and quick-reply buttons;
  * answers straight from the FAQ when a question matches one, and from answers that the bank's
    staff taught it (Back office > Assistant training); questions it could not answer are listed
    there, with the 👍 / 👎 feedback of the users;
  * it answers in the language of the question when the catalog knows the text (Russian, French,
    Spanish, Arabic, Chinese).

A language model is still optional (setting assistant_llm = 1 and ANTHROPIC_API_KEY): only questions
that nothing else understood are sent, and only the question - never account data.
"""

import json
import re
from datetime import date
from difflib import SequenceMatcher

import roles
import smartsearch
from assistant import Assistant, answer, parse_amount_words, parse_period
from core import BankError, now
from world import format_money

CHAT_SCHEMA = """
CREATE TABLE IF NOT EXISTS chat_messages (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    who        TEXT NOT NULL,
    role       TEXT NOT NULL,
    text       TEXT NOT NULL,
    data       TEXT NOT NULL DEFAULT '',
    kind       TEXT NOT NULL DEFAULT '',
    feedback   INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS bot_answers (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    question   TEXT NOT NULL,
    keywords   TEXT NOT NULL DEFAULT '',
    answer     TEXT NOT NULL,
    link       TEXT NOT NULL DEFAULT '',
    active     INTEGER NOT NULL DEFAULT 1,
    hits       INTEGER NOT NULL DEFAULT 0,
    created_by TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
"""
HISTORY = 40
GREET = r"^(hi+|hello|hey|good (morning|afternoon|evening)|привет|здравствуй\w*|добрый (день|вечер)|bonjour|salut|hola|مرحبا|你好|muraho)\b"
THANKS = r"\b(thanks?|thank you|спасибо|merci|gracias|شكرا|谢谢|murakoze)\b"
BYE = r"^(bye|goodbye|see you|пока|до свидания|au revoir|adiós|adios)\b"
WHO = r"who are you|what are you|your name|are you (a )?(bot|human|robot)|кто ты|ты бот"
HELP = r"what can you do|help me|^help$|how can you help|что ты умеешь|помоги"
YES = r"^(yes|yep|ok|okay|sure|confirm|да|oui|sí|si)\b"
NO = r"^(no|nope|cancel|stop|нет|отмена|non)\b"
FOLLOW = r"^(and|what about|how about|same for|а|и|а за|et|y)\b"
PERIOD_RE = re.compile(r"\b(today|yesterday|this (month|week|year)|last (month|year|\d+ days)|previous month|"
                       r"(in )?(january|february|march|april|may|june|july|august|september|october|november|december)"
                       r"( \d{4})?)\b", re.I)
LANG_HINTS = [("ru", r"[а-яё]"), ("ar", r"[؀-ۿ]"), ("zh", r"[一-鿿]"),
              ("fr", r"\b(je|mon|ma|mes|combien|virement|solde|carte|bonjour|merci|où)\b"),
              ("es", r"\b(mi|mis|cuánto|cuanto|saldo|tarjeta|hola|gracias|dónde|transferencia)\b")]


def detect_lang(text):
    t = (text or "").lower()
    for lang, pattern in LANG_HINTS:
        if re.search(pattern, t):
            return lang
    return "en"


def reply(text="", links=(), items=(), kind="info", suggestions=(), confirm=None, llm=False):
    r = answer(text, links, items, kind, llm)
    r["suggestions"] = list(suggestions)
    r["confirm"] = confirm
    return r


class ChatBot:
    def __init__(self, bank, office=None):
        self.bank, self.office = bank, office
        self.helper = Assistant(bank)

    # ===== memory =====

    def _save(self, who, role, text, data=None, kind=""):
        with self.bank._connect() as conn:
            return conn.execute("INSERT INTO chat_messages (who, role, text, data, kind, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                                (who, role, text[:2000], json.dumps(data or {}, ensure_ascii=False)[:8000], kind,
                                 now())).lastrowid

    def history(self, who, limit=HISTORY):
        with self.bank._connect() as conn:
            rows = [dict(r) for r in conn.execute("SELECT * FROM chat_messages WHERE who = ? ORDER BY id DESC LIMIT ?",
                                                  (who, limit))]
        out = []
        for r in reversed(rows):
            d = json.loads(r["data"] or "{}")
            if d.get("confirm") and (d["confirm"].get("done") or d["confirm"].get("cancelled")):
                d["confirm"] = None                            # an old question that was answered already
            out.append({"id": r["id"], "role": r["role"], "text": r["text"], "links": d.get("links", []),
                        "items": d.get("items", []), "confirm": d.get("confirm"), "feedback": r["feedback"],
                        "at": r["created_at"]})
        return out

    def clear(self, who):
        with self.bank._connect() as conn:
            conn.execute("DELETE FROM chat_messages WHERE who = ?", (who,))

    def feedback(self, who, msg_id, value):
        with self.bank._connect() as conn:
            conn.execute("UPDATE chat_messages SET feedback = ? WHERE id = ? AND who = ? AND role = 'bot'",
                         (1 if value > 0 else -1 if value < 0 else 0, msg_id, who))

    def _last_question(self, who):
        with self.bank._connect() as conn:
            r = conn.execute("SELECT text, data FROM chat_messages WHERE who = ? AND role = 'bot' AND kind NOT IN "
                             "('unknown', 'smalltalk', 'action', 'done') ORDER BY id DESC LIMIT 1", (who,)).fetchone()
        return json.loads(r[1] or "{}").get("question", "") if r else ""

    def _pending(self, who):
        with self.bank._connect() as conn:
            r = conn.execute("SELECT id, data FROM chat_messages WHERE who = ? AND role = 'bot' ORDER BY id DESC LIMIT 1",
                             (who,)).fetchone()
        if r is None:
            return None, None
        d = json.loads(r[1] or "{}")
        c = d.get("confirm")
        if c and not c.get("done") and not c.get("cancelled"):
            return r[0], d
        return None, None

    def _mark(self, msg_id, **flags):
        with self.bank._connect() as conn:
            r = conn.execute("SELECT data FROM chat_messages WHERE id = ?", (msg_id,)).fetchone()
            d = json.loads(r[0] or "{}")
            d["confirm"].update(flags)
            conn.execute("UPDATE chat_messages SET data = ? WHERE id = ?", (json.dumps(d, ensure_ascii=False), msg_id))

    # ===== the conversation =====

    def ask(self, who, text, docs, customer=None, staff=None, can=None, account=None, faq_docs=()):
        """One message from a person. who = 'customer:PB..', 'staff:EMP..' or 'public:<random>'."""
        text = " ".join(str(text or "").split())[:400]
        self._save(who, "user", text or "…")
        r = self._answer(who, text, docs, customer, staff, can, account, faq_docs)
        r["lang"] = detect_lang(text)
        data = {"links": r["links"], "items": r["items"], "confirm": r.get("confirm"), "question": r.get("question", text)}
        r["id"] = self._save(who, "bot", r["text"], data, r["kind"])
        return r

    def _answer(self, who, text, docs, customer, staff, can, account, faq_docs):
        t = text.lower()
        pend_id, pend = self._pending(who)
        if pend_id and re.search(NO, t):
            self._mark(pend_id, cancelled=True)
            return reply("All right, I cancelled it. Nothing was done.", kind="smalltalk")
        if pend_id and re.search(YES, t) and not pend["confirm"].get("pin") and not pend["confirm"].get("fields"):
            return self.confirm(who, pend_id, {}, customer=customer, staff=staff, can=can, _inner=True)
        small = self._small_talk(t, customer, staff)
        if small:
            return small
        trained = self._trained(text)
        if trained:
            return trained
        if customer is not None:
            act = self._customer_action(text, t, customer)
            if act:
                return act
        if staff is not None:
            act = self._staff_action(text, t, staff, can)
            if act:
                return act
        follow = self._follow_up(who, text, t)
        if follow:
            text, t = follow, follow.lower()
        if customer is not None:
            r = self.helper.customer(customer.customer_id, text, docs, account)
        elif staff is not None:
            r = self.helper.staff(self.office, staff, text, docs, can)
        else:
            r = self.helper.public(text, docs)
        if r["kind"] in ("unknown", "info") and not r["items"] and (not r["text"] or r["text"].startswith(
                ("This may help", "Here is where", "These pages", "Sorry"))):
            faq = self._faq(text, faq_docs)
            if faq:
                return faq
        out = reply(r["text"], r["links"], r["items"], r["kind"], self._suggest(customer, staff), llm=r.get("llm"))
        out["question"] = text
        return out

    def _suggest(self, customer, staff):
        if customer is not None:
            return ["My balance", "Spent this month", "Send money", "Block my card", "FD rates"]
        if staff is not None:
            return ["What is waiting?", "My cash drawer", "Large transactions today"]
        return ["Open an account", "FD rates", "Branches", "100 USD in INR"]

    def _small_talk(self, t, customer, staff):
        name = (customer.full_name.split()[0] if customer is not None else
                staff["full_name"].split()[0] if staff is not None else "")
        if re.search(GREET, t) and len(t.split()) <= 4:
            return reply(f"Hello{', ' + name if name else ''}! How can I help you today?", kind="smalltalk",
                         suggestions=self._suggest(customer, staff))
        if re.search(THANKS, t) and len(t.split()) <= 6:
            return reply("You are welcome! Anything else?", kind="smalltalk", suggestions=self._suggest(customer, staff))
        if re.search(BYE, t):
            return reply("Goodbye! Your chat is kept here if you need it again.", kind="smalltalk")
        if re.search(WHO, t):
            return reply("I am Ask PyBank, the bank's assistant program - not a person. I answer from the bank's own "
                         "data and pages, and I never ask for your password or PIN in a message.", kind="smalltalk")
        if re.search(r"how are you|как дела|ça va|qué tal", t):
            return reply("I am fine, thank you - ready to help. What do you need?", kind="smalltalk",
                         suggestions=self._suggest(customer, staff))
        if re.search(HELP, t):
            if customer is not None:
                items = ["Your balance, spending and income (“food last month”, “salary this year”)",
                         "Find payments (“transfers to Priya above 5000”)",
                         "Send money: “send 500 to Priya” - you confirm with your PIN",
                         "Block or unblock your debit card, pay your credit card bill",
                         "Open an FD: “open FD of 20000 for 12 months”, add money to a goal",
                         "Ask for a cheque book, open a statement for a month",
                         "Rates, EMI and currency questions"]
            elif staff is not None:
                items = ["The work waiting for you (“what is waiting?”)", "“open my drawer with 200000”",
                         "“freeze PB10000003” / “activate PB10000003” (with the ability)",
                         "“customer PB10000001” - a short summary", "Customers by country, large transactions",
                         "How to do something in the back office"]
            else:
                items = ["Accounts, cards and loans", "Interest and exchange rates, EMI", "Branches and contacts",
                         "How to open an account"]
            return reply("Here is what I can do:", items=items, kind="smalltalk", suggestions=self._suggest(customer, staff))
        return None

    def _trained(self, text):
        q = smartsearch.norm(text)
        if not q:
            return None
        with self.bank._connect() as conn:
            rows = [dict(r) for r in conn.execute("SELECT * FROM bot_answers WHERE active = 1")]
        best, score = None, 0.0
        for r in rows:
            s = SequenceMatcher(None, q, smartsearch.norm(r["question"])).ratio()
            keys = [smartsearch.norm(k) for k in r["keywords"].split(",") if k.strip()]
            if keys and all(k in q for k in keys):
                s = max(s, 0.9)
            if s > score:
                best, score = r, s
        if best is None or score < 0.8:
            return None
        with self.bank._connect() as conn:
            conn.execute("UPDATE bot_answers SET hits = hits + 1 WHERE id = ?", (best["id"],))
        return reply(best["answer"], [("More", best["link"])] if best["link"] else [], kind="trained")

    def _faq(self, text, faq_docs):
        if not faq_docs:
            return None
        vocab = smartsearch.build_vocab(faq_docs)
        q = smartsearch.Query(text, vocab)
        if not q.words:
            return None
        scored = sorted(((smartsearch.score_doc(q, d), i, d) for i, d in enumerate(faq_docs)), key=lambda x: (-x[0], x[1]))
        if not scored or scored[0][0] < 4:
            return None
        d = scored[0][2]
        return reply(d.get("answer") or d.get("text", ""), [(d["title"], d["url"])], kind="faq")

    def _follow_up(self, who, text, t):
        """'and last month?' after 'how much did I spend on food this month?' -> a full question."""
        rest = re.sub(FOLLOW, "", t).strip(" ?.!,")
        if not rest or len(rest.split()) > 5:
            return None
        if not re.search(FOLLOW, t) and len(rest.split()) > 3:
            return None
        last = self._last_question(who)
        if not last:
            return None
        period = parse_period(rest)[2]
        q = smartsearch.Query(rest, [])
        if not (period or q.concepts):
            return None
        new = last
        if period:
            new = PERIOD_RE.sub(" ", new)
        if q.concepts:
            concept_words = {smartsearch.norm(w) for c in smartsearch.SYNONYMS for w in smartsearch.SYNONYMS[c]
                             if " " not in w}
            new = " ".join(w for w in new.split() if smartsearch.norm(w) not in concept_words and
                           w.lower() not in ("on", "for"))
            rest = ("on " + rest) if not period else rest
        new = " ".join(f"{new.strip(' ?.!')} {rest}".split())
        return new if new.lower() != last.lower() else None

    # ===== actions: customers =====

    def _confirm(self, title, lines, action, params, pin=False, fields=(), button="Confirm"):
        return reply(title, items=lines, kind="action",
                     confirm={"action": action, "params": params, "pin": pin, "fields": list(fields), "button": button})

    def _customer_action(self, text, t, c):
        bank, cid = self.bank, c.customer_id
        amount = parse_amount_words(re.sub(r"\b\d{9,}\b", "", text))      # an account number is not an amount
        # ---- send money
        m = re.search(r"\b(send|transfer|pay|переведи|перевести|отправь|envoie|envía|envia)\b\s*(?:rs\.?|₹|inr)?\s*[\d,.]+\s*"
                      r"(?:k|lakh|thousand|rupees?|руб\w*|inr)?\s*(?:to|для|на|à|a)\s+(.+)$", t)
        if m and amount and not re.search(r"bill|card bill|счёт за|facture", t):
            target = m.group(2).strip(" .?!")
            to, name = self._resolve_payee(cid, target)
            if to is None:
                return reply(f"I could not find “{target}” among your payees. Write the 12-digit PyBank account number, "
                             "or add the person in Payees first.", [("Payees", "/netbanking/payees")], kind="unknown")
            acc = bank.get_account(cid)
            minor = int(round(amount * 100))
            return self._confirm("Please check and confirm the transfer:",
                                 [f"To: {name} (A/c XX{to[-4:]})", f"Amount: {format_money(minor, acc.currency)}",
                                  f"From: your account XX{acc.number[-4:]}"],
                                 "transfer", {"to": to, "amount": minor}, pin=True, button="Send money")
        # ---- debit card
        if re.search(r"\b(block|freeze|lost|stolen|заблокир|bloquer|bloquear)\b.*\b(card|карт|carte|tarjeta)", t) or \
                re.search(r"\b(card|карт)\w*\b.*\b(block|lost|stolen|заблокир)", t):
            if re.search(r"credit", t):
                return self._confirm("Block your credit card? You can unblock it later.", [], "cc_block", {})
            card = bank.card(cid)
            if card is None:
                return reply("Your account has no debit card.", kind="info")
            if card["status"] == "Blocked":
                return reply(f"Your debit card XX{card['last4']} is already blocked.", [("My card", "/netbanking/card")])
            return self._confirm(f"Block your debit card XX{card['last4']} now? Payments and cash will stop until you "
                                 "unblock it.", [], "card_block", {}, button="Block card")
        if re.search(r"\b(unblock|activate|разблокир|débloquer|desbloquear)\b.*\b(card|карт|carte|tarjeta)", t):
            return self._confirm("Unblock your debit card? Please confirm with your PIN.", [], "card_unblock", {}, pin=True,
                                 button="Unblock card")
        # ---- fixed deposit
        if re.search(r"\b(open|book|start|create|открой|открыть)\b.*\b(fd|fixed deposit|вклад)\b", t) and amount:
            mm = re.search(r"(\d{1,2})\s*(months?|мес)", t)
            yy = re.search(r"(\d)\s*(years?|год|лет)", t)
            months = int(mm.group(1)) if mm else int(yy.group(1)) * 12 if yy else 12
            if months not in bank.fd_rates():
                return reply("Fixed deposits are for 6, 12, 24 or 36 months. Which one?", kind="unknown",
                             suggestions=[f"Open FD of {amount:,.0f} for {m} months" for m in (6, 12, 24, 36)])
            rate = bank.fd_rates()[months]
            minor = int(round(amount * 100))
            return self._confirm("Open this fixed deposit?", [f"Amount: {format_money(minor, 'INR')}",
                                                               f"Period: {months} months at {rate:.2f}% a year",
                                                               "Paid from your main account"],
                                 "fd", {"amount": minor, "months": months}, pin=True, button="Open FD")
        # ---- savings goal
        m = re.search(r"\b(add|put|save|move|отложи|добавь)\b.*?\b(?:to|into|for|в|на)\s+(?:my\s+)?(.+?)(?:\s+goal)?\s*$", t)
        if m and amount and re.search(r"goal|цел|laptop|trip|save", t):
            goals = bank.goals(cid)
            want = smartsearch.norm(m.group(2).replace("goal", ""))
            best = max(goals, key=lambda g: SequenceMatcher(None, want, smartsearch.norm(g["name"])).ratio(), default=None)
            if best is None or SequenceMatcher(None, want, smartsearch.norm(best["name"])).ratio() < 0.5:
                return reply("I could not find that goal. Your goals: " + (", ".join(g["name"] for g in goals) or "none yet")
                             + ".", [("RD & Goals", "/netbanking/savings")], kind="unknown")
            minor = int(round(amount * 100))
            return self._confirm(f"Put {format_money(minor, best['currency'])} into your goal “{best['name']}”?",
                                 [f"Saved now: {format_money(best['saved'], best['currency'])} of "
                                  f"{format_money(best['target'], best['currency'])}"],
                                 "goal_in", {"goal": best["id"], "amount": minor}, pin=True, button="Add to goal")
        # ---- credit card bill
        if re.search(r"\bpay\b.*\bcredit card\b|credit card bill|оплати\w* кредитн", t):
            k = bank.credit_card(cid)
            if k is None or k["status"] not in ("Active", "Blocked"):
                return reply("You have no credit card. You can apply in Net Banking > Credit card.",
                             [("Credit card", "/netbanking/credit-card")])
            if not k["outstanding"]:
                return reply("Your credit card has nothing to pay. 🎉", [("Credit card", "/netbanking/credit-card")])
            how = "minimum" if re.search(r"minimum|мин", t) else "full"
            return self._confirm(f"Pay the {'minimum of the' if how == 'minimum' else 'whole'} credit card bill?",
                                 [f"You owe {format_money(k['outstanding'], 'INR')}"], "cc_pay", {"how": how}, pin=True,
                                 button="Pay bill")
        # ---- service requests
        for pattern, rtype in ((r"cheque ?book|chequebook|чековую книжку", "Cheque book"),
                               (r"new (debit )?card|replace(ment)? card", "New debit card"),
                               (r"address proof", "Address proof letter"),
                               (r"statement by post", "Account statement by post")):
            if re.search(pattern, t) and re.search(r"need|want|order|request|give|send|get|нужн|хочу|закаж", t):
                return self._confirm(f"Send the request “{rtype}” to the bank?", ["A bank officer will answer in Service requests."],
                                     "request", {"type": rtype}, button="Send request")
        # ---- statement
        if re.search(r"\bstatement|выписк|relevé|extracto", t):
            start, end, period = parse_period(t)
            if start:
                url = f"/netbanking/statement?from={start.isoformat()}&to={end.isoformat()}"
                return reply(f"Here is your statement for {period}:", [("Open the statement", url),
                                                                       ("Download CSV", url.replace("statement?", "statement.csv?"))])
        return None

    def _resolve_payee(self, cid, target):
        digits = re.sub(r"\D", "", target)
        if len(digits) == 12:
            try:
                info = self.bank.name_enquiry_internal(digits, full=False)
                return digits, info["name"]
            except BankError:
                return None, None
        want = smartsearch.norm(target)
        best, score = None, 0.0
        for b in self.bank.beneficiaries(cid):
            for name in (b["nickname"], b.get("full_name") or ""):
                if not name:
                    continue
                n = smartsearch.norm(name)
                s = max(SequenceMatcher(None, want, n).ratio(), SequenceMatcher(None, want, n.split()[0]).ratio())
                if s > score:
                    best, score = b, s
        if best is None or score < 0.75:
            return None, None
        return best["account_number"], best.get("full_name") or best["nickname"]

    # ===== actions: staff =====

    def _staff_action(self, text, t, me, can):
        bank = self.bank
        m = re.search(r"\bopen (?:my )?(?:cash )?(?:drawer|till)\b(?: with)?\s*([\d,.]+\s*(?:k|lakh)?)?\s*(inr|usd|eur|gbp|rub|cny|aed|rwf)?", t)
        if m:
            if not can("till.own"):
                return reply("Your abilities do not include a cash drawer.", kind="denied")
            cur = (m.group(2) or "INR").upper()
            amt = parse_amount_words(m.group(1)) if m.group(1) else None
            if amt is None:
                return reply("With how much cash from the vault? For example: “open my drawer with 200000”.", kind="unknown")
            minor = int(round(amt * 100))
            return self._confirm(f"Open your {cur} cash drawer with {format_money(minor, cur)} from the vault?", [],
                                 "till_open", {"currency": cur, "amount": minor}, button="Open drawer")
        m = re.search(r"\b(freeze|block|activate|unfreeze|заморо\w*|разморо\w*)\b\s+(?:customer\s+)?(pb\d{8})", t)
        if m:
            if not can("customers.status"):
                return reply("Your abilities do not include freezing or activating accounts.", kind="denied")
            cid = m.group(2).upper()
            try:
                c = bank.get_customer(cid)
            except BankError:
                return reply("No customer has this ID.", kind="unknown")
            freeze = m.group(1).startswith(("freeze", "block", "замор"))
            return self._confirm(f"{'Freeze' if freeze else 'Activate'} all accounts of {c.full_name} ({cid})?", [],
                                 "freeze" if freeze else "activate", {"cid": cid},
                                 fields=[{"name": "reason", "label": "Reason (for the audit log)"}],
                                 button="Freeze" if freeze else "Activate")
        m = re.search(r"\b(?:customer|show|about|клиент)\s+(pb\d{8})\b", t)
        if m:
            if not can("customers.view"):
                return reply("Your abilities do not include customers.", kind="denied")
            cid = m.group(1).upper()
            try:
                c = bank.get_customer(cid)
            except BankError:
                return reply("No customer has this ID.", kind="unknown")
            items = [f"{c.full_name} · born {c.dob} · {c.mobile} · {c.status}",
                     f"Risk: {getattr(c, 'risk_rating', 'Low')}"]
            items += [f"{a.type} XX{a.number[-4:]} ({a.currency}): {format_money(a.balance, a.currency)} · {a.status}"
                      for a in bank.accounts_of(cid)]
            links = [("Customer page", f"/admin/customers/{cid}")]
            if can("counter.deposit") or can("counter.transfer"):
                links.append(("Serve at the counter", f"/admin/counter/{cid}"))
            return reply(f"Customer {cid}:", links, items)
        return None

    # ===== doing the confirmed action =====

    def confirm(self, who, msg_id, form, customer=None, staff=None, can=None, _inner=False):
        """The person pressed Confirm on a question of the bot (form: pin, reason ...)."""
        with self.bank._connect() as conn:
            r = conn.execute("SELECT who, data FROM chat_messages WHERE id = ? AND role = 'bot'", (msg_id,)).fetchone()
        if r is None or r[0] != who:
            raise BankError("This question is not yours or no longer exists.")
        data = json.loads(r[1] or "{}")
        c = data.get("confirm")
        if not c or c.get("done") or c.get("cancelled"):
            raise BankError("This was already done or cancelled.")
        p, action = c["params"], c["action"]
        pin = (form.get("pin") or "").strip()
        bank = self.bank
        try:
            if customer is not None:
                cid = customer.customer_id
                if action == "transfer":
                    t = bank.transfer(cid, p["to"], int(p["amount"]), pin, "Chat transfer")
                    text = f"Done! {format_money(int(p['amount']), bank.get_account(cid).currency)} was sent. Reference {t.ref}."
                    links = [("Statement", "/netbanking/statement")]
                elif action == "card_block":
                    bank.block_card(cid, True)
                    text, links = "Your debit card is blocked.", [("My card", "/netbanking/card")]
                elif action == "card_unblock":
                    bank._check_pin(cid, pin)
                    bank.block_card(cid, False)
                    text, links = "Your debit card works again.", [("My card", "/netbanking/card")]
                elif action == "cc_block":
                    bank.block_credit_card(cid, True)
                    text, links = "Your credit card is blocked.", [("Credit card", "/netbanking/credit-card")]
                elif action == "fd":
                    fd = bank.open_fd(cid, int(p["amount"]), int(p["months"]), pin)
                    text, links = f"Your fixed deposit {fd} is open.", [("Fixed deposits", "/netbanking/fixed-deposits")]
                elif action == "goal_in":
                    bank.move_goal(cid, int(p["goal"]), int(p["amount"]), True, pin)
                    text, links = "Done - the money is in your goal.", [("RD & Goals", "/netbanking/savings")]
                elif action == "cc_pay":
                    t = bank.pay_credit_card(cid, p["how"], 0, pin)
                    text, links = f"Paid {format_money(t.amount, 'INR')} to your credit card.", [("Credit card", "/netbanking/credit-card")]
                elif action == "request":
                    rid = bank.create_request(cid, p["type"], f"{p['type']} - asked in the Ask PyBank chat.")
                    text, links = f"Your request #{rid} was sent to the bank.", [("Service requests", "/netbanking/requests")]
                else:
                    raise BankError("Unknown action.")
            elif staff is not None:
                if action == "till_open":
                    if not can("till.own"):
                        raise BankError("Your abilities do not include a cash drawer.")
                    self.office.open_till(staff, p["currency"], int(p["amount"]))
                    text, links = "Your cash drawer is open.", [("My cash drawer", "/admin/till")]
                elif action in ("freeze", "activate"):
                    if not can("customers.status"):
                        raise BankError("Your abilities do not include freezing or activating accounts.")
                    reason = " ".join((form.get("reason") or "").split())
                    if len(reason) < 3:
                        raise BankError("Please write the reason.")
                    self.office.set_account_status(staff, p["cid"], "Frozen" if action == "freeze" else "Active",
                                                   reason + " (chat)")
                    text = f"The accounts of {p['cid']} are {'frozen' if action == 'freeze' else 'active again'}."
                    links = [("Customer page", f"/admin/customers/{p['cid']}")]
                else:
                    raise BankError("Unknown action.")
            else:
                raise BankError("Please sign in first.")
        except BankError as e:
            if not _inner:
                self._save(who, "bot", f"It did not work: {e}", {}, "error")
            raise
        self._mark(msg_id, done=True)
        out = reply(text, links, kind="done")
        if not _inner:
            out["id"] = self._save(who, "bot", text, {"links": out["links"]}, "done")
        return out

    # ===== training (back office) =====

    def trained_answers(self):
        with self.bank._connect() as conn:
            return [dict(r) for r in conn.execute("SELECT * FROM bot_answers ORDER BY active DESC, hits DESC, id DESC")]

    def save_answer(self, by, form, answer_id=None):
        question = " ".join((form.get("question") or "").split())[:200]
        text = (form.get("answer") or "").strip()[:1500]
        if len(question) < 3 or len(text) < 3:
            raise BankError("Please write the question and the answer.")
        link = (form.get("link") or "").strip()[:200]
        if link and not link.startswith("/"):
            raise BankError("The link must be an address on this site, starting with /.")
        keywords = ",".join(k.strip().lower() for k in (form.get("keywords") or "").split(",") if k.strip())[:200]
        active = 0 if form.get("active") == "0" else 1
        with self.bank._connect() as conn:
            if answer_id:
                conn.execute("UPDATE bot_answers SET question = ?, keywords = ?, answer = ?, link = ?, active = ? WHERE id = ?",
                             (question, keywords, text, link, active, answer_id))
            else:
                answer_id = conn.execute("INSERT INTO bot_answers (question, keywords, answer, link, active, created_by, "
                                         "created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                                         (question, keywords, text, link, active, by["staff_id"], now())).lastrowid
            conn.execute("INSERT INTO audit_log (staff_id, action, target, details, at) VALUES (?, ?, ?, ?, ?)",
                         (by["staff_id"], "Assistant answer saved", str(answer_id), question, now()))
        return answer_id

    def delete_answer(self, by, answer_id):
        with self.bank._connect() as conn:
            conn.execute("DELETE FROM bot_answers WHERE id = ?", (answer_id,))
            conn.execute("INSERT INTO audit_log (staff_id, action, target, details, at) VALUES (?, ?, ?, ?, ?)",
                         (by["staff_id"], "Assistant answer deleted", str(answer_id), "", now()))

    def unanswered(self, limit=50):
        """Questions the bot did not understand or that got a 👎, newest first."""
        with self.bank._connect() as conn:
            bots = [dict(r) for r in conn.execute(
                "SELECT id, who, text, kind, feedback, created_at FROM chat_messages WHERE role = 'bot' AND "
                "(kind = 'unknown' OR feedback < 0) ORDER BY id DESC LIMIT ?", (limit,))]
            for b in bots:
                q = conn.execute("SELECT text FROM chat_messages WHERE who = ? AND role = 'user' AND id < ? "
                                 "ORDER BY id DESC LIMIT 1", (b["who"], b["id"])).fetchone()
                b["question"] = q[0] if q else ""
                b["who_kind"] = b["who"].split(":")[0]
        return bots

    def stats(self):
        today = date.today().isoformat()
        with self.bank._connect() as conn:
            q = lambda sql, *a: conn.execute(sql, a).fetchone()[0]          # noqa: E731
            total = q("SELECT COUNT(*) FROM chat_messages WHERE role = 'user'")
            return {"questions": total,
                    "today": q("SELECT COUNT(*) FROM chat_messages WHERE role = 'user' AND created_at >= ?", today),
                    "unknown": q("SELECT COUNT(*) FROM chat_messages WHERE role = 'bot' AND kind = 'unknown'"),
                    "actions": q("SELECT COUNT(*) FROM chat_messages WHERE role = 'bot' AND kind = 'done'"),
                    "up": q("SELECT COUNT(*) FROM chat_messages WHERE feedback > 0"),
                    "down": q("SELECT COUNT(*) FROM chat_messages WHERE feedback < 0"),
                    "people": q("SELECT COUNT(DISTINCT who) FROM chat_messages")}


def who_can(staff):
    return lambda ability: roles.can(staff, ability)

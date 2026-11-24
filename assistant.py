"""
assistant.py - "Ask PyBank", a chat assistant for customers, visitors and staff (version 5).

The assistant first tries to understand the question itself (it works without internet):

    balance            "what is my balance", "how much money do I have", "мой баланс"
    spending / income  "how much did I spend on food last month", "salary this year"
    find payments      "show transfers to Priya above 5000 in October", "last 5 transactions"
    rates              "FD rate", "loan interest", "savings rate", "RD rate"
    exchange           "100 USD in INR", "euro rate"
    EMI                "EMI for 5 lakh for 3 years", "emi 200000 24 months at 9%"
    how to ...         everything else goes to the smart search (typos and synonyms included) and
                       the best help topics are given as links
  staff only:          "pending loans", "cash approvals", "frozen accounts", "customers from Rwanda",
                       "large transactions today", "who is Rahul" - limited to the worker's abilities

When the bank switches it on (setting "assistant_llm" = 1 and the secret ANTHROPIC_API_KEY), a
question the rules do not understand is sent to a large language model - only the question
itself, never account data - and the answer is marked as general information.
"""

import calendar
import json
import os
import re
import urllib.request
from datetime import date, timedelta

import smartsearch
from core import BankError
from world import format_money

MONTHS = {m.lower(): i for i, m in enumerate(calendar.month_name) if m}
MONTHS.update({m.lower(): i for i, m in enumerate(calendar.month_abbr) if m})
MONTHS.update({"январ": 1, "феврал": 2, "март": 3, "апрел": 4, "ма": 5, "июн": 6, "июл": 7, "август": 8,
               "сентябр": 9, "октябр": 10, "ноябр": 11, "декабр": 12})
CATEGORY_WORDS = {"food": "Food & dining", "shopping": "Shopping", "bill": "Bills & utilities", "salary": "Salary",
                  "transfer": "Transfer", "withdrawal": "Withdrawal", "deposit": "Deposit", "loan": "Loan",
                  "interest": "Interest"}
MULTIPLIERS = {"k": 1_000, "thousand": 1_000, "lakh": 100_000, "lakhs": 100_000, "lac": 100_000, "crore": 10_000_000,
               "m": 1_000_000, "million": 1_000_000, "тыс": 1_000, "млн": 1_000_000}


def parse_amount_words(text):
    """'5 lakh' -> 500000.0, '2.5k' -> 2500.0, '10,000' -> 10000.0 (None if no amount)."""
    m = re.search(r"(\d[\d,]*(?:\.\d+)?)\s*(k|thousand|lakhs?|lac|crore|m\b|million|тыс|млн)?", text.lower())
    if not m:
        return None
    value = float(m.group(1).replace(",", ""))
    return value * MULTIPLIERS.get(m.group(2) or "", 1)


def parse_period(text, today=None):
    """(start, end, words) of a time phrase; (None, None, '') if there is none."""
    t, today = text.lower(), today or date.today()
    first = today.replace(day=1)
    if re.search(r"\btoday\b|сегодня", t):
        return today, today, "today"
    if re.search(r"\byesterday\b|вчера", t):
        y = today - timedelta(days=1)
        return y, y, "yesterday"
    m = re.search(r"last (\d{1,3}) days|за (\d{1,3}) дн", t)
    if m:
        n = int(m.group(1) or m.group(2))
        return today - timedelta(days=n - 1), today, f"the last {n} days"
    if re.search(r"this week|эта неделя|на этой неделе", t):
        return today - timedelta(days=today.weekday()), today, "this week"
    if re.search(r"last month|previous month|прошл\w* месяц", t):
        end = first - timedelta(days=1)
        return end.replace(day=1), end, end.strftime("%B %Y")
    if re.search(r"this month|этот месяц|в этом месяце", t):
        return first, today, "this month"
    if re.search(r"last year|прошл\w* год", t):
        return date(today.year - 1, 1, 1), date(today.year - 1, 12, 31), str(today.year - 1)
    if re.search(r"this year|этот год|в этом году", t):
        return date(today.year, 1, 1), today, str(today.year)
    for name, num in sorted(MONTHS.items(), key=lambda x: -len(x[0])):
        if len(name) >= 3 and re.search(r"\b" + re.escape(name), t):
            year = today.year if num <= today.month else today.year - 1
            last = calendar.monthrange(year, num)[1]
            return date(year, num, 1), date(year, num, last), date(year, num, 1).strftime("%B %Y")
    return None, None, ""


def answer(text="", links=(), items=(), kind="info", llm=False):
    links = [{"title": l[0], "url": l[1]} if isinstance(l, (tuple, list)) else l for l in links]
    return {"text": text, "links": links, "items": list(items), "kind": kind, "llm": llm}


class Assistant:
    def __init__(self, bank):
        self.bank = bank

    # ===== shared =====

    def _rates(self, t):
        s = self.bank.settings()
        if re.search(r"\bfd\b|fixed|вклад|dépôt à terme|plazo", t):
            rates = self.bank.fd_rates()
            return answer("Fixed deposit rates: " + ", ".join(f"{m} months {r:.2f}%" for m, r in sorted(rates.items()))
                          + f". Customers aged 60 or more get {float(s.get('fd_senior_extra', '0.5')):.2f}% more.",
                          [("Interest rates", "/rates")])
        if re.search(r"\brd\b|recurring", t):
            return answer("Recurring deposits earn the fixed-deposit rate of the same period, compounded every quarter.",
                          [("Interest rates", "/rates")])
        if re.search(r"loan|emi|кредит|prêt|préstamo", t):
            return answer("Loan interest: " + ", ".join(f"{p} {s.get('loan_rate_' + p, '?')}%"
                                                       for p in ("Personal", "Vehicle", "Education")) + " a year.",
                          [("Interest rates", "/rates")])
        return answer(f"Savings accounts earn {s.get('savings_rate', '3.50')}% a year "
                      f"({s.get('savings_rate_high', '4.00')}% on the part above the slab), paid every quarter.",
                      [("Interest rates", "/rates")])

    def _fx(self, text):
        codes = re.findall(r"\b(INR|RUB|USD|EUR|GBP|CNY|AED|RWF)\b", text.upper())
        names = {"dollar": "USD", "euro": "EUR", "rupee": "INR", "ruble": "RUB", "rouble": "RUB", "pound": "GBP",
                 "yuan": "CNY", "dirham": "AED", "franc": "RWF", "доллар": "USD", "евро": "EUR", "рубл": "RUB"}
        for word, code in names.items():
            if word in text.lower() and code not in codes:
                codes.append(code)
        if not codes:
            return None
        src = codes[0]
        dst = codes[1] if len(codes) > 1 else ("INR" if src != "INR" else "USD")
        amount = parse_amount_words(text) or 1
        try:
            q = self.bank.quote(src, dst, int(round(amount * 100)))
        except BankError as e:
            return answer(str(e), [("Exchange rates", "/rates")])
        return answer(f"{format_money(int(round(amount * 100)), src)} = {format_money(q['amount'], dst)} at the bank's "
                      f"rate 1 {src} = {q['rate']:.4f} {dst} (the market rate is {q['mid']:.4f}; the bank keeps "
                      f"{q['margin']:.2f}%).", [("Exchange rates", "/rates")])

    def _emi(self, text):
        amount = parse_amount_words(text)
        months = re.search(r"(\d{1,3})\s*(months?|mo\b|мес)", text.lower())
        years = re.search(r"(\d{1,2})\s*(years?|yrs?|год|лет)", text.lower())
        n = int(months.group(1)) if months else int(years.group(1)) * 12 if years else 0
        rate_m = re.search(r"(\d{1,2}(?:\.\d+)?)\s*%", text)
        if not amount or not n:
            return answer("Tell me the amount and the period, for example: “EMI for 5 lakh for 3 years”.",
                          [("Loans", "/netbanking/loans")])
        rate = float(rate_m.group(1)) if rate_m else float(self.bank.settings().get("loan_rate_Personal", "10.75"))
        principal = int(round(amount * 100))
        emi = self.bank.emi(principal, rate, n)
        return answer(f"EMI for {format_money(principal, 'INR')} over {n} months at {rate:.2f}%: "
                      f"{format_money(emi, 'INR')} a month; you pay {format_money(emi * n, 'INR')} in total "
                      f"({format_money(emi * n - principal, 'INR')} interest).", [("Apply for a loan", "/netbanking/loans")])

    def _general(self, t, text, docs):
        if re.search(r"\b(emi|instal?ment)\b", t) and re.search(r"\d", t):
            return self._emi(text)
        if re.search(r"exchange|convert|rate of|курс|обмен|\b(usd|eur|gbp|inr|rub|cny|aed|rwf)\b|dollar|euro", t):
            fx = self._fx(text)
            if fx:
                return fx
        if re.search(r"rate|interest|процент|ставк|taux|interés", t):
            return self._rates(t)
        return None

    def _from_search(self, text, docs, intro="This may help:"):
        found = smartsearch.search(text, docs, limit=4)
        links = [(d["title"], d["url"]) for d in found["results"] if d.get("url")]
        extra = f" (I read your question as “{found['did_you_mean']}”.)" if found["did_you_mean"] else ""
        if links:
            return answer(intro + extra, links)
        llm = self._ask_llm(text)
        if llm:
            return llm
        return answer("Sorry, I did not understand. Try other words, for example “block my card”, “FD rate”, "
                      "“how much did I spend last month” or “100 USD in INR”.", kind="unknown")

    def _ask_llm(self, text):
        key = os.environ.get("ANTHROPIC_API_KEY", "")
        if not key or self.bank.settings().get("assistant_llm") != "1":
            return None
        body = {"model": os.environ.get("ASSISTANT_MODEL", "claude-haiku-4-5"), "max_tokens": 300,
                "system": "You answer general questions about banking for customers of PyBank, a fictional demo bank. "
                          "Answer in 2-4 short, simple sentences in the language of the question. Never ask for "
                          "passwords, PINs or card numbers. If the question needs account data, say that the customer "
                          "should use Net Banking or call the branch.",
                "messages": [{"role": "user", "content": text[:500]}]}
        req = urllib.request.Request("https://api.anthropic.com/v1/messages", data=json.dumps(body).encode(),
                                     headers={"content-type": "application/json", "x-api-key": key,
                                              "anthropic-version": "2023-06-01"})
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                data = json.loads(r.read())
            reply = " ".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text").strip()
        except Exception:
            return None
        return answer(reply, kind="info", llm=True) if reply else None

    # ===== visitors =====

    def public(self, text, docs):
        t = text.lower()
        if not t.strip():
            return answer("Hello! Ask me about accounts, cards, loans, rates or branches.")
        return self._general(t, text, docs) or self._from_search(text, docs)

    # ===== customers =====

    def customer(self, cid, text, docs, account=None):
        t = " ".join(text.lower().split())
        if not t:
            return answer("Hello! I can tell you your balance, your spending, find payments, give rates, work out an "
                          "EMI or show you where to do something.")
        bank = self.bank
        accounts = bank.accounts_of(cid)
        if re.search(r"balance|how much (money )?(do i|have i)|баланс|сколько (у меня )?денег|solde|saldo|رصيد|余额", t) \
                and not re.search(r"spen|paid|трат|потрат", t):
            items = [f"{a.type} XX{a.number[-4:]} ({a.currency}): {format_money(a.balance, a.currency)}"
                     + (f", available {format_money(bank.available_balance(a.number), a.currency)}"
                        if bank.available_balance(a.number) != a.balance else "") for a in accounts]
            return answer("Your balance:", [("My accounts", "/netbanking/accounts")], items)
        general = self._general(t, text, docs)
        if general:
            return general
        acc = account or accounts[0]
        start, end, period = parse_period(t)
        q = smartsearch.Query(text, [])
        concept = next((c for c in q.concepts if c in CATEGORY_WORDS), None)
        category = CATEGORY_WORDS.get(concept)
        hints = [smartsearch.norm(w) for w in smartsearch.SYNONYMS.get(concept, [])] if concept else []
        if re.search(r"spen[dt]|expens|paid out|потратил|расход|dépens|gast", t):
            start, end = start or date.today().replace(day=1), end or date.today()
            rows = bank.transactions(acc.number, start.isoformat(), end.isoformat(), "DR")
            if category and category not in ("Transfer", "Withdrawal", "Loan"):
                rows = [r for r in rows if any(h and h in smartsearch.norm(r.category + " " + r.description) for h in hints)]
            elif category:
                rows = [r for r in rows if r.category == category]
            total = sum(r.amount for r in rows)
            what = f" on {category.lower()}" if category else ""
            return answer(f"You spent {format_money(total, acc.currency)}{what} {('in ' + period) if period and not period.startswith(('this', 'the', 'today', 'yesterday')) else (period or 'this month')} "
                          f"({len(rows)} payments, account XX{acc.number[-4:]}).",
                          [("Insights", "/netbanking/insights"), ("Statement", "/netbanking/statement")])
        if re.search(r"income|received|earned|salary|got paid|получил|доход|зарплат", t):
            start, end = start or date.today().replace(day=1), end or date.today()
            rows = bank.transactions(acc.number, start.isoformat(), end.isoformat(), "CR")
            if category == "Salary":
                rows = [r for r in rows if "SALARY" in r.description.upper() or r.category == "Salary"]
            total = sum(r.amount for r in rows)
            return answer(f"Money in {period or 'this month'}: {format_money(total, acc.currency)} ({len(rows)} credits).",
                          [("Statement", "/netbanking/statement")])
        if re.search(r"show|find|list|last|recent|search|transactions?|payments?|transfers?|покажи|найди|последн", t) \
                and not re.search(r"how (do|can|to)|как ", t):
            rows = bank.transactions(acc.number, start.isoformat() if start else None, end.isoformat() if end else None)
            m = re.search(r"\b(?:to|from|для|от)\s+([a-zа-я]{3,})", t)
            if m:
                name = m.group(1)
                close = [r for r in rows if name in smartsearch.norm(r.description)]
                if not close:
                    from difflib import SequenceMatcher
                    close = [r for r in rows if any(SequenceMatcher(None, name, w).ratio() >= 0.8
                                                    for w in smartsearch.norm(r.description).split())]
                rows = close
            above = re.search(r"(?:above|over|more than|bigger than|больше|свыше)\s*([\d,.]+\s*(?:k|lakh)?)", t)
            below = re.search(r"(?:below|under|less than|меньше)\s*([\d,.]+\s*(?:k|lakh)?)", t)
            if above:
                rows = [r for r in rows if r.amount >= parse_amount_words(above.group(1)) * 100]
            if below:
                rows = [r for r in rows if r.amount <= parse_amount_words(below.group(1)) * 100]
            if category:
                rows = [r for r in rows if r.category == category or category.split()[0].lower() in r.description.lower()]
            n = re.search(r"(?:last|recent|последни\w*)\s+(\d{1,2})", t)
            rows = rows[:int(n.group(1)) if n else 10]
            items = [f"{r.created_at[:10]} · {'+' if r.type == 'CR' else '−'}{format_money(r.amount, acc.currency)} · "
                     f"{r.description[:60]} · {r.ref}" for r in rows]
            return answer(f"I found {len(items)} transaction(s)" + (f" in {period}" if period else "") + ":" if items
                          else "I found no transactions like that.", [("Statement", "/netbanking/statement")], items)
        return self._from_search(text, docs, "Here is where you can do that:")

    # ===== bank staff =====

    def staff(self, office, me, text, docs, can):
        t = " ".join(text.lower().split())
        if not t:
            return answer("Ask me for work lists (pending loans, cash approvals, frozen accounts, large transactions "
                          "today), for customers (customers from Rwanda, who is Rahul) or for a back-office page.")
        o = office.overview()
        checks = [(r"pending loan|loan application", "loans.decide", "pending_loans", "Loan applications waiting",
                   "/admin/loans"),
                  (r"cash (approval|request)|deposit request|withdrawal request", "cash.approve", "pending_cash",
                   "Cash requests waiting", "/admin/approvals"),
                  (r"new account|application|kyc", "applications", "pending_customers", "New accounts waiting",
                   "/admin/applications"),
                  (r"second approval|four.eyes|checker", "checker", "checker_waiting", "Entries waiting for a second person",
                   "/admin/checker"),
                  (r"suspicious|aml|flag", "compliance", "aml_open", "Open suspicious-activity flags", "/admin/compliance"),
                  (r"service request|complaint", "requests.answer", "open_requests", "Open service requests",
                   "/admin/requests")]
        if re.search(r"(approvals?|work) (waiting|pending|to do)|what is waiting|my work|to-?do|ожида|en attente|pendiente", t):
            items = [(label, url, o.get(key, 0)) for _p, ability, key, label, url in checks if can(ability)]
            if can("till.supervise"):
                items.append(("Cash drawers with a difference", "/admin/tills", o.get("till_differences", 0)))
            if can("screening"):
                items.append(("Open sanctions matches", "/admin/screening", o.get("screening_open", 0)))
            if not items:
                return answer("Your abilities do not include any approval list.", kind="denied")
            total = sum(n for _l, _u, n in items)
            return answer(f"Waiting for you and your colleagues: {total}.",
                          [(label, url) for label, url, n in items if n][:4],
                          [f"{label}: {n}" for label, url, n in items])
        if re.search(r"cash drawer|my drawer|\btill\b|касс|caisse|caja", t):
            if not (can("till.own") or can("till.supervise")):
                return answer("Your abilities do not include cash drawers.", kind="denied")
            tills = office.my_tills(me)
            if not tills:
                return answer("Your cash drawer is not open today. Open it in Counter > My cash drawer with the cash "
                              "you received from the vault.", [("My cash drawer", "/admin/till")])
            return answer("Your cash drawer today:", [("My cash drawer", "/admin/till")],
                          [f"{x['currency']}: {format_money(x['expected'], x['currency'])} in the drawer "
                           f"(opened with {format_money(x['opening'], x['currency'])}, in {format_money(x['cash_in'], x['currency'])}, "
                           f"out {format_money(x['cash_out'], x['currency'])}) - {x['status']}" for x in tills])
        if re.search(r"screening|sanction|санкц", t):
            if not can("screening"):
                return answer("Your abilities do not include this list.", kind="denied")
            return answer(f"Open sanctions matches: {o.get('screening_open', 0)}.", [("Screening & risk", "/admin/screening")])
        for pattern, ability, key, label, url in checks:
            if re.search(pattern, t):
                if not can(ability):
                    return answer("Your abilities do not include this list.", kind="denied")
                return answer(f"{label}: {o.get(key, 0)}.", [(label, url)])
        if re.search(r"frozen|dormant", t) and can("customers.view"):
            status = "Dormant" if "dormant" in t else "Frozen"
            with office._connect() as conn:
                rows = conn.execute("SELECT c.customer_id, c.full_name, a.number FROM accounts a JOIN customers c "
                                    "USING (customer_id) WHERE a.status = ? ORDER BY c.full_name LIMIT 20",
                                    (status,)).fetchall()
            return answer(f"{status} accounts: {len(rows)}.", [], [f"{r[0]} · {r[1]} · XX{r[2][-4:]}" for r in rows])
        m = re.search(r"customers? (?:from|in|of) ([a-zа-я ]{3,})", t)
        if m and can("customers.view"):
            import world
            want = m.group(1).strip()
            codes = [c for c, n in world.countries("en") if smartsearch.norm(n).startswith(smartsearch.norm(want)[:5])]
            with office._connect() as conn:
                rows = conn.execute("SELECT customer_id, full_name, country FROM customers WHERE country IN (" +
                                    ",".join("?" * len(codes)) + ") ORDER BY full_name LIMIT 30", codes).fetchall() if codes else []
            return answer(f"Customers from {want.title()}: {len(rows)}.", [],
                          [f"{r[0]} · {r[1]}" for r in rows])
        if re.search(r"large|big|above|over|more than", t) and re.search(r"transaction|payment|cash", t) and can("transactions.view"):
            limit = parse_amount_words(t) or 100_000
            day = date.today().isoformat()
            with office._connect() as conn:
                rows = conn.execute("SELECT t.ref, t.type, t.amount, t.description, a.currency FROM transactions t JOIN "
                                    "accounts a ON a.number = t.account_number WHERE t.amount >= ? AND t.created_at >= ? "
                                    "ORDER BY t.amount DESC LIMIT 20",
                                    (int(limit * 100), day if "today" in t else
                                     (date.today() - timedelta(days=30)).isoformat())).fetchall()
            return answer(f"Transactions of {limit:,.0f} or more ({'today' if 'today' in t else 'last 30 days'}): {len(rows)}.",
                          [("Transactions", "/admin/transactions")],
                          [f"{r[0]} · {r[1]} {format_money(r[2], r[4])} · {r[3][:50]}" for r in rows])
        m = re.search(r"(?:who is|find|customer|клиент)\s+([a-zа-я][a-zа-я .'-]{2,})", t)
        if m and can("customers.view"):
            found = smartsearch.search(m.group(1), [d for d in docs if d.get("kind") == "Customer"], limit=5)
            if found["results"]:
                return answer("Customers:" + (f" (read as “{found['did_you_mean']}”)" if found["did_you_mean"] else ""),
                              [(d["title"], d["url"]) for d in found["results"]])
        general = self._general(t, text, docs)
        return general or self._from_search(text, docs, "These pages may help:")

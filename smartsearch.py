"""
smartsearch.py - one search box that understands people (version 5).

    * typing mistakes are corrected  ("withdrawl" -> "withdrawal", "trasnfer" -> "transfer"),
      with "Did you mean ...?" when the corrected words find more;
    * words with the same meaning find the same things ("cash out" = "withdrawal", "FD" = "fixed deposit",
      "снять" = "withdrawal", "virement" = "transfer") - in the six languages of the bank;
    * suggestions appear while typing (/search/suggest, /netbanking/suggest, /admin/suggest);
    * amounts and references are found too ("5000", "PB2611...").

Three places use it, each with only what that person may see:
    public website  pages, questions (FAQ), products, branches, charges, help topics
    Net Banking     the customer's own transactions, payees, deposits, goals, bills + help topics
    back office     customers (names with typos, IDs, account numbers, email, phone), transactions by
                    reference, staff, pages of the back office - only those his abilities allow

No outside service is used: the correction compares the typed word with the words the bank
knows (difflib, similarity >= 0.75), and the synonyms are a list in this file.
"""

import re
import unicodedata
from difflib import get_close_matches

# words with the same meaning -> one concept (English, Russian, French, Spanish, Arabic, Chinese, Kinyarwanda)
SYNONYMS = {
    "deposit": ["deposit", "deposits", "pay in", "cash in", "put money", "add money", "top up", "credit", "lodge",
                "депозит", "внести", "пополнить", "пополнение", "взнос", "dépôt", "versement", "déposer", "depósito",
                "ingreso", "ingresar", "إيداع", "存款", "kubitsa"],
    "withdrawal": ["withdrawal", "withdraw", "withdrawals", "cash out", "take out", "take money", "cash", "atm",
                   "снять", "снятие", "наличные", "retrait", "retirer", "retiro", "retirar", "efectivo", "سحب", "取款",
                   "取现", "kubikuza"],
    "transfer": ["transfer", "transfers", "send", "send money", "pay", "payment", "move money", "remit", "imps", "neft",
                 "upi", "swift", "sepa", "перевод", "перевести", "отправить", "платеж", "платёж", "virement", "envoyer",
                 "paiement", "transferencia", "enviar", "pago", "تحويل", "دفع", "转账", "汇款", "付款", "kohereza"],
    "card": ["card", "debit card", "atm card", "visa", "карта", "карточка", "carte", "tarjeta", "بطاقة", "卡", "银行卡",
             "ikarita"],
    "block": ["block", "freeze", "stop", "lost", "stolen", "lock", "заблокировать", "блокировка", "потерял", "bloquer",
              "perdu", "bloquear", "perdida", "حظر", "إيقاف", "冻结", "挂失", "guhagarika"],
    "loan": ["loan", "loans", "emi", "borrow", "credit line", "кредит", "заём", "займ", "prêt", "emprunt", "préstamo",
             "crédito", "قرض", "贷款", "inguzanyo"],
    "fixed deposit": ["fixed deposit", "fd", "term deposit", "вклад", "срочный вклад", "dépôt à terme", "plazo fijo",
                      "وديعة", "定期存款", "定期"],
    "recurring deposit": ["recurring deposit", "rd", "monthly saving", "monthly savings", "накопительный вклад",
                          "épargne mensuelle", "ahorro mensual", "零存整取"],
    "goal": ["goal", "goals", "save for", "saving goal", "pot", "цель", "копилка", "objectif", "objetivo", "هدف",
             "目标", "intego"],
    "statement": ["statement", "history", "transactions", "transaction", "passbook", "выписка", "история", "операции",
                  "relevé", "historique", "extracto", "movimientos", "كشف", "سجل", "对账单", "流水", "交易"],
    "interest": ["interest", "rate", "rates", "проценты", "ставка", "taux", "intérêt", "interés", "tasa", "فائدة",
                 "利率", "利息", "inyungu"],
    "salary": ["salary", "wage", "wages", "payroll", "income", "зарплата", "доход", "salaire", "salario", "sueldo",
               "راتب", "工资", "umushahara"],
    "food": ["food", "restaurant", "dining", "grocery", "groceries", "swiggy", "zomato", "еда", "ресторан", "продукты",
             "nourriture", "comida", "طعام", "餐饮"],
    "shopping": ["shopping", "shop", "amazon", "flipkart", "purchase", "покупки", "achats", "compras", "تسوق", "购物"],
    "bill": ["bill", "bills", "electricity", "utility", "water", "gas", "mobile recharge", "счет", "счёт", "коммунальные",
             "facture", "factura", "فاتورة", "账单", "fagitire"],
    "branch": ["branch", "branches", "office", "address", "near me", "отделение", "филиал", "банкомат", "agence",
               "sucursal", "فرع", "网点", "ishami"],
    "password": ["password", "pin", "login", "sign in", "two-factor", "2fa", "otp", "пароль", "пин", "вход",
                 "mot de passe", "contraseña", "كلمة المرور", "密码", "ijambo ry'ibanga"],
    "exchange": ["exchange", "currency", "currencies", "fx", "convert", "forex", "dollar", "euro", "обмен", "валюта",
                 "change", "devise", "cambio", "divisa", "صرف", "عملة", "汇率", "换汇", "ivunjisha"],
    "payee": ["payee", "payees", "beneficiary", "beneficiaries", "получатель", "bénéficiaire", "beneficiario",
              "مستفيد", "收款人"],
    "customer": ["customer", "client", "account holder", "клиент", "client", "cliente", "عميل", "客户", "umukiriya"],
    "approve": ["approve", "approval", "approvals", "pending", "waiting", "одобрить", "ожидает", "approuver",
                "aprobar", "موافقة", "审批"],
    "complaint": ["complaint", "problem", "dispute", "wrong", "жалоба", "проблема", "réclamation", "reclamo", "queja",
                  "شكوى", "投诉"],
    "open account": ["open account", "new account", "register", "sign up", "открыть счет", "открыть счёт",
                     "ouvrir un compte", "abrir cuenta", "فتح حساب", "开户"],
    "contact": ["contact", "phone", "call", "email", "help", "support", "контакты", "телефон", "помощь", "aide",
                "ayuda", "اتصال", "联系", "ubufasha"],
    "open banking": ["open banking", "connected apps", "connect app", "third party", "consent", "подключенные приложения"],
    "till": ["till", "cash drawer", "drawer", "касса", "caisse", "caja"],
    "counter": ["counter", "teller", "cashier", "окно кассы", "guichet", "ventanilla"],
}
_TERM_TO_CONCEPT = {}
for _c, _terms in SYNONYMS.items():
    for _t in _terms + [_c]:
        _TERM_TO_CONCEPT.setdefault(_t, set()).add(_c)
STOP = {"the", "a", "an", "my", "me", "i", "to", "of", "for", "in", "on", "and", "or", "how", "do", "can", "is", "what",
        "where", "please", "show", "find", "want", "with", "by", "from", "at", "it", "this", "that", "your", "you", "be",
        "мой", "моя", "как", "где", "что", "и", "в", "на", "le", "la", "les", "de", "du", "un", "une", "el", "los",
        "las", "del", "una", "y"}


def norm(text):
    t = unicodedata.normalize("NFKD", str(text or ""))
    t = "".join(ch for ch in t if not unicodedata.combining(ch)).lower()
    return " ".join(re.sub(r"[^\w@.\-]+", " ", t).split())


def words(text):
    return [w for w in norm(text).split() if w not in STOP]


class Query:
    """A typed question: the words, the corrected words, the concepts they mean and any amount."""

    def __init__(self, text, vocab):
        self.text = " ".join(str(text or "").split())[:120]
        self.words = words(self.text)
        self.corrected, self.changes = [], []
        for w in self.words:
            if w in vocab or w.isdigit() or len(w) < 4 or not re.search(r"[a-z]", w):
                self.corrected.append(w)
                continue
            cut = 0.75 if len(w) >= 6 else 0.8
            best = get_close_matches(w, vocab, n=1, cutoff=cut)
            if best and best[0] != w:
                self.corrected.append(best[0])
                self.changes.append((w, best[0]))
            else:
                self.corrected.append(w)
        joined = " ".join(self.corrected)
        self.concepts = set()
        for term, concepts in _TERM_TO_CONCEPT.items():
            nt = norm(term)
            if nt and (f" {nt} " in f" {joined} " or nt in self.corrected):
                self.concepts |= concepts
        self.terms = set(self.corrected)
        for c in self.concepts:
            self.terms |= {norm(t) for t in SYNONYMS[c]}
        self.amount = None
        m = re.search(r"\b(\d[\d,]*(?:\.\d{1,2})?)\b", self.text.replace(" ", " "))
        if m:
            try:
                self.amount = int(round(float(m.group(1).replace(",", "")) * 100))
            except ValueError:
                pass

    @property
    def did_you_mean(self):
        if not self.changes:
            return ""
        out = self.text
        for old, new in self.changes:
            out = re.sub(re.escape(old), new, out, flags=re.I) if re.search(re.escape(old), out, re.I) else out
        return out if norm(out) != norm(self.text) else ""


def score_doc(q, doc):
    """How well one document fits the query (0 = not at all)."""
    title, text, keys = norm(doc.get("title")), norm(doc.get("text")), norm(doc.get("keywords"))
    hay = f" {title} {keys} {text} "
    s = 0.0
    for t in q.terms:
        if not t:
            continue
        if f" {t} " in f" {title} " or f" {t} " in f" {keys} ":
            s += 3
        elif f" {t} " in hay:
            s += 1
        elif len(t) >= 4 and (t in title or t in keys):
            s += 1.5
        elif len(t) >= 5 and t in hay:
            s += 0.5
    for w in q.corrected:                      # numbers, references, account numbers
        if (w.isdigit() and len(w) >= 3 or re.fullmatch(r"[a-z]{2}\d{6,}", w)) and w in hay:
            s += 4
    if q.amount and doc.get("amount") == q.amount:
        s += 3
    for c in q.concepts:
        if c in (doc.get("concepts") or ()):
            s += 2
    return s


def build_vocab(docs):
    vocab = set()
    for d in docs:
        for part in (d.get("title"), d.get("keywords"), d.get("text")):
            vocab |= {w for w in norm(part).split() if len(w) >= 3 and not w.isdigit()}
    for c, terms in SYNONYMS.items():
        for t in terms + [c]:
            vocab |= {w for w in norm(t).split() if len(w) >= 3}
    return sorted(vocab)


def search(text, docs, limit=30):
    """Rank `docs` (dicts with title, text, keywords, url, kind) for `text`.
    Returns {"query", "did_you_mean", "results", "suggestions"}."""
    vocab = build_vocab(docs)
    q = Query(text, vocab)
    if not q.words:
        return {"query": q.text, "did_you_mean": "", "results": [], "corrected": False}
    scored = [(score_doc(q, d), i, d) for i, d in enumerate(docs)]
    results = [d for s, i, d in sorted((x for x in scored if x[0] > 0), key=lambda x: (-x[0], x[1]))][:limit]
    return {"query": q.text, "did_you_mean": q.did_you_mean, "results": results, "corrected": bool(q.changes),
            "concepts": sorted(q.concepts)}


def suggest(prefix, docs, limit=8):
    """Titles and words for the list under the search box."""
    p = norm(prefix)
    if len(p) < 2:
        return []
    out, seen = [], set()
    for d in docs:
        t = d.get("title") or ""
        if norm(t).startswith(p) or f" {p}" in f" {norm(t)}":
            if t.lower() not in seen:
                out.append({"text": t, "url": d.get("url", ""), "kind": d.get("kind", "")})
                seen.add(t.lower())
        if len(out) >= limit:
            return out
    last = p.split()[-1]
    for w in get_close_matches(last, build_vocab(docs), n=limit - len(out), cutoff=0.7):
        guess = (p.rsplit(" ", 1)[0] + " " + w) if " " in p else w
        if guess not in seen:
            out.append({"text": guess, "url": "", "kind": "word"})
            seen.add(guess)
    return out[:limit]

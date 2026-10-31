"""
onboarding.py - things that bank staff must approve (mixin class used by Bank):

    * a new account opened on the website (the customer uploads an ID document)
    * a change of personal data asked by the customer (name, contacts, address, country ...)
    * uploaded documents (ID proof, address proof, income proof) - kept in the database
"""

import base64
import json

import world
from core import BankError, now
from world import format_money

DOC_KINDS = ["ID proof", "Address proof", "Income proof", "Other"]
DOC_MAX_BYTES = 2 * 1024 * 1024                     # 2 MB per file
FILE_TYPES = [(b"%PDF-", "application/pdf", ".pdf"), (b"\x89PNG\r\n\x1a\n", "image/png", ".png"),
              (b"\xff\xd8\xff", "image/jpeg", ".jpg")]

# personal data a customer may ask to change, and the proof each change needs
PROFILE_FIELDS = ["full_name", "email", "mobile", "address", "city", "state", "pincode", "country", "occupation"]
FIELD_LABELS = {"full_name": "Full name", "email": "Email", "mobile": "Mobile number", "address": "Address",
                "city": "City", "state": "State / region", "pincode": "Postal code", "country": "Country",
                "occupation": "Occupation"}
PROOF_NEEDED = {"full_name": "ID proof", "country": "Address proof", "address": "Address proof",
                "city": "Address proof", "state": "Address proof", "pincode": "Address proof"}


class OnboardingMixin:

    # ===== documents =====

    @staticmethod
    def check_document(kind, filename, blob):
        """Check an uploaded file. Returns (kind, file name, mime type, bytes)."""
        if kind not in DOC_KINDS:
            raise BankError("Unknown document type.")
        if not blob:
            raise BankError("The uploaded file is empty.")
        if len(blob) > DOC_MAX_BYTES:
            raise BankError("A document can be at most 2 MB.")
        for magic, mime, ext in FILE_TYPES:
            if blob.startswith(magic):                       # look at the content, not only the name
                name = "".join(ch for ch in (filename or "document") if ch.isalnum() or ch in "._- ")[:80].strip()
                if not name.lower().endswith((ext, ".jpeg") if ext == ".jpg" else ext):
                    name = (name or "document") + ext
                return kind, name, mime, blob
        raise BankError("Only PDF, JPG and PNG files can be uploaded.")

    def save_document(self, conn, cid, kind, purpose, filename, mime, blob, when=None):
        cur = conn.execute("INSERT INTO documents (customer_id, kind, purpose, filename, mime, size, data, uploaded_at) "
                           "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                           (cid, kind, purpose, filename, mime, len(blob), base64.b64encode(blob).decode(), when or now()))
        return cur.lastrowid

    def add_document(self, cid, kind, filename, blob, purpose="account"):
        kind, name, mime, blob = self.check_document(kind, filename, blob)
        with self._connect() as conn:
            return self.save_document(conn, cid, kind, purpose, name, mime, blob)

    def documents(self, cid, purpose=None):
        sql = "SELECT id, customer_id, kind, purpose, filename, mime, size, uploaded_at FROM documents WHERE customer_id = ?"
        args = [cid]
        if purpose:
            sql += " AND purpose = ?"
            args.append(purpose)
        with self._connect() as conn:
            return [dict(r) for r in conn.execute(sql + " ORDER BY id DESC", args)]

    def document(self, doc_id, cid=None):
        """One document with its bytes. With `cid`, only that customer's own document is returned."""
        with self._connect() as conn:
            r = conn.execute("SELECT * FROM documents WHERE id = ?", (doc_id,)).fetchone()
        if r is None or (cid is not None and r["customer_id"] != cid):
            raise BankError("Document not found.")
        d = dict(r)
        d["data"] = base64.b64decode(d["data"])
        return d

    # ===== new accounts opened online =====

    def pending_customers(self):
        with self._connect() as conn:
            rows = [dict(r) for r in conn.execute(
                "SELECT c.*, a.number, a.type, a.currency FROM customers c JOIN accounts a USING (customer_id) "
                "WHERE c.status = 'Pending' ORDER BY c.created_at")]
        for r in rows:
            r["documents"] = self.documents(r["customer_id"])
        return rows

    def decide_customer(self, cid, approve, decided_by, reason=""):
        """Bank staff approve (account opens, opening deposit is credited) or reject an online application."""
        reason = " ".join((reason or "").split())[:200]
        if not approve and len(reason) < 3:
            raise BankError("Please write the reason for rejecting the application.")
        with self._connect() as conn:
            if conn.execute("UPDATE customers SET status = ?, status_reason = ?, decided_by = ?, decided_at = ? "
                            "WHERE customer_id = ? AND status = 'Pending'",
                            ("Active" if approve else "Rejected", reason, decided_by, now(), cid)).rowcount == 0:
                raise BankError("This application was already handled.")
            row = dict(conn.execute("SELECT * FROM customers WHERE customer_id = ?", (cid,)).fetchone())
            conn.execute("UPDATE accounts SET status = ? WHERE customer_id = ?",
                         ("Active" if approve else "Rejected", cid))
            if approve:
                number = conn.execute("SELECT number FROM accounts WHERE customer_id = ?", (cid,)).fetchone()[0]
                acc = self._load(conn, number)
                if row["opening_deposit"]:
                    self._save(conn, acc.credit(row["opening_deposit"], "Deposit", "Account opening deposit"))
                    conn.execute("UPDATE customers SET opening_deposit = 0 WHERE customer_id = ?", (cid,))
                self._alert(conn, cid, "Your PyBank account is open",
                            f"Welcome to PyBank! Your account {number} was approved. You can now log in to Net "
                            f"Banking with your Customer ID {cid}.")
            else:
                self._alert(conn, cid, "Your account application was not approved",
                            f"Sorry, your application {cid} was not approved. Reason: {reason}")
        return row

    # ===== changes of personal data (approved by staff) =====

    def request_profile_change(self, cid, form, documents=None):
        """The customer asks to change personal data. Nothing changes until bank staff approve it."""
        c = self.get_customer(cid)
        new = {k: " ".join(str(form.get(k, getattr(c, k)) or "").split()) for k in PROFILE_FIELDS}
        new["country"] = (new["country"] or c.country).upper()
        if len(new["full_name"]) < 3 or len(new["full_name"].split()) < 2:
            raise BankError("Please enter your full name (first and last name).")
        if not new["occupation"]:
            raise BankError("Please select your occupation.")
        (new["email"], new["mobile"], new["address"], new["city"], new["state"], new["pincode"],
         new["country"]) = self.check_contact(cid, new["email"], new["mobile"], new["address"], new["city"],
                                               new["state"], new["pincode"], new["country"])
        new["full_name"] = new["full_name"].title()
        changes = {k: [getattr(c, k), new[k]] for k in PROFILE_FIELDS if str(getattr(c, k)) != new[k]}
        if not changes:
            raise BankError("Nothing was changed.")
        files = [self.check_document(kind, name, blob) for kind, name, blob in (documents or [])]
        needed = {PROOF_NEEDED[k] for k in changes if k in PROOF_NEEDED}
        missing = needed - {f[0] for f in files}
        if missing:
            raise BankError("Please upload a document that proves this change: " + ", ".join(sorted(missing)) + ".")
        with self._connect() as conn:
            if conn.execute("SELECT 1 FROM profile_changes WHERE customer_id = ? AND status = 'Pending'",
                            (cid,)).fetchone():
                raise BankError("You already have a change waiting for approval. Please wait or cancel it first.")
            cur = conn.execute("INSERT INTO profile_changes (customer_id, changes, created_at) VALUES (?, ?, ?)",
                               (cid, json.dumps(changes, ensure_ascii=False), now()))
            pid = cur.lastrowid
            for kind, name, mime, blob in files:
                self.save_document(conn, cid, kind, f"profile:{pid}", name, mime, blob)
            self._alert(conn, cid, "Change of personal data received",
                        "Your request to change your personal data was sent to the bank. Your details change as soon "
                        "as a bank officer approves it.", only_in_app=True)
        return pid

    def profile_changes(self, cid=None, status=None):
        sql = ("SELECT p.*, c.full_name AS customer_name FROM profile_changes p "
               "JOIN customers c ON c.customer_id = p.customer_id WHERE 1 = 1")
        args = []
        if cid:
            sql += " AND p.customer_id = ?"
            args.append(cid)
        if status:
            sql += " AND p.status = ?"
            args.append(status)
        with self._connect() as conn:
            rows = [dict(r) for r in conn.execute(sql + " ORDER BY p.id DESC", args)]
        for r in rows:
            r["changes"] = json.loads(r["changes"])
            r["documents"] = self.documents(r["customer_id"], f"profile:{r['id']}")
        return rows

    def cancel_profile_change(self, cid, pid):
        with self._connect() as conn:
            if conn.execute("UPDATE profile_changes SET status = 'Cancelled', decided_at = ? WHERE id = ? "
                            "AND customer_id = ? AND status = 'Pending'", (now(), pid, cid)).rowcount == 0:
                raise BankError("Only a change that is still waiting can be cancelled.")

    def decide_profile_change(self, pid, approve, decided_by, reason=""):
        reason = " ".join((reason or "").split())[:200]
        if not approve and len(reason) < 3:
            raise BankError("Please write the reason for rejecting the change.")
        with self._connect() as conn:
            r = conn.execute("SELECT * FROM profile_changes WHERE id = ?", (pid,)).fetchone()
            if r is None:
                raise BankError("Request not found.")
            r = dict(r)
            if conn.execute("UPDATE profile_changes SET status = ?, decided_at = ?, decided_by = ?, reason = ? "
                            "WHERE id = ? AND status = 'Pending'",
                            ("Approved" if approve else "Rejected", now(), decided_by, reason, pid)).rowcount == 0:
                raise BankError(f"This request was already handled ({r['status']}).")
            changes = json.loads(r["changes"])
            if approve:
                for field, (old, value) in changes.items():
                    if field in ("email", "mobile") and conn.execute(
                            f"SELECT 1 FROM customers WHERE {field} = ? AND customer_id != ?",
                            (value, r["customer_id"])).fetchone():
                        raise BankError("This email or mobile number is used by another customer.")
                sets = ", ".join(f"{k} = ?" for k in changes)
                conn.execute(f"UPDATE customers SET {sets} WHERE customer_id = ?",
                             [v[1] for v in changes.values()] + [r["customer_id"]])
                names = ", ".join(FIELD_LABELS[k].lower() for k in changes)
                self._alert(conn, r["customer_id"], "Your personal data was updated",
                            f"The bank approved your change ({names}).")
            else:
                self._alert(conn, r["customer_id"], "Change of personal data not approved",
                            f"Your requested change was not approved. Reason: {reason}")
        r["changes"] = changes
        return r

    def set_language(self, cid, lang):
        if lang in world.LANGUAGES:
            with self._connect() as conn:
                conn.execute("UPDATE customers SET lang = ? WHERE customer_id = ?", (lang, cid))

    def money(self, amount, cid):
        """Format an amount in the currency of the customer's account."""
        return format_money(amount, self.get_account(cid).currency)

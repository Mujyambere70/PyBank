"""
iso20022.py - payments abroad in the ISO 20022 message format (version 4).

Version 3 sent SWIFT payments to an imaginary bank that answered by itself after one hour. Real banks
send their payments to the payment network in files of the ISO 20022 standard and receive a status
report back. Version 4 works the same way (still with a test partner - no real network):

    pain.001.001.09  "Customer Credit Transfer Initiation": PyBank -> the network, one file with many
                     payments (SWIFT and SEPA). Bank staff create it in Back office -> Payments.
    pain.002.001.10  "Payment Status Report": the network -> PyBank. For each payment ACSC (settled),
                     ACCP / ACSP (accepted) or RJCT (rejected, with a reason code such as AC04 =
                     closed account). A rejected payment is returned to the customer's account.

There is also a new rail, SEPA (euro payments inside the Single Euro Payments Area): EUR accounts only,
the IBAN of a SEPA country, settled the same way.

The setting "payments_mode" decides how SWIFT and SEPA payments finish:
    sandbox   - the simulated partner answers after a while (as in version 3)
    iso_files - they wait until a pain.002 status report is uploaded (or a test report is made)
"""

import random
import re
import xml.etree.ElementTree as ET
from datetime import datetime
from decimal import Decimal

import world
from core import BankError, now
from world import format_money

ISO_SCHEMA = """
CREATE TABLE IF NOT EXISTS iso_files (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    msg_id     TEXT NOT NULL UNIQUE,
    kind       TEXT NOT NULL,
    created_at TEXT NOT NULL,
    created_by TEXT NOT NULL DEFAULT '',
    payments   INTEGER NOT NULL,
    control_sum TEXT NOT NULL DEFAULT '',
    xml        TEXT NOT NULL,
    status     TEXT NOT NULL DEFAULT 'Created',
    answer_id  INTEGER
);
"""
PAIN001 = "urn:iso:std:iso:20022:tech:xsd:pain.001.001.09"
PAIN002 = "urn:iso:std:iso:20022:tech:xsd:pain.002.001.10"
SEPA_COUNTRIES = {"AT", "BE", "BG", "HR", "CY", "CZ", "DK", "EE", "FI", "FR", "DE", "GR", "HU", "IE", "IT", "LV",
                  "LT", "LU", "MT", "NL", "PL", "PT", "RO", "SK", "SI", "ES", "SE", "IS", "LI", "NO", "CH", "GB",
                  "MC", "SM", "VA", "AD", "AL", "MD", "ME", "MK", "RS"}
OK_CODES = {"ACSC": "Settled", "ACCP": "Accepted", "ACSP": "Accepted, settlement in process", "ACTC": "Accepted (technical)"}
REJECT_REASONS = {"AC01": "Wrong account number", "AC04": "Account closed", "AC06": "Account blocked",
                  "AM04": "Not enough money at the other bank", "BE01": "Name does not match the account",
                  "RC01": "Wrong BIC", "MS03": "Reason not given"}
PYBANK_BIC = "PYBKINBBAMB"


def _el(parent, tag, text=None, **attrs):
    e = ET.SubElement(parent, tag, attrs)
    if text is not None:
        e.text = str(text)
    return e


def amount_text(minor):
    return f"{Decimal(minor) / 100:.2f}"


def build_pain001(msg_id, payments, debtor_bic=PYBANK_BIC, created=None):
    """One pain.001.001.09 document. payments: dicts from external_transfers plus 'debtor_name', 'debtor_iban'."""
    ET.register_namespace("", PAIN001)
    q = lambda t: f"{{{PAIN001}}}{t}"                       # noqa: E731
    doc = ET.Element(q("Document"))
    root = ET.SubElement(doc, q("CstmrCdtTrfInitn"))
    hdr = ET.SubElement(root, q("GrpHdr"))
    _el(hdr, q("MsgId"), msg_id)
    _el(hdr, q("CreDtTm"), (created or datetime.now()).strftime("%Y-%m-%dT%H:%M:%S"))
    _el(hdr, q("NbOfTxs"), len(payments))
    _el(hdr, q("CtrlSum"), amount_text(sum(p["amount"] for p in payments)))
    init = ET.SubElement(hdr, q("InitgPty"))
    _el(init, q("Nm"), "PyBank (demo bank)")
    # one payment information block per (rail, currency, debtor account)
    groups = {}
    for p in payments:
        groups.setdefault((p["rail"], p["currency"], p["account_number"]), []).append(p)
    for n, ((rail, cur, acct), items) in enumerate(sorted(groups.items()), 1):
        pmt = ET.SubElement(root, q("PmtInf"))
        _el(pmt, q("PmtInfId"), f"{msg_id}-{n}")
        _el(pmt, q("PmtMtd"), "TRF")
        _el(pmt, q("NbOfTxs"), len(items))
        _el(pmt, q("CtrlSum"), amount_text(sum(p["amount"] for p in items)))
        tp = ET.SubElement(pmt, q("PmtTpInf"))
        svc = ET.SubElement(tp, q("SvcLvl"))
        _el(svc, q("Cd"), "SEPA" if rail == "SEPA" else "URGP")
        rex = ET.SubElement(pmt, q("ReqdExctnDt"))
        _el(rex, q("Dt"), (created or datetime.now()).strftime("%Y-%m-%d"))
        dbtr = ET.SubElement(pmt, q("Dbtr"))
        _el(dbtr, q("Nm"), items[0]["debtor_name"][:70])
        dacct = ET.SubElement(ET.SubElement(pmt, q("DbtrAcct")), q("Id"))
        _el(ET.SubElement(dacct, q("Othr")), q("Id"), acct)
        dagt = ET.SubElement(ET.SubElement(pmt, q("DbtrAgt")), q("FinInstnId"))
        _el(dagt, q("BICFI"), debtor_bic)
        _el(pmt, q("ChrgBr"), "SLEV" if rail == "SEPA" else "SHAR")
        for p in items:
            tx = ET.SubElement(pmt, q("CdtTrfTxInf"))
            ids = ET.SubElement(tx, q("PmtId"))
            _el(ids, q("InstrId"), p["ref"])
            _el(ids, q("EndToEndId"), p["ref"])
            amt = ET.SubElement(tx, q("Amt"))
            _el(amt, q("InstdAmt"), amount_text(p["amount"]), Ccy=p["currency"])
            if p.get("bank_code"):
                cagt = ET.SubElement(ET.SubElement(tx, q("CdtrAgt")), q("FinInstnId"))
                _el(cagt, q("BICFI"), p["bank_code"])
            cdtr = ET.SubElement(tx, q("Cdtr"))
            _el(cdtr, q("Nm"), p["beneficiary_name"][:70])
            cid = ET.SubElement(ET.SubElement(tx, q("CdtrAcct")), q("Id"))
            if re.fullmatch(r"[A-Z]{2}\d{2}[A-Z0-9]{10,30}", p["beneficiary_account"]):
                _el(cid, q("IBAN"), p["beneficiary_account"])
            else:
                _el(ET.SubElement(cid, q("Othr")), q("Id"), p["beneficiary_account"])
            if p.get("remarks"):
                _el(ET.SubElement(tx, q("RmtInf")), q("Ustrd"), p["remarks"][:140])
    ET.indent(doc)
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(doc, encoding="unicode")


def build_pain002(msg_id, original_msg_id, results, created=None):
    """A status report. results: [(end_to_end_id, status code, reason code or '')]."""
    ET.register_namespace("", PAIN002)
    q = lambda t: f"{{{PAIN002}}}{t}"                       # noqa: E731
    doc = ET.Element(q("Document"))
    root = ET.SubElement(doc, q("CstmrPmtStsRpt"))
    hdr = ET.SubElement(root, q("GrpHdr"))
    _el(hdr, q("MsgId"), msg_id)
    _el(hdr, q("CreDtTm"), (created or datetime.now()).strftime("%Y-%m-%dT%H:%M:%S"))
    org = ET.SubElement(root, q("OrgnlGrpInfAndSts"))
    _el(org, q("OrgnlMsgId"), original_msg_id)
    _el(org, q("OrgnlMsgNmId"), "pain.001.001.09")
    pmt = ET.SubElement(root, q("OrgnlPmtInfAndSts"))
    _el(pmt, q("OrgnlPmtInfId"), f"{original_msg_id}-1")
    for e2e, status, reason in results:
        tx = ET.SubElement(pmt, q("TxInfAndSts"))
        _el(tx, q("OrgnlEndToEndId"), e2e)
        _el(tx, q("TxSts"), status)
        if reason:
            rsn = ET.SubElement(ET.SubElement(tx, q("StsRsnInf")), q("Rsn"))
            _el(rsn, q("Cd"), reason)
    ET.indent(doc)
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(doc, encoding="unicode")


def parse_pain002(text):
    """(original message id, [(end_to_end_id, status, reason)]) from a pain.002 file of any version."""
    if len(text) > 2_000_000:
        raise BankError("The file is too big for a status report.")
    if "<!DOCTYPE" in text or "<!ENTITY" in text:
        raise BankError("This file is not accepted (it contains a DOCTYPE).")
    try:
        root = ET.fromstring(text.encode() if isinstance(text, str) else text)
    except ET.ParseError:
        raise BankError("This is not an XML file.")
    local = lambda e: e.tag.rsplit("}", 1)[-1]               # noqa: E731
    if local(root) != "Document" or not root.tag.startswith("{urn:iso:std:iso:20022:tech:xsd:pain.002"):
        raise BankError("This is not an ISO 20022 pain.002 status report.")
    original, out = "", []
    for e in root.iter():
        if local(e) == "OrgnlMsgId" and not original:
            original = (e.text or "").strip()
        if local(e) == "TxInfAndSts":
            e2e = status = reason = ""
            for c in e.iter():
                if local(c) == "OrgnlEndToEndId":
                    e2e = (c.text or "").strip()
                elif local(c) == "TxSts":
                    status = (c.text or "").strip()
                elif local(c) == "Cd" and not reason:
                    reason = (c.text or "").strip()
            if e2e and status:
                out.append((e2e, status, reason))
    if not original or not out:
        raise BankError("The status report has no payments in it.")
    return original, out


class IsoMixin:

    def payments_mode(self):
        return self.settings().get("payments_mode", "sandbox")

    def iso_files(self, limit=50):
        with self._connect() as conn:
            return [dict(r) for r in conn.execute("SELECT id, msg_id, kind, created_at, created_by, payments, control_sum, "
                                                  "status, answer_id FROM iso_files ORDER BY id DESC LIMIT ?", (limit,))]

    def iso_file(self, file_id):
        with self._connect() as conn:
            r = conn.execute("SELECT * FROM iso_files WHERE id = ?", (file_id,)).fetchone()
        if r is None:
            raise BankError("File not found.")
        return dict(r)

    def waiting_for_file(self):
        """SWIFT / SEPA payments not yet put into a pain.001 file."""
        with self._connect() as conn:
            return [dict(r) for r in conn.execute(
                "SELECT * FROM external_transfers WHERE status = 'Processing' AND rail IN ('SWIFT', 'SEPA') "
                "AND iso_file = '' ORDER BY id")]

    def create_pain001(self, staff_id):
        """Put every waiting SWIFT / SEPA payment into one new pain.001 file. Returns the file id."""
        items = self.waiting_for_file()
        if not items:
            raise BankError("There are no SWIFT or SEPA payments waiting for a file.")
        created = datetime.now()
        with self._connect() as conn:
            msg_id = self._unique(conn, "SELECT 1 FROM iso_files WHERE msg_id = ?",
                                  lambda: f"PYBK{created:%Y%m%d%H%M}{random.randint(0, 9999):04d}")
            for p in items:
                p["debtor_name"] = conn.execute("SELECT full_name FROM customers WHERE customer_id = ?",
                                                (p["customer_id"],)).fetchone()[0]
            xml = build_pain001(msg_id, items, created=created)
            fid = conn.execute("INSERT INTO iso_files (msg_id, kind, created_at, created_by, payments, control_sum, xml) "
                               "VALUES (?, 'pain.001', ?, ?, ?, ?, ?)",
                               (msg_id, now(), staff_id, len(items),
                                amount_text(sum(p["amount"] for p in items)), xml)).lastrowid
            for p in items:
                conn.execute("UPDATE external_transfers SET iso_file = ? WHERE id = ? AND iso_file = ''", (msg_id, p["id"]))
        return fid

    def test_pain002(self, file_id, staff_id=""):
        """Make the answer that the test partner bank would send for a pain.001 file:
        a beneficiary account ending in 0000 is rejected (AC04 account closed), the rest is settled."""
        f = self.iso_file(file_id)
        if f["kind"] != "pain.001":
            raise BankError("Only a payment file can be answered.")
        with self._connect() as conn:
            pays = conn.execute("SELECT ref, beneficiary_account FROM external_transfers WHERE iso_file = ?",
                                (f["msg_id"],)).fetchall()
        results = [(p["ref"], "RJCT" if p["beneficiary_account"].endswith("0000") else "ACSC",
                    "AC04" if p["beneficiary_account"].endswith("0000") else "") for p in pays]
        return build_pain002(f"ANS{f['msg_id'][4:]}", f["msg_id"], results)

    def apply_pain002(self, text, staff_id=""):
        """Read a status report and finish the payments in it. Returns counts."""
        original, results = parse_pain002(text)
        with self._connect() as conn:
            f = conn.execute("SELECT * FROM iso_files WHERE msg_id = ? AND kind = 'pain.001'", (original,)).fetchone()
            if f is None:
                raise BankError(f"PyBank did not send a payment file with the number {original}.")
            msg_id = f"STS{original[4:]}{random.randint(0, 999):03d}"
            answer_id = conn.execute("INSERT INTO iso_files (msg_id, kind, created_at, created_by, payments, xml, status) "
                                     "VALUES (?, 'pain.002', ?, ?, ?, ?, 'Read')",
                                     (msg_id, now(), staff_id, len(results),
                                      text if isinstance(text, str) else text.decode("utf-8", "replace"))).lastrowid
        done = {"completed": 0, "returned": 0, "unknown": 0, "already": 0}
        for e2e, status, reason in results:
            with self._connect() as conn:
                p = conn.execute("SELECT * FROM external_transfers WHERE ref = ? AND iso_file = ?", (e2e, original)).fetchone()
                if p is None:
                    done["unknown"] += 1
                    continue
                p = dict(p)
                if p["status"] != "Processing":
                    done["already"] += 1
                    continue
                if status in OK_CODES:
                    utr = f"ISO{original[4:16]}{random.randint(0, 999_999):06d}"
                    conn.execute("UPDATE external_transfers SET status = 'Completed', settled_at = ?, utr = ? "
                                 "WHERE id = ? AND status = 'Processing'", (now(), utr, p["id"]))
                    self._alert(conn, p["customer_id"], f"{p['rail']} payment {p['ref']} completed",
                                f"Your {p['rail']} payment of {format_money(p['amount'], p['currency'], 'en')} to "
                                f"{p['beneficiary_name']} reached the other bank. Reference {utr}.", only_in_app=True)
                    done["completed"] += 1
                elif status == "RJCT":
                    why = REJECT_REASONS.get(reason, "Rejected by the other bank") + (f" ({reason})" if reason else "")
                    acc = self._load(conn, p["account_number"], must_be_active=False)
                    back = acc.credit(p["amount"], "Refund", f"{p['rail']} RETURN/{p['ref']}/{why.upper()}")
                    self._save(conn, back)
                    conn.execute("UPDATE external_transfers SET status = 'Returned', settled_at = ?, reason = ?, "
                                 "return_ref = ? WHERE id = ?", (now(), why, back.ref, p["id"]))
                    self._alert(conn, p["customer_id"], f"{p['rail']} payment {p['ref']} returned",
                                f"Your {p['rail']} payment of {format_money(p['amount'], p['currency'], 'en')} to "
                                f"{p['beneficiary_name']} was returned by the other bank ({why}). The money is back "
                                f"in your account.")
                    done["returned"] += 1
                else:
                    done["unknown"] += 1
        with self._connect() as conn:
            left = conn.execute("SELECT COUNT(*) FROM external_transfers WHERE iso_file = ? AND status = 'Processing'",
                                (original,)).fetchone()[0]
            conn.execute("UPDATE iso_files SET status = ?, answer_id = ? WHERE msg_id = ?",
                         ("Answered" if not left else "Partly answered", answer_id, original))
        return done

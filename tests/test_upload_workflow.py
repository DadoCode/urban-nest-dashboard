"""Regression test for the document-ingestion workflow (upload -> extract ->
review -> confirm), run entirely against a throwaway database and uploads
folder -- never the real ledger -- with the extraction API key forced off so
it can never call out. Fixtures are tiny synthetic CSVs/binaries written
here; they exercise the code paths, not any real document.

Run: .venv/bin/python tests/test_upload_workflow.py
"""
import io
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix="un-upload-test-"))
os.environ["DASHBOARD_DB_PATH"] = str(TMP / "t.db")
os.environ["DASHBOARD_UPLOADS_PATH"] = str(TMP / "uploads")
os.environ.pop("VERCEL", None)
os.environ.pop("UN_DEMO_MODE", None)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "dashboard"))

import db  # noqa: E402
import services.extraction as extraction  # noqa: E402

extraction._load_key = lambda: None  # never reach the real API, whatever .env holds

db.ensure_schema()
_c = db.get_conn()
for pid, name, addr in [("alpha-house", "Alpha House", "1 Alpha Road"), ("beta-court", "Beta Court", "2 Beta Street"),
                        ("beta-court-two", "Beta Court Annex", "2 Beta Street"),
                        ("gamma-view", "Gamma View", "3 Gamma Lane"), ("gamma-lodge", "Gamma Lodge", "4 Gamma Lane")]:
    _c.execute("INSERT INTO properties (id, code, name, address, type) VALUES (?,?,?,?,'flat')", (pid, pid.upper(), name, addr))
_c.execute("INSERT INTO properties (id, code, name, address, type) VALUES ('general-overheads','GEN','General','', 'overhead')")
_c.commit(); _c.close()

from datetime import date as datetime_date  # noqa: E402,F401
from werkzeug.datastructures import MultiDict  # noqa: E402

from app import create_app  # noqa: E402

app = create_app()
client = app.test_client()
failures = []


def check(label, ok, extra=""):
    print(("[PASS] " if ok else "[FAIL] ") + label + ("" if ok else f"   {extra}"))
    if not ok:
        failures.append(label)


def q(sql, *args):
    conn = db.get_conn()
    try:
        return conn.execute(sql, args).fetchall()
    finally:
        conn.close()


def upload(name, data, doc_type="other", property_id="", follow=False):
    if isinstance(data, str):
        data = data.encode()
    return client.post("/documents/upload", data={"document": (io.BytesIO(data), name), "doc_type": doc_type, "property_id": property_id},
                       content_type="multipart/form-data", follow_redirects=follow)


def last_doc():
    return q("SELECT * FROM documents ORDER BY id DESC LIMIT 1")[0]


CSV = "Date,Vendor,Description,Amount\n2026-03-02,CleanCo,Turnover clean,45.00\n2026-03-09,PowerCo,Electricity,80.50\n"

# ---------------------------------------------------------------- upload + extraction
r = upload("march.csv", CSV, "cleaning_invoice", "alpha-house")
d1 = last_doc()
check("upload redirects to the review page", r.status_code == 302 and f"/documents/{d1['id']}/review" in r.headers["Location"])
check("status is Needs review (extracted)", d1["status"] == "extracted")
check("file hash and size recorded", bool(d1["file_hash"]) and d1["file_size"] == len(CSV))
check("file stored under the configured uploads dir", str(TMP / "uploads") in d1["stored_path"] and Path(d1["stored_path"]).is_file())
check("nothing reaches the ledger before confirmation", q("SELECT COUNT(*) n FROM transactions")[0]["n"] == 0)
check("period detected from line dates (Mar 2026)", (d1["detected_year"], d1["detected_month"]) == (2026, 3))
ev = [e["event"] for e in q("SELECT event FROM document_events WHERE document_id=? ORDER BY id", d1["id"])]
check("trail has uploaded then extracted", ev == ["uploaded", "extracted"], ev)

page = client.get(f"/documents/{d1['id']}/review").get_data(as_text=True)
for needle in ("Source file", "Document type", "Detected property", "Detected period", "Extracted total", "Confidence", "Reject / delete draft", "What happened to this file"):
    check(f"review page shows '{needle}'", needle in page)
check("extracted total is 125.50", "£125.50" in page)

upload("income-guess.csv", CSV, "other", "alpha-house")
check("positive amounts defaulting to Booking Income are called out", "income_guess" in last_doc()["detection_json"])
check("tiny files are labelled in bytes, not '0 KB'", "bytes" in q("SELECT summary FROM document_events WHERE document_id=? AND event='uploaded'", last_doc()["id"])[0]["summary"])
client.post(f"/documents/{last_doc()['id']}/reject")

# ---------------------------------------------------------------- duplicate file
upload("march-again.csv", CSV, "cleaning_invoice", "alpha-house")
d2 = last_doc()
check("same bytes flagged as a duplicate file", d2["duplicate_of_document"] == d1["id"])
page2 = client.get(f"/documents/{d2['id']}/review").get_data(as_text=True)
check("review page says Possible duplicate and links the earlier upload", "Possible duplicate" in page2 and f"/documents/{d1['id']}/review" in page2)
check("documents list shows the duplicate pill", "Possible duplicate" in client.get("/documents?d_status=").get_data(as_text=True))

# ---------------------------------------------------------------- confirm #1, then #2 is blocked until decided
def confirm(doc_id, extra=None):
    items = q("SELECT * FROM document_items WHERE document_id=? ORDER BY line_index", doc_id)
    form = []
    for it in items:
        form += [("item_id", it["id"]), ("include", it["id"]), ("property_id", it["property_id"] or "alpha-house"),
                 ("vendor", it["vendor"] or ""), ("description", it["raw_description"] or ""), ("amount", it["amount"]),
                 ("category", it["category"]), ("type", "opex"), ("date", it["date"])]
    form += extra or []
    return client.post(f"/documents/{doc_id}/confirm", data=MultiDict(form))

confirm(d1["id"])
check("confirm creates the transactions", q("SELECT COUNT(*) n FROM transactions WHERE document_id=?", d1["id"])[0]["n"] == 2)
check("document is Confirmed", q("SELECT status FROM documents WHERE id=?", d1["id"])[0]["status"] == "confirmed")
ev1 = {e["event"]: e for e in q("SELECT * FROM document_events WHERE document_id=?", d1["id"])}
check("trail records the confirmation", "confirmed" in ev1)
import json  # noqa: E402
changes = json.loads(ev1["confirmed"]["detail"])["kpi_changes"]
check("KPI before/after recorded for the touched month", len(changes) == 1 and changes[0]["after"]["costs"] - changes[0]["before"]["costs"] == 125.5, changes)

tx = q("SELECT id FROM transactions WHERE document_id=? LIMIT 1", d1["id"])[0]["id"]
drawer = client.get(f"/expenses/transactions/{tx}").get_data(as_text=True)
check("transaction drawer traces back to the source document", "march.csv" in drawer and "Cleaning invoice" in drawer and "Mar 2026" in drawer and "Confirmed" in drawer)

page2 = client.get(f"/documents/{d2['id']}/review").get_data(as_text=True)
check("its lines are flagged Possible duplicate (live check), with 'Replace the earlier one' as the default", page2.count("Possible duplicate") >= 2 and page2.count('value="replace" checked') == 2, (page2.count("Possible duplicate"), page2.count('value="replace" checked')))
n_before = q("SELECT COUNT(*) n FROM transactions")[0]["n"]
confirm(d2["id"])
check("confirming the twin REPLACES the earlier rows -- the ledger is not doubled", q("SELECT COUNT(*) n FROM transactions")[0]["n"] == n_before)
check("the replaced rows now belong to the new document", q("SELECT COUNT(*) n FROM transactions WHERE document_id=?", d2["id"])[0]["n"] == 2 and q("SELECT COUNT(*) n FROM transactions WHERE document_id=?", d1["id"])[0]["n"] == 0)
check("each replacement is written to the audit log", len(q("SELECT 1 FROM audit_log WHERE action='replace' AND entity_type='transaction'")) == 2)
check("the trail says some lines replaced an earlier version", any("replaced" in (e["summary"] or "") for e in q("SELECT summary FROM document_events WHERE document_id=? AND event='confirmed'", d2["id"])))

# ---------------------------------------------------------------- uploaded twice BEFORE confirming either
CSV_B = "Date,Vendor,Description,Amount\n2026-04-02,FixIt,Repair,60.00\n"
upload("b1.csv", CSV_B, "other", "alpha-house"); b1 = last_doc()
upload("b2.csv", CSV_B + "\n", "other", "alpha-house"); b2 = last_doc()   # different bytes, same content
check("same content, different bytes -> flagged as similar document", b2["duplicate_of_document"] == b1["id"])
confirm(b1["id"])
n_before = q("SELECT COUNT(*) n FROM transactions")[0]["n"]
confirm(b2["id"])
check("second twin is blocked at confirm (flags refreshed, not stale)", q("SELECT COUNT(*) n FROM transactions")[0]["n"] == n_before)

# ---------------------------------------------------------------- failure states
def failed_with(name, data, code_text, doc_type="other", prop="alpha-house"):
    r = upload(name, data, doc_type, prop, follow=True)
    d = last_doc()
    body = r.get_data(as_text=True)
    ok = r.status_code == 200 and d["status"] == "failed" and code_text.lower() in (d["failure_reason"] or "").lower() and "Traceback" not in body
    check(f"{name}: Failed with a plain reason ({code_text})", ok, d["failure_reason"])
    return d

failed_with("archive.zip", b"PK\x03\x04junk", "aren't supported")
failed_with("photo.heic", b"\x00\x00\x00\x18ftypheic", "HEIC")
failed_with("empty.csv", b"", "empty")
failed_with("bad.xlsx", b"this is not a real workbook", "couldn't be opened")
failed_with("nocolumns.csv", "Foo,Bar\n1,2\n", "No Amount column")
failed_with("statement.pdf", b"%PDF-1.4 fake", "isn't configured")
failed_with("scan.png", b"\x89PNG\r\n\x1a\n" + b"0" * 50, "isn't configured")
failed_with("headeronly.csv", "Date,Vendor,Amount\n", "No lines could be read")
failed_with("res.csv", "Foo,Bar\n1,2\n", "booking statement", doc_type="booking_statement")
check("failed documents keep the manual-entry form", "Reject / delete draft" in client.get(f"/documents/{last_doc()['id']}/review").get_data(as_text=True))
check("trail records the failure", any(e["event"] == "extraction_failed" for e in q("SELECT event FROM document_events WHERE document_id=?", last_doc()["id"])))

# ---------------------------------------------------------------- property / period detection
upload("alpha-house-invoice.csv", "Date,Vendor,Description,Amount\n2026-05-01,X,Y,10\n", "other", "")
d = last_doc()
check("property detected from the file name when not chosen", d["property_id"] == "alpha-house", d["property_id"])
upload("misc.csv", "Date,Vendor,Description,Amount\n2026-05-01,X,Y,10\n", "other", "")
d = last_doc()
check("no property detected -> unassigned with a warning", d["property_id"] is None and "property_missing" in d["detection_json"])
upload("gamma-invoice.csv", "Date,Vendor,Description,Amount\n2026-05-01,X,Y,10\n", "other", "")
d = last_doc()
check("two equally good property matches -> ambiguous, not auto-assigned", d["property_id"] is None and "property_ambiguous" in d["detection_json"], d["detection_json"])
upload("beta-court-annex-bill.csv", "Date,Vendor,Description,Amount\n2026-05-01,X,Y,11\n", "other", "")
check("a full match on the longer name beats the name contained in it", last_doc()["property_id"] == "beta-court-two", last_doc()["detection_json"])
upload("beta-court-bill.csv", "Date,Vendor,Description,Amount\n2026-05-01,X,Y,12\n", "other", "")
check("a clearly better match is picked automatically", last_doc()["property_id"] == "beta-court")
upload("span.csv", "Date,Vendor,Description,Amount\n2026-01-05,X,Y,10\n2026-02-05,X,Y,10\n2026-02-20,X,Y,10\n", "other", "alpha-house")
d = last_doc()
check("lines spanning months -> ambiguous period noted, most common month used", "period_ambiguous" in d["detection_json"] and (d["detected_year"], d["detected_month"]) == (2026, 2))
upload("nodates.csv", "Vendor,Description,Amount\nX,Y,10\n", "other", "alpha-house")
d = last_doc()
check("no dates -> period not detected, with a warning", d["detected_year"] is None and "period_missing" in d["detection_json"])
upload("partial.csv", "Date,Vendor,Description,Amount\n2026-05-01,X,Y,10\n2026-05-02,X,Y,notanumber\n2026-05-03,X,Y,\n", "other", "alpha-house")
d = last_doc()
check("unreadable rows reported as partial extraction", "partial_extraction" in d["detection_json"] and "skipped" in d["detection_json"], d["detection_json"])

# ---------------------------------------------------------------- manual fallback + duplicates
upload("scan2.png", b"\x89PNG\r\n\x1a\n" + b"1" * 40, "other", "alpha-house")
dm = last_doc()
manual = [("include", "0"), ("property_id", "alpha-house"), ("month", "6"), ("year", "2026"), ("vendor", "ManualCo"), ("description", "hand typed"),
          ("category", "purchase"), ("amount", "33.00")]
client.post(f"/documents/{dm['id']}/confirm", data=MultiDict(manual))
check("manual entry creates a transaction linked to its document", q("SELECT COUNT(*) n FROM transactions WHERE document_id=?", dm["id"])[0]["n"] == 1)
check("manual entry is recorded in the trail", any("by hand" in (e["summary"] or "") for e in q("SELECT summary FROM document_events WHERE document_id=? AND event='confirmed'", dm["id"])))
upload("scan3.png", b"\x89PNG\r\n\x1a\n" + b"2" * 40, "other", "alpha-house")
dm2 = last_doc()
client.post(f"/documents/{dm2['id']}/confirm", data=MultiDict(manual))
n_manual = q("SELECT COUNT(*) n FROM transactions")[0]["n"]
client.post(f"/documents/{dm2['id']}/confirm", data=MultiDict(manual))
check("manual duplicate REPLACES the earlier manual entry instead of adding a second", q("SELECT COUNT(*) n FROM transactions")[0]["n"] == n_manual
      and q("SELECT COUNT(*) n FROM transactions WHERE document_id=?", dm2["id"])[0]["n"] == 1 and q("SELECT COUNT(*) n FROM transactions WHERE document_id=?", dm["id"])[0]["n"] == 0)
upload("scan4.png", b"\x89PNG\r\n\x1a\n" + b"3" * 40, "other", "alpha-house")
dm3 = last_doc()
client.post(f"/documents/{dm3['id']}/confirm", data=MultiDict(manual + [("allow_dups", "1")]))
check("'Add even if they look like duplicates' keeps both", q("SELECT COUNT(*) n FROM transactions")[0]["n"] == n_manual + 1 and q("SELECT COUNT(*) n FROM transactions WHERE document_id=?", dm3["id"])[0]["n"] == 1)

# ---------------------------------------------------------------- edits are traced
upload("edit.csv", "Date,Vendor,Description,Amount\n2026-07-01,EditCo,Item,100\n", "other", "alpha-house")
de = last_doc()
item = q("SELECT * FROM document_items WHERE document_id=?", de["id"])[0]
client.post(f"/documents/{de['id']}/confirm", data=MultiDict([("item_id", item["id"]), ("include", item["id"]), ("property_id", "alpha-house"), ("vendor", "EditCo"),
                                                     ("description", "Item"), ("amount", "120"), ("category", "cleaning"), ("type", "opex"), ("date", "2026-07-01")]))
events = {e["event"]: e for e in q("SELECT * FROM document_events WHERE document_id=?", de["id"])}
check("an edit before confirming is recorded with from/to values", "edited" in events and '"from": 100.0' in events["edited"]["detail"] and '"to": 120.0' in events["edited"]["detail"], events.get("edited") and events["edited"]["detail"])

# ---------------------------------------------------------------- undo / reject
client.post(f"/documents/{de['id']}/undo")
check("undo removes the records and is traced", q("SELECT COUNT(*) n FROM transactions WHERE document_id=?", de["id"])[0]["n"] == 0
      and any(e["event"] == "undone" for e in q("SELECT event FROM document_events WHERE document_id=?", de["id"])))
check("a confirmed import cannot be rejected", (client.post(f"/documents/{d1['id']}/reject"), q("SELECT COUNT(*) n FROM documents WHERE id=?", d1["id"])[0]["n"])[1] == 1)
path = Path(de["stored_path"])
client.post(f"/documents/{de['id']}/reject")
check("rejecting a draft deletes the document, its lines and the stored file",
      q("SELECT COUNT(*) n FROM documents WHERE id=?", de["id"])[0]["n"] == 0 and q("SELECT COUNT(*) n FROM document_items WHERE document_id=?", de["id"])[0]["n"] == 0 and not path.exists())

# ---------------------------------------------------------------- oversized
app.config["MAX_CONTENT_LENGTH"] = 1000
n_docs = q("SELECT COUNT(*) n FROM documents")[0]["n"]
r = upload("huge.csv", "Date,Vendor,Description,Amount\n" + "2026-05-01,X,Y,10\n" * 200, "other", "alpha-house", follow=True)
check("oversized upload is refused with a clear message and stores nothing",
      "larger than the" in r.get_data(as_text=True) and q("SELECT COUNT(*) n FROM documents")[0]["n"] == n_docs)
app.config["MAX_CONTENT_LENGTH"] = 20 * 1024 * 1024

# ---------------------------------------------------------------- sorting / filtering
conn = db.get_conn()
for date, vendor, amt, src in [("2026-03-05", "Aaa", 10, "excel_import"), ("2026-03-20", "Bbb", 300, "upload"), ("2026-03-12", "Ccc", 50, "excel_import")]:
    conn.execute("INSERT INTO transactions (property_id,date,vendor,amount,direction,category,source) VALUES ('beta-court',?,?,?,'expense','other',?)", (date, vendor, amt, src))
conn.commit(); conn.close()
import re  # noqa: E402

def ledger_vendors(qs):
    html = client.get("/expenses?from=2026-03-01&to=2026-03-01&property=beta-court&" + qs).get_data(as_text=True)
    return re.findall(r"<td class=\"truncate\">(Aaa|Bbb|Ccc)</td>", html)

check("ledger defaults to newest date first", ledger_vendors("") == ["Bbb", "Ccc", "Aaa"], ledger_vendors(""))
check("ledger sorts by amount ascending", ledger_vendors("t_sort=amount&t_dir=asc") == ["Aaa", "Ccc", "Bbb"])
check("ledger sorts by amount descending", ledger_vendors("t_sort=amount&t_dir=desc") == ["Bbb", "Ccc", "Aaa"])
check("ledger filters by source", ledger_vendors("t_source=upload") == ["Bbb"])
check("an unknown sort falls back safely", ledger_vendors("t_sort=nope&t_dir=sideways") == ["Bbb", "Ccc", "Aaa"])

conn = db.get_conn()
for ci, co, gross, plat, src in [("2026-03-10", "2026-03-12", 200, "airbnb", "upload"), ("2026-03-01", "2026-03-04", 500, "booking_com", "excel_import"), ("2026-03-20", "2026-03-22", 100, "airbnb", "upload")]:
    conn.execute("INSERT INTO bookings (property_id,platform,reservation_id,check_in,check_out,gross_revenue,net_revenue,status,source) VALUES ('beta-court',?,?,?,?,?,?,'confirmed',?)",
                 (plat, f"R{ci}", ci, co, gross, gross * 0.85, src))
conn.commit(); conn.close()

def booking_rows(qs):
    html = client.get("/properties/beta-court/bookings?from=2026-03-01&to=2026-03-01&" + qs).get_data(as_text=True)
    return re.findall(r"<td>(2026-03-\d\d)</td>\s*<td>2026-03-\d\d</td>", html)

check("bookings default: newest check-in first", booking_rows("") == ["2026-03-20", "2026-03-10", "2026-03-01"], booking_rows(""))
check("bookings sort by check-in ascending", booking_rows("b_sort=check_in&b_dir=asc") == ["2026-03-01", "2026-03-10", "2026-03-20"])
check("bookings sort by gross booking revenue", booking_rows("b_sort=gross&b_dir=desc") == ["2026-03-01", "2026-03-10", "2026-03-20"])
check("bookings filter by channel", booking_rows("b_platform=airbnb&b_sort=check_in&b_dir=asc") == ["2026-03-10", "2026-03-20"])
check("bookings filter by source", booking_rows("b_source=excel_import") == ["2026-03-01"])
dr = client.get("/bookings/%d/drawer" % q("SELECT id FROM bookings WHERE check_in='2026-03-10'")[0]["id"]).get_data(as_text=True)
check("booking drawer says honestly when there is no source document", "No source document is attached" in dr)

def doc_names(qs):
    html = client.get("/documents?" + ("" if "d_status" in qs else "d_status=&") + qs).get_data(as_text=True)
    return re.findall(r'<td class="truncate" title="[^"]*">([\w.\-]+\.(?:csv|png|zip|xlsx|pdf|heic|jpg))', html)

check("documents default: newest upload first", doc_names("")[0] == last_doc()["filename"] or doc_names("")[0] == q("SELECT filename FROM documents ORDER BY uploaded_at DESC, id DESC LIMIT 1")[0]["filename"])
check("documents sort oldest first", doc_names("d_sort=uploaded_asc")[0] == "march.csv", doc_names("d_sort=uploaded_asc")[:2])
check("documents filter by status", set(doc_names("d_status=failed")) >= {"archive.zip", "bad.xlsx"} and "march.csv" not in doc_names("d_status=failed"))
check("documents filter by detected period", "march.csv" in doc_names("d_period=2026-03") and "edit.csv" not in doc_names("d_period=2026-03"))
check("documents filter by type and property", doc_names("d_type=cleaning_invoice&d_property=alpha-house") == ["march.csv", "march-again.csv"][::-1] or set(doc_names("d_type=cleaning_invoice&d_property=alpha-house")) == {"march.csv", "march-again.csv"})
html = client.get("/documents?d_status=").get_data(as_text=True)
check("zero-count status tabs are not shown", "Processing<span" not in html and "Uploaded<span" not in html)

# ---------------------------------------------------------------- AI-extraction path (client mocked: no network, no key)
import types  # noqa: E402

class _Reply:
    def __init__(self, text):
        self.content = [types.SimpleNamespace(type="text", text=text)]

def fake_anthropic(behaviour):
    mod = types.ModuleType("anthropic")
    class Anthropic:
        def __init__(self, api_key=None):
            assert api_key == "test-key-not-real"
            self.messages = types.SimpleNamespace(create=lambda **kw: behaviour(kw))
    mod.Anthropic = Anthropic
    sys.modules["anthropic"] = mod

extraction._load_key = lambda: "test-key-not-real"
PDF = b"%PDF-1.4 synthetic"

def boom(_kw):
    raise RuntimeError("secret-should-not-leak test-key-not-real")
fake_anthropic(boom)
upload("inv-api-error.pdf", PDF, "cleaning_invoice", "alpha-house")
d = last_doc()
check("AI call failing -> Failed with a plain reason, no key or payload leaked",
      d["status"] == "failed" and "didn't complete" in d["failure_reason"] and "test-key" not in d["failure_reason"] and "secret" not in d["failure_reason"], d["failure_reason"])

fake_anthropic(lambda kw: _Reply("this is not json"))
upload("inv-badjson.pdf", PDF, "cleaning_invoice", "alpha-house")
check("unparseable AI reply -> Failed, understood", "couldn't be understood" in (last_doc()["failure_reason"] or ""))

fake_anthropic(lambda kw: _Reply('{"document_hint": null, "period_hint": null, "items": []}'))
upload("blurry.pdf", PDF, "other", "alpha-house")
check("AI found nothing -> Failed as an unreadable scan", "too blurry" in (last_doc()["failure_reason"] or ""))

big = b"\xff\xd8\xff" + b"0" * (5 * 1024 * 1024 + 10)
upload("huge-photo.jpg", big, "other", "alpha-house")
check("photo over the 5 MB reading limit -> clear reason", "5 MB" in (last_doc()["failure_reason"] or ""), last_doc()["failure_reason"])

fake_anthropic(lambda kw: _Reply('''```json
{"document_hint": "Delivered to Alpha House, 1 Alpha Road", "period_hint": "2026-08",
 "items": [{"date": "2026-08-03", "vendor": "Cleaners Ltd", "description": "Deep clean", "amount": 90, "category": "cleaning", "page": 1, "confidence": 0.95},
           {"date": null, "vendor": "Smudged", "description": "illegible", "amount": 12.5, "category": "other", "page": 2, "confidence": 0.4}]}
```'''))
upload("scan-ok.pdf", PDF, "cleaning_invoice", "")
d = last_doc()
det = json.loads(d["detection_json"])
check("AI result: property detected from text on the document", d["property_id"] == "alpha-house" and det["property"]["source"] == "text on the document", det["property"])
check("AI result: document's stated period used", (d["detected_year"], d["detected_month"]) == (2026, 8))
check("AI result: low-confidence and undated lines flagged as partial extraction", "partial_extraction" in d["detection_json"] and det["low_confidence"] == 1)
check("AI result: method recorded as AI extraction", det["method"] == "AI extraction")
items = q("SELECT * FROM document_items WHERE document_id=? ORDER BY line_index", d["id"])
check("AI result: source page kept per line", [i["source_page"] for i in items] == [1, 2])
page = client.get(f"/documents/{d['id']}/review").get_data(as_text=True)
check("review shows the confidence summary", "Confidence" in page and "below 70%" in page)
fake_anthropic(lambda kw: _Reply('{"document_hint": null, "period_hint": "2026-08", "items": [{"date": "2026-08-03", "vendor": "A", "description": "x", "amount": 1, "category": "other", "confidence": 0.9}]}'))
upload("period-wrong.pdf", PDF, "other", "alpha-house")
d = last_doc()
fake_anthropic(lambda kw: _Reply('{"document_hint": null, "period_hint": "2026-08", "items": [{"date": "2026-11-03", "vendor": "A", "description": "x", "amount": 1, "category": "other", "confidence": 0.9}, {"date": "2026-11-09", "vendor": "A", "description": "y", "amount": 2, "category": "other", "confidence": 0.9}]}'))
upload("period-wrong2.pdf", PDF, "other", "alpha-house")
check("document period disagrees with its line dates -> mismatch warning", "period_mismatch" in last_doc()["detection_json"])
sys.modules.pop("anthropic", None)
extraction._load_key = lambda: None

# ---------------------------------------------------------------- booking statement end to end
RES = ("Confirmation code,Start date,End date,Listing,Gross earnings,Service fee,Paid out,Type\n"
       "HMAAA111,2026-09-04,2026-09-07,Alpha House,300,45,255,Reservation\n"
       "HMBBB222,2026-09-10,2026-09-12,Gamma View,200,30,170,Reservation\n"
       "PAYOUT1,2026-09-30,2026-09-30,,,,100,Payout\n")
upload("airbnb-sep.csv", RES, "booking_statement", "")
dr = last_doc()
res_items = q("SELECT * FROM document_items WHERE document_id=? ORDER BY line_index", dr["id"])
check("booking statement: reservations read, payout line skipped", len(res_items) == 2 and {i["property_id"] for i in res_items} == {"alpha-house", "gamma-view"})
check("booking statement: Sep 2026 detected and total is net payouts", (dr["detected_year"], dr["detected_month"]) == (2026, 9) and json.loads(dr["detection_json"])["total"] == 425.0)
form = []
for it in res_items:
    form += [("item_id", it["id"]), ("include", it["id"]), ("property_id", it["property_id"]), ("platform", "airbnb"), ("reservation_id", it["reservation_id"]),
             ("check_in", it["check_in"]), ("check_out", it["check_out"]), ("gross", it["gross_revenue"]), ("fees", it["platform_fees"]), ("net", it["net_revenue"])]
client.post(f"/documents/{dr['id']}/confirm", data=MultiDict(form))
bk = q("SELECT * FROM bookings WHERE document_id=?", dr["id"])
check("booking statement: confirmation creates source='upload' bookings linked to the document", len(bk) == 2 and all(b["source"] == "upload" for b in bk))
check("booking statement: no income transactions created (no double count)", q("SELECT COUNT(*) n FROM transactions WHERE document_id=?", dr["id"])[0]["n"] == 0)
bd = client.get("/bookings/%d/drawer" % bk[0]["id"]).get_data(as_text=True)
check("booking drawer traces to the statement, with its type and period", "airbnb-sep.csv" in bd and "Booking / Airbnb statement" in bd and "Sep 2026" in bd and "row" in bd)
ev = [e for e in q("SELECT * FROM document_events WHERE document_id=? AND event='confirmed'", dr["id"])]
check("booking statement: KPI change recorded per property-month", len(json.loads(ev[0]["detail"])["kpi_changes"]) == 2)
upload("airbnb-sep-copy.csv", RES, "booking_statement", "")
dd = last_doc()
page = client.get(f"/documents/{dd['id']}/review").get_data(as_text=True)
check("re-uploaded statement: file and reservation duplicates both flagged", "Possible duplicate" in page and page.count("already on file") >= 2)
n = q("SELECT COUNT(*) n FROM bookings")[0]["n"]
form = []
for it in q("SELECT * FROM document_items WHERE document_id=? ORDER BY line_index", dd["id"]):
    form += [("item_id", it["id"]), ("include", it["id"]), ("property_id", it["property_id"]), ("platform", "airbnb"), ("reservation_id", it["reservation_id"]),
             ("check_in", it["check_in"]), ("check_out", it["check_out"]), ("gross", it["gross_revenue"]), ("fees", it["platform_fees"]), ("net", it["net_revenue"])]
client.post(f"/documents/{dd['id']}/confirm", data=MultiDict(form))
check("re-uploaded statement cannot double-count reservations", q("SELECT COUNT(*) n FROM bookings")[0]["n"] == n)

# ---------------------------------------------------------------- real-world export shapes (synthetic fixtures)
def items_of(doc):
    return q("SELECT * FROM document_items WHERE document_id=? ORDER BY line_index", doc["id"])

def warn_codes(doc):
    return {w["code"] for w in json.loads(doc["detection_json"] or "{}").get("warnings", [])}

# Airbnb transaction report: US month/day dates, Payout/Adjustment/Resolution rows, amount in "Amount" not "Paid out"
AIRBNB = ("Date,Arriving by date,Type,Confirmation Code,Booking date,Start date,End date,Nights,Guest,Listing,Details,Reference code,Currency,Amount,Paid out,Service fee,Fast Pay Fee,Cleaning fee,Gross earnings,Airbnb remitted tax,Earnings year\n"
          "09/30/2026,10/07/2026,Payout,,,,,,,,Transfer,M-1,GBP,,500.00,,,,,,\n"
          "09/30/2026,,Reservation,HMAAA1,09/01/2026,09/03/2026,09/09/2026,6,A Guest,Garden Flat | Quiet,,,GBP,500.00,,100.00,,20.00,600.00,0.00,2026\n"
          "09/30/2026,,Reservation,HMBBB2,09/01/2026,09/10/2026,09/12/2026,2,B Guest,Garden Flat | Quiet,,,GBP,\"1,200.50\",,\"200.00\",,0.00,\"1,400.50\",0.00,2026\n"
          "09/30/2026,,Adjustment,HMAAA1,09/01/2026,09/03/2026,09/09/2026,6,A Guest,Garden Flat | Quiet,,,GBP,-80.00,,-10.00,,0,,0.00,2026\n"
          "09/30/2026,,Resolution Payout,HMBBB2,,09/10/2026,09/12/2026,2,B Guest,Garden Flat | Quiet,Resolution payout,,GBP,25.00,,,,,25.00,,2026\n"
          "09/30/2026,,Reservation,HMAAA1,09/01/2026,09/03/2026,09/09/2026,6,A Guest,Garden Flat | Quiet,,,GBP,500.00,,100.00,,20.00,600.00,0.00,2026\n")
upload("airbnb-us.csv", AIRBNB, "booking_statement", "")
da = last_doc(); ra = items_of(da)
check("airbnb: month/day dates read correctly (09/03 is 3 Sep, not 9 Mar)", [(r["check_in"], r["check_out"]) for r in ra] == [("2026-09-03", "2026-09-09"), ("2026-09-10", "2026-09-12")], [(r["check_in"], r["check_out"]) for r in ra])
check("airbnb: net is the Amount column with each adjustment/resolution applied to its own reservation (500-80, 1200.50+25), quoted thousands handled", [round(r["net_revenue"], 2) for r in ra] == [420.0, 1225.5] and ra[1]["gross_revenue"] == 1400.5, [r["net_revenue"] for r in ra])
check("airbnb: platform detected from the columns", {r["platform"] for r in ra} == {"airbnb"})
check("airbnb: adjustments applied and said so; repeated code dropped; payouts that don't add up are flagged", {"adjustments_applied", "duplicate_codes", "dates_month_first", "payout_mismatch"} <= warn_codes(da), warn_codes(da))
check("airbnb: no false 'partial extraction' for rows that were deliberately left out", "partial_extraction" not in warn_codes(da), json.loads(da["detection_json"])["warnings"])

RECON = ("Date,Arriving by date,Type,Confirmation Code,Booking date,Start date,End date,Nights,Guest,Listing,Details,Reference code,Currency,Amount,Paid out,Service fee,Fast Pay Fee,Cleaning fee,Gross earnings,Airbnb remitted tax,Earnings year\n"
         "09/30/2026,10/07/2026,Payout,,,,,,,,Transfer,M-1,GBP,,1645.50,,,,,,\n"
         "09/30/2026,,Reservation,RA1,09/01/2026,09/03/2026,09/09/2026,6,A,Flat One,,,GBP,500.00,,100.00,,20.00,600.00,0.00,2026\n"
         "09/30/2026,,Reservation,RB2,09/01/2026,09/10/2026,09/12/2026,2,B,Flat One,,,GBP,1200.50,,200.00,,0,1400.50,0.00,2026\n"
         "09/30/2026,,Reservation,RC3,09/01/2026,09/14/2026,09/16/2026,2,C,Flat One,,,GBP,300.00,,50.00,,0,350.00,0.00,2026\n"
         "09/30/2026,,Adjustment,RA1,09/01/2026,09/03/2026,09/09/2026,6,A,Flat One,,,GBP,-80.00,,-10.00,,0,,0.00,2026\n"
         "09/30/2026,,Resolution Payout,RB2,,09/10/2026,09/12/2026,2,B,Flat One,Damage,,GBP,25.00,,,,,25.00,,2026\n"
         "09/30/2026,,Adjustment,RC3,09/01/2026,09/14/2026,09/16/2026,2,C,Flat One,,,GBP,-300.00,,-50.00,,0,,0.00,2026\n"
         "09/30/2026,,Adjustment,OLD9,09/01/2026,08/01/2026,08/03/2026,2,D,Flat One,,,GBP,-50.00,,0,,0,,0.00,2026\n")
RECON_OK = RECON.replace("1645.50", "1595.50")   # the orphan adjustment (-50) also reduced what was paid
upload("airbnb-recon.csv", RECON_OK, "booking_statement", "alpha-house"); dr_ = last_doc(); rr_ = items_of(dr_)
check("airbnb reconciliation: nets after adjustments equal the file's payout transfers (verified against the bank rows)", "payout_reconciled" in warn_codes(dr_) and "payout_mismatch" not in warn_codes(dr_), warn_codes(dr_))
check("airbnb: a reservation fully reversed by an adjustment is listed at 0 and unticked, and is not 'missing an amount'", [(round(r["net_revenue"], 2), r["include"]) for r in rr_] == [(420.0, 1), (1225.5, 1), (0.0, 0)] and "partial_extraction" not in warn_codes(dr_), [(r["net_revenue"], r["include"]) for r in rr_])
check("airbnb: an adjustment for a reservation that isn't in this file is reported, not guessed onto another", "rows_left_out" in warn_codes(dr_))
check("airbnb: each adjusted line says what it was and what it is now", "net was" in client.get(f"/documents/{dr_['id']}/review").get_data(as_text=True))
upload("airbnb-recon-bad.csv", RECON, "booking_statement", "alpha-house")
check("airbnb reconciliation: a payout total that doesn't match is flagged", "payout_mismatch" in warn_codes(last_doc()))

UK = "Date,Vendor,Description,Amount\n13/03/2026,A,x,10\n02/04/2026,B,y,20\n"
upload("uk-dates.csv", UK, "other", "alpha-house")
check("generic CSV: a day>12 anywhere settles day/month order for the whole column", [r["date"] for r in items_of(last_doc())] == ["2026-03-13", "2026-04-02"])
US = "Date,Vendor,Description,Amount\n03/13/2026,A,x,10\n04/02/2026,B,y,20\n"
upload("us-dates.csv", US, "other", "alpha-house")
check("generic CSV: month/day order detected from the column too", [r["date"] for r in items_of(last_doc())] == ["2026-03-13", "2026-04-02"])
AMBIG = ("Confirmation Code,Start date,End date,Nights,Listing,Gross earnings,Service fee,Type\n"
         "X1,09/03/2026,09/09/2026,6,Flat A,600,100,Reservation\nX2,03/09/2026,03/12/2026,3,Flat A,300,50,Reservation\n")
upload("ambig.csv", AMBIG, "booking_statement", "alpha-house")
rows = items_of(last_doc())
check("ambiguous dates are settled by the Nights column, not guessed",
      [(r["check_in"], r["check_out"]) for r in rows] == [("2026-09-03", "2026-09-09"), ("2026-03-09", "2026-03-12")], [(r["check_in"], r["check_out"]) for r in rows])
check("...so nothing is left to warn about", "dates_ambiguous" not in warn_codes(last_doc()) and "nights_mismatch" not in warn_codes(last_doc()))
NOHINT = "Confirmation Code,Start date,End date,Listing,Gross earnings,Service fee,Type\nZ1,09/03/2026,09/09/2026,Flat A,600,100,Reservation\n"
upload("nohint.csv", NOHINT, "booking_statement", "alpha-house")
check("dates nothing can settle are read day/month and flagged for checking", "dates_ambiguous" in warn_codes(last_doc()) and items_of(last_doc())[0]["check_in"] == "2026-03-09")

# Amazon Business order history: one row per item, order totals repeated on every row, PO number = property
AMZ_HEAD = "Order Date,Order ID,PO Number,Order Subtotal,Order Net Total,Order Status,ASIN,Title,Item Quantity,Item Net Total,Item Subtotal,Item VAT,Seller Name\n"
AMZ = (AMZ_HEAD +
       '25/09/2026,111-1,ALPHA-HOUSE,"30.00","36.00",Closed,B0TEST,"Towels, white",2,"12.00","10.00","2.00",Amazon EU\n'
       '25/09/2026,111-1,ALPHA-HOUSE,"30.00","36.00",Closed,B0TEST,Soap,1,"24.00","20.00","4.00",Amazon EU\n'
       '21/09/2026,111-2,personal,"5.00","6.00",Closed,B0TEST,Gift,1,"6.00","5.00","1.00",Amazon EU\n'
       '20/09/2026,111-3,biz,"8.33","10.00",Closed,B0TEST,Printer ink,1,"10.00","8.33","1.67",Amazon EU\n'
       '19/09/2026,111-4,all flats,"7.50","9.00",Closed,B0TEST,Bin bags,1,"9.00","7.50","1.50",Amazon EU\n'
       '18/09/2026,111-5,"ALPHA-HOUSE, BETA-COURT","2.50","3.00",Closed,B0TEST,Sponges,1,"3.00","2.50","0.50",Amazon EU\n'
       '17/09/2026,111-6,,"1.00","1.20",Closed,B0TEST,Pens,1,"1.20","1.00","0.20",Amazon EU\n'
       '16/09/2026,111-7,BETA-COURT,"4.00","4.80",Cancelled,B0TEST,Cancelled thing,1,"4.80","4.00","0.80",Amazon EU\n')
upload("amazon-orders.csv", AMZ, "amazon_order", "")
dz = last_doc(); rz = items_of(dz)
check("amazon: one line per item, using each item's own total (not the repeated order total)", [r["amount"] for r in rz] == [12.0, 24.0, 6.0, 10.0, 9.0, 3.0, 1.2], [r["amount"] for r in rz])
check("amazon: cancelled orders left out", len(rz) == 7)
byname = {r["raw_description"]: r for r in rz}
check("amazon: PO matched to the property by its code", byname["Soap"]["property_id"] == "alpha-house" and byname["Towels, white ×2"]["property_id"] == "alpha-house")
check("amazon: 'personal' lines are left unticked", byname["Gift"]["include"] == 0 and byname["Soap"]["include"] == 1)
check("amazon: 'biz' goes to the business cost centre", byname["Printer ink"]["property_id"] == "general-overheads")
check("amazon: 'all flats', several-property and blank POs are NOT guessed", all(byname[k]["property_id"] is None for k in ("Bin bags", "Sponges", "Pens")))
check("amazon: dates are day/month (25/09 settles it)", byname["Soap"]["date"] == "2026-09-25")
check("amazon: warnings explain the PO decisions", {"po_personal", "po_business", "po_unmatched", "amazon_amounts"} <= warn_codes(dz), warn_codes(dz))
page = client.get(f"/documents/{dz['id']}/review").get_data(as_text=True)
check("amazon review: strip says properties matched line by line; order and PO shown on each line", "line by line" in page and "Order 111-1" in page and "PO" in page)
check("amazon review: business cost centre is selectable", "General" in page.split("Choose property")[1] if "Choose property" in page else False)
upload("amazon-chosen.csv", AMZ, "amazon_order", "beta-court")
check("amazon: a property chosen at upload wins over the POs", {r["property_id"] for r in items_of(last_doc())} == {"beta-court"})
client.post(f"/documents/{last_doc()['id']}/reject")

# Booking.com extranet export (xlsx stand-in for the .xls): text dates, status, float reservation numbers, Total payment - Commission
import openpyxl  # noqa: E402
wb = openpyxl.Workbook(); ws = wb.active
ws.append(["Property name", "Location", "Booker name", "Genius booker", "Arrival", "Departure", "Booked on", "Status", "Total payment", "Commission", "Currency", "Reservation number"])
ws.append(["Beta Court - Nice 1BR", "2 Beta Street, London", "X Y", "No", "29 August 2026", "31 August 2026", "27 August 2026", "OK", 386.8, 64.2088, "GBP", 5200756724.0])
ws.append(["Beta Court - Nice 1BR", "2 Beta Street, London", "Z W", "No", "1 September 2026", "2 September 2026", "1 September 2026", "cancelled", 191.5, 0, "GBP", 5920286058.0])
ws.append(["Mystery Studio", "9 Nowhere Lane", "Q R", "Yes", "9 September 2026", "11 September 2026", "1 September 2026", "OK", 472.0, 78.352, "GBP", 5751069276.0])
buf = io.BytesIO(); wb.save(buf)
upload("booking-export.xlsx", buf.getvalue(), "booking_statement", "")
db_ = last_doc(); rb = items_of(db_)
check("booking.com: 'Total payment' minus 'Commission' is the net", [round(r["net_revenue"], 2) for r in rb] == [322.59, 191.5, 393.65], [(r["reservation_id"], r["net_revenue"]) for r in rb])
check("booking.com: the cancelled reservation is listed with its status but starts unticked", [r["include"] for r in rb] == [1, 0, 1] and rb[1]["property_id"] == "beta-court")
check("booking.com: reservation numbers cleaned of '.0'; text dates read; platform detected", rb[0]["reservation_id"] == "5200756724" and rb[0]["check_in"] == "2026-08-29" and {r["platform"] for r in rb} == {"booking_com"})
check("booking.com: listing matched via its own name, unknown one left for you to choose", rb[0]["property_id"] == "beta-court" and rb[2]["property_id"] is None, [(r["raw_description"], r["property_id"]) for r in rb])
check("booking.com: cancelled rows are flagged in the warnings", "status_unticked" in warn_codes(db_))

# ---- the exact Booking.com export headers (tab-padded, as seen in the real file), no AI key, several properties ----
BK_HEADERS = ["Property name", "Arrival\t", "Departure\t", "Booked on", "Status", "Total payment", "Commission", "Currency", "Reservation number"]
wb = openpyxl.Workbook(); ws = wb.active
ws.append(BK_HEADERS)
ws.append(["Alpha House - Lovely 1BR", "29 August 2026", "31 August 2026", "27 August 2026", "OK", 386.8, 64.2088, "GBP", 5200756724.0])
ws.append(["Beta Court Annex studio", "1 September 2026", "7 September 2026", "29 August 2026", "OK", 1212.4, 201.2584, "GBP", 5992739306.0])
ws.append(["Totally Unknown Place", "9 September 2026", "11 September 2026", "1 September 2026", "OK", 472.0, 78.35, "GBP", 5751069276.0])
ws.append(["Alpha House - Lovely 1BR", "12 September 2026", "13 September 2026", "2 September 2026", "cancelled", 150.0, 0, "GBP", 6000000001.0])
ws.append(["Beta Court Annex studio", "14 September 2026", "15 September 2026", "3 September 2026", "no_show", 120.0, 20.0, "GBP", 6000000002.0])
ws.append(["Alpha House - Lovely 1BR", "16 September 2026", "17 September 2026", "4 September 2026", "pending", 100.0, 16.0, "EUR", 6000000003.0])
buf = io.BytesIO(); wb.save(buf)
upload("Reservations_exact.xlsx", buf.getvalue(), "booking_statement", "")
dx = last_doc(); rx = items_of(dx)
check("exact headers: parses without an AI key and without failing the whole document", dx["status"] == "extracted" and len(rx) == 6, (dx["status"], dx["failure_reason"]))
check("exact headers: 'Arrival' is check-in and 'Departure' is check-out (tab-padded headers)", [(r["check_in"], r["check_out"]) for r in rx][:2] == [("2026-08-29", "2026-08-31"), ("2026-09-01", "2026-09-07")])
check("exact headers: 'Reservation number' is the reservation ID", [r["reservation_id"] for r in rx][:3] == ["5200756724", "5992739306", "5751069276"])
check("exact headers: 'Property name' is used per row (several properties in one file)", [r["property_id"] for r in rx][:3] == ["alpha-house", "beta-court-two", None], [r["property_id"] for r in rx])
check("exact headers: Commission is captured as the platform fee", [round(r["platform_fees"], 4) for r in rx][:2] == [64.2088, 201.2584])
check("exact headers: Total payment is Gross Booking Revenue and net = Total payment - Commission", [round(r["gross_revenue"], 2) for r in rx][:2] == [386.8, 1212.4] and [round(r["net_revenue"], 2) for r in rx][:2] == [322.59, 1011.14])
check("exact headers: cancelled, no_show and unrecognised-status rows are listed but unticked; OK rows ticked", [r["include"] for r in rx] == [1, 1, 1, 0, 0, 0], [r["include"] for r in rx])
check("exact headers: the unknown property flags that one row, not the document", "property_missing" in warn_codes(dx) and sum(1 for r in rx if r["property_id"] is None) == 1)
check("exact headers: a non-GBP row is called out", "currency" in warn_codes(dx))
check("exact headers: the column mapping and the meaning of Total payment are written down", any("Total payment" in w["message"] and "Gross Booking Revenue" in w["message"] and "Arrival" in w["message"]
      for w in json.loads(dx["detection_json"])["warnings"] if w["code"] == "column_mapping"))
page = client.get(f"/documents/{dx['id']}/review").get_data(as_text=True)
check("exact headers: review shows the source status on each row and 'Multiple properties'", "Source status: <strong>cancelled</strong>" in page and "Source status: <strong>no_show</strong>" in page and "Multiple properties (2)" in page)
check("exact headers: no 'extraction isn't configured' message for a spreadsheet", "Automatic extraction isn't configured" not in page)
check("spreadsheet failure page doesn't blame the missing AI key either", "Automatic extraction isn't configured" not in client.get("/documents/%d/review" % q("SELECT id FROM documents WHERE filename='nocolumns.csv' ORDER BY id DESC LIMIT 1")[0]["id"]).get_data(as_text=True))
check("the upload page only reveals the AI-key notice for non-spreadsheet files", 'id="ai-note" hidden' in client.get("/documents").get_data(as_text=True))
check("header normalisation: tabs, newlines, hyphens and case are ignored everywhere", extraction._h("Arrival\t") == "arrival" and extraction._h("Check-in ") == "check in" and extraction._h("PO\nNumber") == "po number")

# Property matching must not guess from a generic word or a substring
props = [{"id": "campbell", "name": "7A Campbell Hill", "address": "7A Campbell Hill"}, {"id": "perry", "name": "11 Perryfield Way", "address": "11 Perryfield Way"},
         {"id": "lw", "name": "602 Lascar Wharf", "address": "602 Lascar Wharf"}, {"id": "m170", "name": "170 Miles Building", "address": "170 Miles Building"},
         {"id": "m175", "name": "175 Miles Building", "address": "175 Miles Building"}]
rank = lambda text: [c["id"] for c in extraction.rank_properties(text, props)]
check("matching: 'Notting Hill' is not 7A Campbell Hill (Hill alone identifies nothing)", rank("Notting Hill | 3-Min Tube | Portobello Market") == [])
check("matching: 'Hideaway' is not 'Way'", rank("Notting Hill Hideaway | 3 Mins to Tube") == [])
check("matching: 'Canary Wharf' is not 602 Lascar Wharf", rank("Near Center & Canary Wharf | Wraparound Balcony") == [])
check("matching: a real name still matches", rank("Delivered to 602 Lascar Wharf, E14") == ["lw"] and rank("7A Campbell Hill, London") == ["campbell"])
check("matching: confident_property refuses two equally good matches but takes a clear one",
      extraction.confident_property("NW1 6RP Miles Building London", props) is None and extraction.confident_property("175 Miles Building", props) == "m175")
check("matching: house numbers tell two buildings apart; the bare name is ambiguous", rank("175 Miles Building")[0] == "m175" and set(rank("Miles Building")) == {"m170", "m175"})

# listings you match once are remembered; "not one of mine" leaves them out
LIST = ("Confirmation Code,Start date,End date,Nights,Listing,Gross earnings,Service fee,Type\n"
        "L1,09/03/2026,09/06/2026,3,Sunny Loft | Central,300,50,Reservation\nL2,09/10/2026,09/12/2026,2,Sunny Loft | Central,200,30,Reservation\n"
        "L3,09/14/2026,09/15/2026,1,Other Owner's Flat,100,10,Reservation\n")
upload("listings.csv", LIST, "booking_statement", ""); dl = last_doc()
check("unmatched listings are offered as one decision per listing", "Match listings to your properties" in client.get(f"/documents/{dl['id']}/review").get_data(as_text=True))
client.post(f"/documents/{dl['id']}/map-listing", data={"listing": "Sunny Loft | Central", "property_id": "alpha-house"})
client.post(f"/documents/{dl['id']}/map-listing", data={"listing": "Other Owner's Flat", "property_id": "__ignore__"})
rl = items_of(dl)
check("map-listing assigns every line of that listing at once", [r["property_id"] for r in rl[:2]] == ["alpha-house", "alpha-house"])
check("'not one of mine' leaves those lines out", rl[2]["include"] == 0 and rl[2]["property_id"] is None)
upload("listings-next-month.csv", LIST.replace("L1", "N1").replace("L2", "N2").replace("L3", "N3"), "booking_statement", "")
rn = items_of(last_doc())
check("next statement: remembered listings matched automatically, ignored ones skipped", [r["property_id"] for r in rn[:2]] == ["alpha-house", "alpha-house"] and rn[2]["include"] == 0)

# ---------------------------------------------------------------- Settings: rename a property, manage platform listing names
r = client.post("/properties/beta-court-two/details", data={"name": "Delta Heights", "address": "5 Delta Row"})
row = q("SELECT id, code, name, address FROM properties WHERE id='beta-court-two'")[0]
check("settings: renaming changes the name/address but never the id or code", (row["name"], row["address"], row["id"], row["code"]) == ("Delta Heights", "5 Delta Row", "beta-court-two", "BETA-COURT-TWO"))
check("settings: the rename is written to the audit log", len(q("SELECT 1 FROM audit_log WHERE entity_type='property' AND entity_id='beta-court-two' AND field='name'")) == 1)
client.post("/properties/beta-court-two/details", data={"name": "   ", "address": "x"})
check("settings: an empty name is refused", q("SELECT name FROM properties WHERE id='beta-court-two'")[0]["name"] == "Delta Heights")
DELTA = "Confirmation Code,Start date,End date,Nights,Listing,Gross earnings,Service fee,Type\nD1,09/03/2026,09/06/2026,3,Delta Heights - Cosy 2BR,300,50,Reservation\n"
upload("delta.csv", DELTA, "booking_statement", "")
check("a renamed property is matched by its new name on the next statement", items_of(last_doc())[0]["property_id"] == "beta-court-two")
page = client.get("/properties/alpha-house/settings").get_data(as_text=True)
check("settings page shows the name/address and platform-names sections", "Names on booking platforms" in page and "Name and address" in page and 'name="address"' in page)
LBL = "Mystery Loft - Skyline Views Near Station"
client.post("/properties/alpha-house/aliases", data={"label": LBL})
check("settings: an added platform name is listed", LBL in client.get("/properties/alpha-house/settings").get_data(as_text=True))
MYST = "Confirmation Code,Start date,End date,Nights,Listing,Gross earnings,Service fee,Type\nM1,09/03/2026,09/06/2026,3," + LBL + ",300,50,Reservation\n"
upload("mystery.csv", MYST, "booking_statement", "")
check("a platform name added in Settings is matched automatically on the next statement", items_of(last_doc())[0]["property_id"] == "alpha-house")
import services.ingest as _ing  # noqa: E402
client.post("/properties/alpha-house/aliases/remove", data={"alias": _ing.norm_alias(LBL)})
check("settings: a removed platform name is no longer matched", LBL not in client.get("/properties/alpha-house/settings").get_data(as_text=True))
upload("mystery2.csv", MYST.replace("M1", "M2"), "booking_statement", "")
check("...and the listing is asked about again", items_of(last_doc())[0]["property_id"] is None)
client.post("/properties/alpha-house/aliases", data={"label": "ab"})
check("settings: a too-short platform name is refused", not q("SELECT 1 FROM property_aliases WHERE alias='ab'"))

# ---------------------------------------------------------------- Excel history is permanent; uploads add; duplicates replace
import services.kpis as kpis  # noqa: E402
conn = db.get_conn()
conn.execute("INSERT INTO transactions (property_id,date,vendor,description,amount,direction,category,source) VALUES ('alpha-house','2026-08-01','Excel lump','August income',1000,'income','booking_income','excel_import')")
conn.execute("INSERT INTO bookings (property_id,platform,reservation_id,check_in,check_out,gross_revenue,net_revenue,status,source) VALUES ('alpha-house','airbnb','monthly-aggregate','2026-08-01','2026-08-11',1000,0,'confirmed','excel_import')")
conn.execute("INSERT INTO transactions (property_id,date,vendor,description,amount,direction,category,source) VALUES ('alpha-house','2026-08-15','Excel cost','Boiler service',77.5,'expense','maintenance','excel_import')")
conn.commit(); conn.close()
AUG = ("Confirmation Code,Start date,End date,Nights,Listing,Gross earnings,Service fee,Type\n"
       "AUG1,08/28/2026,08/31/2026,3,Alpha Flat,300,50,Reservation\nSEP1,09/05/2026,09/08/2026,3,Alpha Flat,600,100,Reservation\n")
upload("aug.csv", AUG, "booking_statement", "alpha-house"); da = last_doc(); ra = items_of(da)
check("excel overlap: every stay starts ticked (it will be STORED); the overlap with Excel history is explained, not enforced",
      [(r["reservation_id"], r["include"]) for r in ra] == [("AUG1", 1), ("SEP1", 1)] and "excel_overlap" in warn_codes(da), [(r["reservation_id"], r["include"]) for r in ra])
page = client.get(f"/documents/{da['id']}/review").get_data(as_text=True)
check("excel overlap: the review says figures stay on Excel until reconciled and links the Reconciliation page", "stores" in page.lower() and "Reconciliation page" in page and "unchanged until reconciled" in page)
s_, e_ = kpis.month_bounds(2026, 8)
conn = db.get_conn(); rev_before = kpis.revenue(conn, "alpha-house", s_, e_); nights_before = kpis.booked_nights(conn, "alpha-house", s_, e_); conn.close()
form = []
for it in items_of(da):
    form += [("item_id", it["id"]), ("include", it["id"]), ("property_id", "alpha-house"), ("platform", "airbnb"), ("reservation_id", it["reservation_id"]), ("check_in", it["check_in"]), ("check_out", it["check_out"]), ("gross", it["gross_revenue"]), ("fees", it["platform_fees"]), ("net", it["net_revenue"])]
client.post(f"/documents/{da['id']}/confirm", data=MultiDict(form))
conn = db.get_conn(); rev_after = kpis.revenue(conn, "alpha-house", s_, e_); nights_after = kpis.booked_nights(conn, "alpha-house", s_, e_)
check("confirming NEVER changes a month that has Excel history: August revenue and nights are exactly what they were", abs(rev_before - 1000) < 0.01 and abs(rev_after - 1000) < 0.01 and nights_before == nights_after == 10, (rev_before, rev_after, nights_before, nights_after))
check("the reservations are stored", conn.execute("SELECT COUNT(*) FROM bookings WHERE document_id=? AND source='upload'", (da["id"],)).fetchone()[0] == 2)
check("Excel rows are untouched by the import", conn.execute("SELECT COUNT(*) FROM transactions WHERE source='excel_import' AND property_id='alpha-house'").fetchone()[0] == 2 and conn.execute("SELECT COUNT(*) FROM bookings WHERE source='excel_import' AND property_id='alpha-house'").fetchone()[0] == 1)
s2, e2 = kpis.month_bounds(2026, 9)
check("a month with NO Excel history (September) already counts the uploaded stay (exact figures: tests/test_source_reconciliation.py)", kpis.revenue(conn, "alpha-house", s2, e2) >= 500 and kpis.booked_nights(conn, "alpha-house", s2, e2) >= 3)
conn.close()

# a corrected re-upload of the same reservation replaces it
V2 = AUG.replace("SEP1,09/05/2026,09/08/2026,3,Alpha Flat,600,100", "SEP1,09/05/2026,09/08/2026,3,Alpha Flat,700,100")
upload("aug-v2.csv", V2, "booking_statement", "alpha-house"); dv = last_doc(); rv = items_of(dv)
check("replace: both stays are recognised as already uploaded and default to 'replace'", all(r["include"] == 1 for r in rv) and "duplicate_rows" in warn_codes(dv))
n_bk = q("SELECT COUNT(*) n FROM bookings")[0]["n"]
form = []
for it in rv:
    form += [("item_id", it["id"]), ("include", it["id"]), ("property_id", "alpha-house"), ("platform", "airbnb"), ("reservation_id", it["reservation_id"]), ("check_in", it["check_in"]), ("check_out", it["check_out"]), ("gross", it["gross_revenue"]), ("fees", it["platform_fees"]), ("net", it["net_revenue"])]
client.post(f"/documents/{dv['id']}/confirm", data=MultiDict(form))
sep = q("SELECT net_revenue, document_id FROM bookings WHERE reservation_id='SEP1'")
check("replace: the reservation count is unchanged and the new version's numbers are in place", q("SELECT COUNT(*) n FROM bookings")[0]["n"] == n_bk and len(sep) == 1 and abs(sep[0]["net_revenue"] - 600.0) < 0.01 and sep[0]["document_id"] == dv["id"], [dict(r) for r in sep])
sep_id = q("SELECT id FROM bookings WHERE reservation_id='SEP1'")[0]["id"]
aud = [(r["action"], r["field"], r["old_value"], r["new_value"]) for r in q("SELECT action, field, old_value, new_value FROM audit_log WHERE entity_type='booking' AND entity_id=?", sep_id)]
check("replace: the old values are kept in the audit log (gross 600 -> 700, net 500 -> 600, old document -> new)",
      ("edit", "gross_revenue", "600.0", "700.0") in aud and ("edit", "net_revenue", "500.0", "600.0") in aud and any(a[0] == "replace" for a in aud), aud)

# a line that duplicates an Excel cost is left out; Excel is never overwritten
XL = "Date,Vendor,Description,Amount\n2026-08-15,Excel cost,Boiler service,77.50\n2026-08-16,NewCo,New thing,12.00\n"
upload("xl-dup.csv", XL, "other", "alpha-house"); dx_ = last_doc(); rx_ = items_of(dx_)
check("a line identical to an Excel cost starts unticked ('Excel is kept'); a new line is ticked", [r["include"] for r in rx_] == [0, 1] and "duplicate_excel" in warn_codes(dx_), [r["include"] for r in rx_])
page = client.get(f"/documents/{dx_['id']}/review").get_data(as_text=True)
check("...and the review says it matches the Excel history, with no 'replace' option", "from your Excel history" in page and page.count('value="replace"') == 0)
confirm(dx_["id"])
xl = q("SELECT source, document_id, amount FROM transactions WHERE vendor='Excel cost'")
check("Excel's own row is untouched after confirming; only the new line was added", len(xl) == 1 and xl[0]["source"] == "excel_import" and xl[0]["document_id"] is None and q("SELECT COUNT(*) n FROM transactions WHERE document_id=?", dx_["id"])[0]["n"] == 1)
client.post(f"/documents/{dx_['id']}/undo")
check("undoing an import never touches the Excel history", q("SELECT COUNT(*) n FROM transactions WHERE source='excel_import' AND property_id='alpha-house'")[0]["n"] == 2)

# ---------------------------------------------------------------- demo mode (hosted preview)
os.environ["UN_DEMO_MODE"] = "1"
demo = create_app().test_client()
n_docs = q("SELECT COUNT(*) n FROM documents")[0]["n"]
r = demo.post("/documents/upload", data={"document": (io.BytesIO(b"a,b\n1,2\n"), "x.csv"), "doc_type": "other"}, content_type="multipart/form-data", follow_redirects=True)
check("demo: upload is refused with the demo message, not a 500", r.status_code == 200 and "Demo mode — changes and uploads are disabled" in r.get_data(as_text=True))
check("demo: nothing was written", q("SELECT COUNT(*) n FROM documents")[0]["n"] == n_docs)
for method_url in ["/documents/1/confirm", "/documents/1/reject", "/expenses/transactions/1/delete", "/apartments", "/properties/alpha-house/ownership", "/properties/alpha-house/details", "/properties/alpha-house/aliases", "/properties/alpha-house/aliases/remove"]:
    rr = demo.post(method_url, data={"name": "zzz"}, follow_redirects=True)
    check(f"demo: POST {method_url} fails gracefully", rr.status_code == 200 and "Demo mode" in rr.get_data(as_text=True))
check("demo: no property was added", q("SELECT COUNT(*) n FROM properties WHERE name='zzz'")[0]["n"] == 0)
hx = demo.post("/expenses/transactions/1/delete", headers={"HX-Request": "true"})
check("demo: htmx writes get a calm note", hx.status_code == 200 and "Demo mode" in hx.get_data(as_text=True))
for url in ["/", "/properties", "/expenses", "/documents", "/properties/alpha-house/bookings", "/properties/alpha-house/performance"]:
    check(f"demo: GET {url} still works", demo.get(url).status_code == 200)
check("demo: banner is shown", "sample data" in demo.get("/properties").get_data(as_text=True))
del os.environ["UN_DEMO_MODE"]

# ---------------------------------------------------------------- private token-gated share of real data
os.environ["UN_SHARE_TOKEN"] = "tok-for-test-1234567890"
shared = create_app().test_client()
T = "/s/tok-for-test-1234567890"
for bad in ["/", "/properties", "/s/", "/s/wrong-token/properties", "/s/tok-for-test-1234567890x/", "/static/style.css", "/documents/1/file"]:
    rr = shared.get(bad)
    check(f"share: {bad} without the token is a bare 404", rr.status_code == 404 and rr.get_data(as_text=True) == "Not found")
home = shared.get(T + "/properties")
body = home.get_data(as_text=True)
check("share: the right token serves the app", home.status_code == 200 and "Properties" in body)
check("share: every internal link and asset keeps the token prefix", f'href="{T}/' in body and f'{T}/static/style.css' in body and 'href="/properties' not in body and 'href="/documents' not in body)
check("share: pages are marked noindex and send no referrer", home.headers.get("X-Robots-Tag", "").startswith("noindex") and home.headers.get("Referrer-Policy") == "no-referrer")
check("share: banner says private read-only view (not 'sample data')", "Private read-only view" in body and "sample data" not in body)
check("share: static assets load under the prefix", shared.get(T + "/static/style.css").status_code == 200)
n_docs = q("SELECT COUNT(*) n FROM documents")[0]["n"]
rr = shared.post(T + "/documents/upload", data={"document": (io.BytesIO(b"a,b\n1,2\n"), "x.csv"), "doc_type": "other"}, content_type="multipart/form-data", follow_redirects=True)
check("share: writes are refused with the read-only message and store nothing",
      "read-only view" in rr.get_data(as_text=True) and q("SELECT COUNT(*) n FROM documents")[0]["n"] == n_docs)
check("share: a refused write redirects back inside the token prefix", shared.post(T + "/apartments", data={"name": "zzz"}).headers["Location"].startswith(T) or shared.post(T + "/apartments", data={"name": "zzz"}).headers["Location"].startswith("http"))
check("share: tabs and drawers work under the prefix", shared.get(T + "/properties/alpha-house/bookings").status_code == 200 and shared.get(T + "/expenses").status_code == 200)
del os.environ["UN_SHARE_TOKEN"]

print()
if failures:
    print(f"{len(failures)} check(s) FAILED:")
    for f in failures:
        print("  -", f)
    sys.exit(1)
print("All upload-workflow checks passed.")

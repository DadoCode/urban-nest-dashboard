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

n_before = q("SELECT COUNT(*) n FROM transactions")[0]["n"]
r = confirm(d2["id"])
check("duplicate document cannot silently double the ledger", q("SELECT COUNT(*) n FROM transactions")[0]["n"] == n_before)
page2 = client.get(f"/documents/{d2['id']}/review").get_data(as_text=True)
check("its lines are flagged Possible duplicate (live check)", page2.count("Possible duplicate") >= 2)
decide = [(f"dup_{it['id']}", "exclude") for it in q("SELECT id FROM document_items WHERE document_id=?", d2["id"])]
confirm(d2["id"], decide)
check("after excluding the duplicates nothing new is added", q("SELECT COUNT(*) n FROM transactions")[0]["n"] == n_before)

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
check("manual duplicate is blocked unless explicitly allowed", q("SELECT COUNT(*) n FROM transactions WHERE document_id=?", dm2["id"])[0]["n"] == 0)
client.post(f"/documents/{dm2['id']}/confirm", data=MultiDict(manual + [("allow_dups", "1")]))
check("'Add even if they look like duplicates' lets it through", q("SELECT COUNT(*) n FROM transactions WHERE document_id=?", dm2["id"])[0]["n"] == 1)

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

# ---------------------------------------------------------------- demo mode (hosted preview)
os.environ["UN_DEMO_MODE"] = "1"
demo = create_app().test_client()
n_docs = q("SELECT COUNT(*) n FROM documents")[0]["n"]
r = demo.post("/documents/upload", data={"document": (io.BytesIO(b"a,b\n1,2\n"), "x.csv"), "doc_type": "other"}, content_type="multipart/form-data", follow_redirects=True)
check("demo: upload is refused with the demo message, not a 500", r.status_code == 200 and "Demo mode — changes and uploads are disabled" in r.get_data(as_text=True))
check("demo: nothing was written", q("SELECT COUNT(*) n FROM documents")[0]["n"] == n_docs)
for method_url in ["/documents/1/confirm", "/documents/1/reject", "/expenses/transactions/1/delete", "/apartments", "/properties/alpha-house/ownership"]:
    rr = demo.post(method_url, data={"name": "zzz"}, follow_redirects=True)
    check(f"demo: POST {method_url} fails gracefully", rr.status_code == 200 and "Demo mode" in rr.get_data(as_text=True))
check("demo: no property was added", q("SELECT COUNT(*) n FROM properties WHERE name='zzz'")[0]["n"] == 0)
hx = demo.post("/expenses/transactions/1/delete", headers={"HX-Request": "true"})
check("demo: htmx writes get a calm note", hx.status_code == 200 and "Demo mode" in hx.get_data(as_text=True))
for url in ["/", "/properties", "/expenses", "/documents", "/properties/alpha-house/bookings", "/properties/alpha-house/performance"]:
    check(f"demo: GET {url} still works", demo.get(url).status_code == 200)
check("demo: banner is shown", "sample data" in demo.get("/properties").get_data(as_text=True))
del os.environ["UN_DEMO_MODE"]

print()
if failures:
    print(f"{len(failures)} check(s) FAILED:")
    for f in failures:
        print("  -", f)
    sys.exit(1)
print("All upload-workflow checks passed.")

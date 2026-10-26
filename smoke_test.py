"""End-to-end smoke test of the SOP workflow: python smoke_test.py  (needs: pip install httpx)"""
import os

os.environ["DATABASE_URL"] = "sqlite:///./smoke_test.db"
os.environ["DEMO_PASSWORD"] = "smoke-pass"

from fastapi.testclient import TestClient  # noqa: E402

import main  # noqa: E402

M_IN = "10000000-0000-4000-8000-000000000007"  # active: used for the in-house path
M_OUT = "10000000-0000-4000-8000-000000000008"  # active: used for the outside path
M_BROKEN = "10000000-0000-4000-8000-000000000001"  # seeded as broken_down


def check(cond, label):
    print(("PASS " if cond else "FAIL ") + label)
    if not cond:
        raise SystemExit(1)


with TestClient(main.app) as c:
    def login(u):
        r = c.post("/api/auth/login", json={"username": u, "password": "smoke-pass"})
        check(r.status_code == 200, f"login {u}")
        return {"Authorization": "Bearer " + r.json()["token"]}

    def call(method, path, who, expect, label, body=None):
        r = c.request(method, path, headers=who, json=body)
        check(r.status_code == expect, f"{label} ({r.status_code})")
        return r.json() if r.content else None

    def upload(ticket, who, kind, data, expect, label, name="doc.pdf"):
        r = c.post(f"/api/tickets/{ticket}/files", params={"kind": kind, "filename": name}, content=data, headers=who)
        check(r.status_code == expect, f"{label} ({r.status_code})")
        return r.json() if r.status_code == 201 else None

    def tickets(who=None):
        return {t["ticket_id"]: t for t in c.get("/api/bootstrap", headers=who or mgmt).json()["tickets"]}

    def machine_status(mid):
        return next(m["status"] for m in c.get("/api/bootstrap", headers=mgmt).json()["machines"] if m["machine_id"] == mid)

    check(c.get("/api/bootstrap").status_code == 401, "bootstrap needs login")
    check(c.post("/api/auth/login", json={"username": "ramesh", "password": "x"}).status_code == 401, "bad password rejected")
    site, deo, mech, pm = login("ramesh"), login("deepak"), login("vikram"), login("meera")
    store, central, mgmt = login("suresh"), login("ananya"), login("rajeshwari")

    # --- SOP 1: reporting ---------------------------------------------------
    call("POST", "/api/tickets/create", central, 403, "central store cannot report", {"machine_id": M_IN})
    call("POST", "/api/tickets/create", mech, 403, "mechanical engineer cannot report", {"machine_id": M_IN})
    t1 = call("POST", "/api/tickets/create", deo, 201, "DEO reports breakdown", {"machine_id": M_IN})["ticket_id"]
    call("POST", "/api/tickets/create", site, 409, "duplicate active breakdown blocked", {"machine_id": M_IN})
    check(machine_status(M_IN) == "broken_down", "machine marked broken")
    check(tickets()[t1]["created_at"].endswith(("Z", "+00:00")), "timestamps carry a timezone")

    # --- SOP 2-6: in-house path with requisition ---------------------------
    inspect = {"issue_description": "Hydraulic hose burst", "repair_venue": "in_house"}
    call("PUT", f"/api/tickets/{t1}/inspect", site, 403, "sr civil engineer cannot inspect", inspect)
    call("PUT", f"/api/tickets/{t1}/clear", mech, 409, "cannot clear before repair")
    call("PUT", f"/api/tickets/{t1}/inspect", mech, 200, "mechanical engineer inspects (in-house)", inspect)
    call("PUT", f"/api/tickets/{t1}/inspect", pm, 409, "cannot inspect twice", inspect)
    call("PUT", f"/api/tickets/{t1}/parts-check", mech, 422, "requisition needs parts list", {"available": False})
    call("PUT", f"/api/tickets/{t1}/parts-check", mech, 200, "parts missing -> requisition", {"available": False, "parts_required": "Hose 1in x2"})
    call("PUT", f"/api/tickets/{t1}/dispatch", mech, 403, "mechanical cannot dispatch", {"dispatch_details": "x", "eta_date": "2030-01-01"})
    call("PUT", f"/api/tickets/{t1}/dispatch", central, 200, "central store dispatches", {"dispatch_details": "Truck MH12-1234", "eta_date": "2030-01-01"})
    call("PUT", f"/api/tickets/{t1}/receive", central, 403, "central store cannot confirm receipt")
    call("PUT", f"/api/tickets/{t1}/receive", store, 200, "site store confirms receipt")
    call("PUT", f"/api/tickets/{t1}/clear", store, 403, "site store cannot clear breakdown")
    call("PUT", f"/api/tickets/{t1}/clear", mech, 200, "mechanical confirms cleared")
    check(machine_status(M_IN) == "active", "machine active after clearance")
    close = {"root_cause": "Worn hose", "repair_cost": 1200.5}
    call("PUT", f"/api/tickets/{t1}/close", mech, 403, "mechanical engineer cannot close register", close)
    call("PUT", f"/api/tickets/{t1}/close", deo, 200, "DEO closes with root cause and cost", close)
    call("PUT", f"/api/tickets/{t1}/close", pm, 409, "cannot close twice", close)
    ev = [e["to_status"] for e in c.get("/api/tickets/%s/events" % t1, headers=mgmt).json()]
    check(ev == ["pending_inspection", "pending_parts_check", "pending_dispatch", "in_transit", "repair_in_progress", "cleared", "closed"], "event log follows SOP")
    check(tickets()[t1]["repair_cost"] == 1200.5 and tickets()[t1]["downtime_hours"] >= 0, "closure record stored")

    # --- SOP 7: outside path with quotation approval -----------------------
    t2 = call("POST", "/api/tickets/create", site, 201, "report second machine", {"machine_id": M_OUT})["ticket_id"]
    call("PUT", f"/api/tickets/{t2}/inspect", pm, 200, "P&M in-charge inspects (outside)", {"issue_description": "Engine overhaul", "repair_venue": "outside"})
    check(tickets()[t2]["status"] == "quotation_requested", "outside inspect waits for vendor quotation")
    call(
        "PUT",
        f"/api/tickets/{t2}/quotation-request",
        mech,
        200,
        "remarks saved while waiting for quotation",
        {"note": "Called Shree Works on 10 Oct; quote expected in 2 days"},
    )
    PDF, PNG = b"%PDF-1.4\n%smoke\n", b"\x89PNG\r\n\x1a\nfakeimage"
    upload(t2, site, "quotation", PDF, 403, "site office cannot attach quotation")
    upload(t2, mech, "quotation", b"just text", 415, "non PDF/JPG/PNG rejected")
    upload(t2, mech, "quotation", b"%PDF" + b"0" * (5 * 1024 * 1024), 413, "file over 5 MB rejected")
    upload(t2, mech, "bill", PDF, 403, "mechanical engineer cannot attach bill")
    qfile = upload(t2, mech, "quotation", PDF, 201, "quotation document attached", name="../evil name?.pdf")
    check(tickets()[t2]["status"] == "pending_quotation", "quotation upload opens vendor quotation stage")
    quote = {"vendor_name": "Shree Works", "quote_amount": 50000}
    call("PUT", f"/api/tickets/{t2}/quotation", mech, 200, "quotation submitted", quote)
    upload(t2, mech, "quotation", PDF, 409, "cannot attach after submission")
    att = tickets()[t2]["files"]
    check(len(att) == 1 and att[0]["filename"] == "evil name_.pdf" and att[0]["kind"] == "quotation", "attachment listed with safe name")
    dl = c.get(f"/api/files/{qfile['file_id']}", headers=mgmt)
    check(dl.status_code == 200 and dl.content == PDF and dl.headers["content-type"] == "application/pdf", "management downloads the quotation")
    check(c.get(f"/api/files/{qfile['file_id']}").status_code == 401, "download needs login")
    call("PUT", f"/api/tickets/{t2}/approval", pm, 403, "only management approves", {"approved": True})
    call("PUT", f"/api/tickets/{t2}/approval", mgmt, 422, "rejection needs a reason", {"approved": False})
    call("PUT", f"/api/tickets/{t2}/approval", mgmt, 200, "management rejects", {"approved": False, "note": "Too high"})
    check(tickets()[t2]["status"] == "pending_quotation" and tickets()[t2]["approval_note"] == "Too high", "rejected quote returns to quotation")
    call("PUT", f"/api/tickets/{t2}/quotation", mech, 200, "revised quotation", {"vendor_name": "Shree Works", "quote_amount": 42000})
    call("PUT", f"/api/tickets/{t2}/approval", mgmt, 200, "management approves", {"approved": True})
    check(tickets()[t2]["status"] == "pending_vendor_dispatch", "approval waits for vendor dispatch")
    call(
        "PUT",
        f"/api/tickets/{t2}/vendor-dispatch",
        pm,
        200,
        "machine sent to vendor",
        {"sent_to_vendor": True, "note": "Low-bed MH-14 to Shree Works, 12 Oct"},
    )
    call("PUT", f"/api/tickets/{t2}/clear", mech, 409, "outside repair uses vehicle receipt not clear")
    call(
        "PUT",
        f"/api/tickets/{t2}/vehicle-receive",
        pm,
        200,
        "vehicle received at site",
        {"note": "Returned on low-bed 18 Oct, test run OK"},
    )
    t3 = call("POST", "/api/tickets/create", site, 201, "can re-report after clearance, before closure", {"machine_id": M_OUT})["ticket_id"]
    close2 = {"root_cause": "Piston wear", "repair_cost": 45000, "invoice_number": "INV-77"}
    call("PUT", f"/api/tickets/{t2}/close", pm, 422, "outside repair needs the vendor bill", close2)
    upload(t3, pm, "bill", PDF, 409, "bill only on cleared outside repairs")
    upload(t2, pm, "bill", PNG, 201, "P&M in-charge attaches bill", name="bill.png")
    call("PUT", f"/api/tickets/{t2}/close", pm, 200, "P&M in-charge closes with bill", close2)
    row2 = tickets()[t2]
    check(row2["cost_over_quote"] == 3000 and row2["invoice_number"] == "INV-77" and len(row2["files"]) == 2, "variance over approved quote flagged")

    # --- SLA and metrics ---------------------------------------------------
    tk = tickets()
    seeded = {k[-3:]: v for k, v in tk.items()}
    check(seeded["101"]["is_overdue"] is True, "3h20m-old inspection is past SLA")
    check(seeded["102"]["is_overdue"] is False and seeded["102"]["sla_due_at"], "40m-old inspection within SLA")
    check(seeded["103"]["is_overdue"] is False, "dispatch within SLA")
    check(tk[t1]["sla_due_at"] is None, "closed ticket has no SLA")
    m = c.get("/api/dashboard/metrics", headers=pm).json()
    check(m["overdue_by_stage"].get("pending_inspection") == 1, "overdue counts by stage")
    check(m["closed_tickets"] == 2 and m["total_repair_cost"] == 46200.5, "closed count and total cost")
    check(m["tickets_by_stage"]["pending_approval"] == 1 and "pending_inspection" in m["stage_average_hours"], "stage counts and averages")
    check(m["average_downtime_hours"] is not None, "average downtime computed")

    # --- demo reset --------------------------------------------------------
    call("POST", "/api/reset-demo", pm, 403, "only management resets")
    call("POST", "/api/reset-demo", mgmt, 200, "management resets")
    check(len(tickets()) == 6, "reset restores seed tickets")

main.engine.dispose()
os.remove("smoke_test.db")
print("ALL PASSED")

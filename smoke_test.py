"""End-to-end smoke test of the SOP workflow: python smoke_test.py  (needs: pip install httpx)"""
import os

if os.path.exists("smoke_test.db"):  # leftover from a run that stopped on a failed check
    os.remove("smoke_test.db")
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
    PDF, PNG = b"%PDF-1.4\n%smoke\n", b"\x89PNG\r\n\x1a\nfakeimage"
    mech_boot = c.get("/api/bootstrap", headers=mech).json()
    check("pending_vendor_dispatch" in mech_boot["task_statuses"], "mech My tasks includes vendor dispatch stage")
    check(
        mech_boot["stage_labels"]["pending_vendor_dispatch"] == "Send machine to vendor",
        "bootstrap carries stage labels",
    )
    check(mech_boot.get("app_version") == main.APP_UI_VERSION, "bootstrap exposes app version for deploy checks")
    st = mech_boot.get("server_time", "")
    check(st.endswith(("Z", "+00:00")) and abs((main._now() - main.datetime.fromisoformat(st)).total_seconds()) < 60, "bootstrap carries timezone-aware server time")
    check(isinstance(mech_boot.get("my_tasks"), list), "bootstrap returns my_tasks queue")

    # --- SOP 1: reporting ---------------------------------------------------
    call("POST", "/api/tickets/create", central, 403, "central store cannot report", {"machine_id": M_IN})
    call("POST", "/api/tickets/create", mech, 403, "mechanical engineer cannot report", {"machine_id": M_IN})
    t1 = call("POST", "/api/tickets/create", deo, 201, "DEO reports breakdown", {"machine_id": M_IN})["ticket_id"]
    call("POST", "/api/tickets/create", site, 409, "duplicate active breakdown blocked", {"machine_id": M_IN})
    check(machine_status(M_IN) == "broken_down", "machine marked broken")

    # --- Board and history are for management and the P&M In-charge only ----
    for who, name in ((site, "Sr civil engineer"), (deo, "DEO"), (mech, "mechanical engineer"), (store, "site store"), (central, "central store")):
        call("GET", f"/api/tickets/{t1}/events", who, 403, f"{name} cannot read ticket history")
    call("GET", f"/api/tickets/{t1}/events", pm, 200, "P&M In-charge reads ticket history")
    call("GET", f"/api/tickets/{t1}/events", mgmt, 200, "management reads ticket history")
    check(c.get("/api/export/events.csv", headers=mech).status_code == 403, "export activity log blocked for mech")
    ev_csv = c.get("/api/export/events.csv", headers=mgmt)
    check(
        ev_csv.status_code == 200
        and ev_csv.headers["content-type"].startswith("text/csv")
        and "event_id" in ev_csv.text,
        "management downloads activity log csv",
    )
    tk_csv = c.get("/api/export/tickets.csv", headers=pm)
    check(tk_csv.status_code == 200 and "ticket_id" in tk_csv.text and t1 in tk_csv.text, "P&M downloads breakdown register csv")
    one_csv = c.get(f"/api/tickets/{t1}/events.csv", headers=mgmt)
    check(one_csv.status_code == 200 and str(t1) in one_csv.text, "single-ticket history csv")
    check(c.get("/api/export/machines.csv", headers=mgmt).status_code == 200, "machine summary csv")
    check(c.get("/api/export/stage-metrics.csv", headers=mgmt).status_code == 200, "stage metrics csv")
    all_ids = set(tickets())
    check(set(tickets(pm)) == all_ids, "P&M In-charge receives every ticket")
    check(tickets(site) == {}, "Sr civil engineer receives no ticket list")
    mech_view = c.get("/api/bootstrap", headers=mech).json()
    check(
        {t["ticket_id"] for t in mech_view["tickets"]} == {t["ticket_id"] for t in mech_view["my_tasks"]}
        and len(mech_view["tickets"]) < len(all_ids),
        "mechanical engineer receives only the tickets waiting on them",
    )
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
    call("PUT", f"/api/tickets/{t1}/dispatch", central, 422, "dispatch needs uploaded documents", {"dispatch_details": "Truck MH12-1234", "eta_date": "2030-01-01"})
    dfile = upload(t1, central, "dispatch", PDF, 201, "central store attaches dispatch challan")
    check(c.delete(f"/api/files/{dfile['file_id']}", headers=mech).status_code == 403, "mech cannot delete dispatch file")
    check(c.delete(f"/api/files/{dfile['file_id']}", headers=central).status_code == 200, "central store removes wrong dispatch file")
    upload(t1, central, "dispatch", PDF, 201, "central store re-attaches dispatch challan")
    call("PUT", f"/api/tickets/{t1}/dispatch", central, 200, "central store dispatches", {"dispatch_details": "Truck MH12-1234", "eta_date": "2030-01-01"})
    call("PUT", f"/api/tickets/{t1}/receive", central, 403, "central store cannot confirm receipt")
    call("PUT", f"/api/tickets/{t1}/receive", mech, 403, "mech cannot confirm receipt")
    check(tickets()[t1]["requisition_by"] == mech_boot["me"]["user_id"], "requisition tied to raiser")  # mech no longer sees t1: it left their queue
    in_transit = [x for x in c.get("/api/bootstrap", headers=store).json()["my_tasks"] if x["ticket_id"] == t1]
    check(len(in_transit) == 1, "in-transit ticket in site store my_tasks")
    call("PUT", f"/api/tickets/{t1}/receive", store, 200, "site store confirms receipt")
    call("PUT", f"/api/tickets/{t1}/clear", store, 403, "site store cannot clear breakdown")
    call("PUT", f"/api/tickets/{t1}/clear", mech, 200, "mechanical confirms cleared")
    check(machine_status(M_IN) == "active", "machine active after clearance")
    check(tickets()[t1]["status"] == "closed", "clearing closes the report automatically")
    raw_events = c.get("/api/tickets/%s/events" % t1, headers=mgmt).json()
    check(raw_events[0]["actor_role"] == "deo" and raw_events[-1]["actor_role"] == "mech_engineer", "events carry the author's role")
    ev = [e["to_status"] for e in raw_events]
    check(ev ==["pending_inspection", "pending_parts_check", "pending_dispatch", "in_transit", "repair_in_progress", "cleared", "closed"], "event log follows SOP")
    check(tickets()[t1]["root_cause"] == "Hydraulic hose burst" and tickets()[t1]["downtime_hours"] >= 0, "inspection findings become the root cause")
    check(
        [e["type"] for e in raw_events]
        == ["breakdown.reported", "inspection.completed_in_house", "parts.requested", "parts.dispatched", "parts.received", "repair.cleared", "record.closed"],
        "events carry stable type codes",
    )
    check(all(e["phase"] and e["tone"] in ("success", "pending", "problem") for e in raw_events), "events carry phase and tone")
    check(raw_events[1]["payload"] == {"venue": "in_house"} and raw_events[1]["note"] == "Hydraulic hose burst", "inspection: clean note plus structured payload")
    check(raw_events[2]["note"] == "Hose 1in x2", "no display prefix is stored in notes")
    check(raw_events[3]["payload"] == {"eta_date": "2030-01-01"} and raw_events[4]["note"] is None, "dispatch keeps its ETA; receipt without an entry has no note")
    check(raw_events[-1]["note"] == "Hydraulic hose burst", "closure event carries the root cause as its note")

    # --- Parts: central store supplies or asks site to buy; wrong parts loop back -----------
    M_ROLLER = "10000000-0000-4000-8000-000000000006"

    def requisition(machine, label):
        tid = call("POST", "/api/tickets/create", site, 201, f"{label}: report", {"machine_id": machine})["ticket_id"]
        call("PUT", f"/api/tickets/{tid}/inspect", mech, 200, f"{label}: inspect", inspect)
        call("PUT", f"/api/tickets/{tid}/parts-check", mech, 200, f"{label}: requisition", {"available": False, "parts_required": "Pump seal kit"})
        return tid

    def stages(tid):
        return [e["to_status"] for e in c.get(f"/api/tickets/{tid}/events", headers=mgmt).json()]

    # A: central store asks the site to purchase; a wrong part is exchanged through the site store
    a = requisition(M_IN, "local purchase")
    call("PUT", f"/api/tickets/{a}/wrong-parts", mech, 409, "wrong parts only once repair has started", {"note": "wrong size"})
    call("PUT", f"/api/tickets/{a}/local-purchase", mech, 409, "cannot order locally before central store asks", {"note": "ordered", "eta_date": "2030-01-01"})
    call("PUT", f"/api/tickets/{a}/request-local-purchase", mech, 403, "mech cannot ask for a local purchase", {"note": "buy at site"})
    call("PUT", f"/api/tickets/{a}/request-local-purchase", central, 422, "local purchase request needs a remark", {"note": ""})
    call("PUT", f"/api/tickets/{a}/request-local-purchase", central, 200, "central store asks site to purchase", {"note": "Not in central stock, buy at site"})
    check(tickets()[a]["status"] == "pending_local_purchase" and tickets()[a]["parts_source"] == "local", "ticket goes back to the mechanical team")
    check(any(x["ticket_id"] == a for x in c.get("/api/bootstrap", headers=mech).json()["my_tasks"]), "local purchase sits in the mechanical queue")
    check(not any(x["ticket_id"] == a for x in c.get("/api/bootstrap", headers=central).json()["my_tasks"]), "and has left the central store queue")
    call("PUT", f"/api/tickets/{a}/dispatch", central, 409, "central store cannot dispatch after handing over", {"dispatch_details": "Dispatched anyway", "eta_date": "2030-01-01"})
    call("PUT", f"/api/tickets/{a}/local-purchase", central, 403, "central store cannot confirm the local order", {"note": "ordered", "eta_date": "2030-01-01"})
    call("PUT", f"/api/tickets/{a}/local-purchase", mech, 200, "mech confirms parts ordered locally", {"note": "Ordered from Patel Hydraulics", "eta_date": "2030-01-01"})
    check(tickets()[a]["status"] == "in_transit" and tickets()[a]["dispatch_details"].startswith("Ordered locally"), "site store now expects the local parts")
    call("PUT", f"/api/tickets/{a}/receive", store, 200, "site store receives and makes the entry", {"note": "Entry GRN-501"})
    call("PUT", f"/api/tickets/{a}/wrong-parts", site, 403, "site office cannot report wrong parts", {"note": "wrong size"})
    call("PUT", f"/api/tickets/{a}/wrong-parts", mech, 422, "wrong parts needs a proper remark", {"note": "no"})
    call("PUT", f"/api/tickets/{a}/wrong-parts", mech, 200, "mech reports wrong part, exchange asked", {"note": "Seal kit is for another model, exchange requested", "eta_date": "2030-01-02"})
    check(tickets()[a]["status"] == "in_transit" and tickets()[a]["parts_issue_note"], "local exchange goes back to site store")
    call("PUT", f"/api/tickets/{a}/receive", store, 200, "site store receives the exchanged part")
    check(tickets()[a]["status"] == "repair_in_progress" and tickets()[a]["parts_issue_note"] is None, "issue note cleared once replaced")
    call("PUT", f"/api/tickets/{a}/clear", mech, 200, "mech installs and clears")
    check(stages(a) == ["pending_inspection", "pending_parts_check", "pending_dispatch", "pending_local_purchase", "in_transit",
                        "repair_in_progress", "in_transit", "repair_in_progress", "cleared"], "event log shows the local purchase and exchange")

    # B: central store supplies; wrong part goes back to central store, then corrected
    b = requisition(M_ROLLER, "central supply")
    upload(b, central, "dispatch", PDF, 201, "central supply: dispatch challan")
    call("PUT", f"/api/tickets/{b}/dispatch", central, 200, "central supply: dispatched", {"dispatch_details": "Dispatched from central store", "eta_date": "2030-01-01"})
    check(tickets()[b]["parts_source"] == "central", "parts source recorded as central")
    call("PUT", f"/api/tickets/{b}/receive", store, 200, "central supply: site store receives")
    call("PUT", f"/api/tickets/{b}/wrong-parts", mech, 200, "mech reports wrong parts, sent back", {"note": "Wrong bearing received, sent back to central store"})
    check(tickets()[b]["status"] == "pending_dispatch" and tickets()[b]["parts_issue_note"], "wrong parts return to central store")
    call("PUT", f"/api/tickets/{b}/dispatch", central, 200, "central store sends the correct item", {"dispatch_details": "Correct bearing dispatched", "eta_date": "2030-02-01"})
    call("PUT", f"/api/tickets/{b}/receive", store, 200, "site store receives the correct item")
    call("PUT", f"/api/tickets/{b}/clear", mech, 200, "mech installs and clears")
    check(stages(b).count("pending_dispatch") == 2 and stages(b)[-1] == "cleared", "event log shows the return to central store")

    # C: parts taken from the site store never had a requisition, so there is nothing to send back
    cc = call("POST", "/api/tickets/create", site, 201, "site-store repair: report", {"machine_id": M_IN})["ticket_id"]
    call("PUT", f"/api/tickets/{cc}/inspect", mech, 200, "site-store repair: inspect", inspect)
    call("PUT", f"/api/tickets/{cc}/parts-check", mech, 200, "site-store repair: parts in stock", {"available": True})
    call("PUT", f"/api/tickets/{cc}/wrong-parts", mech, 409, "no wrong-parts loop without a requisition", {"note": "wrong size part"})
    call("PUT", f"/api/tickets/{cc}/clear", mech, 200, "site-store repair: cleared")

    # --- SOP 7: outside path with quotation approval -----------------------
    t2 = call("POST", "/api/tickets/create", site, 201, "report second machine", {"machine_id": M_OUT})["ticket_id"]
    call("PUT", f"/api/tickets/{t2}/inspect", pm, 200, "P&M in-charge inspects (outside)", {"issue_description": "Engine overhaul", "repair_venue": "outside"})
    check(tickets()[t2]["status"] == "quotation_requested", "outside inspect waits for vendor quotation")
    upload(t2, mech, "quotation", PDF, 409, "vendor quotation only on vendor quotation stage")
    call(
        "PUT",
        f"/api/tickets/{t2}/quotation-request/message",
        mech,
        200,
        "first request remark",
        {"note": "Called Shree Works on 10 Oct"},
    )
    upload(t2, mech, "quotation_request", PDF, 201, "request-support file", name="../evil name?.pdf")
    call(
        "PUT",
        f"/api/tickets/{t2}/quotation-request/message",
        mech,
        200,
        "remark with attachment name",
        {"note": "", "attachment_name": "evil name_.pdf"},
    )
    call("PUT", f"/api/tickets/{t2}/quotation-request/continue", mech, 200, "advance to vendor quotation")
    check(tickets()[t2]["status"] == "pending_quotation", "continue opens vendor quotation stage")
    upload(t2, site, "quotation", PDF, 403, "site office cannot attach quotation")
    upload(t2, mech, "quotation", b"just text", 415, "non PDF/JPG/PNG rejected")
    upload(t2, mech, "quotation", b"%PDF" + b"0" * (5 * 1024 * 1024), 413, "file over 5 MB rejected")
    upload(t2, mech, "bill", PDF, 403, "mechanical engineer cannot attach bill")
    qfile = upload(t2, mech, "quotation", PDF, 201, "quotation document attached")
    quote = {"quote_details": "Engine overhaul per attached quotation from Shree Works"}
    call("PUT", f"/api/tickets/{t2}/quotation", mech, 200, "quotation submitted", quote)
    upload(t2, mech, "quotation", PDF, 409, "cannot attach after submission")
    att = tickets()[t2]["files"]
    kinds = {f["kind"] for f in att}
    check(kinds == {"quotation", "quotation_request"}, "request-support and vendor quotation kept separate")
    check(any(f["filename"] == "evil name_.pdf" and f["kind"] == "quotation_request" for f in att), "request file safe name")
    dl = c.get(f"/api/files/{qfile['file_id']}", headers=mgmt)
    check(dl.status_code == 200 and dl.content == PDF and dl.headers["content-type"] == "application/pdf", "management downloads the quotation")
    check(c.get(f"/api/files/{qfile['file_id']}").status_code == 401, "download needs login")
    call("PUT", f"/api/tickets/{t2}/approval", pm, 403, "only management approves", {"approved": True})
    call("PUT", f"/api/tickets/{t2}/approval", mgmt, 422, "rejection needs a reason", {"approved": False})
    call("PUT", f"/api/tickets/{t2}/approval", mgmt, 200, "management rejects", {"approved": False, "note": "Too high"})
    check(tickets()[t2]["status"] == "pending_quotation" and tickets()[t2]["approval_note"] == "Too high", "rejected quote returns to quotation")
    t2_events = c.get(f"/api/tickets/{t2}/events", headers=mgmt).json()
    rej = [e for e in t2_events if e["type"] == "quotation.rejected"]
    check(len(rej) == 1 and rej[0]["tone"] == "problem" and rej[0]["note"] == "Too high", "rejection is a problem event with the bare reason as its note")
    remarks = [e for e in t2_events if e["type"] == "quotation.remark_added"]
    check(remarks and all(e["from_status"] == e["to_status"] for e in remarks), "remarks are events that keep the same stage")
    check(any(e["payload"].get("attachment") for e in remarks), "remark with a file records the attachment in its payload")
    call(
        "PUT",
        f"/api/tickets/{t2}/quotation",
        mech,
        200,
        "revised quotation",
        {"quote_details": "Revised quotation per attached PDF from Shree Works"},
    )
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
    upload(t3, pm, "bill", PDF, 409, "bill only on cleared outside repairs")
    row2 = tickets()[t2]
    check(row2["status"] == "closed" and row2["root_cause"], "outside repair closes automatically on vehicle receipt")

    # --- SLA and metrics ---------------------------------------------------
    tk = tickets()
    seeded = {k[-3:]: v for k, v in tk.items()}
    check(seeded["101"]["is_overdue"] is True, "3h20m-old inspection is past SLA")
    check(seeded["102"]["is_overdue"] is False and seeded["102"]["sla_due_at"], "40m-old inspection within SLA")
    check(seeded["103"]["is_overdue"] is False, "dispatch within SLA")
    check(tk[t1]["sla_due_at"] is None, "closed ticket has no SLA")
    m = c.get("/api/dashboard/metrics", headers=pm).json()
    check(m["overdue_by_stage"].get("pending_inspection") == 1, "overdue counts by stage")
    check(m["closed_tickets"] == 7, "closed count (incl. 5 seeded past repairs)")
    past = [t for t in tk.values() if t["machine_id"] == M_BROKEN]
    in_year = [t for t in past if (main._now() - main.datetime.fromisoformat(t["created_at"])).days <= 365]
    check(len(past) == 5 and len(in_year) == 4, "seed gives machine 1 four breakdowns in the last year and one older")
    check(m["tickets_by_stage"]["pending_vendor_dispatch"] == 1 and "pending_inspection" in m["stage_average_hours"], "stage counts and averages")
    check(m["average_downtime_hours"] is not None, "average downtime computed")

    # --- old rows (display text in notes, no type) are cleaned up once at startup --------
    with main.SessionLocal() as db:
        legacy_ticket = next(iter(tickets()))
        db.add(main.TicketEvent(ticket_id=legacy_ticket, actor_id=main.U_MECH, from_status="pending_approval",
                                to_status="pending_quotation", note="Rejected: price too high", event_type="x"))
        db.add(main.TicketEvent(ticket_id=legacy_ticket, actor_id=main.U_MECH, from_status="in_transit",
                                to_status="repair_in_progress", note="Spares received at site"))
        db.commit()
        db.query(main.TicketEvent).filter(main.TicketEvent.ticket_id == legacy_ticket, main.TicketEvent.note.in_(
            ["Rejected: price too high", "Spares received at site"])).update({"event_type": None, "phase": None, "tone": None}, synchronize_session=False)
        db.commit()
    main.backfill_event_types()
    legacy = [e for e in c.get(f"/api/tickets/{legacy_ticket}/events", headers=mgmt).json() if e["type"] in ("quotation.rejected", "parts.received")]
    check({(e["type"], e["note"]) for e in legacy} >= {("quotation.rejected", "price too high"), ("parts.received", None)}, "legacy notes are classified and stripped of display text")

    # --- demo reset --------------------------------------------------------
    call("POST", "/api/reset-demo", pm, 403, "only management resets")
    call("POST", "/api/reset-demo", mgmt, 200, "management resets")
    check(len(tickets()) == 11, "reset restores seed tickets")

main.engine.dispose()
os.remove("smoke_test.db")
print("ALL PASSED")

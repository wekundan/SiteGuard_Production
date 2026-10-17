"""End-to-end smoke test: python smoke_test.py  (needs: pip install httpx)"""
import os

os.environ["DATABASE_URL"] = "sqlite:///./smoke_test.db"
os.environ["DEMO_PASSWORD"] = "smoke-pass"

from fastapi.testclient import TestClient  # noqa: E402

import main  # noqa: E402

M_BUSY = "10000000-0000-4000-8000-000000000001"  # seeded as broken_down
M_FREE = "10000000-0000-4000-8000-000000000004"  # seeded as active


def check(cond, label):
    print(("PASS " if cond else "FAIL ") + label)
    if not cond:
        raise SystemExit(1)


with TestClient(main.app) as c:
    def login(u):
        r = c.post("/api/auth/login", json={"username": u, "password": "smoke-pass"})
        check(r.status_code == 200, f"login {u}")
        return {"Authorization": "Bearer " + r.json()["token"]}

    check(c.get("/api/bootstrap").status_code == 401, "bootstrap needs login")
    check(c.post("/api/auth/login", json={"username": "ramesh", "password": "x"}).status_code == 401, "bad password rejected")

    site, sup, ho, mgmt = login("ramesh"), login("vikram"), login("ananya"), login("rajeshwari")

    check(c.post("/api/tickets/create", json={"machine_id": M_FREE}, headers=ho).status_code == 403, "HO cannot report")
    r = c.post("/api/tickets/create", json={"machine_id": M_FREE}, headers=site)
    check(r.status_code == 201, "site office reports breakdown")
    tid = r.json()["ticket_id"]
    check(c.post("/api/tickets/create", json={"machine_id": M_FREE}, headers=site).status_code == 409, "duplicate open ticket blocked")

    body = {"issue_description": "Hose burst", "resolution_type": "head_office"}
    check(c.put(f"/api/tickets/{tid}/inspect", json=body, headers=site).status_code == 403, "site office cannot inspect")
    check(c.put(f"/api/tickets/{tid}/resolve", headers=sup).status_code == 409, "cannot resolve from pending_inspection")
    check(c.put(f"/api/tickets/{tid}/inspect", json=body, headers=sup).status_code == 200, "supervisor escalates")
    check(c.put(f"/api/tickets/{tid}/inspect", json=body, headers=sup).status_code == 409, "cannot inspect twice")

    po = {"po_details": "PO-1", "eta_date": "2030-01-01"}
    check(c.put(f"/api/tickets/{tid}/ho-update", json=po, headers=sup).status_code == 403, "supervisor cannot raise PO")
    check(c.put(f"/api/tickets/{tid}/ho-update", json=po, headers=ho).status_code == 200, "HO raises PO")
    check(c.put(f"/api/tickets/{tid}/resolve", headers=sup).status_code == 200, "supervisor resolves")
    check(c.put(f"/api/tickets/{tid}/resolve", headers=sup).status_code == 409, "cannot resolve twice")

    ev = c.get(f"/api/tickets/{tid}/events", headers=mgmt).json()
    check([e["to_status"] for e in ev] == ["pending_inspection", "pending_ho_action", "pending_arrival", "resolved"], "event log complete")

    seeded = {t["ticket_id"][-3:]: t for t in c.get("/api/bootstrap", headers=mgmt).json()["tickets"]}
    check(seeded["101"]["is_overdue"] is True, "3h20m-old inspection is past 2h SLA")
    check(seeded["102"]["is_overdue"] is False and seeded["102"]["sla_due_at"], "40m-old inspection within SLA")
    check(seeded["103"]["is_overdue"] is False, "HO ticket within 24h SLA")
    check(seeded[tid[-3:]]["sla_due_at"] is None, "resolved ticket has no SLA")

    r = c.post("/api/tickets/create", json={"machine_id": M_FREE}, headers=site)
    t2 = r.json()["ticket_id"]
    local = {"issue_description": "Belt", "resolution_type": "local", "eta_date": "2000-01-01"}
    check(c.put(f"/api/tickets/{t2}/inspect", json=local, headers=sup).status_code == 200, "local sourcing with past ETA")
    t2row = next(t for t in c.get("/api/bootstrap", headers=mgmt).json()["tickets"] if t["ticket_id"] == t2)
    check(t2row["is_overdue"] is True, "local part past ETA is overdue")

    m = c.get("/api/dashboard/metrics", headers=mgmt).json()
    check(m["overdue_by_stage"].get("pending_inspection") == 1 and m["overdue_by_stage"].get("pending_local_part") == 1, "overdue counts by stage")
    check("pending_inspection" in m["stage_average_hours"], "stage averages computed")
    check(c.post("/api/reset-demo", headers=sup).status_code == 403, "only management resets")
    check(c.post("/api/reset-demo", headers=mgmt).status_code == 200, "management resets")
    check(len(c.get("/api/bootstrap", headers=mgmt).json()["tickets"]) == 3, "reset restores seed tickets")

main.engine.dispose()
os.remove("smoke_test.db")
print("ALL PASSED")

"""
SiteGuard Machinery Breakdown Tracker - FastAPI backend (SOP workflow)
=====================================================================
- PostgreSQL via DATABASE_URL (Render/Neon/Supabase/local); SQLite fallback if unset.
- Creates tables and seeds demo users, machines and tickets on first startup.
- Workflow follows the plant breakdown SOP: report -> inspect -> in-house (site store check,
  requisition, central store dispatch, receipt) or outside (quotation, management approval)
  -> repair -> cleared -> closure record. Every status change is logged in ticket_events.
- Serves the REST API (`/api/...`) and the mobile web app at `/`.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
import uuid
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from enum import Enum
from pathlib import Path as FilePath
from typing import Callable, Generator, List, Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import (
    Date,
    DateTime,
    Enum as SAEnum,
    Float,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    create_engine,
    func,
    inspect,
    text,
)
from sqlalchemy.exc import DataError, IntegrityError, OperationalError, StatementError
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    Session,
    mapped_column,
    relationship,
    selectinload,
    sessionmaker,
)

# ============================================================================
# 1. Database configuration
# ============================================================================

raw_db_url = os.getenv("DATABASE_URL", "sqlite:///./siteguard_local.db")
if raw_db_url.startswith("postgres://"):
    DATABASE_URL = raw_db_url.replace("postgres://", "postgresql+psycopg2://", 1)
elif raw_db_url.startswith("postgresql://"):
    DATABASE_URL = raw_db_url.replace("postgresql://", "postgresql+psycopg2://", 1)
else:
    DATABASE_URL = raw_db_url

connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}
engine = create_engine(DATABASE_URL, pool_pre_ping=True, connect_args=connect_args)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


class Base(DeclarativeBase):
    pass


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# ============================================================================
# 2. Domain enums, workflow rules, SLA
# ============================================================================


class UserRole(str, Enum):
    SITE_OFFICE = "site_office"  # Senior Civil Engineer: reports breakdowns
    DEO = "deo"  # P&M Data Entry Operator: reports, closes the register entry
    MECH_ENGINEER = "mech_engineer"  # Mechanical Engineer: inspects, repairs
    PM_INCHARGE = "pm_incharge"  # P&M In-charge: inspects, repairs, closes
    SITE_STORE = "site_store"  # site store stock check (receipt is mech engineer / P&M)
    CENTRAL_STORE = "central_store"  # dispatches spares to site
    MANAGEMENT = "management"  # approves outside-repair quotations, oversight


class MachineStatus(str, Enum):
    ACTIVE = "active"
    BROKEN_DOWN = "broken_down"


class TicketStatus(str, Enum):
    PENDING_INSPECTION = "pending_inspection"
    PENDING_PARTS_CHECK = "pending_parts_check"  # in-house: mechanical team checks site store
    PENDING_DISPATCH = "pending_dispatch"  # requisition raised, central store to dispatch
    IN_TRANSIT = "in_transit"  # dispatched; requisition raiser (mech) confirms receipt
    QUOTATION_REQUESTED = "quotation_requested"  # outside: waiting for the vendor to send a quotation
    PENDING_QUOTATION = "pending_quotation"  # outside: quotation received, details to be submitted
    PENDING_APPROVAL = "pending_approval"  # management approves quotation
    PENDING_VENDOR_DISPATCH = "pending_vendor_dispatch"  # outside: mech logs vehicle sent to vendor
    VENDOR_REPAIR_IN_PROGRESS = "vendor_repair_in_progress"  # outside: repair at vendor workshop
    REPAIR_IN_PROGRESS = "repair_in_progress"  # in-house repair at site
    CLEARED = "cleared"  # breakdown cleared, closure record pending
    CLOSED = "closed"


class RepairVenue(str, Enum):
    IN_HOUSE = "in_house"
    OUTSIDE = "outside"


S = TicketStatus
ALLOWED_TRANSITIONS = {
    S.PENDING_INSPECTION: {S.PENDING_PARTS_CHECK, S.QUOTATION_REQUESTED},
    S.QUOTATION_REQUESTED: {S.PENDING_QUOTATION},  # engineer confirms request remarks + quotation file
    S.PENDING_PARTS_CHECK: {S.REPAIR_IN_PROGRESS, S.PENDING_DISPATCH},
    S.PENDING_DISPATCH: {S.IN_TRANSIT},
    S.IN_TRANSIT: {S.REPAIR_IN_PROGRESS},
    S.PENDING_QUOTATION: {S.PENDING_APPROVAL},
    S.PENDING_APPROVAL: {S.PENDING_VENDOR_DISPATCH, S.PENDING_QUOTATION},  # approve / reject
    S.PENDING_VENDOR_DISPATCH: {S.VENDOR_REPAIR_IN_PROGRESS},
    S.VENDOR_REPAIR_IN_PROGRESS: {S.CLEARED},
    S.REPAIR_IN_PROGRESS: {S.CLEARED},
    S.CLEARED: {S.CLOSED},
    S.CLOSED: set(),
}


def as_utc(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


# Hours allowed in each stage before it counts as overdue (placeholders; override with env
# vars such as SLA_PENDING_INSPECTION_HOURS=2).
_SLA_DEFAULTS = {
    S.PENDING_INSPECTION: 2,
    S.PENDING_PARTS_CHECK: 4,
    S.PENDING_DISPATCH: 24,
    S.IN_TRANSIT: 48,
    S.QUOTATION_REQUESTED: 48,
    S.PENDING_QUOTATION: 24,
    S.PENDING_APPROVAL: 24,
    S.PENDING_VENDOR_DISPATCH: 24,
    S.VENDOR_REPAIR_IN_PROGRESS: 72,
    S.REPAIR_IN_PROGRESS: 72,
    S.CLEARED: 24,
}
SLA_HOURS = {s: float(os.getenv(f"SLA_{s.name}_HOURS", h)) for s, h in _SLA_DEFAULTS.items()}
# In these stages an ETA date, when given, replaces the hour limit (due at end of that day).
ETA_BASED_STAGES = {S.IN_TRANSIT, S.REPAIR_IN_PROGRESS, S.VENDOR_REPAIR_IN_PROGRESS}
SITE_TZ = timezone(timedelta(minutes=int(os.getenv("SITE_UTC_OFFSET_MINUTES", "330"))))  # IST


def sla_due_at(status_: TicketStatus, stage_entered_at: datetime, eta_date: Optional[date]) -> Optional[datetime]:
    if status_ not in SLA_HOURS:
        return None
    if status_ in ETA_BASED_STAGES and eta_date:
        end_of_eta = datetime.combine(eta_date + timedelta(days=1), datetime.min.time(), tzinfo=SITE_TZ)
        return end_of_eta.astimezone(timezone.utc)
    return as_utc(stage_entered_at) + timedelta(hours=SLA_HOURS[status_])


# ============================================================================
# 3. Auth helpers (stdlib only): PBKDF2 passwords + HMAC-signed bearer tokens
# ============================================================================

SECRET_KEY = os.getenv("SECRET_KEY", "dev-insecure-secret-change-me").encode()
TOKEN_TTL_SECONDS = 12 * 3600
DEMO_PASSWORD = os.getenv("DEMO_PASSWORD") or "siteguard123"  # `or`: blank env var falls back too
APP_UI_VERSION = os.getenv("APP_UI_VERSION", "3.2.9")


def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), 200_000).hex()
    return f"{salt}${digest}"


def verify_password(password: str, stored: str) -> bool:
    salt, digest = stored.split("$", 1)
    candidate = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), 200_000).hex()
    return hmac.compare_digest(candidate, digest)


def make_token(user_id: str) -> str:
    payload = base64.urlsafe_b64encode(
        json.dumps({"uid": user_id, "exp": int(time.time()) + TOKEN_TTL_SECONDS}).encode()
    ).decode()
    sig = hmac.new(SECRET_KEY, payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}.{sig}"


def read_token(token: str) -> Optional[str]:
    try:
        payload, sig = token.rsplit(".", 1)
        expected = hmac.new(SECRET_KEY, payload.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expected):
            return None
        data = json.loads(base64.urlsafe_b64decode(payload.encode()))
        return data["uid"] if data["exp"] > time.time() else None
    except Exception:
        return None


# ============================================================================
# 4. ORM models
# ============================================================================


def _enum(enum_cls, name):
    return SAEnum(enum_cls, name=name, values_callable=lambda e: [m.value for m in e])


def _uuid() -> str:
    return str(uuid.uuid4())


def _now() -> datetime:
    return datetime.now(timezone.utc)


class User(Base):
    __tablename__ = "users"

    user_id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    username: Mapped[str] = mapped_column(String(50), unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[UserRole] = mapped_column(_enum(UserRole, "user_role"), nullable=False)


class Machine(Base):
    __tablename__ = "machines"

    machine_id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    location: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[MachineStatus] = mapped_column(
        _enum(MachineStatus, "machine_status"), nullable=False, default=MachineStatus.ACTIVE
    )


class Ticket(Base):
    __tablename__ = "tickets"
    # DB-level guarantee: one active breakdown per machine (blocks race conditions). A machine can
    # be reported again once its previous breakdown is cleared, even if the register entry is open.
    __table_args__ = (
        Index(
            "uq_one_active_breakdown_per_machine",
            "machine_id",
            unique=True,
            postgresql_where=text("status NOT IN ('cleared', 'closed')"),
            sqlite_where=text("status NOT IN ('cleared', 'closed')"),
        ),
    )

    ticket_id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    machine_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("machines.machine_id", ondelete="RESTRICT"), nullable=False, index=True
    )
    reported_by: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.user_id", ondelete="RESTRICT"), nullable=False, index=True
    )
    status: Mapped[TicketStatus] = mapped_column(
        _enum(TicketStatus, "ticket_status"),
        nullable=False,
        default=TicketStatus.PENDING_INSPECTION,
        index=True,
    )
    issue_description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    repair_venue: Mapped[Optional[RepairVenue]] = mapped_column(_enum(RepairVenue, "repair_venue"), nullable=True)
    # in-house path
    parts_required: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    requisition_by: Mapped[Optional[str]] = mapped_column(
        String(36), ForeignKey("users.user_id", ondelete="SET NULL"), nullable=True, index=True
    )
    dispatch_details: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # outside path
    vendor_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    quote_amount: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    quote_details: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    approval_note: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    quotation_request_note: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    vendor_dispatch_note: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    vehicle_received_note: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # ETA of the current stage (spares arrival or vendor completion)
    eta_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    # closure record
    root_cause: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    repair_cost: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    closure_notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    invoice_number: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now, index=True)
    stage_entered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now)
    inspected_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    cleared_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    closed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    machine: Mapped["Machine"] = relationship("Machine")
    reporter: Mapped["User"] = relationship("User", foreign_keys=[reported_by])
    files: Mapped[List["TicketFile"]] = relationship("TicketFile", order_by="TicketFile.uploaded_at")


class TicketFile(Base):
    """Vendor quotation / bill attachments, stored in the database (see storage notes in README)."""

    __tablename__ = "ticket_files"

    file_id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    ticket_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("tickets.ticket_id", ondelete="CASCADE"), nullable=False, index=True
    )
    kind: Mapped[str] = mapped_column(String(20), nullable=False)  # "quotation" | "bill"
    filename: Mapped[str] = mapped_column(String(150), nullable=False)
    content_type: Mapped[str] = mapped_column(String(50), nullable=False)
    size: Mapped[int] = mapped_column(Integer, nullable=False)
    data: Mapped[bytes] = mapped_column(LargeBinary, nullable=False, deferred=True)  # not loaded with listings
    uploaded_by: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.user_id", ondelete="RESTRICT"), nullable=False
    )
    uploaded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now)


class TicketEvent(Base):
    """Append-only audit log: one row per ticket state change."""

    __tablename__ = "ticket_events"

    event_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ticket_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("tickets.ticket_id", ondelete="CASCADE"), nullable=False, index=True
    )
    actor_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.user_id", ondelete="RESTRICT"), nullable=False
    )
    from_status: Mapped[Optional[str]] = mapped_column(String(30), nullable=True)
    to_status: Mapped[str] = mapped_column(String(30), nullable=False)
    note: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now)


# ============================================================================
# 5. Seed data and startup
# ============================================================================

U_SITE = "11111111-1111-4111-8111-111111111111"
U_DEO = "22222222-2222-4222-8222-222222222222"
U_MECH = "33333333-3333-4333-8333-333333333333"
U_CENTRAL = "44444444-4444-4444-8444-444444444444"
U_MGMT = "55555555-5555-4555-8555-555555555555"
U_PM = "66666666-6666-4666-8666-666666666666"
U_STORE = "77777777-7777-4777-8777-777777777777"


def _m(n: int) -> str:
    return f"10000000-0000-4000-8000-{n:012d}"


def seed_initial_data(db: Session) -> None:
    if db.query(User).count() > 0:
        return

    now = _now()
    ago = lambda **kw: now - timedelta(**kw)  # noqa: E731
    pw = lambda: hash_password(DEMO_PASSWORD)  # noqa: E731
    users = [
        User(user_id=U_SITE, name="Ramesh Kumar (Sr Civil Engineer)", username="ramesh", password_hash=pw(), role=UserRole.SITE_OFFICE),
        User(user_id=U_DEO, name="Deepak Verma (P&M DEO)", username="deepak", password_hash=pw(), role=UserRole.DEO),
        User(user_id=U_MECH, name="Vikram Singh (Mechanical Engineer)", username="vikram", password_hash=pw(), role=UserRole.MECH_ENGINEER),
        User(user_id=U_PM, name="Meera Joshi (P&M In-charge)", username="meera", password_hash=pw(), role=UserRole.PM_INCHARGE),
        User(user_id=U_STORE, name="Suresh Patil (Site Store)", username="suresh", password_hash=pw(), role=UserRole.SITE_STORE),
        User(user_id=U_CENTRAL, name="Ananya Nair (Central Store)", username="ananya", password_hash=pw(), role=UserRole.CENTRAL_STORE),
        User(user_id=U_MGMT, name="Rajeshwari Iyer (Management)", username="rajeshwari", password_hash=pw(), role=UserRole.MANAGEMENT),
    ]
    B, A = MachineStatus.BROKEN_DOWN, MachineStatus.ACTIVE
    machines = [
        Machine(machine_id=_m(1), name="Excavator 3000 (CAT 320D)", location="Site A - Zone 1", status=B),
        Machine(machine_id=_m(2), name="Tower Crane TC-6015", location="Site A - Zone 2", status=B),
        Machine(machine_id=_m(3), name="Transit Mixer TM-09", location="Site B - Batching Plant", status=B),
        Machine(machine_id=_m(4), name="Backhoe Loader JCB-3DX", location="Site B - Utility Trench", status=B),
        Machine(machine_id=_m(5), name="Motor Grader MG-140", location="Site A - Access Road", status=B),
        Machine(machine_id=_m(6), name="Vibro Roller VR-210", location="Site A - Embankment", status=A),
        Machine(machine_id=_m(7), name="Concrete Boom Placer BP-42", location="Site B - Podium Slab", status=A),
        Machine(machine_id=_m(8), name="Wheel Loader WL-950", location="Site A - Stockpile", status=A),
    ]

    tickets, events = [], []

    def add(n: int, machine: int, status_: TicketStatus, steps, **fields):
        """steps: [(actor, to_status, hours_ago)] oldest first; first step is the report."""
        tid = f"90000000-0000-4000-8000-{n:012d}"
        prev = None
        for actor, to, hrs in steps:
            events.append(TicketEvent(ticket_id=tid, actor_id=actor, from_status=prev, to_status=to, created_at=ago(hours=hrs)))
            prev = to
        tickets.append(
            Ticket(
                ticket_id=tid,
                machine_id=_m(machine),
                reported_by=steps[0][0],
                status=status_,
                created_at=ago(hours=steps[0][2]),
                stage_entered_at=ago(hours=steps[-1][2]),
                **fields,
            )
        )

    PI, PPC, PD = "pending_inspection", "pending_parts_check", "pending_dispatch"
    QR, PQ, PA, PVD, RIP, CL = (
        "quotation_requested",
        "pending_quotation",
        "pending_approval",
        "pending_vendor_dispatch",
        "repair_in_progress",
        "cleared",
    )
    add(101, 1, S.PENDING_INSPECTION, [(U_SITE, PI, 3.3)])  # overdue
    add(102, 2, S.PENDING_INSPECTION, [(U_SITE, PI, 0.7)])
    add(
        103, 3, S.PENDING_DISPATCH,
        [(U_DEO, PI, 7), (U_MECH, PPC, 6.5), (U_MECH, PD, 5)],
        issue_description="Main planetary drum gearbox bearing seized.",
        repair_venue=RepairVenue.IN_HOUSE,
        parts_required="OEM planetary gearbox bearing assembly x1 (not in site store)",
        requisition_by=U_MECH,
        inspected_at=ago(hours=6.5),
    )
    add(
        104, 4, S.PENDING_VENDOR_DISPATCH,
        [(U_SITE, PI, 10), (U_PM, QR, 9.5), (U_PM, PQ, 9), (U_PM, PA, 3), (U_MGMT, PVD, 2)],
        issue_description="Stabiliser hydraulic cylinder leaking; seals and rod damaged.",
        repair_venue=RepairVenue.OUTSIDE,
        vendor_name="Shree Hydraulics, Pune",
        quote_amount=48500.0,
        quote_details="Rebuild cylinder, new seal kit, 5 working days",
        eta_date=date.today() + timedelta(days=5),
        inspected_at=ago(hours=9),
    )
    add(
        105, 5, S.REPAIR_IN_PROGRESS,
        [(U_SITE, PI, 14), (U_MECH, PPC, 13), (U_MECH, RIP, 12)],
        issue_description="Tandem drive chain snapped; link kit in site store.",
        repair_venue=RepairVenue.IN_HOUSE,
        inspected_at=ago(hours=13),
    )
    add(
        106, 6, S.CLEARED,
        [(U_SITE, PI, 28), (U_MECH, PPC, 27), (U_MECH, RIP, 26), (U_MECH, CL, 20)],
        issue_description="Alternator belt tensioner pulley failed; replaced from site store.",
        repair_venue=RepairVenue.IN_HOUSE,
        inspected_at=ago(hours=27),
        cleared_at=ago(hours=20),
    )

    db.add_all(users)
    db.add_all(machines)
    db.flush()  # parents must exist before dependent rows
    db.add_all(tickets)
    db.flush()
    db.add_all(events)
    db.commit()


def check_schema() -> None:
    """Fail fast with a clear message if the DB still has the pre-SOP table layout."""
    insp = inspect(engine)
    if insp.has_table("tickets") and "repair_venue" not in {c["name"] for c in insp.get_columns("tickets")}:
        raise RuntimeError(
            "Database has an old SiteGuard schema (missing repair_venue on tickets). "
            "Render/GitHub deploy will fail until DATABASE_URL points at a fresh Neon database, "
            "or you run DROP SCHEMA public CASCADE; CREATE SCHEMA public; in the Neon SQL editor."
        )


def add_missing_columns() -> None:
    """Tiny forward migration for databases created before a column existed (create_all skips those)."""
    insp = inspect(engine)
    if not insp.has_table("tickets"):
        return
    cols = {c["name"] for c in insp.get_columns("tickets")}
    with engine.begin() as conn:
        if "invoice_number" not in cols:
            conn.execute(text("ALTER TABLE tickets ADD COLUMN invoice_number VARCHAR(100)"))
        if "quotation_request_note" not in cols:
            conn.execute(text("ALTER TABLE tickets ADD COLUMN quotation_request_note TEXT"))
        if "vendor_dispatch_note" not in cols:
            conn.execute(text("ALTER TABLE tickets ADD COLUMN vendor_dispatch_note TEXT"))
        if "vehicle_received_note" not in cols:
            conn.execute(text("ALTER TABLE tickets ADD COLUMN vehicle_received_note TEXT"))
        if "requisition_by" not in cols:
            conn.execute(text("ALTER TABLE tickets ADD COLUMN requisition_by VARCHAR(36)"))


def _requisition_raiser_from_events(db: Session, ticket_id: str) -> Optional[str]:
    """Who moved the ticket to pending_dispatch (parts requisition)."""
    ev = (
        db.query(TicketEvent)
        .filter(TicketEvent.ticket_id == ticket_id, TicketEvent.to_status == S.PENDING_DISPATCH.value)
        .order_by(TicketEvent.created_at.desc(), TicketEvent.event_id.desc())
        .first()
    )
    return ev.actor_id if ev else None


def backfill_requisition_by() -> None:
    """Tickets requisitioned before requisition_by existed stay routed to site store otherwise."""
    if not inspect(engine).has_table("tickets") or not inspect(engine).has_table("ticket_events"):
        return
    with SessionLocal() as db:
        open_req = (
            db.query(Ticket)
            .filter(
                Ticket.requisition_by.is_(None),
                Ticket.status.in_((S.PENDING_DISPATCH, S.IN_TRANSIT)),
            )
            .all()
        )
        if not open_req:
            return
        for ticket in open_req:
            raiser = _requisition_raiser_from_events(db, ticket.ticket_id)
            if raiser:
                ticket.requisition_by = raiser
        db.commit()


def sync_postgres_enum_labels(type_name: str, labels: list[str]) -> None:
    """create_all does not add new values to an existing PostgreSQL ENUM type."""
    if "postgresql" not in DATABASE_URL:
        return
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT e.enumlabel FROM pg_enum e "
                "JOIN pg_type t ON e.enumtypid = t.oid "
                "WHERE t.typname = :typ"
            ),
            {"typ": type_name},
        ).fetchall()
        conn.commit()
    if not rows:
        return
    existing = {r[0] for r in rows}
    # Must use a separate engine with AUTOCOMMIT; cannot change isolation on a connection mid-transaction.
    autocommit_engine = engine.execution_options(isolation_level="AUTOCOMMIT")
    for label in labels:
        if label in existing:
            continue
        with autocommit_engine.connect() as conn:
            conn.execute(text(f"ALTER TYPE {type_name} ADD VALUE '{label}'"))
        existing.add(label)


@asynccontextmanager
async def lifespan(app: FastAPI):
    check_schema()
    Base.metadata.create_all(bind=engine)
    sync_postgres_enum_labels("ticket_status", [s.value for s in TicketStatus])
    sync_postgres_enum_labels("user_role", [r.value for r in UserRole])
    sync_postgres_enum_labels("repair_venue", [v.value for v in RepairVenue])
    add_missing_columns()
    migrate_legacy_user_roles()
    backfill_requisition_by()
    with SessionLocal() as db:
        seed_initial_data(db)
    yield


# ============================================================================
# 6. Pydantic schemas
# ============================================================================


class LoginRequest(BaseModel):
    username: str = Field(..., max_length=50)
    password: str = Field(..., max_length=200)


class TicketCreateRequest(BaseModel):
    machine_id: str


class InspectRequest(BaseModel):
    issue_description: str = Field(..., min_length=3, max_length=2000)
    repair_venue: RepairVenue


class PartsCheckRequest(BaseModel):
    available: bool
    parts_required: Optional[str] = Field(default=None, max_length=1000)

    @model_validator(mode="after")
    def need_parts_list(self) -> "PartsCheckRequest":
        if not self.available and not (self.parts_required or "").strip():
            raise ValueError("List the parts required when they are not in the site store.")
        return self


class DispatchRequest(BaseModel):
    dispatch_details: str = Field(..., min_length=2, max_length=500)
    eta_date: date


class QuotationRequestNote(BaseModel):
    note: Optional[str] = Field(default=None, max_length=1000)
    quotation_received: bool = False  # True: vendor quotation file attached → pending_quotation stage

    @model_validator(mode="after")
    def validate_note(self) -> "QuotationRequestNote":
        if self.quotation_received:
            return self
        text = (self.note or "").strip()
        if len(text) < 3:
            raise ValueError("Enter at least 3 characters of remarks.")
        self.note = text
        return self


class QuotationRequest(BaseModel):
    quote_details: str = Field(..., min_length=3, max_length=1000)  # remarks; amounts live on the attachment


class ApprovalRequest(BaseModel):
    approved: bool
    note: Optional[str] = Field(default=None, max_length=500)

    @model_validator(mode="after")
    def rejection_needs_note(self) -> "ApprovalRequest":
        if not self.approved and not (self.note or "").strip():
            raise ValueError("Give a reason when rejecting a quotation.")
        return self


class VendorDispatchRequest(BaseModel):
    sent_to_vendor: bool
    note: str = Field(..., min_length=3, max_length=1000)


class VehicleReceiveRequest(BaseModel):
    note: str = Field(..., min_length=3, max_length=1000)


class CloseRequest(BaseModel):
    root_cause: str = Field(..., min_length=3, max_length=1000)
    repair_cost: float = Field(..., ge=0)  # for outside repairs: the final billed amount
    invoice_number: Optional[str] = Field(default=None, max_length=100)
    closure_notes: Optional[str] = Field(default=None, max_length=1000)


class FileInfo(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    file_id: str
    kind: str
    filename: str
    size: int
    uploaded_at: datetime

    @field_validator("uploaded_at")
    @classmethod
    def force_utc(cls, v: datetime) -> datetime:
        return as_utc(v)


class TicketResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    ticket_id: str
    machine_id: str
    reported_by: str
    status: TicketStatus
    issue_description: Optional[str] = None
    repair_venue: Optional[RepairVenue] = None
    parts_required: Optional[str] = None
    requisition_by: Optional[str] = None
    dispatch_details: Optional[str] = None
    vendor_name: Optional[str] = None
    quote_amount: Optional[float] = None
    quote_details: Optional[str] = None
    approval_note: Optional[str] = None
    quotation_request_note: Optional[str] = None
    vendor_dispatch_note: Optional[str] = None
    vehicle_received_note: Optional[str] = None
    eta_date: Optional[date] = None
    root_cause: Optional[str] = None
    repair_cost: Optional[float] = None
    closure_notes: Optional[str] = None
    invoice_number: Optional[str] = None
    files: List[FileInfo] = []
    cost_over_quote: Optional[float] = None  # set when the final cost exceeds the approved quotation
    created_at: datetime
    stage_entered_at: datetime
    inspected_at: Optional[datetime] = None
    cleared_at: Optional[datetime] = None
    closed_at: Optional[datetime] = None
    sla_due_at: Optional[datetime] = None
    is_overdue: bool = False
    downtime_hours: float = 0.0

    @field_validator("created_at", "stage_entered_at", "inspected_at", "cleared_at", "closed_at")
    @classmethod
    def force_utc(cls, v: Optional[datetime]) -> Optional[datetime]:
        # SQLite returns naive datetimes; without a timezone the browser would read them as local time.
        return as_utc(v) if v else v

    @model_validator(mode="after")
    def compute_derived(self) -> "TicketResponse":
        self.sla_due_at = sla_due_at(self.status, self.stage_entered_at, self.eta_date)
        self.is_overdue = self.sla_due_at is not None and _now() > self.sla_due_at
        end = as_utc(self.cleared_at) if self.cleared_at else _now()
        self.downtime_hours = round((end - as_utc(self.created_at)).total_seconds() / 3600.0, 2)
        if self.repair_cost is not None and self.quote_amount is not None and self.repair_cost > self.quote_amount:
            self.cost_over_quote = round(self.repair_cost - self.quote_amount, 2)
        return self


# ============================================================================
# 7. App, auth dependencies, helpers
# ============================================================================

app = FastAPI(title="SiteGuard Machinery Breakdown Tracker", version="3.0.0", lifespan=lifespan)


@app.exception_handler(StatementError)
@app.exception_handler(DataError)
@app.exception_handler(OperationalError)
def database_error_handler(_request: Request, exc: Exception) -> JSONResponse:
    """Return JSON (not plain text) so the mobile UI can show a useful message."""
    msg = str(exc.orig) if getattr(exc, "orig", None) else str(exc)
    if "quotation_requested" in msg and "enum" in msg.lower():
        detail = (
            "Database enum is missing the quotation_requested status. "
            "Restart the app so startup migrations can run, or use a fresh database."
        )
    else:
        detail = "A database error occurred. Restart the app or reset the demo database."
    return JSONResponse(status_code=500, content={"detail": detail})


# The UI is served from the same origin, so CORS is off unless origins are listed explicitly.
_cors_origins = [o.strip() for o in os.getenv("CORS_ORIGINS", "").split(",") if o.strip()]
if _cors_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_cors_origins,
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )


def get_current_user(authorization: Optional[str] = Header(default=None), db: Session = Depends(get_db)) -> User:
    scheme, _, token = (authorization or "").partition(" ")
    user_id = read_token(token) if scheme.lower() == "bearer" else None
    user = db.get(User, user_id) if user_id else None
    if not user:
        raise HTTPException(status_code=401, detail="Please log in again.")
    return user


def require_roles(*roles: UserRole) -> Callable[..., User]:
    def checker(user: User = Depends(get_current_user)) -> User:
        if effective_role(user.role) not in roles:
            raise HTTPException(status_code=403, detail="Your role cannot do this action.")
        return user

    return checker


MECH = (UserRole.MECH_ENGINEER, UserRole.PM_INCHARGE)

_MECH_TASK_STATUSES = (
    S.PENDING_INSPECTION,
    S.PENDING_PARTS_CHECK,
    S.QUOTATION_REQUESTED,
    S.PENDING_QUOTATION,
    S.PENDING_VENDOR_DISPATCH,
    S.VENDOR_REPAIR_IN_PROGRESS,
    S.REPAIR_IN_PROGRESS,
    S.IN_TRANSIT,
)
# Single source of truth for the "My tasks" queue (mirrored in bootstrap for the SPA).
TASK_STATUSES_BY_ROLE: dict[UserRole, tuple[TicketStatus, ...]] = {
    UserRole.SITE_OFFICE: (),
    UserRole.DEO: (S.CLEARED,),
    UserRole.MECH_ENGINEER: _MECH_TASK_STATUSES,
    UserRole.PM_INCHARGE: _MECH_TASK_STATUSES + (S.CLEARED,),
    UserRole.SITE_STORE: (),
    UserRole.CENTRAL_STORE: (S.PENDING_DISPATCH,),
    UserRole.MANAGEMENT: (S.PENDING_APPROVAL,),
}

STAGE_LABELS: dict[str, str] = {
    "pending_inspection": "Needs inspection",
    "pending_parts_check": "Site store check",
    "pending_dispatch": "Awaiting central store",
    "in_transit": "Spares in transit",
    "quotation_requested": "Quotation pending / requested",
    "pending_quotation": "Vendor quotation",
    "pending_approval": "Awaiting approval",
    "pending_vendor_dispatch": "Send machine to vendor",
    "vendor_repair_in_progress": "At vendor workshop",
    "repair_in_progress": "Repair in progress",
    "cleared": "Cleared, record pending",
    "closed": "Closed",
}


_LEGACY_USER_ROLE: dict[str, UserRole] = {
    "supervisor": UserRole.MECH_ENGINEER,
    "head_office": UserRole.CENTRAL_STORE,
}


def effective_role(role: UserRole) -> UserRole:
    return _LEGACY_USER_ROLE.get(role.value, role)


def task_statuses_for_role(role: UserRole) -> list[str]:
    return [s.value for s in TASK_STATUSES_BY_ROLE.get(effective_role(role), ())]


def may_act_on_in_transit(me: User, ticket: Ticket) -> bool:
    """In-transit receipt goes to whoever raised the requisition; P&M In-charge can cover any."""
    if ticket.status != S.IN_TRANSIT:
        return False
    if not ticket.requisition_by:
        return effective_role(me.role) in MECH
    if effective_role(me.role) == UserRole.PM_INCHARGE:
        return True
    if effective_role(me.role) == UserRole.MECH_ENGINEER:
        return ticket.requisition_by == me.user_id
    return False


def ticket_in_my_queue(me: User, ticket: Ticket) -> bool:
    if ticket.status.value not in task_statuses_for_role(me.role):
        return False
    if ticket.status == S.IN_TRANSIT:
        return may_act_on_in_transit(me, ticket)
    return True


def migrate_legacy_user_roles() -> None:
    """Databases created from sql/seed.sql still label Vikram as supervisor, etc."""
    if not inspect(engine).has_table("users"):
        return
    pairs = (("supervisor", "mech_engineer"), ("head_office", "central_store"))
    with engine.begin() as conn:
        for old, new in pairs:
            if "postgresql" in DATABASE_URL:
                conn.execute(
                    text("UPDATE users SET role = CAST(:new AS user_role) WHERE role::text = :old"),
                    {"old": old, "new": new},
                )
            else:
                conn.execute(text("UPDATE users SET role = :new WHERE role = :old"), {"old": old, "new": new})


def get_ticket(db: Session, ticket_id: str) -> Ticket:
    ticket = db.get(Ticket, ticket_id)
    if not ticket:
        raise HTTPException(status_code=404, detail="Ticket not found.")
    return ticket


def move_ticket(db: Session, ticket: Ticket, new_status: TicketStatus, actor: User, note: Optional[str] = None) -> None:
    """Single entry point for status changes: validates the transition and logs it."""
    if new_status not in ALLOWED_TRANSITIONS[ticket.status]:
        raise HTTPException(
            status_code=409,
            detail=f"Cannot move ticket from '{ticket.status.value}' to '{new_status.value}'.",
        )
    now = _now()
    db.add(
        TicketEvent(
            ticket_id=ticket.ticket_id,
            actor_id=actor.user_id,
            from_status=ticket.status.value,
            to_status=new_status.value,
            note=note,
            created_at=now,
        )
    )
    ticket.status = new_status
    ticket.stage_entered_at = now


def finish(db: Session, ticket: Ticket) -> TicketResponse:
    db.commit()
    db.refresh(ticket)
    return TicketResponse.model_validate(ticket)


def user_payload(u: User) -> dict:
    return {"user_id": u.user_id, "name": u.name, "role": u.role.value}


# ============================================================================
# 8. Routes
# ============================================================================


@app.post("/api/auth/login")
def login(payload: LoginRequest, db: Session = Depends(get_db)) -> dict:
    user = db.query(User).filter(User.username == payload.username.strip().lower()).first()
    if not user or not verify_password(payload.password, user.password_hash):
        raise HTTPException(status_code=401, detail="Wrong username or password.")
    return {"token": make_token(user.user_id), "user": user_payload(user)}


@app.get("/api/bootstrap")
def get_bootstrap_data(me: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict:
    """Current user, machines and all tickets (one call keeps every device in sync)."""
    machines = db.query(Machine).all()
    tickets = db.query(Ticket).options(selectinload(Ticket.files)).order_by(Ticket.created_at.desc()).all()
    ticket_payloads = [TicketResponse.model_validate(t).model_dump(mode="json") for t in tickets]
    queue = set(task_statuses_for_role(me.role))
    return {
        "me": user_payload(me),
        "users": [user_payload(u) for u in db.query(User).all()],
        "machines": [
            {"machine_id": m.machine_id, "name": m.name, "location": m.location, "status": m.status.value}
            for m in machines
        ],
        "tickets": ticket_payloads,
        "task_statuses": sorted(queue),
        "my_tasks": [
            TicketResponse.model_validate(t).model_dump(mode="json")
            for t in tickets
            if ticket_in_my_queue(me, t)
        ],
        "stage_labels": STAGE_LABELS,
        "app_version": APP_UI_VERSION,
    }


# SOP 1: breakdown reported by Senior Civil Engineer or P&M DEO
@app.post("/api/tickets/create", response_model=TicketResponse, status_code=status.HTTP_201_CREATED)
def create_ticket(
    payload: TicketCreateRequest,
    reporter: User = Depends(require_roles(UserRole.SITE_OFFICE, UserRole.DEO)),
    db: Session = Depends(get_db),
) -> TicketResponse:
    machine = db.get(Machine, payload.machine_id)
    if not machine:
        raise HTTPException(status_code=404, detail="Machine not found.")
    existing = (
        db.query(Ticket)
        .filter(Ticket.machine_id == payload.machine_id, Ticket.status.notin_([S.CLEARED, S.CLOSED]))
        .first()
    )
    if existing:
        raise HTTPException(status_code=409, detail=f"{machine.name} already has an open breakdown.")

    ticket = Ticket(reported_by=reporter.user_id, machine_id=machine.machine_id, status=S.PENDING_INSPECTION)
    machine.status = MachineStatus.BROKEN_DOWN
    db.add(ticket)
    db.flush()
    db.add(TicketEvent(ticket_id=ticket.ticket_id, actor_id=reporter.user_id, from_status=None, to_status=S.PENDING_INSPECTION.value))
    try:
        return finish(db, ticket)
    except IntegrityError:  # two reports raced; the partial unique index caught it
        db.rollback()
        raise HTTPException(status_code=409, detail=f"{machine.name} already has an open breakdown.")


# SOP 2-3: inspection and repair venue decision (Mechanical Engineer / P&M In-charge)
@app.put("/api/tickets/{ticket_id}/inspect", response_model=TicketResponse)
def inspect_ticket(
    ticket_id: str,
    payload: InspectRequest,
    actor: User = Depends(require_roles(*MECH)),
    db: Session = Depends(get_db),
) -> TicketResponse:
    ticket = get_ticket(db, ticket_id)
    target = S.PENDING_PARTS_CHECK if payload.repair_venue == RepairVenue.IN_HOUSE else S.QUOTATION_REQUESTED
    description = payload.issue_description.strip()
    move_ticket(db, ticket, target, actor, note=f"{payload.repair_venue.value}: {description}")
    ticket.issue_description = description
    ticket.repair_venue = payload.repair_venue
    ticket.inspected_at = _now()
    return finish(db, ticket)


# SOP 4: site store spare parts check; if unavailable, requisition goes to central store
@app.put("/api/tickets/{ticket_id}/parts-check", response_model=TicketResponse)
def parts_check(
    ticket_id: str,
    payload: PartsCheckRequest,
    actor: User = Depends(require_roles(*MECH)),
    db: Session = Depends(get_db),
) -> TicketResponse:
    ticket = get_ticket(db, ticket_id)
    if payload.available:
        move_ticket(db, ticket, S.REPAIR_IN_PROGRESS, actor, note="Spares available in site store")
    else:
        parts = payload.parts_required.strip()
        move_ticket(db, ticket, S.PENDING_DISPATCH, actor, note=f"Requisition: {parts}")
        ticket.parts_required = parts
        ticket.requisition_by = actor.user_id
    return finish(db, ticket)


# SOP 5: central store arranges and dispatches spares
@app.put("/api/tickets/{ticket_id}/dispatch", response_model=TicketResponse)
def dispatch_spares(
    ticket_id: str,
    payload: DispatchRequest,
    actor: User = Depends(require_roles(UserRole.CENTRAL_STORE)),
    db: Session = Depends(get_db),
) -> TicketResponse:
    ticket = get_ticket(db, ticket_id)
    if (
        REQUIRE_DISPATCH_DOCS
        and ticket.status == S.PENDING_DISPATCH
        and not db.query(TicketFile).filter(TicketFile.ticket_id == ticket_id, TicketFile.kind == "dispatch").count()
    ):
        raise HTTPException(status_code=422, detail="Attach the dispatch documents (PDF) before confirming dispatch.")
    if not ticket.requisition_by:
        raiser = _requisition_raiser_from_events(db, ticket.ticket_id)
        if raiser:
            ticket.requisition_by = raiser
    details = payload.dispatch_details.strip()
    move_ticket(db, ticket, S.IN_TRANSIT, actor, note=details)
    ticket.dispatch_details = details
    ticket.eta_date = payload.eta_date
    return finish(db, ticket)


# SOP 6: engineer who raised the requisition confirms spares received at site
@app.put("/api/tickets/{ticket_id}/receive", response_model=TicketResponse)
def receive_spares(
    ticket_id: str,
    actor: User = Depends(require_roles(*MECH)),
    db: Session = Depends(get_db),
) -> TicketResponse:
    ticket = get_ticket(db, ticket_id)
    if ticket.status != S.IN_TRANSIT:
        raise HTTPException(status_code=409, detail="This ticket is not awaiting spare receipt.")
    if ticket.requisition_by and effective_role(actor.role) == UserRole.MECH_ENGINEER and ticket.requisition_by != actor.user_id:
        raise HTTPException(
            status_code=403,
            detail="Only the engineer who raised the requisition can confirm receipt.",
        )
    move_ticket(db, ticket, S.REPAIR_IN_PROGRESS, actor, note="Spares received at site")
    ticket.eta_date = None
    return finish(db, ticket)


# SOP 7a: outside repair — log that the vendor has been asked for a quotation
@app.put("/api/tickets/{ticket_id}/quotation-request", response_model=TicketResponse)
def note_quotation_requested(
    ticket_id: str,
    payload: QuotationRequestNote,
    actor: User = Depends(require_roles(*MECH)),
    db: Session = Depends(get_db),
) -> TicketResponse:
    ticket = get_ticket(db, ticket_id)
    if ticket.status != S.QUOTATION_REQUESTED:
        raise HTTPException(status_code=409, detail="This ticket is not waiting for a vendor quotation.")
    if payload.quotation_received:
        if not ticket.quotation_request_note:
            raise HTTPException(status_code=422, detail="Log the quotation request before marking the quotation received.")
        has_file = (
            db.query(TicketFile).filter(TicketFile.ticket_id == ticket_id, TicketFile.kind == "quotation").count()
        )
        if not has_file:
            raise HTTPException(
                status_code=422,
                detail="Attach the vendor quotation (PDF or photo) before continuing.",
            )
        move_ticket(
            db,
            ticket,
            S.PENDING_QUOTATION,
            actor,
            note=f"Quotation received: {ticket.quotation_request_note}",
        )
    else:
        note = (payload.note or "").strip()
        ticket.quotation_request_note = note
        db.add(
            TicketEvent(
                ticket_id=ticket.ticket_id,
                actor_id=actor.user_id,
                from_status=S.QUOTATION_REQUESTED.value,
                to_status=S.QUOTATION_REQUESTED.value,
                note=f"Quotation requested: {note}",
                created_at=_now(),
            )
        )
    return finish(db, ticket)


# SOP 7b: vendor quotation details submitted for management approval
@app.put("/api/tickets/{ticket_id}/quotation", response_model=TicketResponse)
def submit_quotation(
    ticket_id: str,
    payload: QuotationRequest,
    actor: User = Depends(require_roles(*MECH)),
    db: Session = Depends(get_db),
) -> TicketResponse:
    ticket = get_ticket(db, ticket_id)
    if ticket.status != S.PENDING_QUOTATION:
        raise HTTPException(status_code=409, detail="This ticket is not in the vendor quotation stage.")
    has_file = (
        db.query(TicketFile).filter(TicketFile.ticket_id == ticket_id, TicketFile.kind == "quotation").count()
    )
    if not has_file:
        raise HTTPException(status_code=422, detail="Attach at least one vendor quotation file before submitting.")
    remarks = payload.quote_details.strip()
    move_ticket(db, ticket, S.PENDING_APPROVAL, actor, note=remarks)
    ticket.quote_details = remarks
    ticket.approval_note = None
    return finish(db, ticket)


# SOP 7: management approves (or sends back) the quotation before work starts
@app.put("/api/tickets/{ticket_id}/approval", response_model=TicketResponse)
def decide_quotation(
    ticket_id: str,
    payload: ApprovalRequest,
    actor: User = Depends(require_roles(UserRole.MANAGEMENT)),
    db: Session = Depends(get_db),
) -> TicketResponse:
    ticket = get_ticket(db, ticket_id)
    note = (payload.note or "").strip() or None
    if payload.approved:
        move_ticket(db, ticket, S.PENDING_VENDOR_DISPATCH, actor, note=note or "Quotation approved")
    else:
        move_ticket(db, ticket, S.PENDING_QUOTATION, actor, note=f"Rejected: {note}")
    ticket.approval_note = note
    return finish(db, ticket)


# Outside repair: mech / PM logs whether the machine was sent to the vendor workshop
@app.put("/api/tickets/{ticket_id}/vendor-dispatch", response_model=TicketResponse)
def log_vendor_dispatch(
    ticket_id: str,
    payload: VendorDispatchRequest,
    actor: User = Depends(require_roles(*MECH)),
    db: Session = Depends(get_db),
) -> TicketResponse:
    ticket = get_ticket(db, ticket_id)
    if ticket.repair_venue != RepairVenue.OUTSIDE:
        raise HTTPException(status_code=409, detail="Vendor dispatch applies to outside repairs only.")
    if ticket.status != S.PENDING_VENDOR_DISPATCH:
        raise HTTPException(status_code=409, detail="This ticket is not awaiting vendor dispatch.")
    note = payload.note.strip()
    ticket.vendor_dispatch_note = note
    if payload.sent_to_vendor:
        move_ticket(db, ticket, S.VENDOR_REPAIR_IN_PROGRESS, actor, note=f"Sent to vendor: {note}")
    else:
        db.add(
            TicketEvent(
                ticket_id=ticket.ticket_id,
                actor_id=actor.user_id,
                from_status=S.PENDING_VENDOR_DISPATCH.value,
                to_status=S.PENDING_VENDOR_DISPATCH.value,
                note=f"Not sent yet: {note}",
                created_at=_now(),
            )
        )
    return finish(db, ticket)


# Outside repair: vehicle returned to site — breakdown cleared, machine active again
@app.put("/api/tickets/{ticket_id}/vehicle-receive", response_model=TicketResponse)
def receive_vehicle_from_vendor(
    ticket_id: str,
    payload: VehicleReceiveRequest,
    actor: User = Depends(require_roles(*MECH)),
    db: Session = Depends(get_db),
) -> TicketResponse:
    ticket = get_ticket(db, ticket_id)
    if ticket.status != S.VENDOR_REPAIR_IN_PROGRESS:
        raise HTTPException(status_code=409, detail="Confirm receipt only after the machine is at the vendor for repair.")
    note = payload.note.strip()
    move_ticket(db, ticket, S.CLEARED, actor, note=f"Received at site: {note}")
    ticket.vehicle_received_note = note
    ticket.cleared_at = ticket.stage_entered_at
    machine = db.get(Machine, ticket.machine_id)
    if machine:
        machine.status = MachineStatus.ACTIVE
    return finish(db, ticket)


# In-house repair: mechanical department confirms the breakdown is cleared; machine is active again
@app.put("/api/tickets/{ticket_id}/clear", response_model=TicketResponse)
def clear_breakdown(
    ticket_id: str,
    actor: User = Depends(require_roles(*MECH)),
    db: Session = Depends(get_db),
) -> TicketResponse:
    ticket = get_ticket(db, ticket_id)
    if ticket.status != S.REPAIR_IN_PROGRESS:
        raise HTTPException(
            status_code=409,
            detail="In-house repairs use Mark breakdown cleared; outside repairs use vehicle receipt at site.",
        )
    move_ticket(db, ticket, S.CLEARED, actor, note="Breakdown cleared")
    ticket.cleared_at = ticket.stage_entered_at
    machine = db.get(Machine, ticket.machine_id)
    if machine:
        machine.status = MachineStatus.ACTIVE
    return finish(db, ticket)


# SOP 9: DEO or P&M In-charge logs downtime, repair cost and root cause to close the report
@app.put("/api/tickets/{ticket_id}/close", response_model=TicketResponse)
def close_ticket(
    ticket_id: str,
    payload: CloseRequest,
    actor: User = Depends(require_roles(UserRole.DEO, UserRole.PM_INCHARGE)),
    db: Session = Depends(get_db),
) -> TicketResponse:
    ticket = get_ticket(db, ticket_id)
    if (
        REQUIRE_BILL_FOR_OUTSIDE
        and ticket.status == S.CLEARED
        and ticket.repair_venue == RepairVenue.OUTSIDE
        and not db.query(TicketFile).filter(TicketFile.ticket_id == ticket_id, TicketFile.kind == "bill").count()
    ):
        raise HTTPException(status_code=422, detail="Attach the vendor bill before closing an outside repair.")
    move_ticket(db, ticket, S.CLOSED, actor, note=f"Root cause: {payload.root_cause.strip()}")
    ticket.root_cause = payload.root_cause.strip()
    ticket.invoice_number = (payload.invoice_number or "").strip() or None
    ticket.repair_cost = payload.repair_cost
    ticket.closure_notes = (payload.closure_notes or "").strip() or None
    ticket.closed_at = ticket.stage_entered_at
    return finish(db, ticket)


# ---- Attachments: vendor quotation (before submitting it) and vendor bill (before closing) -------------

MAX_UPLOAD_BYTES = int(os.getenv("MAX_UPLOAD_BYTES", str(5 * 1024 * 1024)))
MAX_FILES_PER_KIND = 5
REQUIRE_BILL_FOR_OUTSIDE = os.getenv("REQUIRE_BILL_FOR_OUTSIDE", "1") != "0"
REQUIRE_DISPATCH_DOCS = os.getenv("REQUIRE_DISPATCH_DOCS", "1") != "0"
# Type is decided from the file's own first bytes, never from the client's claim.
FILE_SIGNATURES = (
    (b"%PDF", "application/pdf"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"\x89PNG\r\n\x1a\n", "image/png"),
)


class FileKind(str, Enum):
    QUOTATION = "quotation"
    QUOTATION_REQUEST = "quotation_request"  # optional proof when the vendor was asked (not the quote itself)
    DISPATCH = "dispatch"
    BILL = "bill"


def safe_filename(name: str) -> str:
    base = (name or "file").replace("\\", "/").rsplit("/", 1)[-1]
    cleaned = "".join(ch if ch.isalnum() or ch in "._- " else "_" for ch in base).strip() or "file"
    return cleaned[:150]


def assert_may_manage_file_kind(user: User, ticket: Ticket, kind: FileKind) -> None:
    """Same stage/role rules for upload and delete."""
    if kind == FileKind.QUOTATION:
        if effective_role(user.role) not in MECH:
            raise HTTPException(status_code=403, detail="Your role cannot do this action.")
        if ticket.status == S.QUOTATION_REQUESTED and not ticket.quotation_request_note:
            raise HTTPException(status_code=409, detail="Log the quotation request before attaching the vendor quotation.")
        if ticket.status not in (S.QUOTATION_REQUESTED, S.PENDING_QUOTATION):
            raise HTTPException(
                status_code=409,
                detail="Quotation files can only be changed while the quotation is being prepared.",
            )
    elif kind == FileKind.QUOTATION_REQUEST:
        if effective_role(user.role) not in MECH:
            raise HTTPException(status_code=403, detail="Your role cannot do this action.")
        if ticket.status != S.QUOTATION_REQUESTED:
            raise HTTPException(
                status_code=409,
                detail="Request-support files can only be added while logging the quotation request.",
            )
    elif kind == FileKind.DISPATCH:
        if effective_role(user.role) != UserRole.CENTRAL_STORE:
            raise HTTPException(status_code=403, detail="Your role cannot do this action.")
        if ticket.status != S.PENDING_DISPATCH:
            raise HTTPException(status_code=409, detail="Dispatch documents can only be changed before dispatch is confirmed.")
    elif kind == FileKind.BILL:
        if effective_role(user.role) not in (UserRole.DEO, UserRole.PM_INCHARGE):
            raise HTTPException(status_code=403, detail="Your role cannot do this action.")
        if ticket.status != S.CLEARED or ticket.repair_venue != RepairVenue.OUTSIDE:
            raise HTTPException(status_code=409, detail="Vendor bills can only be changed before the report is closed.")
    else:
        raise HTTPException(status_code=422, detail="Unknown attachment type.")


@app.post("/api/tickets/{ticket_id}/files", status_code=status.HTTP_201_CREATED)
async def upload_file(
    ticket_id: str,
    request: Request,
    kind: FileKind,
    filename: str = "file",
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    """Raw-body upload (no multipart dependency): POST the file bytes with ?kind=...&filename=..."""
    ticket = get_ticket(db, ticket_id)
    assert_may_manage_file_kind(user, ticket, kind)

    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail=f"File is larger than {MAX_UPLOAD_BYTES // (1024 * 1024)} MB.")
    data = await request.body()
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail=f"File is larger than {MAX_UPLOAD_BYTES // (1024 * 1024)} MB.")
    if not data:
        raise HTTPException(status_code=422, detail="The file is empty.")
    content_type = next((ct for sig, ct in FILE_SIGNATURES if data.startswith(sig)), None)
    if not content_type:
        raise HTTPException(status_code=415, detail="Only PDF, JPG or PNG files are accepted.")
    existing = db.query(TicketFile).filter(TicketFile.ticket_id == ticket_id, TicketFile.kind == kind.value).count()
    if existing >= MAX_FILES_PER_KIND:
        raise HTTPException(status_code=409, detail=f"At most {MAX_FILES_PER_KIND} files per {kind.value}.")

    record = TicketFile(
        ticket_id=ticket_id,
        kind=kind.value,
        filename=safe_filename(filename),
        content_type=content_type,
        size=len(data),
        data=data,
        uploaded_by=user.user_id,
    )
    db.add(record)
    db.commit()
    return {"file_id": record.file_id}


@app.get("/api/files/{file_id}")
def download_file(file_id: str, _: User = Depends(get_current_user), db: Session = Depends(get_db)) -> Response:
    record = db.get(TicketFile, file_id)
    if not record:
        raise HTTPException(status_code=404, detail="File not found.")
    return Response(
        content=record.data,
        media_type=record.content_type,
        headers={
            "Content-Disposition": f'inline; filename="{record.filename}"',
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "private, no-store",
        },
    )


@app.delete("/api/files/{file_id}")
def delete_file(file_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict:
    record = db.get(TicketFile, file_id)
    if not record:
        raise HTTPException(status_code=404, detail="File not found.")
    ticket = get_ticket(db, record.ticket_id)
    try:
        kind = FileKind(record.kind)
    except ValueError:
        raise HTTPException(status_code=409, detail="Unknown attachment type.")
    assert_may_manage_file_kind(user, ticket, kind)
    db.delete(record)
    db.commit()
    return {"ok": True}


@app.get("/api/tickets/{ticket_id}/events")
def get_ticket_events(ticket_id: str, _: User = Depends(get_current_user), db: Session = Depends(get_db)) -> list:
    rows = (
        db.query(TicketEvent, User.name)
        .join(User, User.user_id == TicketEvent.actor_id)
        .filter(TicketEvent.ticket_id == ticket_id)
        .order_by(TicketEvent.created_at.asc(), TicketEvent.event_id.asc())
        .all()
    )
    return [
        {
            "from_status": e.from_status,
            "to_status": e.to_status,
            "actor": name,
            "note": e.note,
            "created_at": as_utc(e.created_at).isoformat(),
        }
        for e, name in rows
    ]


@app.post("/api/reset-demo")
def reset_demo(_: User = Depends(require_roles(UserRole.MANAGEMENT)), db: Session = Depends(get_db)) -> dict:
    """Wipes all data and re-seeds the demo set. Disable with DEMO_RESET=0."""
    if os.getenv("DEMO_RESET", "1") == "0":
        raise HTTPException(status_code=403, detail="Demo reset is disabled.")
    db.query(TicketFile).delete()
    db.query(TicketEvent).delete()
    db.query(Ticket).delete()
    db.query(Machine).delete()
    db.query(User).delete()
    db.commit()
    seed_initial_data(db)
    return {"ok": True}


def stage_average_hours(db: Session) -> dict:
    """Average hours tickets spent in each stage, from completed stays in the event log."""
    events = db.query(TicketEvent).order_by(TicketEvent.ticket_id, TicketEvent.created_at, TicketEvent.event_id).all()
    totals: dict = {}
    for prev, nxt in zip(events, events[1:]):
        if prev.ticket_id != nxt.ticket_id:
            continue
        hours = (as_utc(nxt.created_at) - as_utc(prev.created_at)).total_seconds() / 3600.0
        bucket = totals.setdefault(prev.to_status, [0.0, 0])
        bucket[0] += hours
        bucket[1] += 1
    return {stage: round(total / n, 2) for stage, (total, n) in totals.items()}


@app.get("/api/dashboard/metrics")
def get_dashboard_metrics(_: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict:
    tickets = [TicketResponse.model_validate(t) for t in db.query(Ticket).options(selectinload(Ticket.files)).all()]
    open_stages = [s.value for s in TicketStatus if s != S.CLOSED]
    in_stage = {s: 0 for s in open_stages}
    overdue: dict = {}
    for t in tickets:
        if t.status == S.CLOSED:
            continue
        in_stage[t.status.value] += 1
        if t.is_overdue:
            overdue[t.status.value] = overdue.get(t.status.value, 0) + 1

    cleared = [t for t in tickets if t.cleared_at]
    closed = [t for t in tickets if t.status == S.CLOSED]
    return {
        "total_machines_broken": db.query(func.count(Machine.machine_id))
        .filter(Machine.status == MachineStatus.BROKEN_DOWN)
        .scalar()
        or 0,
        "tickets_by_stage": in_stage,
        "overdue_by_stage": overdue,
        "average_downtime_hours": round(sum(t.downtime_hours for t in cleared) / len(cleared), 2) if cleared else None,
        "cleared_tickets": len(cleared),
        "closed_tickets": len(closed),
        "total_repair_cost": round(sum(t.repair_cost or 0 for t in closed), 2),
        "stage_average_hours": stage_average_hours(db),
    }


# Serve static files & root UI
STATIC_DIR = FilePath(__file__).parent / "static"
if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/", response_class=HTMLResponse)
def serve_app():
    index_file = STATIC_DIR / "index.html"
    if index_file.exists():
        html = index_file.read_text(encoding="utf-8").replace("{{SITEGUARD_BUILD}}", APP_UI_VERSION)
        return HTMLResponse(
            html,
            headers={
                "Cache-Control": "no-cache, no-store, must-revalidate",
                "Pragma": "no-cache",
                "X-SiteGuard-Build": APP_UI_VERSION,
            },
        )
    return HTMLResponse("<h1>SiteGuard API Running — Visit /docs</h1>")

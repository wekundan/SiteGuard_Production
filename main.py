"""
SiteGuard Machinery Breakdown Tracker - Production FastAPI Backend
==================================================================
- Connects to PostgreSQL via DATABASE_URL (supports Render, Railway, Supabase, Neon, and local Postgres;
  automatically normalizes `postgres://` to `postgresql+psycopg2://`).
- Falls back to local SQLite (`sqlite:///./siteguard_local.db`) ONLY if DATABASE_URL is not set,
  so you can also test locally with zero database setup.
- Automatically creates tables and seeds initial users & construction machines on first startup.
- Serves all REST API endpoints (`/api/...`) and the live connected Mobile PWA at `/`.
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

from fastapi import Depends, FastAPI, Header, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import (
    Date,
    DateTime,
    Enum as SAEnum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    create_engine,
    func,
    text,
)
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    Session,
    mapped_column,
    relationship,
    sessionmaker,
)

# ============================================================================
# 1. Database Configuration (PostgreSQL in Production, SQLite Local Fallback)
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
# 2. Domain Enums
# ============================================================================


class UserRole(str, Enum):
    SITE_OFFICE = "site_office"
    SUPERVISOR = "supervisor"
    HEAD_OFFICE = "head_office"
    MANAGEMENT = "management"


class MachineStatus(str, Enum):
    ACTIVE = "active"
    BROKEN_DOWN = "broken_down"


class TicketStatus(str, Enum):
    PENDING_INSPECTION = "pending_inspection"
    PENDING_LOCAL_PART = "pending_local_part"
    PENDING_HO_ACTION = "pending_ho_action"
    PENDING_ARRIVAL = "pending_arrival"
    RESOLVED = "resolved"


class ResolutionType(str, Enum):
    LOCAL = "local"
    HEAD_OFFICE = "head_office"


# Only these ticket transitions are legal; anything else is rejected with 409.
ALLOWED_TRANSITIONS = {
    TicketStatus.PENDING_INSPECTION: {TicketStatus.PENDING_LOCAL_PART, TicketStatus.PENDING_HO_ACTION},
    TicketStatus.PENDING_LOCAL_PART: {TicketStatus.RESOLVED},
    TicketStatus.PENDING_HO_ACTION: {TicketStatus.PENDING_ARRIVAL},
    TicketStatus.PENDING_ARRIVAL: {TicketStatus.RESOLVED},
    TicketStatus.RESOLVED: set(),
}


def as_utc(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


# SLA targets. Hour-based stages are env-configurable; ETA-based stages are due at the end of
# the ETA date in the site's timezone (default IST, UTC+5:30).
SLA_INSPECTION_HOURS = float(os.getenv("SLA_INSPECTION_HOURS", "2"))
SLA_HO_ACTION_HOURS = float(os.getenv("SLA_HO_ACTION_HOURS", "24"))
SITE_TZ = timezone(timedelta(minutes=int(os.getenv("SITE_UTC_OFFSET_MINUTES", "330"))))


def sla_due_at(
    status: TicketStatus,
    created_at: datetime,
    ho_received_at: Optional[datetime],
    eta_date: Optional[date],
) -> Optional[datetime]:
    """When the ticket's current stage breaches its SLA (None if the stage has no deadline)."""
    if status == TicketStatus.PENDING_INSPECTION:
        return as_utc(created_at) + timedelta(hours=SLA_INSPECTION_HOURS)
    if status == TicketStatus.PENDING_HO_ACTION:
        return as_utc(ho_received_at or created_at) + timedelta(hours=SLA_HO_ACTION_HOURS)
    if status in (TicketStatus.PENDING_LOCAL_PART, TicketStatus.PENDING_ARRIVAL) and eta_date:
        end_of_eta = datetime.combine(eta_date + timedelta(days=1), datetime.min.time(), tzinfo=SITE_TZ)
        return end_of_eta.astimezone(timezone.utc)
    return None


# ============================================================================
# Auth helpers (stdlib only): PBKDF2 passwords + HMAC-signed bearer tokens
# ============================================================================

SECRET_KEY = os.getenv("SECRET_KEY", "dev-insecure-secret-change-me").encode()
TOKEN_TTL_SECONDS = 12 * 3600
DEMO_PASSWORD = os.getenv("DEMO_PASSWORD", "siteguard123")


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
# 3. SQLAlchemy ORM Models
# ============================================================================


class User(Base):
    __tablename__ = "users"

    user_id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    username: Mapped[str] = mapped_column(String(50), unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[UserRole] = mapped_column(
        SAEnum(
            UserRole,
            name="user_role",
            values_callable=lambda enum_cls: [e.value for e in enum_cls],
        ),
        nullable=False,
    )

    reported_tickets: Mapped[List["Ticket"]] = relationship(
        "Ticket", back_populates="reporter"
    )


class Machine(Base):
    __tablename__ = "machines"

    machine_id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    location: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[MachineStatus] = mapped_column(
        SAEnum(
            MachineStatus,
            name="machine_status",
            values_callable=lambda enum_cls: [e.value for e in enum_cls],
        ),
        nullable=False,
        default=MachineStatus.ACTIVE,
    )

    tickets: Mapped[List["Ticket"]] = relationship("Ticket", back_populates="machine")


class Ticket(Base):
    __tablename__ = "tickets"
    # DB-level guarantee: at most one unresolved ticket per machine (blocks race conditions).
    __table_args__ = (
        Index(
            "uq_one_open_ticket_per_machine",
            "machine_id",
            unique=True,
            postgresql_where=text("status <> 'resolved'"),
            sqlite_where=text("status <> 'resolved'"),
        ),
    )

    ticket_id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    machine_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("machines.machine_id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    reported_by: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("users.user_id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    issue_description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    status: Mapped[TicketStatus] = mapped_column(
        SAEnum(
            TicketStatus,
            name="ticket_status",
            values_callable=lambda enum_cls: [e.value for e in enum_cls],
        ),
        nullable=False,
        default=TicketStatus.PENDING_INSPECTION,
        index=True,
    )
    resolution_type: Mapped[Optional[ResolutionType]] = mapped_column(
        SAEnum(
            ResolutionType,
            name="resolution_type",
            values_callable=lambda enum_cls: [e.value for e in enum_cls],
        ),
        nullable=True,
    )
    po_details: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    eta_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        index=True,
    )
    inspected_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    ho_received_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    closed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    machine: Mapped["Machine"] = relationship("Machine", back_populates="tickets")
    reporter: Mapped["User"] = relationship("User", back_populates="reported_tickets")


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
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )


# ============================================================================
# 4. Auto-Initialize & Seed Database on First Boot
# ============================================================================


def seed_initial_data(db: Session) -> None:
    if db.query(User).count() > 0:
        return

    now = datetime.now(timezone.utc)
    pw = lambda: hash_password(DEMO_PASSWORD)  # noqa: E731
    users = [
        User(user_id="11111111-1111-4111-8111-111111111111", name="Ramesh Kumar (Site A)", username="ramesh", password_hash=pw(), role=UserRole.SITE_OFFICE),
        User(user_id="22222222-2222-4222-8222-222222222222", name="Deepak Verma (Site B)", username="deepak", password_hash=pw(), role=UserRole.SITE_OFFICE),
        User(user_id="33333333-3333-4333-8333-333333333333", name="Vikram Singh (Supervisor)", username="vikram", password_hash=pw(), role=UserRole.SUPERVISOR),
        User(user_id="44444444-4444-4444-8444-444444444444", name="Ananya Nair (Head Office)", username="ananya", password_hash=pw(), role=UserRole.HEAD_OFFICE),
        User(user_id="55555555-5555-4555-8555-555555555555", name="Rajeshwari Iyer (Management)", username="rajeshwari", password_hash=pw(), role=UserRole.MANAGEMENT),
    ]
    machines = [
        Machine(machine_id="10000000-0000-4000-8000-000000000001", name="Excavator 3000 (CAT 320D)", location="Site A - Zone 1", status=MachineStatus.BROKEN_DOWN),
        Machine(machine_id="10000000-0000-4000-8000-000000000002", name="Tower Crane TC-6015", location="Site A - Zone 2", status=MachineStatus.BROKEN_DOWN),
        Machine(machine_id="10000000-0000-4000-8000-000000000003", name="Transit Mixer TM-09", location="Site B - Batching Plant", status=MachineStatus.BROKEN_DOWN),
        Machine(machine_id="10000000-0000-4000-8000-000000000004", name="Backhoe Loader JCB-3DX", location="Site B - Utility Trench", status=MachineStatus.ACTIVE),
        Machine(machine_id="10000000-0000-4000-8000-000000000005", name="Motor Grader MG-140", location="Site A - Access Road", status=MachineStatus.ACTIVE),
        Machine(machine_id="10000000-0000-4000-8000-000000000006", name="Vibro Roller VR-210", location="Site A - Embankment", status=MachineStatus.ACTIVE),
        Machine(machine_id="10000000-0000-4000-8000-000000000007", name="Concrete Boom Placer BP-42", location="Site B - Podium Slab", status=MachineStatus.ACTIVE),
        Machine(machine_id="10000000-0000-4000-8000-000000000008", name="Wheel Loader WL-950", location="Site A - Stockpile", status=MachineStatus.ACTIVE),
    ]
    tickets = [
        # Overdue > 2 hours
        Ticket(
            ticket_id="90000000-0000-4000-8000-000000000101",
            machine_id="10000000-0000-4000-8000-000000000001",
            reported_by="11111111-1111-4111-8111-111111111111",
            status=TicketStatus.PENDING_INSPECTION,
            created_at=now - timedelta(hours=3, minutes=20),
        ),
        # Recent < 2 hours
        Ticket(
            ticket_id="90000000-0000-4000-8000-000000000102",
            machine_id="10000000-0000-4000-8000-000000000002",
            reported_by="11111111-1111-4111-8111-111111111111",
            status=TicketStatus.PENDING_INSPECTION,
            created_at=now - timedelta(minutes=40),
        ),
        # Escalated to HO
        Ticket(
            ticket_id="90000000-0000-4000-8000-000000000103",
            machine_id="10000000-0000-4000-8000-000000000003",
            reported_by="22222222-2222-4222-8222-222222222222",
            issue_description="Main planetary drum gearbox bearing seized; requires OEM replacement assembly.",
            status=TicketStatus.PENDING_HO_ACTION,
            resolution_type=ResolutionType.HEAD_OFFICE,
            created_at=now - timedelta(hours=6),
            inspected_at=now - timedelta(hours=5),
            ho_received_at=now - timedelta(hours=5),
        ),
    ]
    sup = "33333333-3333-4333-8333-333333333333"
    ev = lambda tid, actor, frm, to, ago: TicketEvent(  # noqa: E731
        ticket_id=tid, actor_id=actor, from_status=frm, to_status=to, created_at=now - ago
    )
    events = [
        ev("90000000-0000-4000-8000-000000000101", "11111111-1111-4111-8111-111111111111", None, "pending_inspection", timedelta(hours=3, minutes=20)),
        ev("90000000-0000-4000-8000-000000000102", "11111111-1111-4111-8111-111111111111", None, "pending_inspection", timedelta(minutes=40)),
        ev("90000000-0000-4000-8000-000000000103", "22222222-2222-4222-8222-222222222222", None, "pending_inspection", timedelta(hours=6)),
        ev("90000000-0000-4000-8000-000000000103", sup, "pending_inspection", "pending_ho_action", timedelta(hours=5)),
    ]
    db.add_all(users)
    db.add_all(machines)
    db.flush()  # parents must exist before dependent rows
    db.add_all(tickets)
    db.flush()
    db.add_all(events)
    db.commit()


@asynccontextmanager
async def lifespan(app: FastAPI):
    Base.metadata.create_all(bind=engine)
    with SessionLocal() as db:
        seed_initial_data(db)
    yield


# ============================================================================
# 5. Pydantic Schemas
# ============================================================================


class LoginRequest(BaseModel):
    username: str = Field(..., max_length=50)
    password: str = Field(..., max_length=200)


class TicketCreateRequest(BaseModel):
    machine_id: str


class TicketInspectRequest(BaseModel):
    issue_description: str = Field(..., min_length=3, max_length=2000)
    resolution_type: ResolutionType
    eta_date: Optional[date] = None

    @model_validator(mode="after")
    def validate_sourcing(self) -> "TicketInspectRequest":
        if self.resolution_type == ResolutionType.LOCAL and self.eta_date is None:
            raise ValueError("eta_date is required when resolution_type is 'local'.")
        return self


class TicketHOUpdateRequest(BaseModel):
    po_details: str = Field(..., min_length=2, max_length=500)
    eta_date: date


class TicketResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    ticket_id: str
    machine_id: str
    reported_by: str
    issue_description: Optional[str] = None
    status: TicketStatus
    resolution_type: Optional[ResolutionType] = None
    po_details: Optional[str] = None
    eta_date: Optional[date] = None
    created_at: datetime
    inspected_at: Optional[datetime] = None
    ho_received_at: Optional[datetime] = None
    closed_at: Optional[datetime] = None
    sla_due_at: Optional[datetime] = None
    is_overdue: bool = False

    @model_validator(mode="after")
    def compute_sla(self) -> "TicketResponse":
        self.sla_due_at = sla_due_at(self.status, self.created_at, self.ho_received_at, self.eta_date)
        self.is_overdue = self.sla_due_at is not None and datetime.now(timezone.utc) > self.sla_due_at
        return self


# ============================================================================
# 6. FastAPI App & Routes
# ============================================================================

app = FastAPI(
    title="SiteGuard Machinery Breakdown Tracker",
    version="2.0.0",
    lifespan=lifespan,
)

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


def get_current_user(
    authorization: Optional[str] = Header(default=None), db: Session = Depends(get_db)
) -> User:
    scheme, _, token = (authorization or "").partition(" ")
    user_id = read_token(token) if scheme.lower() == "bearer" else None
    user = db.get(User, user_id) if user_id else None
    if not user:
        raise HTTPException(status_code=401, detail="Please log in again.")
    return user


def require_roles(*roles: UserRole) -> Callable[..., User]:
    def checker(user: User = Depends(get_current_user)) -> User:
        if user.role not in roles:
            raise HTTPException(status_code=403, detail="Your role cannot do this action.")
        return user

    return checker


def move_ticket(
    db: Session, ticket: Ticket, new_status: TicketStatus, actor: User, note: Optional[str] = None
) -> None:
    """Single entry point for status changes: validates the transition and logs it."""
    if new_status not in ALLOWED_TRANSITIONS[ticket.status]:
        raise HTTPException(
            status_code=409,
            detail=f"Cannot move ticket from '{ticket.status.value}' to '{new_status.value}'.",
        )
    db.add(
        TicketEvent(
            ticket_id=ticket.ticket_id,
            actor_id=actor.user_id,
            from_status=ticket.status.value,
            to_status=new_status.value,
            note=note,
        )
    )
    ticket.status = new_status


def user_payload(u: User) -> dict:
    return {"user_id": u.user_id, "name": u.name, "role": u.role.value}


@app.post("/api/auth/login")
def login(payload: LoginRequest, db: Session = Depends(get_db)) -> dict:
    user = db.query(User).filter(User.username == payload.username.strip().lower()).first()
    if not user or not verify_password(payload.password, user.password_hash):
        raise HTTPException(status_code=401, detail="Wrong username or password.")
    return {"token": make_token(user.user_id), "user": user_payload(user)}


@app.get("/api/bootstrap")
def get_bootstrap_data(
    me: User = Depends(get_current_user), db: Session = Depends(get_db)
) -> dict:
    """Returns the current user, users, machines, and all tickets for multi-device sync."""
    users = db.query(User).all()
    machines = db.query(Machine).all()
    tickets = db.query(Ticket).order_by(Ticket.created_at.desc()).all()
    return {
        "me": user_payload(me),
        "users": [user_payload(u) for u in users],
        "machines": [
            {"machine_id": m.machine_id, "name": m.name, "location": m.location, "status": m.status.value}
            for m in machines
        ],
        "tickets": [TicketResponse.model_validate(t).model_dump(mode="json") for t in tickets],
    }


@app.post("/api/tickets/create", response_model=TicketResponse, status_code=status.HTTP_201_CREATED)
def create_ticket(
    payload: TicketCreateRequest,
    reporter: User = Depends(require_roles(UserRole.SITE_OFFICE, UserRole.SUPERVISOR)),
    db: Session = Depends(get_db),
) -> TicketResponse:
    machine = db.query(Machine).filter(Machine.machine_id == payload.machine_id).first()
    if not machine:
        raise HTTPException(status_code=404, detail="Machine not found.")

    existing = (
        db.query(Ticket)
        .filter(Ticket.machine_id == payload.machine_id, Ticket.status != TicketStatus.RESOLVED)
        .first()
    )
    if existing:
        raise HTTPException(status_code=409, detail=f"{machine.name} already has an open ticket.")

    new_ticket = Ticket(
        ticket_id=str(uuid.uuid4()),
        machine_id=payload.machine_id,
        reported_by=reporter.user_id,
        status=TicketStatus.PENDING_INSPECTION,
        created_at=datetime.now(timezone.utc),
    )
    machine.status = MachineStatus.BROKEN_DOWN
    db.add(new_ticket)
    db.flush()
    db.add(
        TicketEvent(
            ticket_id=new_ticket.ticket_id,
            actor_id=reporter.user_id,
            from_status=None,
            to_status=TicketStatus.PENDING_INSPECTION.value,
        )
    )
    try:
        db.commit()
    except IntegrityError:  # two reports raced; the partial unique index caught it
        db.rollback()
        raise HTTPException(status_code=409, detail=f"{machine.name} already has an open ticket.")
    db.refresh(new_ticket)
    return TicketResponse.model_validate(new_ticket)


@app.get("/api/tickets/queue", response_model=List[TicketResponse])
def get_role_queue(
    me: User = Depends(get_current_user), db: Session = Depends(get_db)
) -> List[TicketResponse]:
    role = me.role
    role_map = {
        UserRole.SITE_OFFICE: [
            TicketStatus.PENDING_INSPECTION,
            TicketStatus.PENDING_LOCAL_PART,
            TicketStatus.PENDING_HO_ACTION,
            TicketStatus.PENDING_ARRIVAL,
        ],
        UserRole.SUPERVISOR: [
            TicketStatus.PENDING_INSPECTION,
            TicketStatus.PENDING_LOCAL_PART,
            TicketStatus.PENDING_ARRIVAL,
        ],
        UserRole.HEAD_OFFICE: [TicketStatus.PENDING_HO_ACTION],
        UserRole.MANAGEMENT: [
            TicketStatus.PENDING_INSPECTION,
            TicketStatus.PENDING_LOCAL_PART,
            TicketStatus.PENDING_HO_ACTION,
            TicketStatus.PENDING_ARRIVAL,
        ],
    }
    rows = (
        db.query(Ticket)
        .filter(Ticket.status.in_(role_map[role]))
        .order_by(Ticket.created_at.asc())
        .all()
    )
    return [TicketResponse.model_validate(r) for r in rows]


@app.put("/api/tickets/{ticket_id}/inspect", response_model=TicketResponse)
def inspect_ticket(
    ticket_id: str,
    payload: TicketInspectRequest,
    actor: User = Depends(require_roles(UserRole.SUPERVISOR)),
    db: Session = Depends(get_db),
) -> TicketResponse:
    ticket = db.query(Ticket).filter(Ticket.ticket_id == ticket_id).first()
    if not ticket:
        raise HTTPException(status_code=404, detail="Ticket not found.")

    now_utc = datetime.now(timezone.utc)
    description = payload.issue_description.strip()
    target = (
        TicketStatus.PENDING_LOCAL_PART
        if payload.resolution_type == ResolutionType.LOCAL
        else TicketStatus.PENDING_HO_ACTION
    )
    move_ticket(db, ticket, target, actor, note=description)
    ticket.issue_description = description
    ticket.resolution_type = payload.resolution_type
    ticket.inspected_at = now_utc

    if payload.resolution_type == ResolutionType.LOCAL:
        ticket.eta_date = payload.eta_date
        ticket.ho_received_at = None
    else:
        ticket.eta_date = None
        ticket.ho_received_at = now_utc

    db.commit()
    db.refresh(ticket)
    return TicketResponse.model_validate(ticket)


@app.put("/api/tickets/{ticket_id}/ho-update", response_model=TicketResponse)
def update_ho_ticket(
    ticket_id: str,
    payload: TicketHOUpdateRequest,
    actor: User = Depends(require_roles(UserRole.HEAD_OFFICE)),
    db: Session = Depends(get_db),
) -> TicketResponse:
    ticket = db.query(Ticket).filter(Ticket.ticket_id == ticket_id).first()
    if not ticket:
        raise HTTPException(status_code=404, detail="Ticket not found.")

    po = payload.po_details.strip()
    move_ticket(db, ticket, TicketStatus.PENDING_ARRIVAL, actor, note=po)
    ticket.po_details = po
    ticket.eta_date = payload.eta_date
    db.commit()
    db.refresh(ticket)
    return TicketResponse.model_validate(ticket)


@app.put("/api/tickets/{ticket_id}/resolve", response_model=TicketResponse)
def resolve_ticket(
    ticket_id: str,
    actor: User = Depends(require_roles(UserRole.SUPERVISOR)),
    db: Session = Depends(get_db),
) -> TicketResponse:
    ticket = db.query(Ticket).filter(Ticket.ticket_id == ticket_id).first()
    if not ticket:
        raise HTTPException(status_code=404, detail="Ticket not found.")

    move_ticket(db, ticket, TicketStatus.RESOLVED, actor)
    ticket.closed_at = datetime.now(timezone.utc)
    machine = db.query(Machine).filter(Machine.machine_id == ticket.machine_id).first()
    if machine:
        machine.status = MachineStatus.ACTIVE

    db.commit()
    db.refresh(ticket)
    return TicketResponse.model_validate(ticket)


@app.post("/api/reset-demo")
def reset_demo(
    _: User = Depends(require_roles(UserRole.MANAGEMENT)), db: Session = Depends(get_db)
) -> dict:
    """Wipes all data and re-seeds the demo set. Disable with DEMO_RESET=0."""
    if os.getenv("DEMO_RESET", "1") == "0":
        raise HTTPException(status_code=403, detail="Demo reset is disabled.")
    db.query(TicketEvent).delete()
    db.query(Ticket).delete()
    db.query(Machine).delete()
    db.query(User).delete()
    db.commit()
    seed_initial_data(db)
    return {"ok": True}


@app.get("/api/tickets/{ticket_id}/events")
def get_ticket_events(
    ticket_id: str, _: User = Depends(get_current_user), db: Session = Depends(get_db)
) -> list:
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
            "created_at": e.created_at.isoformat(),
        }
        for e, name in rows
    ]


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


def overdue_by_stage(db: Session) -> dict:
    counts: dict = {}
    for t in db.query(Ticket).filter(Ticket.status != TicketStatus.RESOLVED).all():
        r = TicketResponse.model_validate(t)
        if r.is_overdue:
            counts[r.status.value] = counts.get(r.status.value, 0) + 1
    return counts


@app.get("/api/dashboard/metrics")
def get_dashboard_metrics(_: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict:
    broken_count = (
        db.query(func.count(Machine.machine_id))
        .filter(Machine.status == MachineStatus.BROKEN_DOWN)
        .scalar()
        or 0
    )
    status_counts = (
        db.query(Ticket.status, func.count(Ticket.ticket_id))
        .filter(Ticket.status != TicketStatus.RESOLVED)
        .group_by(Ticket.status)
        .all()
    )
    bottlenecks = {
        "pending_inspection": 0,
        "pending_local_part": 0,
        "pending_ho_action": 0,
        "pending_arrival": 0,
    }
    for st, count in status_counts:
        key = st.value if isinstance(st, TicketStatus) else str(st)
        bottlenecks[key] = count

    resolved = (
        db.query(Ticket)
        .filter(Ticket.status == TicketStatus.RESOLVED, Ticket.closed_at.isnot(None))
        .all()
    )
    avg_hours = None
    if resolved:
        total_h = 0.0
        for t in resolved:
            c_at = t.created_at if t.created_at.tzinfo else t.created_at.replace(tzinfo=timezone.utc)
            cl_at = t.closed_at if t.closed_at.tzinfo else t.closed_at.replace(tzinfo=timezone.utc)
            total_h += (cl_at - c_at).total_seconds() / 3600.0
        avg_hours = round(total_h / len(resolved), 2)

    return {
        "total_machines_broken": broken_count,
        "active_bottlenecks": bottlenecks,
        "average_resolution_time_hours": avg_hours,
        "total_resolved_tickets": len(resolved),
        "stage_average_hours": stage_average_hours(db),
        "overdue_by_stage": overdue_by_stage(db),
    }


# Serve PWA static files & root UI
STATIC_DIR = FilePath(__file__).parent / "static"
if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/", response_class=HTMLResponse)
def serve_pwa():
    index_file = STATIC_DIR / "index.html"
    if index_file.exists():
        return FileResponse(str(index_file))
    return HTMLResponse("<h1>SiteGuard API Running — Visit /docs</h1>")

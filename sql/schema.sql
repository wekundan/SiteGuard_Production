-- ============================================================================
-- Machinery Breakdown Tracking System - Phase 1 PostgreSQL Schema
-- ============================================================================

-- Enable pgcrypto for UUID generation (gen_random_uuid() is built-in on PG 13+)
CREATE EXTENSION IF NOT EXISTS "pgcrypto";

-- ============================================================================
-- 1. ENUM Types
-- ============================================================================

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'user_role') THEN
        CREATE TYPE user_role AS ENUM (
            'site_office',
            'supervisor',
            'head_office',
            'management'
        );
    END IF;

    IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'machine_status') THEN
        CREATE TYPE machine_status AS ENUM (
            'active',
            'broken_down'
        );
    END IF;

    IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'ticket_status') THEN
        CREATE TYPE ticket_status AS ENUM (
            'pending_inspection',
            'pending_local_part',
            'pending_ho_action',
            'pending_arrival',
            'resolved'
        );
    END IF;

    IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'resolution_type') THEN
        CREATE TYPE resolution_type AS ENUM (
            'local',
            'head_office'
        );
    END IF;
END$$;

-- ============================================================================
-- 2. Tables
-- ============================================================================

-- Table: Users
CREATE TABLE IF NOT EXISTS users (
    user_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name VARCHAR(255) NOT NULL,
    role user_role NOT NULL
);

-- Table: Machines
CREATE TABLE IF NOT EXISTS machines (
    machine_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name VARCHAR(255) NOT NULL,
    location VARCHAR(255) NOT NULL,
    status machine_status NOT NULL DEFAULT 'active'
);

-- Table: Tickets (The Core Engine)
CREATE TABLE IF NOT EXISTS tickets (
    ticket_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    machine_id UUID NOT NULL REFERENCES machines(machine_id) ON DELETE RESTRICT,
    reported_by UUID NOT NULL REFERENCES users(user_id) ON DELETE RESTRICT,
    issue_description TEXT NULL,
    status ticket_status NOT NULL DEFAULT 'pending_inspection',
    resolution_type resolution_type NULL,
    po_details TEXT NULL,
    eta_date DATE NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    inspected_at TIMESTAMPTZ NULL,
    ho_received_at TIMESTAMPTZ NULL,
    closed_at TIMESTAMPTZ NULL,

    -- Business logic integrity constraints
    CONSTRAINT chk_local_sourcing_requires_eta
        CHECK (
            resolution_type IS DISTINCT FROM 'local'
            OR eta_date IS NOT NULL
        ),
    CONSTRAINT chk_ho_escalation_timestamp
        CHECK (
            status IS DISTINCT FROM 'pending_ho_action'
            OR ho_received_at IS NOT NULL
        ),
    CONSTRAINT chk_resolved_requires_closed_at
        CHECK (
            status IS DISTINCT FROM 'resolved'
            OR closed_at IS NOT NULL
        )
);

-- ============================================================================
-- 3. Indexes for Role Queues & Management Analytics
-- ============================================================================

CREATE INDEX IF NOT EXISTS idx_tickets_status ON tickets(status);
CREATE INDEX IF NOT EXISTS idx_tickets_machine_id ON tickets(machine_id);
CREATE INDEX IF NOT EXISTS idx_tickets_reported_by ON tickets(reported_by);
CREATE INDEX IF NOT EXISTS idx_tickets_created_at ON tickets(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_machines_status ON machines(status);

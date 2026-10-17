-- ============================================================================
-- Machinery Breakdown Tracking System - Phase 2 Seed & Sample Data (PostgreSQL)
-- ============================================================================

INSERT INTO users (user_id, name, role) VALUES
    ('11111111-1111-4111-8111-111111111111', 'Ramesh Kumar', 'site_office'),
    ('22222222-2222-4222-8222-222222222222', 'Deepak Verma', 'site_office'),
    ('33333333-3333-4333-8333-333333333333', 'Vikram Singh', 'supervisor'),
    ('44444444-4444-4444-8444-444444444444', 'Ananya Nair', 'head_office'),
    ('55555555-5555-4555-8555-555555555555', 'Rajeshwari Iyer', 'management')
ON CONFLICT (user_id) DO NOTHING;

INSERT INTO machines (machine_id, name, location, status) VALUES
    ('10000000-0000-4000-8000-000000000001', 'Excavator 3000 (CAT 320D)', 'Site A - Zone 1 (Deep Excavation)', 'broken_down'),
    ('10000000-0000-4000-8000-000000000002', 'Tower Crane TC-6015', 'Site A - Zone 2 (Core Tower)', 'broken_down'),
    ('10000000-0000-4000-8000-000000000003', 'Transit Mixer TM-09', 'Site B - Batching Plant', 'broken_down'),
    ('10000000-0000-4000-8000-000000000004', 'Backhoe Loader JCB-3DX', 'Site B - Utility Trench', 'broken_down'),
    ('10000000-0000-4000-8000-000000000005', 'Motor Grader MG-140', 'Site A - Access Road', 'broken_down'),
    ('10000000-0000-4000-8000-000000000006', 'Vibro Roller VR-210', 'Site A - Embankment', 'active'),
    ('10000000-0000-4000-8000-000000000007', 'Concrete Boom Placer BP-42', 'Site B - Podium Slab', 'active'),
    ('10000000-0000-4000-8000-000000000008', 'Wheel Loader WL-950', 'Site A - Aggregate Stockpile', 'active')
ON CONFLICT (machine_id) DO NOTHING;

INSERT INTO tickets (
    ticket_id,
    machine_id,
    reported_by,
    issue_description,
    status,
    resolution_type,
    po_details,
    eta_date,
    created_at,
    inspected_at,
    ho_received_at,
    closed_at
) VALUES
    -- Overdue Pending Inspection (> 2 hours old)
    (
        '90000000-0000-4000-8000-000000000101',
        '10000000-0000-4000-8000-000000000001',
        '11111111-1111-4111-8111-111111111111',
        NULL,
        'pending_inspection',
        NULL,
        NULL,
        NULL,
        NOW() - INTERVAL '3 hours 25 minutes',
        NULL,
        NULL,
        NULL
    ),
    -- Recent Pending Inspection (< 2 hours old)
    (
        '90000000-0000-4000-8000-000000000102',
        '10000000-0000-4000-8000-000000000002',
        '11111111-1111-4111-8111-111111111111',
        NULL,
        'pending_inspection',
        NULL,
        NULL,
        NULL,
        NOW() - INTERVAL '45 minutes',
        NULL,
        NULL,
        NULL
    ),
    -- Escalated to Head Office (pending_ho_action)
    (
        '90000000-0000-4000-8000-000000000103',
        '10000000-0000-4000-8000-000000000003',
        '22222222-2222-4222-8222-222222222222',
        'Main planetary drum gearbox bearing seized; requires OEM replacement assembly from regional depot.',
        'pending_ho_action',
        'head_office',
        NULL,
        NULL,
        NOW() - INTERVAL '6 hours 30 minutes',
        NOW() - INTERVAL '5 hours 12 minutes',
        NOW() - INTERVAL '5 hours 12 minutes',
        NULL
    ),
    -- Sourced Locally (pending_local_part)
    (
        '90000000-0000-4000-8000-000000000104',
        '10000000-0000-4000-8000-000000000004',
        '22222222-2222-4222-8222-222222222222',
        'Burst high-pressure stabilizer hydraulic hose; local hydraulic fabricator crimping replacement.',
        'pending_local_part',
        'local',
        NULL,
        CURRENT_DATE + 1,
        NOW() - INTERVAL '5 hours',
        NOW() - INTERVAL '4 hours 6 minutes',
        NULL,
        NULL
    ),
    -- HO PO Submitted (pending_arrival)
    (
        '90000000-0000-4000-8000-000000000105',
        '10000000-0000-4000-8000-000000000005',
        '11111111-1111-4111-8111-111111111111',
        'Tandem drive chain snapped under grading load; OEM link kit required.',
        'pending_arrival',
        'head_office',
        'PO-2026-4419 (Larsen & Toubro Heavy Spares)',
        CURRENT_DATE + 2,
        NOW() - INTERVAL '14 hours',
        NOW() - INTERVAL '12 hours 48 minutes',
        NOW() - INTERVAL '12 hours 48 minutes',
        NULL
    ),
    -- Resolved Ticket
    (
        '90000000-0000-4000-8000-000000000106',
        '10000000-0000-4000-8000-000000000006',
        '11111111-1111-4111-8111-111111111111',
        'Alternator belt tensioner pulley failed; replaced from local vendor stock.',
        'resolved',
        'local',
        NULL,
        CURRENT_DATE - 1,
        NOW() - INTERVAL '28 hours',
        NOW() - INTERVAL '27 hours 6 minutes',
        NULL,
        NOW() - INTERVAL '21 hours 30 minutes'
    )
ON CONFLICT (ticket_id) DO NOTHING;

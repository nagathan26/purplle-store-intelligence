from __future__ import annotations

from app.sessions import (
    build_sessions,
    correlate_conversions,
    physical_id,
)


class _Row(dict):
    """Mimic sqlite3.Row enough for build_sessions: __getitem__ + .keys()."""

    def keys(self):
        return list(super().keys())


def _row(
    visitor_id="VIS_a",
    event_type="ENTRY",
    ts=0.0,
    *,
    camera_id="CAM_ENTRY_01",
    zone_id=None,
    dwell_ms=0,
):
    return _Row(
        visitor_id=visitor_id,
        event_type=event_type,
        ts_epoch=ts,
        camera_id=camera_id,
        zone_id=zone_id,
        dwell_ms=dwell_ms,
    )


def test_physical_id_strips_reentry_suffix():
    assert physical_id("VIS_abc#2") == "VIS_abc"
    assert physical_id("VIS_xyz") == "VIS_xyz"


def test_physical_id_large_suffix():
    assert physical_id("VIS_abc#99") == "VIS_abc"


def test_physical_id_nested_suffix():
    assert physical_id("VIS_abc#1#2") == "VIS_abc"


def test_dwell_uses_max_not_sum_of_cumulative_checkpoints():
    rows = [
        _row("VIS_a", "ZONE_ENTER", ts=0, zone_id="SKINCARE"),
        _row(
            "VIS_a",
            "ZONE_DWELL",
            ts=30,
            zone_id="SKINCARE",
            dwell_ms=30_000,
        ),
        _row(
            "VIS_a",
            "ZONE_DWELL",
            ts=60,
            zone_id="SKINCARE",
            dwell_ms=60_000,
        ),
        _row(
            "VIS_a",
            "ZONE_EXIT",
            ts=70,
            zone_id="SKINCARE",
            dwell_ms=70_000,
        ),
    ]

    sessions = build_sessions(rows)

    s = next(iter(sessions.values()))

    assert s.dwell_by_zone["SKINCARE"] == 70_000


def test_cross_camera_link_folds_floor_into_entry():
    rows = [
        _row(
            "VIS_entry",
            "ENTRY",
            ts=100,
            camera_id="CAM_ENTRY_01",
        ),
        _row(
            "VIS_floor",
            "ZONE_ENTER",
            ts=110,
            camera_id="CAM_FLOOR_01",
            zone_id="SKINCARE",
        ),
        _row(
            "VIS_floor",
            "ZONE_DWELL",
            ts=140,
            camera_id="CAM_FLOOR_01",
            zone_id="SKINCARE",
            dwell_ms=30_000,
        ),
    ]

    sessions = build_sessions(rows)

    assert len(sessions) == 1

    s = next(iter(sessions.values()))

    assert s.entered
    assert "SKINCARE" in s.zones_visited


def test_cross_camera_link_respects_window():
    rows = [
        _row(
            "VIS_entry",
            "ENTRY",
            ts=0,
            camera_id="CAM_ENTRY_01",
        ),
        _row(
            "VIS_floor",
            "ZONE_ENTER",
            ts=3600,
            camera_id="CAM_FLOOR_01",
            zone_id="HAIRCARE",
        ),
    ]

    sessions = build_sessions(rows)

    assert len(sessions) == 2


def test_link_never_merges_two_entry_cam_sessions():
    rows = [
        _row(
            "VIS_a",
            "ENTRY",
            ts=0,
            camera_id="CAM_ENTRY_01",
        ),
        _row(
            "VIS_b",
            "ENTRY",
            ts=1,
            camera_id="CAM_ENTRY_01",
        ),
    ]

    sessions = build_sessions(rows)

    assert len(sessions) == 2


class _Tx(dict):
    pass


def test_correlate_converts_at_most_one_session_per_transaction():
    rows = [
        _row(
            "VIS_a",
            "BILLING_QUEUE_JOIN",
            ts=100,
            camera_id="CAM_BILL_01",
        ),
        _row(
            "VIS_b",
            "BILLING_QUEUE_JOIN",
            ts=200,
            camera_id="CAM_BILL_01",
        ),
    ]

    sessions = build_sessions(rows)

    correlate_conversions(
        sessions,
        [_Tx(ts_epoch=250)],
    )

    converted = [
        s
        for s in sessions.values()
        if s.converted
    ]

    assert len(converted) == 1
    assert converted[0].visitor_id == "VIS_b"


def test_correlate_two_transactions_match_two_sessions():
    rows = [
        _row(
            "VIS_a",
            "BILLING_QUEUE_JOIN",
            ts=100,
            camera_id="CAM_BILL_01",
        ),
        _row(
            "VIS_b",
            "BILLING_QUEUE_JOIN",
            ts=200,
            camera_id="CAM_BILL_01",
        ),
    ]

    sessions = build_sessions(rows)

    correlate_conversions(
        sessions,
        [
            _Tx(ts_epoch=150),
            _Tx(ts_epoch=250),
        ],
    )

    converted = {
        s.visitor_id
        for s in sessions.values()
        if s.converted
    }

    assert converted == {"VIS_a", "VIS_b"}
# PROMPT: "Write pytest tests for a FastAPI retail analytics service backed by
#          SQLite. Cover the ingest endpoint (happy path, idempotent replay,
#          partial success on a malformed event), the /metrics endpoint excluding
#          staff and handling zero-purchase stores, the /funnel session
#          deduplication so re-entries don't double-count, /heatmap data_confidence
#          flag under 20 sessions, and a graceful 503 when storage is down."
#
# CHANGES MADE:
# - Analytics assertions use window_s=None for deterministic all-time behavior.
# - Added all-staff edge case.
# - Added storage outage test using simulate_outage().
# - Replaced tempfile.mktemp() with pytest tmp_path.
# - Reloads app.main after setting environment variables.
# - Funnel assertions no longer depend on stage ordering.
# - Added explicit default-window test.

import importlib
import os
import uuid
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app import metrics
from app.ingestion import ingest_batch
from app.storage import Store


def _ev(
    vid,
    etype,
    ts,
    store="S1",
    staff=False,
    zone=None,
    dwell=0,
    conf=0.9,
    qd=None,
    camera="CAM_ENTRY_01",
):
    return {
        "event_id": str(uuid.uuid4()),
        "store_id": store,
        "camera_id": camera,
        "visitor_id": vid,
        "event_type": etype,
        "timestamp": ts,
        "zone_id": zone,
        "dwell_ms": dwell,
        "is_staff": staff,
        "confidence": conf,
        "metadata": {
            "queue_depth": qd,
            "sku_zone": zone,
            "session_seq": 1,
        },
    }


def _now_iso(offset_s=0):
    from time import time

    return datetime.fromtimestamp(
        time() + offset_s,
        tz=timezone.utc,
    ).strftime("%Y-%m-%dT%H:%M:%SZ")


def test_ingest_happy_path(store):
    res = ingest_batch(
        store,
        [_ev("VIS_a", "ENTRY", _now_iso())],
    )

    assert res.accepted == 1
    assert res.rejected == 0
    assert res.duplicates == 0


def test_ingest_is_idempotent(store):
    e = _ev("VIS_a", "ENTRY", _now_iso())

    ingest_batch(store, [e])

    res = ingest_batch(
        store,
        [e],
    )

    assert res.duplicates == 1
    assert res.accepted == 0


def test_ingest_partial_success_on_malformed(store):
    good = _ev("VIS_a", "ENTRY", _now_iso())

    bad = {
        "event_id": "not-a-uuid",
        "store_id": "S1",
    }

    res = ingest_batch(
        store,
        [good, bad],
    )

    assert res.accepted == 1
    assert res.rejected == 1
    assert res.errors[0].index == 1


def test_metrics_excludes_staff(store):
    now = _now_iso()

    ingest_batch(
        store,
        [
            _ev("VIS_cust", "ENTRY", now),
            _ev("VIS_staff", "ENTRY", now, staff=True),
        ],
    )

    m = metrics.compute_metrics(
        store,
        "S1",
        window_s=None,
    )

    assert m["unique_visitors"] == 1


def test_metrics_zero_purchase_store_no_crash(store):
    ingest_batch(
        store,
        [_ev("VIS_a", "ENTRY", _now_iso())],
    )

    m = metrics.compute_metrics(
        store,
        "S1",
        window_s=None,
    )

    assert m["conversion_rate"] == 0.0
    assert m["queue_abandonment_rate"] == 0.0


def test_metrics_empty_store_returns_zeroes(store):
    m = metrics.compute_metrics(
        store,
        "GHOST_STORE",
        window_s=None,
    )

    assert m["unique_visitors"] == 0
    assert m["conversion_rate"] == 0.0


def test_all_staff_clip_yields_no_customers(store):
    now = _now_iso()

    ingest_batch(
        store,
        [
            _ev("VIS_s1", "ENTRY", now, staff=True),
            _ev("VIS_s2", "ENTRY", now, staff=True),
        ],
    )

    m = metrics.compute_metrics(
        store,
        "S1",
        window_s=None,
    )

    f = metrics.compute_funnel(
        store,
        "S1",
        window_s=None,
    )

    assert m["unique_visitors"] == 0

    visitors = next(
        s
        for s in f["stages"]
        if s["name"] == "Visitors"
    )

    assert visitors["count"] == 0


def test_funnel_reentry_not_double_counted(store):
    now = _now_iso()

    ingest_batch(
        store,
        [
            _ev("VIS_x", "ENTRY", now),
            _ev("VIS_x", "EXIT", now),
            _ev("VIS_x#1", "REENTRY", now),
        ],
    )

    f = metrics.compute_funnel(
        store,
        "S1",
        window_s=None,
    )

    visitors = next(
        s
        for s in f["stages"]
        if s["name"] == "Visitors"
    )

    assert visitors["count"] == 1


def test_heatmap_low_confidence_flag(store):
    now = _now_iso()

    ingest_batch(
        store,
        [
            _ev(
                f"VIS_{i}",
                "ZONE_ENTER",
                now,
                zone="SKINCARE",
            )
            for i in range(5)
        ],
    )

    hm = metrics.compute_heatmap(
        store,
        "S1",
        window_s=None,
    )

    assert hm["data_confidence"] == "LOW"


def test_conversion_correlation_by_time_window(store):
    now = _now_iso()

    ingest_batch(
        store,
        [
            _ev(
                "VIS_buyer",
                "BILLING_QUEUE_JOIN",
                now,
                zone="BILLING",
                qd=1,
            )
        ],
    )

    from time import time

    tx_ts = datetime.fromtimestamp(
        time() + 120,
        tz=timezone.utc,
    ).strftime("%Y-%m-%dT%H:%M:%SZ")

    store.load_pos(
        [
            {
                "transaction_id": "T1",
                "store_id": "S1",
                "timestamp": tx_ts,
                "basket_value_inr": "500",
            }
        ]
    )

    m = metrics.compute_metrics(
        store,
        "S1",
        window_s=None,
    )

    assert m["converted_visitors"] == 1


def test_ingest_oversize_batch_rejected_with_422(store):
    from app.models import IngestRequest
    from pydantic import ValidationError
    import pytest as _pt

    over = [
        _ev(f"VIS_{i}", "ENTRY", _now_iso())
        for i in range(501)
    ]

    with _pt.raises(ValidationError):
        IngestRequest.model_validate(
            {"events": over}
        )


def test_metrics_default_window_returns_dict(store):
    ingest_batch(
        store,
        [_ev("VIS_a", "ENTRY", _now_iso())],
    )

    m = metrics.compute_metrics(
        store,
        "S1",
    )

    assert isinstance(m, dict)
    assert "unique_visitors" in m


def test_503_on_storage_outage(tmp_path):
    db_path = tmp_path / "outage_test.db"

    os.environ["DB_PATH"] = str(db_path)
    os.environ["POS_PATH"] = "/nonexistent.csv"

    import app.main

    importlib.reload(app.main)

    app = app.main.app

    with TestClient(app) as c:
        live_store = app.main.store

        live_store.simulate_outage(True)

        try:
            r = c.get("/stores/S1/metrics")

            assert r.status_code == 503

            body = r.json()

            assert body["error"] == "storage_unavailable"
            assert "Traceback" not in str(body)

        finally:
            live_store.simulate_outage(False)
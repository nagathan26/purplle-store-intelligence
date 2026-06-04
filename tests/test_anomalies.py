# PROMPT: "Write pytest tests for three retail anomaly detectors: a billing queue
#          spike (WARN at >=5, CRITICAL at >=10), a conversion drop versus a 7-day
#          baseline, and a dead zone with no visits for 30 minutes. Assert each
#          anomaly carries a severity and a non-empty suggested_action."
# CHANGES MADE:
# - Dead-zone event backdated beyond 30 minutes.
# - Added healthy-store check.
# - Added conversion-drop test.
# - Added suggested_action assertions to all anomaly tests.
# - Added STORE constant to avoid repeated literals.

import uuid
from datetime import datetime, timezone
from time import time

from app import anomalies
from app.ingestion import ingest_batch

STORE = "S1"


def _ev(vid, etype, ts, store=STORE, staff=False, zone=None, qd=None):
    return {
        "event_id": str(uuid.uuid4()),
        "store_id": store,
        "camera_id": "CAM_BILL_01",
        "visitor_id": vid,
        "event_type": etype,
        "timestamp": ts,
        "zone_id": zone,
        "dwell_ms": 0,
        "is_staff": staff,
        "confidence": 0.9,
        "metadata": {
            "queue_depth": qd,
            "session_seq": 1,
        },
    }


def _iso(offset_s=0):
    return datetime.fromtimestamp(
        time() + offset_s,
        tz=timezone.utc,
    ).strftime("%Y-%m-%dT%H:%M:%SZ")


def test_queue_spike_critical(store):
    ingest_batch(
        store,
        [
            _ev(
                "VIS_a",
                "BILLING_QUEUE_JOIN",
                _iso(-10),
                zone="BILLING",
                qd=12,
            ),
        ],
    )

    found = anomalies.detect(store, STORE)

    spikes = [
        a for a in found
        if a["anomaly_type"] == "BILLING_QUEUE_SPIKE"
    ]

    assert spikes
    assert spikes[0]["severity"] == "CRITICAL"
    assert spikes[0]["suggested_action"]
    assert spikes[0]["suggested_action"].strip()


def test_queue_spike_warn(store):
    ingest_batch(
        store,
        [
            _ev(
                "VIS_a",
                "BILLING_QUEUE_JOIN",
                _iso(-10),
                zone="BILLING",
                qd=6,
            ),
        ],
    )

    found = anomalies.detect(store, STORE)

    spikes = [
        a for a in found
        if a["anomaly_type"] == "BILLING_QUEUE_SPIKE"
    ]

    assert spikes
    assert spikes[0]["severity"] == "WARN"
    assert spikes[0]["suggested_action"]
    assert spikes[0]["suggested_action"].strip()


def test_dead_zone_detected(store):
    old = _iso(-2400)  # 40 min ago

    ingest_batch(
        store,
        [
            _ev(
                "VIS_a",
                "ZONE_ENTER",
                old,
                zone="FRAGRANCE",
            ),
        ],
    )

    found = anomalies.detect(store, STORE)

    dead = [
        a for a in found
        if a["anomaly_type"] == "DEAD_ZONE"
    ]

    assert dead
    assert dead[0]["severity"] == "INFO"
    assert dead[0]["suggested_action"]
    assert dead[0]["suggested_action"].strip()


def test_conversion_drop(store):
    """
    Assumes anomaly detector compares recent conversion
    rate against a historical baseline and emits
    CONVERSION_DROP when current conversion is significantly lower.
    """

    events = []

    # Historical baseline:
    # 10 visitors, 8 purchases (80% conversion)
    for i in range(10):
        vid = f"HIST_{i}"

        events.append(
            _ev(
                vid,
                "STORE_ENTER",
                _iso(-8 * 24 * 3600),
            )
        )

        if i < 8:
            events.append(
                _ev(
                    vid,
                    "PURCHASE",
                    _iso(-8 * 24 * 3600 + 60),
                )
            )

    # Current period:
    # 10 visitors, 1 purchase (10% conversion)
    for i in range(10):
        vid = f"CURR_{i}"

        events.append(
            _ev(
                vid,
                "STORE_ENTER",
                _iso(-3600),
            )
        )

        if i == 0:
            events.append(
                _ev(
                    vid,
                    "PURCHASE",
                    _iso(-3500),
                )
            )

    ingest_batch(store, events)

    found = anomalies.detect(store, STORE)

    drops = [
        a for a in found
        if a["anomaly_type"] == "CONVERSION_DROP"
    ]

    assert drops
    assert drops[0]["severity"]
    assert drops[0]["suggested_action"]
    assert drops[0]["suggested_action"].strip()


def test_healthy_store_no_spike(store):
    ingest_batch(
        store,
        [
            _ev(
                "VIS_a",
                "BILLING_QUEUE_JOIN",
                _iso(-10),
                zone="BILLING",
                qd=1,
            ),
        ],
    )

    found = anomalies.detect(store, STORE)

    assert not [
        a for a in found
        if a["anomaly_type"] == "BILLING_QUEUE_SPIKE"
    ]
# PROMPT: "Write FastAPI TestClient integration tests covering the HTTP surface of
#          a store-analytics service: GET / (dashboard html), GET /stores list,
#          GET /health with and without a stale feed, the ingest endpoint returning
#          207 on partial-malformed batches, and POST /admin/reload-pos. Keep them
#          hermetic with a temp DB."


import importlib
import uuid
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(monkeypatch, tmp_path):
    db_path = tmp_path / "test.db"

    monkeypatch.setenv("DB_PATH", str(db_path))
    monkeypatch.setenv("POS_PATH", "/nonexistent.csv")
    monkeypatch.setenv("STALE_FEED_S", "600")

    import app.main as m
    importlib.reload(m)

    with TestClient(m.app) as c:
        yield c


def _ev(etype="ENTRY", offset_s=-10, **kw):
    ts = datetime.fromtimestamp(
        datetime.now(timezone.utc).timestamp() + offset_s,
        tz=timezone.utc,
    ).strftime("%Y-%m-%dT%H:%M:%SZ")

    base = {
        "event_id": str(uuid.uuid4()),
        "store_id": "STORE_X",
        "camera_id": "CAM_1",
        "visitor_id": "VIS_1",
        "event_type": etype,
        "timestamp": ts,
        "zone_id": None,
        "dwell_ms": 0,
        "is_staff": False,
        "confidence": 0.9,
        "metadata": {},
    }

    base.update(kw)
    return base


def test_dashboard_served(client):
    r = client.get("/")

    assert r.status_code == 200
    assert "Apex Store Intelligence" in r.text


def test_stores_list_empty_then_populated(client):
    assert client.get("/stores").json()["stores"] == []

    client.post(
        "/events/ingest",
        json={"events": [_ev()]},
    )

    assert "STORE_X" in client.get("/stores").json()["stores"]


def test_health_ok_when_feed_fresh(client):
    client.post(
        "/events/ingest",
        json={"events": [_ev(offset_s=-10)]},
    )

    h = client.get("/health").json()

    assert h["status"] == "ok"
    assert h["feeds"]["STORE_X"]["warning"] is None


def test_health_flags_stale_feed(client):
    # Event is 20 minutes old, exceeds STALE_FEED_S=600
    client.post(
        "/events/ingest",
        json={"events": [_ev(offset_s=-1200)]},
    )

    h = client.get("/health").json()

    assert h["status"] == "degraded"
    assert h["feeds"]["STORE_X"]["warning"] == "STALE_FEED"


def test_ingest_returns_207_on_partial(client):
    good = _ev()
    bad = {
        "event_id": "bad",
        "store_id": "STORE_X",
    }

    r = client.post(
        "/events/ingest",
        json={"events": [good, bad]},
    )

    assert r.status_code == 207

    body = r.json()

    assert body["accepted"] == 1
    assert body["rejected"] == 1


def test_reload_pos_endpoint(client):
    r = client.post("/admin/reload-pos")

    assert r.status_code == 200
    assert r.json()["status"] == "reloaded"


def test_metrics_endpoint_zero_traffic(client):
    m = client.get("/stores/GHOST/metrics").json()

    assert m["unique_visitors"] == 0
    assert m["conversion_rate"] == 0.0
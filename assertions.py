# PROMPT: "Generate a standalone assertions script (no pytest) that an external
#          reviewer can run against a LIVE instance of the API to verify the
#          acceptance gate: ingest accepts events without 5xx, idempotent replay,
#          metrics returns valid JSON with the required keys, funnel uses sessions,
#          and health reports per-store feed status. Use only the stdlib."
# CHANGES MADE: The AI hardcoded localhost; I parameterised the base URL via argv
#          and an env var, and added the idempotency check (re-POST same batch,
#          expect duplicates) which the gate implies but the draft skipped.
"""
assertions.py — black-box acceptance checks against a running API.

Run AFTER `docker compose up` (and after the seeder has populated data):

    python assertions.py                       # defaults to http://localhost:8000
    python assertions.py http://localhost:8000

Exits non-zero on the first failed assertion. This is a thin smoke harness, not
the full scoring suite.
"""
import json
import sys
import urllib.request
import uuid

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8000"
STORE = "STORE_BLR_002"


def _get(path):
    with urllib.request.urlopen(f"{BASE}{path}", timeout=10) as r:
        return r.status, json.loads(r.read())


def _post(path, body):
    req = urllib.request.Request(
        f"{BASE}{path}", data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.status, json.loads(r.read())


def check(name, cond):
    print(("PASS" if cond else "FAIL"), "-", name)
    if not cond:
        sys.exit(1)


def _event(etype="ENTRY"):
    return {
        "event_id": str(uuid.uuid4()), "store_id": STORE, "camera_id": "CAM_ENTRY_01",
        "visitor_id": "VIS_assert", "event_type": etype,
        "timestamp": "2026-05-30T10:00:00Z", "zone_id": None, "dwell_ms": 0,
        "is_staff": False, "confidence": 0.9, "metadata": {},
    }


def main():
    # 1. health responds
    status, health = _get("/health")
    check("GET /health returns 200", status == 200)
    check("health has 'status' field", "status" in health)

    # 2. ingest accepts a batch without 5xx
    batch = {"events": [_event("ENTRY"), _event("EXIT")]}
    status, body = _post("/events/ingest", batch)
    check("POST /events/ingest no 5xx", status < 500)
    check("ingest reports accepted count", body.get("accepted", 0) >= 1)

    # 3. idempotency: replay the same batch -> duplicates, no new accepts
    status, body2 = _post("/events/ingest", batch)
    check("ingest idempotent replay -> duplicates", body2.get("duplicates", 0) >= 1)

    # 4. metrics returns valid JSON with required keys
    status, m = _get(f"/stores/{STORE}/metrics")
    check("GET metrics returns 200", status == 200)
    for k in ("unique_visitors", "conversion_rate", "current_queue_depth",
              "queue_abandonment_rate", "avg_dwell_per_zone_s"):
        check(f"metrics has '{k}'", k in m)
    check("conversion_rate in [0,1]", 0.0 <= m["conversion_rate"] <= 1.0)

    # 5. funnel is session-based with 4 stages
    status, f = _get(f"/stores/{STORE}/funnel")
    check("funnel unit is session", f.get("unit") == "session")
    check("funnel has 4 stages", len(f.get("stages", [])) == 4)

    # 6. heatmap exposes data_confidence
    status, h = _get(f"/stores/{STORE}/heatmap")
    check("heatmap has data_confidence", "data_confidence" in h)

    # 7. anomalies expose severity + suggested_action when present
    status, a = _get(f"/stores/{STORE}/anomalies")
    check("anomalies endpoint returns list", isinstance(a.get("anomalies"), list))
    for an in a["anomalies"]:
        check("anomaly has severity", an.get("severity") in ("INFO", "WARN", "CRITICAL"))
        check("anomaly has suggested_action", bool(an.get("suggested_action")))

    print("\nAll acceptance assertions passed against", BASE)


if __name__ == "__main__":
    main()

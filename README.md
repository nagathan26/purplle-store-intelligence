# Apex Store Intelligence

End-to-end pipeline that turns raw retail CCTV into a live, queryable analytics API.
Raw video → person detection & tracking → structured behavioural events → an
Intelligence API computing the **North Star metric, offline-store conversion rate** →
a live dashboard. Every stage is implemented and connected.

```
 CCTV clips ─▶ Detection Layer ─▶ Event Stream ─▶ Intelligence API ─▶ Live Dashboard
 (mp4)         YOLOv8+ByteTrack    JSON events      FastAPI + SQLite    polling web UI
               + Re-ID tracker     (schema below)   metrics/funnel/…    (Part E)
```

---

## TL;DR — run it in 5 commands

```bash
git clone <repo-url> store-intelligence
cd store-intelligence
docker compose up --build          # starts API + auto-discovers CCTV clips
# wait for the API to come up, then:
open http://localhost:8000         # live dashboard
python assertions.py               # black-box acceptance checks (needs Python 3)
```

`docker compose up` builds the API image, starts it on port 8000, and the API's
lifespan launches a watcher thread that scans `CCTV Footage/` for `.mp4` clips and
spawns a `pipeline/detect.py` subprocess per clip. Each detector run posts events
through the real `/events/ingest` endpoint as it processes frames, so the dashboard
and analytics endpoints fill with live data while detection is in flight.

A CPU-only host works (the Dockerfile uses a PyTorch CUDA base for portability, but
detection falls back to CPU when no GPU is visible) — the `docker-compose.yml`
deliberately does **not** require an NVIDIA device so the acceptance gate is not
blocked on reviewer hardware.

---

## What runs when you `docker compose up`

1. The API starts and clears the events table for a clean re-run (see
   `app/main.py:lifespan`). The clips themselves are unchanged.
2. The watcher thread enumerates `*.mp4` files in `CCTV Footage/`, parses
   `store_id` / `camera_id` from the filename, and starts one detector
   subprocess per clip (`pipeline/detect.py`).
3. Each detector loads YOLOv8 + ByteTrack, runs through the clip on CPU or GPU,
   and POSTs events in batches of ≤500 to `/events/ingest` as they are produced.
4. When the billing-camera clip finishes, the API re-reads `pos.csv` so
   conversion correlation has POS data available.
5. The dashboard at `/` polls the analytics endpoints every two seconds and
   updates conversion, the funnel, the heatmap, and active anomalies live.

You can also run the detector by hand against a specific clip (see below).

---

## API surface

| Method & path | Returns |
|---|---|
| `POST /events/ingest` | Accepts ≤500 events. Validates, **deduplicates by `event_id`**, stores. Idempotent. Returns counts + per-record errors. `200` clean / `207` partial. |
| `GET /stores/{id}/metrics` | Unique visitors, conversion rate, avg dwell per zone, current queue depth, abandonment rate. Excludes staff. Computed live. |
| `GET /stores/{id}/funnel` | Session-based funnel Entry → Zone visit → Billing queue → Purchase with drop-off %. Re-entries are not double-counted. |
| `GET /stores/{id}/heatmap` | Per-zone visit frequency + avg dwell, normalised 0–100, with a `data_confidence` flag when < 20 sessions. |
| `GET /stores/{id}/anomalies` | Active anomalies (queue spike, conversion drop vs 7-day, dead zone) with `severity` and `suggested_action`. |
| `GET /health` | Service status + last-event timestamp and lag per store, with `STALE_FEED` warning when lag > 10 min. |
| `GET /stores` | List of stores with ingested data. |
| `GET /pipeline/status` | Status of all known clips (PROCESSING / COMPLETED / FAILED) with % progress. |
| `GET /` | Live dashboard (Part E). |
| `POST /admin/reload-pos` | Re-reads the POS CSV from the data volume. |

### Event schema (the pipeline emits, the API ingests)

```json
{
  "event_id": "uuid-v4",
  "store_id": "STORE_BLR_002",
  "camera_id": "CAM_ENTRY_01",
  "visitor_id": "VIS_c8a2f1",
  "event_type": "ZONE_DWELL",
  "timestamp": "2026-03-03T14:22:10Z",
  "zone_id": "SKINCARE",
  "dwell_ms": 8400,
  "is_staff": false,
  "confidence": 0.91,
  "metadata": { "queue_depth": null, "sku_zone": "MOISTURISER", "session_seq": 5 }
}
```

Event types: `ENTRY`, `EXIT`, `ZONE_ENTER`, `ZONE_EXIT`, `ZONE_DWELL`,
`BILLING_QUEUE_JOIN`, `BILLING_QUEUE_ABANDON`, `REENTRY`.

---

## Running detection on a specific clip by hand

```bash
pip install -r requirements-detection.txt     # YOLOv8 + OpenCV (heavy; CPU is fine)

python pipeline/detect.py \
  --clip "CCTV Footage/CAM 1.mp4" \
  --store STORE_BLR_002 --camera CAM_ENTRY_01 \
  --start 2026-05-30T10:00:00Z \
  --api http://localhost:8000 \
  --out data_output_cam1.jsonl
```

Or process all clips at once via the seeder (idempotent — the API ingest dedupes
on `event_id`):

```bash
API_URL=http://localhost:8000 ./pipeline/run.sh
```

---

## Live dashboard (Part E)

Open `http://localhost:8000` once the API is up. It polls the metrics / funnel /
heatmap / anomaly endpoints every 2 seconds and updates conversion rate, the
funnel, the zone heatmap, and active anomalies in place.

To replay the generated JSONL events on a wall clock (so the dashboard moves in
real time even after detection has finished), use the streamer:

```bash
API_URL=http://localhost:8000 python pipeline/stream.py --speed 60
```

This re-stamps the JSONL events to "now" and posts them in small batches, so the
conversion number and queue anomalies visibly change as traffic arrives — proof
the pipeline and API are genuinely connected, not batch-loaded.

---

## Running locally without Docker (virtualenv)

Everything runs inside a single virtualenv — no global installs.

**macOS / Linux (bash):**
```bash
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt -r requirements-detection.txt   # detection needs lapx (ByteTrack)
export PYTHONPATH=$PWD DB_PATH=./store.db POS_PATH=./pos.csv
uvicorn app.main:app --port 8000                # API + CCTV watcher in one process
```

**Windows (PowerShell):**
```powershell
python -m venv venv ; .\venv\Scripts\Activate.ps1
pip install -r requirements.txt -r requirements-detection.txt
$env:PYTHONPATH = $PWD ; $env:DB_PATH = ".\store.db" ; $env:POS_PATH = ".\pos.csv"
python -m uvicorn app.main:app --port 8000
```

On startup the lifespan clears the events table and the watcher discovers every
`*.mp4` under `CCTV Footage/`, mapping each to a camera role (see
`app/parse_clip_info` / `store_layout.json`) and spawning one `pipeline/detect.py`
subprocess per clip. The detectors POST events to the same API as they run, so
`/stores/STORE_BLR_002/metrics` and the dashboard fill in live. The first detector
run downloads `yolov8n.pt` (~6 MB) if it isn't already present.

> The supplied clips are a ~2.5-minute sample at 25–30 fps (not the 20-minute clips
> the spec describes), so detection finishes in a few minutes per clip on CPU.

## Tests

```bash
pip install -r requirements.txt pytest pytest-cov
PYTHONPATH=$PWD pytest --cov=app --cov=pipeline
# 37 tests, ~94% statement coverage on the API + session + tracker logic.
# `.coveragerc` omits the heavy CV-only modules (pipeline/detect.py, seed.py,
# stream.py and the CCTV-watcher driver in app/main.py); those run end-to-end
# in `docker compose up` against the real clips but can't run in unit tests.
```

Edge cases covered explicitly: empty store, all-staff clip, zero purchases,
re-entry in the funnel, cross-camera session linking, partial-success ingest,
idempotent replay, storage outage → 503, stale feed detection, dead-zone anomaly,
queue-spike WARN/CRITICAL thresholds.

---

## Repository layout

```
store-intelligence/
├── app/                     # Intelligence API (FastAPI)
│   ├── main.py              # entrypoint, routes, lifespan, CCTV watcher
│   ├── models.py            # Pydantic event schema + responses
│   ├── storage.py           # SQLite store, idempotency, graceful degradation
│   ├── ingestion.py         # validate → dedup → store, partial success
│   ├── sessions.py          # session reconstruction + re-entry dedup + cross-camera linker + POS correlation
│   ├── metrics.py           # metrics, funnel, heatmap
│   ├── anomalies.py         # queue spike / conversion drop / dead zone
│   ├── logging_mw.py        # structured per-request JSON logging
│   └── static/dashboard.html
├── pipeline/                # Detection layer
│   ├── detect.py            # YOLOv8 + ByteTrack + per-camera event logic
│   ├── tracker.py           # Re-ID / re-entry / stale-track sweep
│   ├── emit.py              # schema-compliant event construction + POST
│   ├── seed.py              # one-shot driver: clears DB, processes all clips
│   ├── stream.py            # simulated real-time JSONL replay (dashboard demo)
│   └── run.sh               # one command: clips → events → API
├── tests/                   # pytest suite (prompt blocks at file tops)
├── docs/
│   ├── DESIGN.md            # architecture + AI-assisted decisions
│   └── CHOICES.md           # model / schema / API decisions with reasoning
├── assertions.py            # black-box acceptance smoke checks
├── store_layout.json        # zones, per-camera coverage, open hours
├── yolov8n.pt               # YOLOv8-nano weights (baked into the image)
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
└── requirements-detection.txt
```

## Configuration

| Env var | Default | Meaning |
|---|---|---|
| `DB_PATH` | `/data/store.db` | SQLite file path |
| `POS_PATH` | `/data/pos_transactions.csv` | POS file loaded at startup / reload |
| `STALE_FEED_S` | `600` | Feed-lag threshold for the health warning |
| `START_WATCHER` | `1` | Set `0` to disable the CCTV watcher (tests/dev) |
| `POS_REBASE_TO_NOW` | `1` | Align the POS export onto the run's start time so conversion correlates (see DESIGN.md → Timeline alignment). Set `0` to keep raw POS timestamps |
| `API_URL` | `http://localhost:8000` | Target API for pipeline scripts |

See `docs/DESIGN.md` for the architecture rationale and `docs/CHOICES.md` for the
model, schema, and API decisions.

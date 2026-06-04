"""
FastAPI entrypoint for the Store Intelligence API.
"""
from __future__ import annotations

import csv
import os
import time
from datetime import datetime, timezone
from pathlib import Path

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles

import subprocess
import re
import threading
import sys

from . import anomalies, metrics
from .ingestion import ingest_batch
from .logging_mw import StructuredLoggingMiddleware
from .models import IngestRequest
from .storage import DatabaseUnavailable, Store

DB_PATH = os.environ.get("DB_PATH", "/data/store.db")

if "POS_PATH" in os.environ and os.path.exists(os.environ["POS_PATH"]):
    POS_PATH = os.environ["POS_PATH"]
elif os.path.exists("CCTV Footage/pos.csv"):
    POS_PATH = "CCTV Footage/pos.csv"
elif os.path.exists("CCTV_Footage/pos.csv"):
    POS_PATH = "CCTV_Footage/pos.csv"
else:
    POS_PATH = "/data/pos_transactions.csv"

STALE_FEED_S = int(os.environ.get("STALE_FEED_S", "600"))
START_WATCHER = os.environ.get("START_WATCHER", "1") == "1"
POS_REBASE_TO_NOW = os.environ.get("POS_REBASE_TO_NOW", "1") == "1"
RUN_BASE_TS: str = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

store: Store = None  # type: ignore
active_pipelines: dict[str, dict] = {}
watcher_stop_event = threading.Event()
watcher_thread: threading.Thread | None = None

CLIP_CAMERA_MAP = {
    "1": "CAM_FLOOR_01",
    "2": "CAM_FLOOR_02",
    "3": "CAM_ENTRY_01",
    "4": "CAM_BACKROOM_01",
    "5": "CAM_BILL_01",
}


def parse_clip_info(filename: str) -> dict:
    path = filename.replace("\\", "/").upper()
    name = os.path.basename(filename).upper()

    if "/STORE 2/" in path or "/STORE2/" in path:
        store_id = "STORE_DEL_004"
    elif "/STORE 1/" in path or "/STORE1/" in path:
        store_id = "STORE_BLR_002"
    else:
        store_id = "STORE_BLR_002"
        if "STORE_DEL_004" in name or ("DEL" in name and "BLR" not in name):
            store_id = "STORE_DEL_004"

    m = re.search(r"CAM[ _]?(\d+)", name)
    if m:
        camera = CLIP_CAMERA_MAP.get(m.group(1))
        if camera:
            return {"store": store_id, "camera": camera}

    if "ENTRY" in name:
        num_match = re.search(r"ENTRY[ _]?(\d+)", name)
        if num_match:
            camera = f"CAM_ENTRY_{int(num_match.group(1)):02d}"
        else:
            camera = "CAM_ENTRY_01"
    elif "BILL" in name:
        num_match = re.search(r"BILL(?:ING)?[ _]?(\d+)", name)
        if num_match:
            camera = f"CAM_BILL_{int(num_match.group(1)):02d}"
        else:
            camera = "CAM_BILL_01"
    elif "BACKROOM" in name or "STOCK" in name:
        camera = "CAM_BACKROOM_01"
    elif "FLOOR" in name or "ZONE" in name:
        num_match = re.search(r"(?:FLOOR|ZONE)[ _]?(\d+)", name)
        if num_match:
            camera = f"CAM_FLOOR_{int(num_match.group(1)):02d}"
        else:
            camera = "CAM_FLOOR_01"
    else:
        base = os.path.splitext(os.path.basename(filename))[0]
        camera = f"CAM_{base.upper().replace(' ', '_')}"

    return {"store": store_id, "camera": camera}


def log_pipeline_message(message: str):
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    log_line = f"[{now}] {message}\n"
    try:
        with open("pipeline.log", "a", encoding="utf-8") as f:
            f.write(log_line)
    except Exception:
        pass


def run_pipeline_for_clip(clip_path: str, store_id: str, camera_id: str):
    log_pipeline_message(f"Starting pipeline for {clip_path} (Store: {store_id}, Camera: {camera_id})")
    active_pipelines[clip_path] = {
        "status": "PROCESSING",
        "progress": 0.0,
        "store": store_id,
        "camera": camera_id,
    }

    def _mark(status: str) -> None:
        slot = active_pipelines.setdefault(clip_path, {
            "status": status, "progress": 0.0,
            "store": store_id, "camera": camera_id,
        })
        slot["status"] = status
        try:
            if store:
                store.insert_or_update_clip(clip_path, status)
        except Exception as e:
            log_pipeline_message(f"Could not persist clip status {status}: {e}")

    if store:
        store.insert_or_update_clip(clip_path, "PROCESSING")

    base_name = os.path.splitext(os.path.basename(clip_path))[0].lower().replace(" ", "")
    out_file = f"data_output_{store_id.lower()}_{base_name}.jsonl"

    here = os.path.dirname(os.path.abspath(__file__))
    detect_script = os.path.join(os.path.dirname(here), "pipeline", "detect.py")

    cmd = [
        sys.executable, detect_script,
        "--clip",   clip_path,
        "--store",  store_id,
        "--camera", camera_id,
        "--start",  RUN_BASE_TS,
        "--api",    "http://localhost:8000",
        "--out",    out_file,
    ]

    env = os.environ.copy()
    env["PYTHONPATH"] = os.path.dirname(here)

    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            env=env,
        )

        progress_re = re.compile(r"\(([\d.]+)%\)")

        if proc.stdout:
            for line in proc.stdout:
                match = progress_re.search(line)
                if match:
                    progress = float(match.group(1))
                    active_pipelines[clip_path]["progress"] = progress
                    if int(progress) % 10 == 0:
                        log_pipeline_message(f"{os.path.basename(clip_path)}: {progress:.1f}% processed.")

        proc.wait()

        if proc.returncode == 0:
            active_pipelines[clip_path]["progress"] = 100.0
            _mark("COMPLETED")
            log_pipeline_message(f"SUCCESS: {clip_path} finished processing successfully.")
            if "bill" in camera_id.lower():
                _maybe_load_pos()
        else:
            _mark("FAILED")
            log_pipeline_message(f"ERROR: {clip_path} failed with exit code {proc.returncode}.")

    except Exception as e:
        _mark("FAILED")
        log_pipeline_message(f"CRITICAL ERROR: Exception running pipeline for {clip_path}: {e}")


def watch_cctv_directory():
    from concurrent.futures import ThreadPoolExecutor

    cctv_dir = Path("CCTV Footage")
    if not cctv_dir.exists():
        cctv_dir = Path("CCTV_Footage")
    if not cctv_dir.exists():
        log_pipeline_message("WARNING: 'CCTV Footage' or 'CCTV_Footage' directory not found.")
        return

    log_pipeline_message(f"Watcher started scanning '{cctv_dir.name}' directory.")

    # Collect all clips to process
    mp4_files = sorted(cctv_dir.rglob("*.mp4"))
    total = len(mp4_files)
    log_pipeline_message(f"Found {total} video clips to process.")

    # Register ALL clips upfront as QUEUED so they appear in the dashboard
    clip_queue: list[tuple[str, dict]] = []
    for file_path in mp4_files:
        clip_path = str(file_path).replace("\\", "/")

        # Skip clips already known (e.g. from a previous hot-reload run)
        if clip_path in active_pipelines:
            continue

        status = store.get_clip_status(clip_path) if store else None
        if status is not None:
            continue

        info = parse_clip_info(clip_path)
        active_pipelines[clip_path] = {
            "status": "QUEUED",
            "progress": 0.0,
            "store": info["store"],
            "camera": info["camera"],
        }
        if store:
            store.insert_or_update_clip(clip_path, "QUEUED")
        clip_queue.append((clip_path, info))

    if not clip_queue:
        log_pipeline_message("No new clips to process.")
        return

    # Process clips with controlled parallelism (2 at a time)
    # 8 simultaneous YOLO processes crash due to memory exhaustion
    MAX_PARALLEL = 2
    log_pipeline_message(f"Processing {len(clip_queue)} clips, {MAX_PARALLEL} at a time.")

    with ThreadPoolExecutor(max_workers=MAX_PARALLEL) as pool:
        futures = []
        for clip_path, info in clip_queue:
            if watcher_stop_event.is_set():
                break
            log_pipeline_message(
                f"Queued: {os.path.basename(clip_path)} "
                f"(Store: {info['store']}, Camera: {info['camera']})"
            )
            fut = pool.submit(run_pipeline_for_clip, clip_path, info["store"], info["camera"])
            futures.append(fut)

        for fut in futures:
            fut.result()

    log_pipeline_message(f"All {len(clip_queue)} clips finished processing.")


@asynccontextmanager
async def lifespan(app: FastAPI):
    global store, watcher_thread, RUN_BASE_TS
    RUN_BASE_TS = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    store = Store(DB_PATH)

    try:
        with open("pipeline.log", "w", encoding="utf-8") as f:
            f.write("=== Store Intelligence Pipeline Log Started ===\n")
    except Exception:
        pass

    # Delete any existing JSONL backup files so they do not stack duplicates
    # and we start processing fresh.
    here = Path(__file__).parent.parent
    jsonl_backups = list(here.glob("data_output_store_*.jsonl"))
    for f in jsonl_backups:
        try:
            f.unlink()
            log_pipeline_message(f"Deleted old backup file: {f.name}")
        except Exception as e:
            log_pipeline_message(f"WARNING: Could not delete backup file {f.name}: {e}")

    # Cold start: clear database for a clean re-run of detection.
    log_pipeline_message("Clearing database and backups for a clean re-run.")
    try:
        with store._cursor() as cur:
            cur.execute("DROP TABLE IF EXISTS pos_transactions")
            cur.execute("DELETE FROM processed_clips")
            cur.execute("DELETE FROM events")
            cur.execute("DELETE FROM pos_details")
        store._init_schema()
    except Exception as e:
        log_pipeline_message(f"ERROR: Could not clear database tables: {e}")

    _maybe_load_pos()

    if START_WATCHER:
        watcher_stop_event.clear()
        watcher_thread = threading.Thread(target=watch_cctv_directory, daemon=True)
        watcher_thread.start()

    yield

    watcher_stop_event.set()
    if watcher_thread:
        watcher_thread.join(timeout=2.0)
    log_pipeline_message("Lifespan shutdown: Watcher thread stopped.")


app = FastAPI(title="Apex Store Intelligence API", version="1.0.0", lifespan=lifespan)
app.add_middleware(StructuredLoggingMiddleware)
footage_dir = "CCTV Footage" if os.path.exists("CCTV Footage") else "CCTV_Footage"
app.mount("/footage", StaticFiles(directory=footage_dir), name="footage")


def _rebase_pos_rows(rows: list[dict]) -> list[dict]:
    def _epoch(ts: str) -> "float | None":
        try:
            return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
        except (ValueError, AttributeError):
            return None

    parsed = [(_epoch(r["timestamp"]), r) for r in rows]
    valid = [e for e, _ in parsed if e is not None]
    if not valid:
        return rows
    anchor = datetime.fromisoformat(RUN_BASE_TS.replace("Z", "+00:00")).timestamp()
    shift = anchor - min(valid)
    out = []
    for e, r in parsed:
        if e is None:
            out.append(r)
            continue
        nr = dict(r)
        nr["timestamp"] = datetime.fromtimestamp(
            e + shift, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        out.append(nr)
    return out


def _maybe_load_pos() -> None:
    p = Path(POS_PATH)
    if not p.exists():
        return

    try:
        with store._cursor() as cur:
            cur.execute("DELETE FROM pos_transactions")
            cur.execute("DELETE FROM pos_details")
    except Exception as e:
        log_pipeline_message(f"ERROR: Could not clear POS tables before load: {e}")

    with p.open() as fh:
        reader = csv.DictReader(fh)
        pos_rows = [
            {
                "transaction_id":   r["transaction_id"].strip(),
                "store_id":         r["store_id"].strip(),
                "timestamp":        r["timestamp"].strip(),
                "basket_value_inr": r["basket_value_inr"].strip(),
            }
            for r in reader
        ]

    shift = 0.0
    if POS_REBASE_TO_NOW and pos_rows:
        def _epoch(ts: str) -> "float | None":
            try:
                return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
            except (ValueError, AttributeError):
                return None
        valid_epochs = [e for r in pos_rows if (e := _epoch(r["timestamp"])) is not None]
        if valid_epochs:
            anchor = datetime.fromisoformat(RUN_BASE_TS.replace("Z", "+00:00")).timestamp()
            shift = anchor - min(valid_epochs)
            for r in pos_rows:
                e = _epoch(r["timestamp"])
                if e is not None:
                    r["timestamp"] = datetime.fromtimestamp(
                        e + shift, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    store.load_pos(pos_rows)

    cctv_dir = Path("CCTV Footage")
    if not cctv_dir.exists():
        cctv_dir = Path("CCTV_Footage")
    if cctv_dir.exists():
        csv_files = list(cctv_dir.rglob("*.csv"))
        temp_rows = []
        for csv_path in csv_files:
            if csv_path.name == "pos.csv":
                continue
            try:
                with csv_path.open(encoding="utf-8") as fh:
                    reader = csv.DictReader(fh)
                    headers = reader.fieldnames or []
                    if "order_id" in headers and ("product_name" in headers or "brand_name" in headers):
                        for r in reader:
                            store_id = r.get("store_id", "").strip()
                            if store_id == "ST1008" or "Bangalore" in str(csv_path) or "BLR" in store_id:
                                store_id = "STORE_BLR_002"
                            elif "Delhi" in str(csv_path) or "DEL" in store_id:
                                store_id = "STORE_DEL_004"
                            elif "Mumbai" in str(csv_path) or "MUM" in store_id:
                                store_id = "STORE_MUM_001"
                            else:
                                store_id = "STORE_BLR_002"

                            raw_ts = r.get("timestamp")
                            if raw_ts:
                                raw_ts = raw_ts.strip()
                            elif "order_date" in r and "order_time" in r:
                                date_str = r["order_date"].strip()
                                time_str = r["order_time"].strip()
                                d, m, y = date_str.split("-")
                                raw_ts = f"{y}-{m}-{d}T{time_str}Z"
                            else:
                                continue

                            try:
                                epoch = datetime.fromisoformat(raw_ts.replace("Z", "+00:00")).timestamp()
                            except ValueError:
                                continue

                            temp_rows.append({
                                "order_id":     r.get("order_id", "").strip() or r.get("invoice_number", "").strip(),
                                "store_id":     store_id,
                                "product_id":   r.get("product_id", "").strip() or r.get("sku", "").strip() or r.get("ean", "").strip(),
                                "product_name": r.get("product_name", "").strip() or f"Product {r.get('product_id', '').strip()}",
                                "brand_name":   r.get("brand_name", "").strip() or "General",
                                "dep_name":     r.get("dep_name", "").strip() or r.get("category", "").strip() or r.get("sub_category", "").strip() or "General",
                                "qty":          int(r.get("qty", "1").strip() or "1"),
                                "total_amount": float(r.get("total_amount", "0").strip() or r.get("NMV", "0").strip() or r.get("GMV", "0").strip() or "0"),
                                "epoch":        epoch,
                            })
            except Exception as e:
                log_pipeline_message(f"ERROR reading detailed CSV {csv_path}: {e}")

        if temp_rows:
            detail_shift = 0.0
            if POS_REBASE_TO_NOW:
                valid_epochs = [r["epoch"] for r in temp_rows if r["epoch"] is not None]
                if valid_epochs:
                    anchor = datetime.fromisoformat(RUN_BASE_TS.replace("Z", "+00:00")).timestamp()
                    detail_shift = anchor - min(valid_epochs)

            detail_rows = []
            for r in temp_rows:
                shifted_ts = datetime.fromtimestamp(
                    r["epoch"] + detail_shift, tz=timezone.utc
                ).strftime("%Y-%m-%dT%H:%M:%SZ")
                detail_rows.append({
                    "order_id":     r["order_id"],
                    "store_id":     r["store_id"],
                    "product_id":   r["product_id"],
                    "product_name": r["product_name"],
                    "brand_name":   r["brand_name"],
                    "dep_name":     r["dep_name"],
                    "qty":          r["qty"],
                    "total_amount": r["total_amount"],
                    "timestamp":    shifted_ts,
                })

            n_details = store.load_pos_details(detail_rows)
            log_pipeline_message(f"Successfully loaded {n_details} itemized POS detailed transactions.")


@app.exception_handler(DatabaseUnavailable)
async def _db_unavailable(request: Request, exc: DatabaseUnavailable):
    return JSONResponse(
        status_code=503,
        content={
            "error":    "storage_unavailable",
            "detail":   "The analytics store is temporarily unavailable.",
            "trace_id": getattr(request.state, "trace_id", None),
        },
    )


@app.exception_handler(Exception)
async def _unhandled(request: Request, exc: Exception):  # pragma: no cover
    return JSONResponse(
        status_code=500,
        content={
            "error":    "internal_error",
            "detail":   "An unexpected error occurred.",
            "trace_id": getattr(request.state, "trace_id", None),
        },
    )


# --- ingest -------------------------------------------------------------
@app.post("/events/ingest")
async def ingest(req: IngestRequest, request: Request):
    request.state.event_count = len(req.events)
    if req.events:
        first = req.events[0]
        request.state.store_id = first.get("store_id") if isinstance(first, dict) else getattr(first, "store_id", None)
    result = ingest_batch(store, req.events)
    status = 200 if result.rejected == 0 else 207
    return JSONResponse(status_code=status, content=result.model_dump(mode="json"))


# --- analytics ----------------------------------------------------------
@app.get("/stores/{store_id}/metrics")
async def get_metrics(store_id: str):
    return metrics.compute_metrics(store, store_id)


@app.get("/stores/{store_id}/funnel")
async def get_funnel(store_id: str):
    return metrics.compute_funnel(store, store_id)


@app.get("/stores/{store_id}/heatmap")
async def get_heatmap(store_id: str):
    return metrics.compute_heatmap(store, store_id)


@app.get("/stores/{store_id}/anomalies")
async def get_anomalies(store_id: str):
    return {"store_id": store_id, "anomalies": anomalies.detect(store, store_id)}


@app.post("/admin/reload-pos")
async def reload_pos():
    _maybe_load_pos()
    return {"status": "reloaded", "pos_path": POS_PATH}


@app.get("/stores")
async def list_stores():
    return {"stores": store.known_stores()}


@app.get("/")
async def dashboard():
    return FileResponse(str(Path(__file__).parent / "static" / "dashboard.html"))


# --- pipeline -----------------------------------------------------------
from pydantic import BaseModel


class PipelineProgressRequest(BaseModel):
    clip: str
    store: str
    camera: str
    status: str
    progress: float


@app.post("/pipeline/progress")
async def post_pipeline_progress(req: PipelineProgressRequest):
    clip_path = req.clip
    active_pipelines[clip_path] = {
        "status":   req.status,
        "progress": req.progress,
        "store":    req.store,
        "camera":   req.camera,
    }
    if store:
        store.insert_or_update_clip(clip_path, req.status)
    return {"status": "ok"}


@app.get("/pipeline/status")
async def get_pipeline_status():
    db_clips = store.get_all_clips()

    result = []
    for c in db_clips:
        path = c["clip_path"]
        if path in active_pipelines:
            result.append({
                "clip":     path,
                "status":   active_pipelines[path]["status"],
                "progress": round(active_pipelines[path]["progress"], 1),
                "store":    active_pipelines[path]["store"],
                "camera":   active_pipelines[path]["camera"],
            })
        else:
            info = parse_clip_info(path)
            result.append({
                "clip":     path,
                "status":   c["status"],
                "progress": 100.0 if c["status"] == "COMPLETED" else 0.0,
                "store":    info["store"],
                "camera":   info["camera"],
            })

    for path, info in active_pipelines.items():
        if path not in [c["clip_path"] for c in db_clips]:
            result.append({
                "clip":     path,
                "status":   info["status"],
                "progress": round(info["progress"], 1),
                "store":    info["store"],
                "camera":   info["camera"],
            })

    return {"clips": sorted(result, key=lambda x: (x["store"], x["camera"], x["clip"]))}


# --- health -------------------------------------------------------------
@app.get("/health")
async def health():
    try:
        last = store.last_event_per_store()
    except DatabaseUnavailable:
        return JSONResponse(status_code=503, content={"status": "degraded",
                            "detail": "storage unavailable"})
    now = datetime.now(timezone.utc)
    feeds = {}
    overall_ok = True
    for sid, ts in last.items():
        last_dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        lag = (now - last_dt).total_seconds()
        stale = lag > STALE_FEED_S
        overall_ok = overall_ok and not stale
        feeds[sid] = {
            "last_event":  ts,
            "lag_seconds": round(lag, 1),
            "warning":     "STALE_FEED" if stale else None,
        }
    return {
        "status":      "ok" if overall_ok else "degraded",
        "service":     "store-intelligence",
        "checked_at":  now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "run_base_ts": RUN_BASE_TS,
        "feeds":       feeds,
    }
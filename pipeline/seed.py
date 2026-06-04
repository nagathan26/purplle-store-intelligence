"""
seed.py — clear the DB and process all real CCTV clips, posting their
real events to the running API, then reload the POS transactions.
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import subprocess
import sys
import time
import urllib.request
import json

# Camera roles assigned by inspecting each clip (see docs/DESIGN.md):
#   CAM 1 floor (skincare) · CAM 2 floor (makeup) · CAM 3 entry/exit ·
#   CAM 4 backroom (staff only, emits no customer events) · CAM 5 billing.
CLIPS = [
    # Store 1 — Brigade Road, Bangalore
    {"clip": "CCTV Footage/CAM 1.mp4",                    "store": "STORE_BLR_002", "camera": "CAM_FLOOR_01",    "out": "data_output_cam1.jsonl"},
    {"clip": "CCTV Footage/CAM 2.mp4",                    "store": "STORE_BLR_002", "camera": "CAM_FLOOR_02",    "out": "data_output_cam2.jsonl"},
    {"clip": "CCTV Footage/CAM 3.mp4",                    "store": "STORE_BLR_002", "camera": "CAM_ENTRY_01",    "out": "data_output_cam3.jsonl"},
    {"clip": "CCTV Footage/CAM 4.mp4",                    "store": "STORE_BLR_002", "camera": "CAM_BACKROOM_01", "out": "data_output_cam4.jsonl"},
    {"clip": "CCTV Footage/CAM 5.mp4",                    "store": "STORE_BLR_002", "camera": "CAM_BILL_01",     "out": "data_output_cam5.jsonl"},
    # Store 2 — Connaught Place, Delhi
    {"clip": "CCTV Footage/Store 2/entry 1.mp4",          "store": "STORE_DEL_004", "camera": "CAM_ENTRY_01",    "out": "data_output_del_entry1.jsonl"},
    {"clip": "CCTV Footage/Store 2/entry 2.mp4",          "store": "STORE_DEL_004", "camera": "CAM_ENTRY_02",    "out": "data_output_del_entry2.jsonl"},
    {"clip": "CCTV Footage/Store 2/zone.mp4",             "store": "STORE_DEL_004", "camera": "CAM_FLOOR_01",    "out": "data_output_del_floor.jsonl"},
    {"clip": "CCTV Footage/Store 2/billing_area.mp4",     "store": "STORE_DEL_004", "camera": "CAM_BILL_01",     "out": "data_output_del_bill.jsonl"},
]


def get_run_base_ts(api_url: str, wait: bool = False, timeout_s: int = 60) -> str:
    deadline = time.time() + (timeout_s if wait else 2)
    while True:
        try:
            with urllib.request.urlopen(f"{api_url}/health", timeout=3) as r:
                data = json.loads(r.read())
            if "run_base_ts" in data:
                return data["run_base_ts"]
        except Exception:
            pass
        if time.time() >= deadline:
            break
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def clear_db(db_path: str):
    if not os.path.exists(db_path):
        print(f"Database at {db_path} does not exist yet. It will be created by the API.")
        return
    try:
        conn = sqlite3.connect(db_path)
        c = conn.cursor()
        c.execute("DELETE FROM events")
        c.execute("DELETE FROM pos_transactions")
        conn.commit()
        conn.close()
        print("Successfully cleared all previous events and POS transactions from SQLite DB.")
    except Exception as e:
        print(f"Warning: Could not clear database: {e}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--api",      default=os.environ.get("API_URL", "http://localhost:8000"))
    ap.add_argument("--db-path",  default=os.environ.get("DB_PATH", "store.db"))
    ap.add_argument("--wait",     action="store_true", help="wait for API readiness first")
    ap.add_argument("--reset-db", action="store_true", help="clear DB before processing (explicit opt-in)")
    args = ap.parse_args()

    print("Connecting to API and retrieving base timestamp...")
    run_base_ts = get_run_base_ts(args.api, wait=args.wait)
    print(f"Using base timestamp: {run_base_ts}")

    # Step 1: Clear the DB only if explicitly requested
    if args.reset_db:
        clear_db(args.db_path)
    else:
        print("Skipping DB clear (pass --reset-db to wipe existing events).")

    # Step 2: Process all discovered CCTV clips
    here = os.path.dirname(os.path.abspath(__file__))
    detect_script = os.path.join(here, "detect.py")

    # Attempt dynamic discovery via parse_clip_info; fall back to CLIPS if unavailable.
    discovered_clips = []
    try:
        sys.path.append(os.path.dirname(here))
        from app.main import parse_clip_info

        cctv_dir = "CCTV Footage"
        if os.path.exists(cctv_dir):
            import glob
            for path in glob.glob(os.path.join(cctv_dir, "**", "*.mp4"), recursive=True):
                path_fixed = path.replace("\\", "/")
                info = parse_clip_info(path_fixed)
                base = os.path.splitext(os.path.basename(path_fixed))[0].lower().replace(" ", "")
                discovered_clips.append({
                    "clip":   path_fixed,
                    "store":  info["store"],
                    "camera": info["camera"],
                    "out":    f"data_output_{info['store'].lower()}_{base}.jsonl"
                })
    except Exception as e:
        print(f"Dynamic clip discovery failed ({e}); falling back to hardcoded CLIPS list.")

    # Fall back to the hardcoded CLIPS list when dynamic discovery finds nothing
    if not discovered_clips:
        print("No clips discovered dynamically; using hardcoded CLIPS list.")
        discovered_clips = list(CLIPS)

    print("Registering all discovered clips with API...")
    for item in discovered_clips:
        try:
            data = json.dumps({
                "clip":     item["clip"],
                "store":    item["store"],
                "camera":   item["camera"],
                "status":   "PENDING",
                "progress": 0.0
            }).encode()
            req = urllib.request.Request(
                f"{args.api}/pipeline/progress", data=data,
                headers={"Content-Type": "application/json"}, method="POST"
            )
            with urllib.request.urlopen(req, timeout=5) as r:
                r.read()
        except Exception as e:
            print(f"Warning: Could not register clip {item['clip']}: {e}")

    print(f"Starting CCTV detection on GPU/CPU for {len(discovered_clips)} clips...")
    failed: list[str] = []
    for item in discovered_clips:
        print(f"\n--- Processing {item['clip']} ({item['store']} / {item['camera']}) ---")
        cmd = [
            sys.executable, detect_script,
            "--clip",   item["clip"],
            "--store",  item["store"],
            "--camera", item["camera"],
            "--start",  run_base_ts,
            "--api",    args.api,
            "--out",    item["out"],
        ]
        p = subprocess.run(cmd, capture_output=False)
        if p.returncode != 0:
            msg = f"{item['clip']} (exit code {p.returncode})"
            print(f"Error processing {msg}")
            failed.append(msg)

    # Step 3: Trigger POS transactions reload
    try:
        req = urllib.request.Request(f"{args.api}/admin/reload-pos", method="POST")
        with urllib.request.urlopen(req, timeout=10) as r:
            body = json.loads(r.read())
        print(f"\nTriggered POS reload on API: {body}")
    except Exception as exc:
        print(f"\nPOS reload request failed (non-fatal): {exc}")

    # Final summary
    print("\n--- Seeding Summary ---")
    print(f"Total clips : {len(discovered_clips)}")
    print(f"Succeeded   : {len(discovered_clips) - len(failed)}")
    if failed:
        print(f"Failed      : {len(failed)}")
        for f in failed:
            print(f"  ✗ {f}")
    else:
        print("All clips processed successfully.")
    print("CCTV Seeding Complete!")


if __name__ == "__main__":
    main()
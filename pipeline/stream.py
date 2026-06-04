"""
stream.py — simulated real-time feed replaying real CCTV events.

Replays events from the generated CCTV JSONL files on a real-time cadence,
updating their timestamps to "now", so the dashboard updates live.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import time
from datetime import datetime, timedelta, timezone

try:
    from emit import post_events
except ModuleNotFoundError:  # imported as `pipeline.stream`
    from pipeline.emit import post_events


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--api", default=os.environ.get("API_URL", "http://localhost:8000"))
    ap.add_argument("--speed", type=float, default=60.0,
                    help="sim seconds per real second (60 = 1 sim-min per sec)")
    ap.add_argument("--batch-window", type=float, default=2.0,
                    help="real seconds between posts")
    args = ap.parse_args()

    # Dynamic discovery — picks up Store 1, Store 2, and any future stores
    jsonl_files = sorted(glob.glob("data_output*.jsonl"))
    if not jsonl_files:
        print("No data_output*.jsonl files found. Make sure to run seed.py first.")
        return

    # Load all events from discovered JSONL files
    events = []
    for fname in jsonl_files:
        with open(fname, encoding="utf-8") as fh:
            for line in fh:
                if line.strip():
                    events.append(json.loads(line))

    if not events:
        print("No CCTV events loaded. Stream exiting.")
        return

    # Sort events by original timestamp (parsed for correctness)
    events.sort(
        key=lambda e: datetime.fromisoformat(e["timestamp"].replace("Z", "+00:00"))
    )

    base = datetime.fromisoformat(events[0]["timestamp"].replace("Z", "+00:00"))
    print(f"Streaming {len(events)} real CCTV events at {args.speed}x into {args.api} ...")
    print(f"Loaded from: {jsonl_files}")

    idx = 0
    sim_clock = 0.0
    while idx < len(events):
        cutoff = base + timedelta(seconds=sim_clock)
        batch = []
        while idx < len(events):
            ets = datetime.fromisoformat(events[idx]["timestamp"].replace("Z", "+00:00"))
            if ets <= cutoff:
                # Re-stamp to now; drop stale event_id so backend regenerates it
                # (original event_id was derived from the old timestamp and would mismatch).
                ev = dict(events[idx])
                ev["timestamp"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                ev.pop("event_id", None)
                batch.append(ev)
                idx += 1
            else:
                break
        if batch:
            try:
                res = post_events(args.api, batch)
                print(f"  +{len(batch)} real events (accepted={res['accepted']})")
            except Exception as e:
                print(f"Failed to post batch: {e}")
        time.sleep(args.batch_window)
        sim_clock += args.speed * args.batch_window

    print("Stream complete.")


if __name__ == "__main__":
    main()
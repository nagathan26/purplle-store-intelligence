"""
detect.py — entry point for the detection layer.

Runs real CV detection on CCTV clips with YOLOv8 + ByteTrack.
Requires ultralytics + opencv + the clips. Emits structured events.
Funnels through the Tracker (re-id) and emit.make_event.
Output is either POSTed to the API (--api) or written to JSONL (--out).
"""
from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timedelta, timezone
from typing import Optional

try:
    from emit import make_event, post_events
    from tracker import Tracker
except ModuleNotFoundError:  # imported as package (pipeline.detect)
    from pipeline.emit import make_event, post_events
    from pipeline.tracker import Tracker

ZONES = ["SKINCARE", "HAIRCARE", "FRAGRANCE", "MAKEUP", "WELLNESS"]
# Fallback sku_zone labels, used only if store_layout.json is missing/unreadable.
SKU_BY_ZONE = {
    "SKINCARE": "MOISTURISER", "HAIRCARE": "SHAMPOO", "FRAGRANCE": "EAU_DE_PARFUM",
    "MAKEUP": "LIPSTICK", "WELLNESS": "SUPPLEMENT",
}
CAM_ENTRY = "CAM_ENTRY_01"
CAM_FLOOR = "CAM_FLOOR_01"
CAM_BILL = "CAM_BILL_01"

# Staff heuristic (documented in CHOICES.md): a track is flagged staff only after
# its torso reads dark (a uniform proxy) in several reasonably large detections -
# never on a single dark frame, which would mis-flag dark-clothed customers and
# shadowed/partially-occluded crops. This is a deliberately conservative baseline;
# the documented upgrade is a VLM/uniform classifier.
STAFF_DARK_BGR = 75          # per-channel mean below this = "dark uniform"
                             # raised from 50: store lighting washes out black shirts
STAFF_MIN_HITS = 3           # dark-torso observations required before flagging
                             # lowered from 6: reduces the classification delay
STAFF_MIN_BOX_FRAC = 0.12    # ignore crops shorter than 12% of frame height

def report_progress(api_url: Optional[str], clip: str, store: str, camera: str, status: str, progress: float):
    if not api_url:
        return
    try:
        import urllib.request
        data = json.dumps({
            "clip": clip,
            "store": store,
            "camera": camera,
            "status": status,
            "progress": progress
        }).encode()
        req = urllib.request.Request(
            f"{api_url}/pipeline/progress",
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            if resp.status not in (200, 204):
                print(f"[{camera}] Progress API returned {resp.status}", flush=True)
    except Exception as e:
        print(f"[{camera}] Error reporting progress to API: {e}", flush=True)
    
def load_sku_by_zone(store_id: str, path: str = "store_layout.json") -> dict:
    """Source sku_zone labels from store_layout.json (per the spec, zone labels
    come from the layout). Falls back to the built-in defaults if the file is
    absent or malformed so the detector never hard-fails on a missing layout."""
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        for s in data.get("stores", []):
            if s.get("store_id") == store_id:
                mapping = {
                    z["zone_id"]: z.get("sku_zone")
                    for z in s.get("zones", []) if z.get("zone_id")
                }
                if mapping:
                    return mapping
    except (FileNotFoundError, json.JSONDecodeError, KeyError, TypeError):
        pass
    return dict(SKU_BY_ZONE)

def load_store_layout(store_id, path="store_layout.json"):
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)

        for store in data.get("stores", []):
            if store.get("store_id") == store_id:
                return store

    except (FileNotFoundError,
            json.JSONDecodeError,
            KeyError,
            TypeError):
        pass

    return None





def get_zone(layout, camera_id, nx, ny):
    if layout is None:
        return None
    for cam in layout["cameras"]:
        if cam["camera_id"] != camera_id:
            continue
        for z in cam.get("zones", []):
            if (
                z["xmin"] <= nx <= z["xmax"]
                and z["ymin"] <= ny <= z["ymax"]
            ):
                return z["zone_id"]
    return None


def in_entry_region(layout, camera_id, nx, ny):
    if layout is None:
        return ny < 20  # fallback: top 20% strip
    for cam in layout["cameras"]:
        if cam["camera_id"] != camera_id:
            continue
        entry = cam.get("entry_region")
        if entry:
            return (
                entry["xmin"] <= nx <= entry["xmax"]
                and entry["ymin"] <= ny <= entry["ymax"]
            )
    return ny < 20  # fallback if no entry_region defined


def get_door_line_config(layout, camera_id):
    """Return (door_line_y, crossing_direction) from store_layout.json for this camera.

    door_line_y       : normalized y% of the black threshold line
    crossing_direction: 'top_to_bottom' (outside above, inside below) or
                        'bottom_to_top' (outside below, inside above)

    Returns (None, None) if not configured — caller falls back to old logic.
    """
    if layout is None:
        return None, None
    for cam in layout["cameras"]:
        if cam["camera_id"] != camera_id:
            continue
        door_y = cam.get("door_line_y")
        direction = cam.get("crossing_direction", "top_to_bottom")
        return door_y, direction
    return None, None


def run_video(args):  # pragma: no cover - requires clips + GPU/CPU heavy deps
    """Real detection path."""
    import cv2
    import torch
    from ultralytics import YOLO

    device = "cuda" if torch.cuda.is_available() else "cpu"
    print(f"Using device for YOLOv8: {device}")

    # Report start
    report_progress(args.api, args.clip, args.store, args.camera, "PROCESSING", 0.0)

    try:
        model = YOLO(args.weights)  # e.g. yolov8n.pt; person class = 0
        events: list[dict] = []
        cap = cv2.VideoCapture(args.clip)
        fps = cap.get(cv2.CAP_PROP_FPS) or 15
        frame_width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)) or 1920
        frame_height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)) or 1080
        tracker = Tracker(frame_width=frame_width)
        base_ts = datetime.fromisoformat(args.start.replace("Z", "+00:00"))
        frame_idx = 0        

        # Initialize output file if writing
        if args.out:
            os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
            with open(args.out, "w") as fh:
                pass  # truncate file

        # Tracking dictionaries for camera-specific behavior
        inside_visitors: dict[str, bool] = {}       # visitor_id -> is_inside (for entry camera)
        moved_off_threshold: dict[str, bool] = {}   # visitor_id -> has been seen away from doorway since ENTRY
        visitor_zones: dict[str, str] = {}          # visitor_id -> current_zone (for floor camera)
        zone_entry_times: dict[str, float] = {}     # visitor_id -> entry_epoch_seconds
        last_dwell_emitted: dict[str, float] = {}   # visitor_id -> last_dwell_epoch_seconds
        visitor_billing: dict[str, bool] = {}       # visitor_id -> is_in_billing_queue (for billing camera)
        visitor_is_staff: dict[str, bool] = {}      # visitor_id -> is_staff (confirmed uniform)
        staff_dark_hits: dict[str, int] = {}        # visitor_id -> count of dark-torso observations
        staff_pink_hits: dict[str, int] = {}        # visitor_id -> count of pink-torso observations (Store 2)

        camera_lower = args.camera.lower()
        sku_by_zone = load_sku_by_zone(args.store)  # zone -> sku label, from store_layout.json
        store_layout = load_store_layout(args.store)

        # Load door-line config for this camera (Store 2 specific: black threshold line)
        door_line_y, crossing_direction = get_door_line_config(store_layout, args.camera)
        # prev_ny[vid] tracks where the visitor was last frame, for direction detection
        prev_ny: dict[str, float] = {}

        # Per-camera reentry window: read from store_layout (default 600s = 10 min).
        # Rule: same person within window = REENTRY (duplicate, don't count as new unique).
        #        same person after window = new unique customer.
        reentry_window_s = 600  # global fallback
        if store_layout:
            for cam in store_layout.get("cameras", []):
                if cam.get("camera_id") == args.camera:
                    rw = cam.get("reentry_window_s")
                    if rw is not None:
                        reentry_window_s = int(rw)
                    break
        tracker.reentry_window_s = reentry_window_s
        tracker.exited_gallery_s  = reentry_window_s  # gallery lifetime matches the window
        print(f"[{args.camera}] Reentry dedup window: {reentry_window_s}s ({reentry_window_s//60} min)", flush=True)
        # Determine staff detection mode from store layout
        staff_mode = "dark"  # default
        if store_layout:
            staff_mode = store_layout.get("staff_uniform_mode", "dark")

        while True:
            ok, frame = cap.read()
            if not ok:
                break
            ts = base_ts + timedelta(seconds=frame_idx / fps)
            res = model.track(frame, persist=True, classes=[0], conf=args.conf,
                              verbose=False, device=device)

            detected_vids: set[str] = set()

            for box in res[0].boxes:
                conf = float(box.conf)
                x1, y1, x2, y2 = box.xyxy[0].tolist()

                # Clamp crop bounds to frame dimensions (fix edge-frame issues)
                x1 = max(0, int(x1))
                y1 = max(0, int(y1))
                x2 = min(frame_width, int(x2))
                y2 = min(frame_height, int(y2))

                centroid = ((x1 + x2) / 2, (y1 + y2) / 2)

                # Normalized coords (used for zone + entry region lookups)
                nx = centroid[0] / frame_width * 100
                ny = centroid[1] / frame_height * 100

                # crude appearance proxy = mean RGB of the crop
                crop = frame[y1:y2, x1:x2]
                appearance = tuple((crop.mean(axis=(0, 1)) / 255).tolist()) if crop.size else (0, 0, 0)

                # Entry threshold from layout (falls back to top-20% strip if not defined)
                at_threshold = in_entry_region(store_layout, args.camera, nx, ny)

                vid, is_reentry = tracker.observe(centroid, appearance, ts.timestamp(), at_threshold)
                detected_vids.add(vid)

                # Staff classification: persistent uniform on a large-enough crop.
                # A single frame is not enough - so we require repeated evidence.
                if not visitor_is_staff.get(vid, False) and crop.size > 0:
                    box_h = y2 - y1
                    h, w = crop.shape[:2]
                    if box_h >= frame_height * STAFF_MIN_BOX_FRAC and h > 0 and w > 0:
                        torso = crop[int(h * 0.25):int(h * 0.55), int(w * 0.20):int(w * 0.80)]
                        if torso.size > 0:
                            b, g, r = torso.mean(axis=(0, 1))
                            if staff_mode == "dark":
                                # Dark uniform (Store 1)
                                if b < STAFF_DARK_BGR and g < STAFF_DARK_BGR and r < STAFF_DARK_BGR:
                                    staff_dark_hits[vid] = staff_dark_hits.get(vid, 0) + 1
                                    if staff_dark_hits[vid] >= STAFF_MIN_HITS:
                                        visitor_is_staff[vid] = True
                            elif staff_mode == "pink_black":
                                # Bright pink top (Store 2 / Delhi)
                                if r > 140 and g < 170 and b < 170:
                                    staff_pink_hits[vid] = staff_pink_hits.get(vid, 0) + 1
                                    if staff_pink_hits[vid] >= STAFF_MIN_HITS:
                                        visitor_is_staff[vid] = True

                is_staff = visitor_is_staff.get(vid, False)

                # --- CAMERA SPECIFIC LOGIC ---
                if "entry" in camera_lower:
                    if not inside_visitors.get(vid, False):
                        # ── Direction gate (Store 2 black door line) ──────────────
                        # If door_line_y is configured, only trigger ENTRY when the
                        # person crosses the line in the inward direction:
                        #   top_to_bottom → prev_ny < door_line_y AND ny >= door_line_y
                        #   bottom_to_top → prev_ny > door_line_y AND ny <= door_line_y
                        crossed_inward = True  # default: no direction gate
                        if door_line_y is not None:
                            old_ny = prev_ny.get(vid)
                            if old_ny is not None:
                                if crossing_direction == "top_to_bottom":
                                    # Outside = top (small y), inside = bottom (large y)
                                    crossed_inward = old_ny < door_line_y <= ny
                                else:  # bottom_to_top
                                    crossed_inward = old_ny > door_line_y >= ny
                            else:
                                # First sighting: only count if already inside the line
                                if crossing_direction == "top_to_bottom":
                                    crossed_inward = ny >= door_line_y
                                else:
                                    crossed_inward = ny <= door_line_y

                        if crossed_inward:
                            inside_visitors[vid] = True
                            moved_off_threshold[vid] = False
                            etype = "REENTRY" if is_reentry else "ENTRY"
                            events.append(make_event(
                                args.store, args.camera, vid, etype, ts,
                                confidence=round(conf, 3),
                                session_seq=tracker.seq_for(vid),
                                is_staff=is_staff
                            ))
                    else:
                        # Track whether visitor has moved away from doorway since ENTRY.
                        # EXIT is NOT emitted here while the visitor is still detected —
                        # it is emitted in the missing-tracks block below once the visitor
                        # is absent from the frame. We only update the debounce flag here.
                        if not at_threshold:
                            moved_off_threshold[vid] = True

                    # Always update previous ny for direction tracking
                    prev_ny[vid] = ny

                elif "floor" in camera_lower:
                    z = get_zone(store_layout, args.camera, nx, ny)

                    if z is None:
                        # No zone matched — skip this detection
                        continue

                    prev_z = visitor_zones.get(vid)
                    if prev_z is None:
                        visitor_zones[vid] = z
                        zone_entry_times[vid] = ts.timestamp()
                        last_dwell_emitted[vid] = ts.timestamp()
                        events.append(make_event(
                            args.store, args.camera, vid, "ZONE_ENTER", ts,
                            zone_id=z, confidence=round(conf, 3),
                            sku_zone=sku_by_zone.get(z),
                            session_seq=tracker.seq_for(vid),
                            is_staff=is_staff
                        ))
                    elif prev_z != z:
                        dwell_ms = int((ts.timestamp() - zone_entry_times[vid]) * 1000)
                        events.append(make_event(
                            args.store, args.camera, vid, "ZONE_EXIT", ts,
                            zone_id=prev_z, dwell_ms=dwell_ms, confidence=round(conf, 3),
                            sku_zone=sku_by_zone.get(prev_z),
                            session_seq=tracker.seq_for(vid),
                            is_staff=is_staff
                        ))
                        visitor_zones[vid] = z
                        zone_entry_times[vid] = ts.timestamp()
                        # Reset dwell timer to new zone entry; without this the 30 s
                        # window is measured from the *previous* zone's entry time and
                        # ZONE_DWELL can fire immediately after every zone transition.
                        last_dwell_emitted[vid] = ts.timestamp()
                        # Emit ZONE_ENTER for the new zone immediately after ZONE_EXIT
                        events.append(make_event(
                            args.store, args.camera, vid, "ZONE_ENTER", ts,
                            zone_id=z, confidence=round(conf, 3),
                            sku_zone=sku_by_zone.get(z),
                            session_seq=tracker.seq_for(vid),
                            is_staff=is_staff
                        ))
                    else:
                        dwell_s = ts.timestamp() - zone_entry_times[vid]
                        if ts.timestamp() - last_dwell_emitted[vid] >= 30.0:
                            events.append(make_event(
                                args.store, args.camera, vid, "ZONE_DWELL", ts,
                                zone_id=z, dwell_ms=int(dwell_s * 1000), confidence=round(conf, 3),
                                sku_zone=sku_by_zone.get(z),
                                session_seq=tracker.seq_for(vid),
                                is_staff=is_staff
                            ))
                            last_dwell_emitted[vid] = ts.timestamp()  # updated after emit (correct)

                elif "bill" in camera_lower:
                    if not visitor_billing.get(vid, False):
                        # Count people already in queue before this person joined
                        queue_depth_now = sum(
                            1 for in_queue in visitor_billing.values()
                            if in_queue
                        )
                        visitor_billing[vid] = True
                        events.append(make_event(
                            args.store, args.camera, vid, "BILLING_QUEUE_JOIN", ts,
                            zone_id="BILLING", confidence=round(conf, 3),
                            queue_depth=queue_depth_now,
                            session_seq=tracker.seq_for(vid),
                            is_staff=is_staff
                        ))

            # Handle exits/abandons for missing tracks
            if "entry" in camera_lower:
                for vid in list(inside_visitors.keys()):
                    if inside_visitors[vid] and vid not in detected_vids:
                        # Visitor was inside but not seen this frame.
                        # Only emit EXIT if they previously moved off the threshold
                        # (debounce: avoids spurious EXIT on first few frames).
                        if moved_off_threshold.get(vid, False):
                            events.append(make_event(
                                args.store, args.camera, vid, "EXIT", ts,
                                confidence=0.85,
                                session_seq=tracker.seq_for(vid),
                                is_staff=visitor_is_staff.get(vid, False)
                            ))
                            tracker.mark_exit(vid)
                            inside_visitors[vid] = False
                            moved_off_threshold[vid] = False

            elif "bill" in camera_lower:
                for vid in list(visitor_billing.keys()):
                    if visitor_billing[vid] and vid not in detected_vids:
                        events.append(make_event(
                            args.store, args.camera, vid, "BILLING_QUEUE_ABANDON", ts,
                            zone_id="BILLING", confidence=0.85,
                            queue_depth=max(0, sum(1 for v in visitor_billing.values() if v) - 1),
                            session_seq=tracker.seq_for(vid),
                            is_staff=visitor_is_staff.get(vid, False)
                        ))
                        visitor_billing[vid] = False

            frame_idx += 1

            # Periodic progress logging (events are NOT flushed mid-video;
            # they are accumulated and retroactively corrected at the end so
            # that the is_staff flag is accurate for every event).
            if frame_idx % 100 == 0:
                total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
                pct = 0.0
                if total_frames > 0:
                    pct = (frame_idx / total_frames) * 100
                    print(f"[{args.camera}] Frame {frame_idx}/{total_frames} ({pct:.1f}%) | Events pending: {len(events)}", flush=True)
                else:
                    print(f"[{args.camera}] Frame {frame_idx} | Events pending: {len(events)}", flush=True)

                report_progress(args.api, args.clip, args.store, args.camera, "PROCESSING", pct)

        cap.release()

        # Clean up remaining tracks at the end of the video
        end_ts = base_ts + timedelta(seconds=frame_idx / fps)
        if "entry" in camera_lower:
            for vid, is_inside in inside_visitors.items():
                if is_inside:
                    events.append(make_event(
                        args.store, args.camera, vid, "EXIT", end_ts,
                        confidence=0.5, session_seq=tracker.seq_for(vid),
                        is_staff=visitor_is_staff.get(vid, False)
                    ))
        elif "floor" in camera_lower:
            for vid, z in visitor_zones.items():
                entry_epoch = zone_entry_times.get(vid, end_ts.timestamp())
                dwell_ms = int((end_ts.timestamp() - entry_epoch) * 1000)
                events.append(make_event(
                    args.store, args.camera, vid, "ZONE_EXIT", end_ts,
                    zone_id=z, dwell_ms=dwell_ms, confidence=0.5,
                    sku_zone=sku_by_zone.get(z),
                    session_seq=tracker.seq_for(vid),
                    is_staff=visitor_is_staff.get(vid, False)
                ))
        elif "bill" in camera_lower:
            for vid, is_billing in visitor_billing.items():
                if is_billing:
                    events.append(make_event(
                        args.store, args.camera, vid, "BILLING_QUEUE_ABANDON", end_ts,
                        zone_id="BILLING", confidence=0.5,
                        queue_depth=0, session_seq=tracker.seq_for(vid),
                        is_staff=visitor_is_staff.get(vid, False)
                    ))

        # --- RETROACTIVE STAFF CORRECTION ---
        # The staff classifier needs several frames to accumulate evidence,
        # so early events (especially ENTRY) are emitted with is_staff=False
        # even for actual staff. Now that we have processed the full video,
        # walk through ALL events and correct the flag for every visitor that
        # was *eventually* confirmed as staff.
        confirmed_staff_vids = {vid for vid, flag in visitor_is_staff.items() if flag}
        corrected = 0
        for evt in events:
            if evt["visitor_id"] in confirmed_staff_vids and not evt["is_staff"]:
                evt["is_staff"] = True
                corrected += 1
        if corrected:
            print(f"[{args.camera}] Retroactively corrected is_staff on {corrected} events "
                  f"for {len(confirmed_staff_vids)} staff visitor(s)", flush=True)

        if events:
            _output(args, events, append=True)
            events.clear()

        report_progress(args.api, args.clip, args.store, args.camera, "COMPLETED", 100.0)
    except Exception as e:
        report_progress(args.api, args.clip, args.store, args.camera, "FAILED", 0.0)
        raise e


def _output(args, events, append=False):
    if not events:
        return
    if args.api:
        try:
            res = post_events(args.api, events)
            print(f"Posted {len(events)} events -> {res}", flush=True)
        except Exception as e:
            print(f"Error posting events to API: {e}", flush=True)
    if args.out:
        os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
        mode = "a" if append else "w"
        with open(args.out, mode) as fh:
            for e in events:
                fh.write(json.dumps(e) + "\n")
        print(f"Wrote {len(events)} events -> {args.out}", flush=True)


def main():
    ap = argparse.ArgumentParser(description="Store Intelligence detection layer")
    ap.add_argument("--clip", required=True, help="Path to CCTV video clip")
    ap.add_argument("--camera", required=True, help="Camera ID (e.g. CAM_ENTRY_01, CAM_FLOOR_01, CAM_BILL_01)")
    ap.add_argument("--store", default="STORE_BLR_002", help="Store ID")
    ap.add_argument("--start", default=None,
                    help="ISO-8601 base timestamp for frame 0. Defaults to 'now' "
                         "(UTC) so events land in the live analytics window.")
    ap.add_argument("--api", help="API base URL, e.g. http://localhost:8000")
    ap.add_argument("--out", help="Write events to JSONL")
    ap.add_argument("--weights", default="yolov8n.pt", help="YOLOv8 weights file")
    ap.add_argument("--conf", type=float, default=0.25,
                    help="YOLO detection confidence floor. Detections below this "
                         "are not tracked; emitted events still carry their true "
                         "confidence (low-confidence events are flagged, not hidden).")
    args = ap.parse_args()

    if not args.start:
        args.start = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    run_video(args)


if __name__ == "__main__":
    main()
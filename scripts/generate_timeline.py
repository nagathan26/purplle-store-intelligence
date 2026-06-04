#!/usr/bin/env python3
"""
generate_timeline.py

Recursively scans a directory for CCTV clips, processes them on GPU using YOLOv8,
and generates structured JSON timelines highlighting significant scenes.
"""

import argparse
import json
import os
import glob
import cv2
import torch
from datetime import datetime, timedelta, timezone
from ultralytics import YOLO

# Zone layout quadrant helper from detect.py
def get_zone_id(centroid, frame_width, frame_height, camera_lower="cam_floor_01", store_id="STORE_BLR_002"):
    nx = centroid[0] / frame_width * 100
    ny = centroid[1] / frame_height * 100
    if store_id == "STORE_BLR_002" and "floor_01" in camera_lower:
        # CAM_FLOOR_01 covers Skincare, Haircare, Fragrance
        if nx < 33:
            return "SKINCARE"
        elif nx < 66:
            return "HAIRCARE"
        else:
            return "FRAGRANCE"
    elif store_id == "STORE_BLR_002" and "floor_02" in camera_lower:
        # CAM_FLOOR_02 covers Makeup, Wellness
        if nx < 50:
            return "MAKEUP"
        else:
            return "WELLNESS"
    else:
        # Default or Store 2 CAM_FLOOR_01 covers Skincare, Haircare, Fragrance, Makeup, Wellness
        if nx < 33 and ny < 50:
            return "SKINCARE"
        elif nx < 66 and ny < 50:
            return "HAIRCARE"
        elif ny < 50:
            return "FRAGRANCE"
        elif nx < 50:
            return "MAKEUP"
        else:
            return "WELLNESS"

def format_time_offset(seconds):
    td = timedelta(seconds=seconds)
    hours, remainder = divmod(td.seconds, 3600)
    minutes, seconds_int = divmod(remainder, 60)
    milliseconds = td.microseconds // 1000
    return f"{hours:02d}:{minutes:02d}:{seconds_int:02d}.{milliseconds:03d}"

def process_video(clip_path, weights_path, conf_floor):
    print(f"\nProcessing clip: {clip_path}")
    device = "cuda" if torch.cuda.is_available() else "cpu"
    print(f"Using device: {device}")

    # Load YOLOv8 model
    model = YOLO(weights_path)
    
    cap = cv2.VideoCapture(clip_path)
    if not cap.isOpened():
        print(f"Error opening video file: {clip_path}")
        return []

    fps = cap.get(cv2.CAP_PROP_FPS) or 25
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    frame_width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)) or 1920
    frame_height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)) or 1080
    filename = os.path.basename(clip_path)
    camera_lower = filename.lower()
    path_upper = clip_path.replace("\\", "/").upper()
    store_id = "STORE_DEL_004" if ("/STORE 2/" in path_upper or "/STORE2/" in path_upper) else "STORE_BLR_002"

    # Staff heuristic thresholds (consistent with detect.py)
    STAFF_DARK_BGR = 50
    STAFF_MIN_HITS = 6
    STAFF_MIN_BOX_FRAC = 0.12

    # State variables
    visitor_is_staff = {}
    staff_dark_hits = {}
    active_visitor_zones = {}
    active_billing_queue = {}
    
    last_detected_vids = set()
    last_detected_objects = {}
    last_brightness = None
    
    timeline = []
    frame_idx = 0

    while True:
        ok, frame = cap.read()
        if not ok:
            break
        
        # Calculate video timestamp offset
        offset_seconds = frame_idx / fps
        time_offset_str = format_time_offset(offset_seconds)

        # 1. Calculate Average Frame Brightness (mean of Y channel in YUV, or simple BGR mean)
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        curr_brightness = float(gray.mean())

        # 2. Run YOLOv8 Tracking (class 0 is person, but we can track all objects if we don't filter classes)
        # We track everything, then filter in python.
        res = model.track(frame, persist=True, conf=conf_floor, verbose=False, device=device)
        
        current_vids = set()
        current_objects = {}
        people_details = []
        actions = []
        env_changes = []

        boxes = res[0].boxes
        if boxes is not None and len(boxes) > 0:
            for box in boxes:
                # Class name
                cls_id = int(box.cls[0])
                cls_name = model.names[cls_id]
                conf = float(box.conf)
                
                # Check if it has a tracking ID
                track_id = int(box.id[0]) if box.id is not None else None
                x1, y1, x2, y2 = box.xyxy[0].tolist()
                centroid = ((x1 + x2) / 2, (y1 + y2) / 2)

                if cls_name == "person" and track_id is not None:
                    vid = f"VIS_{track_id}"
                    current_vids.add(vid)

                    # Staff classification heuristic
                    crop = frame[int(y1):int(y2), int(x1):int(x2)]
                    if not visitor_is_staff.get(vid, False) and crop.size > 0:
                        box_h = y2 - y1
                        h, w = crop.shape[:2]
                        if box_h >= frame_height * STAFF_MIN_BOX_FRAC and h > 0 and w > 0:
                            torso = crop[int(h*0.25):int(h*0.55), int(w*0.20):int(w*0.80)]
                            if torso.size > 0:
                                b_val, g_val, r_val = torso.mean(axis=(0, 1))
                                if b_val < STAFF_DARK_BGR and g_val < STAFF_DARK_BGR and r_val < STAFF_DARK_BGR:
                                    staff_dark_hits[vid] = staff_dark_hits.get(vid, 0) + 1
                                    if staff_dark_hits[vid] >= STAFF_MIN_HITS:
                                        visitor_is_staff[vid] = True
                    
                    is_staff = visitor_is_staff.get(vid, False)
                    role = "Staff" if is_staff else "Customer"

                    # Camera-specific actions & zones
                    current_zone = None
                    if "entry" in camera_lower:
                        pass
                    elif "floor" in camera_lower or "zone" in camera_lower:
                        current_zone = get_zone_id(centroid, frame_width, frame_height, camera_lower, store_id)
                        prev_zone = active_visitor_zones.get(vid)
                        if prev_zone is None:
                            active_visitor_zones[vid] = current_zone
                            actions.append(f"{role} {vid} entered the {current_zone} zone.")
                        elif prev_zone != current_zone:
                            actions.append(f"{role} {vid} moved from {prev_zone} to the {current_zone} zone.")
                            active_visitor_zones[vid] = current_zone
                    elif "bill" in camera_lower or "queue" in camera_lower:
                        # Simple heuristic: billing area tracking
                        if not active_billing_queue.get(vid, False):
                            active_billing_queue[vid] = True
                            # Compute queue depth (number of people currently detected)
                            queue_depth = max(0, len(current_vids) - 1)
                            actions.append(f"{role} {vid} joined the billing queue (Queue Depth: {queue_depth}).")

                    people_details.append({
                        "visitor_id": vid,
                        "role": role,
                        "position": {"x": round(centroid[0], 1), "y": round(centroid[1], 1)},
                        "bbox": [round(x1, 1), round(y1, 1), round(x2, 1), round(y2, 1)],
                        "zone": current_zone,
                        "confidence": round(conf, 3)
                    })
                else:
                    # Non-person objects (backpack, handbag, umbrella, suitcase, etc.)
                    current_objects[cls_name] = current_objects.get(cls_name, 0) + 1

        # Check for Entry / Exit actions
        entered_vids = current_vids - last_detected_vids
        exited_vids = last_detected_vids - current_vids

        for vid in entered_vids:
            is_staff = visitor_is_staff.get(vid, False)
            role = "Staff" if is_staff else "Customer"
            actions.append(f"{role} {vid} entered the scene.")

        for vid in exited_vids:
            is_staff = visitor_is_staff.get(vid, False)
            role = "Staff" if is_staff else "Customer"
            actions.append(f"{role} {vid} exited the scene.")
            # Clean up billing or zone states
            if vid in active_visitor_zones:
                actions.append(f"{role} {vid} left the {active_visitor_zones[vid]} zone.")
                del active_visitor_zones[vid]
            if vid in active_billing_queue:
                actions.append(f"{role} {vid} left the billing queue.")
                del active_billing_queue[vid]

        # Check for non-person object count changes
        all_object_types = set(current_objects.keys()) | set(last_detected_objects.keys())
        for obj_type in all_object_types:
            curr_cnt = current_objects.get(obj_type, 0)
            prev_cnt = last_detected_objects.get(obj_type, 0)
            if curr_cnt != prev_cnt:
                if curr_cnt > prev_cnt:
                    actions.append(f"Detected new object: {obj_type} (Count: {curr_cnt}).")
                else:
                    actions.append(f"Object left view: {obj_type} (Count: {curr_cnt}).")

        # Check for Environment Changes: Lighting change
        if last_brightness is not None:
            brightness_pct_change = abs(curr_brightness - last_brightness) / max(1.0, last_brightness) * 100
            if brightness_pct_change > 15.0: # threshold of 15% change
                env_changes.append(f"Significant lighting shift detected: average brightness changed by {brightness_pct_change:.1f}% (from {last_brightness:.1f} to {curr_brightness:.1f}).")

        # Check for Environment Changes: Occupancy change
        if len(current_vids) != len(last_detected_vids):
            env_changes.append(f"Occupancy count changed: total people in view went from {len(last_detected_vids)} to {len(current_vids)}.")

        # Determine if this frame is a "significant scene"
        is_significant = (
            frame_idx == 0 or
            len(actions) > 0 or
            len(env_changes) > 0 or
            frame_idx == total_frames - 1
        )

        if is_significant:
            # If no specific environment change was flagged, default message
            if not env_changes:
                env_changes.append("No significant environmental changes.")

            timeline.append({
                "frame": frame_idx,
                "timestamp": time_offset_str,
                "objects": current_objects,
                "people": people_details,
                "actions": actions if actions else ["Scene static / ongoing tracking."],
                "environment_changes": env_changes
            })

        # Update frame-to-frame states
        last_detected_vids = current_vids
        last_detected_objects = current_objects
        last_brightness = curr_brightness
        frame_idx += 1

        if frame_idx % 100 == 0:
            pct = (frame_idx / total_frames) * 100 if total_frames > 0 else 0.0
            print(f"  Frame {frame_idx}/{total_frames} ({pct:.1f}%) processed...", end="\r", flush=True)

    cap.release()
    print(f"\nFinished processing. Generated {len(timeline)} timeline events.")
    return timeline

def main():
    parser = argparse.ArgumentParser(description="Generate frame-by-frame JSON timeline from CCTV video files using GPU.")
    parser.add_argument("--dir", default="CCTV Footage", help="Folder containing CCTV clips (will search recursively)")
    parser.add_argument("--clip", default=None, help="Process a single clip instead of scanning the folder")
    parser.add_argument("--out-dir", default="timelines", help="Directory where timeline JSONs will be written")
    parser.add_argument("--weights", default="yolov8n.pt", help="Path to YOLOv8 weights file")
    parser.add_argument("--conf", type=float, default=0.25, help="YOLO confidence floor")
    args = parser.parse_args()

    # Ensure output directory exists
    os.makedirs(args.out_dir, exist_ok=True)

    # Discovered files list
    clips = []
    if args.clip:
        if os.path.exists(args.clip):
            clips.append(args.clip)
        else:
            print(f"Error: clip file {args.clip} does not exist.")
            return
    else:
        # Search recursively for .mp4 files
        pattern = os.path.join(args.dir, "**", "*.mp4")
        clips = glob.glob(pattern, recursive=True)
        if not clips:
            print(f"No .mp4 video clips found in '{args.dir}'")
            return

    print(f"Found {len(clips)} clip(s) to process:")
    for c in clips:
        print(f"  - {c}")

    all_timelines = {}

    for clip in clips:
        timeline = process_video(clip, args.weights, args.conf)
        
        # Save individual timeline JSON
        clip_name = os.path.splitext(os.path.basename(clip))[0]
        safe_name = clip_name.replace(" ", "_").replace("-", "_").lower()
        out_filename = os.path.join(args.out_dir, f"timeline_{safe_name}.json")
        
        with open(out_filename, "w", encoding="utf-8") as f:
            json.dump(timeline, f, indent=2)
        
        print(f"Saved timeline to: {out_filename}")
        all_timelines[clip_name] = timeline

    # Save a master index file mapping all clip names to their timelines
    master_path = os.path.join(args.out_dir, "master_timeline.json")
    with open(master_path, "w", encoding="utf-8") as f:
        json.dump(all_timelines, f, indent=2)
    print(f"\nSaved master timeline mapping for all clips to: {master_path}")

if __name__ == "__main__":
    main()

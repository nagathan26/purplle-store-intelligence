"""Analyze staff vs customer classification across timeline JSONs."""
import argparse
import glob
import json
import os


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--fps",
        type=float,
        default=15.0,
        help="Video FPS used for converting frame delays to seconds",
    )
    ap.add_argument(
        "--threshold",
        type=int,
        default=150,
        help="Minimum appearances before a non-staff visitor is considered long-lived",
    )
    args = ap.parse_args()

    for f in sorted(glob.glob("timelines/timeline_*.json")):
        with open(f, encoding="utf-8") as fh:
            data = json.load(fh)

        first_seen = {}
        first_staff = {}
        total_appearances = {}

        for ev in data:
            frame = ev.get("frame")

            if frame is None:
                continue

            for p in ev.get("people", []):
                vid = p.get("visitor_id")

                if vid is None:
                    continue

                if vid not in first_seen:
                    first_seen[vid] = frame

                total_appearances[vid] = (
                    total_appearances.get(vid, 0) + 1
                )

                if (
                    p.get("role") == "Staff"
                    and vid not in first_staff
                ):
                    first_staff[vid] = frame

        fname = os.path.basename(f)

        # Long-lived visitors never classified as staff
        long_lived = {
            vid: total_appearances[vid]
            for vid in first_seen
            if (
                vid not in first_staff
                and total_appearances[vid] > args.threshold
            )
        }

        if long_lived:
            threshold_sec = args.threshold / args.fps

            print(
                f"{fname}: Long-lived 'Customers' "
                f"(>{args.threshold} frames, "
                f">{threshold_sec:.1f}s, "
                f"possibly undetected staff):"
            )

            for vid, cnt in sorted(
                long_lived.items(),
                key=lambda x: -x[1]
            )[:5]:
                duration_sec = cnt / args.fps

                print(
                    f"  {vid}: "
                    f"{cnt} frames "
                    f"({duration_sec:.1f}s)"
                )

        # Staff classification delay
        delays = []

        for vid in first_staff:
            if vid not in first_seen:
                continue

            delays.append(
                first_staff[vid] - first_seen[vid]
            )

        if delays:
            avg_frames = sum(delays) / len(delays)

            min_frames = min(delays)
            max_frames = max(delays)

            min_sec = min_frames / args.fps
            avg_sec = avg_frames / args.fps
            max_sec = max_frames / args.fps

            print(
                f"{fname}: Staff classification delay\n"
                f"  Frames : "
                f"min={min_frames}, "
                f"avg={avg_frames:.0f}, "
                f"max={max_frames}\n"
                f"  Seconds: "
                f"min={min_sec:.2f}s, "
                f"avg={avg_sec:.2f}s, "
                f"max={max_sec:.2f}s"
            )

        print()


if __name__ == "__main__":
    main()
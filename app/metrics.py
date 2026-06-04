"""
Real-time analytics computation: metrics, funnel, heatmap.

All functions read straight from storage at request time — nothing is cached from
"yesterday". Zero-traffic and zero-purchase stores return well-formed zeroes, never
null or a divide-by-zero crash.
"""
from __future__ import annotations

import time
from typing import Optional

from .sessions import build_sessions, correlate_conversions
from .storage import Store


def _window_start(window_s: Optional[int]) -> Optional[float]:
    return None if window_s is None else time.time() - window_s


def compute_trends(rows, pos_rows, bin_size_s: float = 30.0) -> dict:
    import json
    all_epochs = [r["ts_epoch"] for r in rows] + [tx["ts_epoch"] for tx in pos_rows]
    if not all_epochs:
        return {"labels": [], "visitors": [], "conversions": [], "queue_depth": []}

    start_epoch = min(all_epochs)
    end_epoch = max(all_epochs)
    duration = end_epoch - start_epoch
    if duration <= 0:
        duration = 150.0

    num_bins = int(duration / bin_size_s) + 1
    if num_bins > 20:
        bin_size_s = duration / 15
        num_bins = 16

    labels = []
    visitors = [0] * num_bins
    conversions = [0] * num_bins
    queue_depth = [0.0] * num_bins
    queue_counts = [0] * num_bins

    for i in range(num_bins):
        relative_s = i * bin_size_s
        labels.append(f"+{int(relative_s)}s")

    for r in rows:
        # Fix #1: count REENTRY as well as ENTRY
        if r["event_type"] in ("ENTRY", "REENTRY"):
            idx = int((r["ts_epoch"] - start_epoch) / bin_size_s)
            if 0 <= idx < num_bins:
                visitors[idx] += 1
        elif r["event_type"] in ("BILLING_QUEUE_JOIN", "BILLING_QUEUE_ABANDON"):
            md = json.loads(r["metadata"] or "{}")
            qd = md.get("queue_depth")
            if isinstance(qd, (int, float)):
                idx = int((r["ts_epoch"] - start_epoch) / bin_size_s)
                if 0 <= idx < num_bins:
                    queue_depth[idx] += qd
                    queue_counts[idx] += 1

    for i in range(num_bins):
        if queue_counts[i] > 0:
            queue_depth[i] = round(queue_depth[i] / queue_counts[i], 1)

    for tx in pos_rows:
        idx = int((tx["ts_epoch"] - start_epoch) / bin_size_s)
        if 0 <= idx < num_bins:
            conversions[idx] += 1

    return {
        "labels": labels,
        "visitors": visitors,
        "conversions": conversions,
        "queue_depth": queue_depth,
    }


def compute_metrics(store: Store, store_id: str, window_s: Optional[int] = 86400) -> dict:
    since = _window_start(window_s)
    rows = store.events_for(store_id, since_epoch=since, include_staff=False)
    pos = store.pos_for(store_id, since_epoch=since)

    sessions = build_sessions(rows)
    correlate_conversions(sessions, pos)

    unique_visitors = len(sessions)
    converted = sum(1 for s in sessions.values() if s.converted)
    conversion_rate = round(converted / unique_visitors, 4) if unique_visitors else 0.0

    # avg dwell per zone (ms -> seconds)
    zone_dwell_totals: dict[str, list[int]] = {}
    for s in sessions.values():
        for z, ms in s.dwell_by_zone.items():
            zone_dwell_totals.setdefault(z, []).append(ms)
    avg_dwell_per_zone = {
        z: round(sum(v) / len(v) / 1000, 1) for z, v in zone_dwell_totals.items()
    }

    # current queue depth = most recently observed depth in the last 5 minutes
    recent = store.events_for(store_id, since_epoch=time.time() - 300, include_staff=False)
    queue_depth = 0
    latest_epoch = -1.0
    import json
    for r in recent:
        if r["event_type"] in ("BILLING_QUEUE_JOIN", "BILLING_QUEUE_ABANDON"):
            md = json.loads(r["metadata"] or "{}")
            qd = md.get("queue_depth")
            if isinstance(qd, int) and r["ts_epoch"] >= latest_epoch:
                latest_epoch = r["ts_epoch"]
                queue_depth = qd

    joined = sum(1 for s in sessions.values() if s.joined_billing_queue)
    abandoned = sum(1 for s in sessions.values() if s.abandoned_queue and not s.converted)
    abandonment_rate = round(abandoned / joined, 4) if joined else 0.0

    # Staff count: unique physical IDs within the window
    all_rows_with_staff = store.events_for(store_id, since_epoch=since, include_staff=True)
    staff_ids = {r["visitor_id"].split("#", 1)[0] for r in all_rows_with_staff if r["is_staff"]}
    staff_count = len(staff_ids)

    # Fix #5: use `is not None` guard so epoch=0 doesn't falsely short-circuit
    wait_times = []
    for s in sessions.values():
        if (
            s.joined_billing_queue
            and s.billing_join_epoch is not None
            and s.billing_leave_epoch is not None
        ):
            dur = s.billing_leave_epoch - s.billing_join_epoch
            if dur >= 0:
                wait_times.append(dur)
    avg_wait_time = round(sum(wait_times) / len(wait_times), 1) if wait_times else 0.0

    # Repeat visitor rate
    repeat_visitors = sum(1 for s in sessions.values() if s.reentered)
    repeat_rate = round(repeat_visitors / unique_visitors, 4) if unique_visitors else 0.0

    # POS revenue calculations
    simple_revenue = sum(float(r["basket_value"]) for r in pos)
    simple_txn_count = len(pos)
    simple_aov = round(simple_revenue / simple_txn_count, 2) if simple_txn_count else 0.0

    detailed_stats = store.get_pos_details_stats(store_id, since_epoch=since)

    # Fix #3: use explicit None check so actual revenue of 0.0 is not overridden
    if detailed_stats["total_revenue"] is not None:
        final_revenue = detailed_stats["total_revenue"]
    else:
        final_revenue = round(simple_revenue, 2)

    final_txn_count = detailed_stats["detailed_txn_count"] or simple_txn_count
    final_aov = (
        round(detailed_stats["total_revenue"] / detailed_stats["detailed_txn_count"], 2)
        if detailed_stats["detailed_txn_count"]
        else simple_aov
    )
    final_rpv = round(final_revenue / unique_visitors, 2) if unique_visitors else 0.0

    # Compute timeline trends (base_epoch removed — unused)
    trends = compute_trends(rows, pos)

    # Fix #2: camera visitor count de-duplicates physical IDs (strips #n suffix)
    with store._cursor() as cur:
        if since is not None:
            cur.execute(
                """SELECT camera_id, COUNT(*) as event_count, visitor_id
                   FROM events WHERE store_id = ? AND ts_epoch >= ?
                   GROUP BY camera_id, visitor_id""",
                (store_id, since),
            )
        else:
            cur.execute(
                """SELECT camera_id, COUNT(*) as event_count, visitor_id
                   FROM events WHERE store_id = ?
                   GROUP BY camera_id, visitor_id""",
                (store_id,),
            )
        raw_rows = cur.fetchall()

    camera_metrics: dict[str, dict] = {}
    for row in raw_rows:
        cam = row["camera_id"]
        physical_id = row["visitor_id"].split("#", 1)[0]
        if cam not in camera_metrics:
            camera_metrics[cam] = {"event_count": 0, "visitor_ids": set()}
        camera_metrics[cam]["event_count"] += row["event_count"]
        camera_metrics[cam]["visitor_ids"].add(physical_id)

    camera_metrics_out = {
        cam: {
            "event_count":   v["event_count"],
            "visitor_count": len(v["visitor_ids"]),
        }
        for cam, v in camera_metrics.items()
    }

    return {
        "store_id": store_id,
        "window_seconds": window_s,
        "unique_visitors": unique_visitors,
        "converted_visitors": converted,
        "conversion_rate": conversion_rate,
        "avg_dwell_per_zone_s": avg_dwell_per_zone,
        "current_queue_depth": queue_depth,
        "queue_abandonment_rate": abandonment_rate,
        "staff_count": staff_count,
        "avg_queue_wait_time_s": avg_wait_time,
        "repeat_visitor_rate": repeat_rate,
        "total_revenue": final_revenue,
        "avg_basket_value": final_aov,
        "revenue_per_visitor": final_rpv,
        "transaction_count": final_txn_count,
        "top_brands": detailed_stats["top_brands"],
        "top_categories": detailed_stats["top_categories"],
        "top_products": detailed_stats["top_products"],
        "avg_items_per_basket": detailed_stats["avg_items_per_basket"],
        "trends": trends,
        "camera_metrics": camera_metrics_out,
        "computed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }


def compute_funnel(store: Store, store_id: str, window_s: Optional[int] = 86400) -> dict:
    since = _window_start(window_s)
    rows = store.events_for(store_id, since_epoch=since, include_staff=False)
    pos = store.pos_for(store_id, since_epoch=since)
    sessions = build_sessions(rows)
    correlate_conversions(sessions, pos)

    entry = sum(1 for s in sessions.values() if s.entered)
    zone = sum(1 for s in sessions.values() if s.entered and s.zones_visited)
    billing = sum(1 for s in sessions.values() if s.joined_billing_queue)
    purchase = sum(1 for s in sessions.values() if s.converted)

    def drop(a: int, b: int) -> float:
        return round((1 - b / a) * 100, 1) if a else 0.0

    return {
        "store_id": store_id,
        "unit": "session",
        "stages": [
            {"stage": "ENTRY",         "count": entry,    "drop_off_pct": 0.0},
            {"stage": "ZONE_VISIT",    "count": zone,     "drop_off_pct": drop(entry, zone)},
            {"stage": "BILLING_QUEUE", "count": billing,  "drop_off_pct": drop(zone, billing)},
            {"stage": "PURCHASE",      "count": purchase, "drop_off_pct": drop(billing, purchase)},
        ],
        "overall_conversion_pct": round(purchase / entry * 100, 1) if entry else 0.0,
    }


def compute_heatmap(store: Store, store_id: str, window_s: Optional[int] = 86400) -> dict:
    since = _window_start(window_s)
    rows = store.events_for(store_id, since_epoch=since, include_staff=False)
    pos = store.pos_for(store_id, since_epoch=since)
    sessions = build_sessions(rows)
    correlate_conversions(sessions, pos)

    visits: dict[str, int] = {}
    dwell: dict[str, list[int]] = {}
    zone_conversions: dict[str, int] = {}

    for s in sessions.values():
        for z in s.zones_visited:
            visits[z] = visits.get(z, 0) + 1
        for z, ms in s.dwell_by_zone.items():
            dwell.setdefault(z, []).append(ms)

        # Fix #6: credit conversion only to the last zone visited before billing
        if s.converted and s.zones_visited:
            last_zone = s.zones_visited[-1]
            zone_conversions[last_zone] = zone_conversions.get(last_zone, 0) + 1

    max_visits = max(visits.values()) if visits else 0
    max_dwell = max((sum(v) / len(v) for v in dwell.values()), default=0) or 1

    zones = []
    for z in sorted(set(visits) | set(dwell)):
        avg_dwell_ms = (sum(dwell[z]) / len(dwell[z])) if z in dwell else 0
        z_visits = visits.get(z, 0)
        z_convs = zone_conversions.get(z, 0)
        z_conv_rate = round(z_convs / z_visits, 4) if z_visits else 0.0
        zones.append({
            "zone_id":         z,
            "visit_frequency": z_visits,
            "visit_score":     round(z_visits / max_visits * 100, 1) if max_visits else 0.0,
            "avg_dwell_s":     round(avg_dwell_ms / 1000, 1),
            "dwell_score":     round(avg_dwell_ms / max_dwell * 100, 1),
            "conversion_rate": z_conv_rate,
        })

    n_sessions = len(sessions)
    return {
        "store_id":        store_id,
        "session_count":   n_sessions,
        "data_confidence": "LOW" if n_sessions < 20 else "OK",
        "zones":           zones,
    }
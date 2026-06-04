"""
Anomaly detection.

Three detectors, each emitting a structured Anomaly with severity and a concrete
suggested_action an on-call retail-ops person can act on:

  * BILLING_QUEUE_SPIKE  - queue depth crosses thresholds right now
  * CONVERSION_DROP      - today's conversion materially below the 7-day baseline
  * DEAD_ZONE            - a zone with prior traffic has had no visits for 30 min

Detectors are intentionally simple and explainable. Thresholds live here as
constants so they are easy to tune and easy to defend in follow-up questions.
"""
from __future__ import annotations

import json
import time

from .metrics import compute_metrics
from .models import Anomaly, Severity
from .storage import Store

QUEUE_WARN = 5
QUEUE_CRIT = 10

CONV_DROP_WARN = 0.20   # 20% relative drop
CONV_DROP_CRIT = 0.40

DEAD_ZONE_S = 1800      # 30 min


def detect(store: Store, store_id: str) -> list[dict]:
    now = time.time()

    out: list[Anomaly] = []

    # ------------------------------------------------------------------
    # BILLING_QUEUE_SPIKE
    # ------------------------------------------------------------------
    recent = store.events_for(
        store_id,
        since_epoch=now - 300,
        include_staff=False,
    )

    max_q = 0

    for r in recent:
        if r["event_type"] != "BILLING_QUEUE_JOIN":
            continue

        md = json.loads(r["metadata"] or "{}")

        qd = md.get("queue_depth")

        try:
            qd = int(qd)
            max_q = max(max_q, qd)
        except (TypeError, ValueError):
            pass

    if max_q >= QUEUE_CRIT:
        out.append(
            _anom(
                "BILLING_QUEUE_SPIKE",
                Severity.CRITICAL,
                store_id,
                f"Queue depth {max_q} (>= {QUEUE_CRIT}).",
                "Open an additional billing counter immediately.",
            )
        )

    elif max_q >= QUEUE_WARN:
        out.append(
            _anom(
                "BILLING_QUEUE_SPIKE",
                Severity.WARN,
                store_id,
                f"Queue depth {max_q} (>= {QUEUE_WARN}).",
                "Alert floor staff to assist at billing.",
            )
        )

    # ------------------------------------------------------------------
    # CONVERSION_DROP
    # ------------------------------------------------------------------
    today = compute_metrics(
        store,
        store_id,
        window_s=86400,
    )["conversion_rate"]

    week = compute_metrics(
        store,
        store_id,
        window_s=7 * 86400,
    )["conversion_rate"]

    if week > 0:
        rel = (week - today) / week

        if rel >= CONV_DROP_CRIT:
            out.append(
                _anom(
                    "CONVERSION_DROP",
                    Severity.CRITICAL,
                    store_id,
                    (
                        f"Conversion {today:.2%} "
                        f"vs 7-day {week:.2%} "
                        f"({rel:.0%} down)."
                    ),
                    (
                        "Escalate to store manager; "
                        "check staffing & stockouts."
                    ),
                )
            )

        elif rel >= CONV_DROP_WARN:
            out.append(
                _anom(
                    "CONVERSION_DROP",
                    Severity.WARN,
                    store_id,
                    (
                        f"Conversion {today:.2%} "
                        f"vs 7-day {week:.2%} "
                        f"({rel:.0%} down)."
                    ),
                    "Review floor coverage during peak hours.",
                )
            )

    # ------------------------------------------------------------------
    # DEAD_ZONE
    # ------------------------------------------------------------------
    all_rows = store.events_for(
        store_id,
        include_staff=False,
    )

    last_seen: dict[str, float] = {}

    zone_activity_events = {
        "ZONE_ENTER",
        "ZONE_DWELL",
        "ZONE_EXIT",
    }

    for r in all_rows:
        if (
            r["zone_id"]
            and r["event_type"] in zone_activity_events
        ):
            last_seen[r["zone_id"]] = max(
                last_seen.get(r["zone_id"], 0),
                r["ts_epoch"],
            )

    for zone, seen in last_seen.items():
        if now - seen >= DEAD_ZONE_S:
            out.append(
                _anom(
                    "DEAD_ZONE",
                    Severity.INFO,
                    store_id,
                    (
                        f"Zone {zone} has had no visits for "
                        f"{int((now - seen) / 60)} min."
                    ),
                    (
                        f"Verify camera covering {zone} "
                        "and check merchandising."
                    ),
                )
            )

    return [
        a.model_dump(mode="json")
        for a in out
    ]


def _anom(
    t: str,
    sev: Severity,
    store_id: str,
    detail: str,
    action: str,
) -> Anomaly:
    return Anomaly(
        anomaly_type=t,
        severity=sev,
        store_id=store_id,
        detected_at=time.strftime(
            "%Y-%m-%dT%H:%M:%SZ",
            time.gmtime(),
        ),
        detail=detail,
        suggested_action=action,
    )
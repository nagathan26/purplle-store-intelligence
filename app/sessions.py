"""
Session reconstruction.

A *session* is the unit of analytics, not the raw event. The challenge is explicit
that re-entries must NOT double-count a visitor. We model this by collapsing a
visitor_id's event stream into a session object that tracks the zones visited, the
billing-queue interaction, and whether the visit converted.

The visitor_id from the detection layer is already per-visit-session, and a REENTRY
event signals "same physical person, new visit". For funnel/unique-visitor counting
we deduplicate on the *physical identity* by stripping the re-entry suffix the
pipeline appends (see pipeline/tracker.py), falling back to visitor_id when absent.

Cross-camera linking: the tracker runs per detect.py invocation, so the entry-cam
visitor_id and the floor-cam visitor_id for the same physical person differ. Without
linking, the funnel ZONE_VISIT stage drops to zero (entry-cam sessions never see
zones; floor-cam sessions never see ENTRY). `link_cross_camera_sessions` heuristically
folds floor/billing-only sessions into the nearest entry-cam session within a short
time window for the same store. This is conservative — it never merges two
entry-cam sessions, so groups entering simultaneously still count as N visitors.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

# Reduced from 180s to 60s — tighter window avoids merging unrelated visitors
# in dense footage where multiple people enter within the same minute.
LINK_WINDOW_S = 60


def physical_id(visitor_id: str) -> str:
    """Collapse re-entry variants (VIS_abc#2) to a single physical identity."""
    return visitor_id.split("#", 1)[0]


def _camera_role(camera_id: str) -> str:
    """Map any camera id to one of {entry, floor, billing, other}."""
    c = (camera_id or "").upper()
    if "ENTRY" in c:
        return "entry"
    if "FLOOR" in c:
        return "floor"
    if "BILL" in c:
        return "billing"
    return "other"


@dataclass
class Session:
    visitor_id: str
    physical: str
    entered: bool = False
    reentered: bool = False
    zones_visited: list = field(default_factory=list)   # ordered list, preserves last-zone for heatmap
    joined_billing_queue: bool = False
    abandoned_queue: bool = False
    converted: bool = False
    first_epoch: Optional[float] = None
    last_epoch: Optional[float] = None
    dwell_by_zone: dict[str, int] = field(default_factory=dict)
    cameras: set[str] = field(default_factory=set)
    billing_join_epoch: Optional[float] = None
    billing_leave_epoch: Optional[float] = None

    def _absorb(self, other: "Session") -> None:
        """Merge `other` into self. `other` should be discarded by the caller."""
        self.entered = self.entered or other.entered
        self.reentered = self.reentered or other.reentered
        # merge zone lists preserving order, deduplicating
        for z in other.zones_visited:
            if z not in self.zones_visited:
                self.zones_visited.append(z)
        self.joined_billing_queue = self.joined_billing_queue or other.joined_billing_queue
        self.abandoned_queue = self.abandoned_queue or other.abandoned_queue
        self.converted = self.converted or other.converted
        self.cameras |= other.cameras
        if other.first_epoch is not None:
            self.first_epoch = min(self.first_epoch or other.first_epoch,
                                   other.first_epoch)
        if other.last_epoch is not None:
            self.last_epoch = max(self.last_epoch or other.last_epoch,
                                  other.last_epoch)
        for z, ms in other.dwell_by_zone.items():
            self.dwell_by_zone[z] = max(self.dwell_by_zone.get(z, 0), ms)
        if other.billing_join_epoch is not None:
            self.billing_join_epoch = min(self.billing_join_epoch or other.billing_join_epoch,
                                          other.billing_join_epoch)
        if other.billing_leave_epoch is not None:
            self.billing_leave_epoch = max(self.billing_leave_epoch or other.billing_leave_epoch,
                                           other.billing_leave_epoch)


def build_sessions(rows, *, link_cross_camera: bool = True) -> dict[str, Session]:
    """Reconstruct sessions keyed by physical identity from ordered event rows.

    Rows are expected sorted by ts_epoch ascending and already staff-filtered by
    the caller when customer-only analytics are required.
    """
    sessions: dict[str, Session] = {}
    for r in rows:
        pid = physical_id(r["visitor_id"])
        s = sessions.get(pid)
        if s is None:
            s = Session(visitor_id=r["visitor_id"], physical=pid)
            sessions[pid] = s
        ev = r["event_type"]
        if s.first_epoch is None:
            s.first_epoch = r["ts_epoch"]
        s.last_epoch = r["ts_epoch"]
        if "camera_id" in r.keys() and r["camera_id"]:
            s.cameras.add(r["camera_id"])

        if ev == "ENTRY":
            s.entered = True
        elif ev == "REENTRY":
            s.reentered = True
            s.entered = True
        elif ev in ("ZONE_ENTER", "ZONE_EXIT", "ZONE_DWELL") and r["zone_id"]:
            if r["zone_id"] not in s.zones_visited:
                s.zones_visited.append(r["zone_id"])
            # dwell_ms on DWELL/EXIT events is cumulative-from-zone-entry, so the
            # correct per-visit dwell is the MAX seen, not the sum of checkpoints.
            if r["dwell_ms"]:
                s.dwell_by_zone[r["zone_id"]] = max(
                    s.dwell_by_zone.get(r["zone_id"], 0), r["dwell_ms"]
                )
        elif ev == "BILLING_QUEUE_JOIN":
            s.joined_billing_queue = True
            if s.billing_join_epoch is None:
                s.billing_join_epoch = r["ts_epoch"]
        elif ev == "BILLING_QUEUE_ABANDON":
            s.abandoned_queue = True
            s.billing_leave_epoch = r["ts_epoch"]

    if link_cross_camera:
        sessions = link_cross_camera_sessions(sessions)
    return sessions


def link_cross_camera_sessions(
    sessions: dict[str, Session], window_s: float = LINK_WINDOW_S,
) -> dict[str, Session]:
    """Fold floor/billing-only sessions into the nearest entry-cam session.

    Two-tracker reality: the per-process tracker assigns different visitor_ids on
    different cameras. Without linking, a customer who entered the store and walked
    to skincare looks like {entry-cam session with zero zones} + {floor-cam session
    with zero entry}. The funnel collapses. We link them by temporal proximity on
    the same store, only when the floor/billing session has *no* ENTRY of its own.
    Conservative on purpose: never merges two entry-cam sessions (so group entries
    still count as N visitors); never crosses a stale window boundary.
    """
    entry_sessions = sorted(
        (s for s in sessions.values() if s.entered and _has_entry_cam(s)),
        key=lambda s: s.first_epoch or 0.0,
    )
    if not entry_sessions:
        return sessions

    survivors: dict[str, Session] = {s.physical: s for s in entry_sessions}
    for s in list(sessions.values()):
        if s.physical in survivors:
            continue
        if s.entered:
            # has its own ENTRY (different camera context) — keep it standalone
            survivors[s.physical] = s
            continue
        target = _nearest_entry(s, entry_sessions, window_s)
        if target is not None:
            target._absorb(s)
        else:
            # orphan floor/billing session — keep it so we don't lose the data
            survivors[s.physical] = s
    return survivors


def _has_entry_cam(s: Session) -> bool:
    return any(_camera_role(c) == "entry" for c in s.cameras)


def _nearest_entry(orphan: Session, entry_sessions: list[Session],
                   window_s: float) -> Optional[Session]:
    if orphan.first_epoch is None:
        return None
    best, best_gap = None, window_s
    for e in entry_sessions:
        if e.first_epoch is None:
            continue
        gap = orphan.first_epoch - e.first_epoch
        if -window_s <= gap <= window_s and abs(gap) <= best_gap:
            best, best_gap = e, abs(gap)
    return best


def correlate_conversions(sessions: dict[str, Session], pos_rows,
                          window_s: int = 300) -> None:
    """Mark sessions converted if the visitor was in billing within `window_s`
    seconds before a POS transaction for the store. POS has no customer_id, so
    correlation is purely time-window based, as the spec dictates.

    A transaction converts the most recent still-open billing session whose last
    activity falls inside the window. Each transaction converts at most one session
    to avoid inflating the numerator.
    """
    billing_sessions = sorted(
        (s for s in sessions.values() if s.joined_billing_queue and s.last_epoch),
        key=lambda s: s.last_epoch,
    )
    for tx in pos_rows:
        tx_epoch = tx["ts_epoch"]
        candidate = None
        for s in billing_sessions:
            if tx_epoch - window_s <= s.last_epoch <= tx_epoch and not s.converted:
                candidate = s
        if candidate:
            candidate.converted = True
            candidate.billing_leave_epoch = tx_epoch
            candidate.abandoned_queue = False
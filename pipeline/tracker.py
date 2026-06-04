"""
Tracking + lightweight Re-ID.

The detection layer (detect.py) hands raw per-frame detections to this module. We
keep tracking decoupled from the detector so either can be swapped.

Re-ID strategy (documented in CHOICES.md): primary association is by tracker ID
(ByteTrack when running on real video). Cross-camera dedup and re-entry detection
use a distance-based gallery on the bounding-box trajectory + a coarse appearance
embedding (mean RGB histogram). This is deliberately cheap: a full OSNet model is
overkill for a take-home and harder to defend than a transparent heuristic.

Re-entry rule: if a previously-EXITed identity reappears at the entry threshold
within REENTRY_WINDOW_S, emit REENTRY (and tag the visit suffix #n) rather than a
fresh ENTRY. This is the explicit "re-entry inflation" problem the spec calls out.

Stale-track sweep: active tracks unobserved for STALE_TRACK_S are moved to the
exited gallery automatically. Without this, a person who walked out of the floor
camera's FOV (where there is no entry threshold to trigger mark_exit) would never
be eligible for re-entry matching, and the active map would grow without bound.
"""
from __future__ import annotations

import math
import uuid
from dataclasses import dataclass, field
from typing import Optional

REENTRY_WINDOW_S  = 600          # 10 min: same person stepping out and back
ASSOC_DIST_FRAC   = 0.06         # max centroid jump as fraction of frame width
APPEARANCE_TOL    = 0.25         # histogram distance for cross-cam match
STALE_TRACK_S     = 5.0          # active track unobserved this long -> exited gallery
EXITED_GALLERY_S  = REENTRY_WINDOW_S  # drop exited tracks older than this


def new_visitor_id() -> str:
    return "VIS_" + uuid.uuid4().hex[:12]


@dataclass
class Track:
    visitor_id: str
    physical_id: str
    last_centroid: tuple[float, float]
    last_epoch: float
    appearance: tuple[float, float, float]  # mean RGB histogram proxy
    exited: bool = False
    reentry_count: int = 0
    session_seq: int = 0

    def next_seq(self) -> int:
        self.session_seq += 1
        return self.session_seq


def _dist(a, b) -> float:
    return math.dist(a, b)


def _appearance_dist(a, b) -> float:
    return (abs(a[0] - b[0]) + abs(a[1] - b[1]) + abs(a[2] - b[2])) / 3


class Tracker:
    """Associates detections across frames/cameras into visitor tracks."""

    def __init__(self, frame_width: int = 1920):
        self.active: dict[str, Track] = {}   # visitor_id -> Track
        self.exited: list[Track] = []        # recently exited, for re-entry match
        self._reentries: dict[str, int] = {} # physical_id -> times re-entered
        self._assoc_dist_px = frame_width * ASSOC_DIST_FRAC

        # These can be overridden per camera by detect.py after construction.
        # Rule: same person within reentry_window_s = REENTRY (duplicate).
        #        same person after reentry_window_s  = new unique customer.
        self.reentry_window_s: float = REENTRY_WINDOW_S
        self.exited_gallery_s: float = EXITED_GALLERY_S

    def set_frame_width(self, frame_width: int) -> None:
        """Update association distance when frame width is known."""
        self._assoc_dist_px = frame_width * ASSOC_DIST_FRAC

    def _sweep(self, epoch: float) -> None:
        """Move silently-dropped tracks to the exited gallery; trim stale exits."""
        for vid in list(self.active.keys()):
            t = self.active[vid]
            if epoch - t.last_epoch > STALE_TRACK_S:
                t.exited = True
                self.exited.append(t)
                del self.active[vid]
        # bound the exited gallery so it can't grow forever
        cutoff = epoch - self.exited_gallery_s
        self.exited = [t for t in self.exited if t.last_epoch >= cutoff]

    def observe(self, centroid, appearance, epoch: float,
                at_threshold: bool) -> tuple[str, bool]:
        """Return (visitor_id, is_reentry) for a detection.

        at_threshold indicates the detection is on the entry/exit line, which is
        where new sessions and re-entries are decided.
        """
        self._sweep(epoch)

        # 1) try to continue an active track (same camera, small jump)
        best_id, best_d = None, self._assoc_dist_px
        for vid, t in self.active.items():
            d = _dist(centroid, t.last_centroid)
            if d < best_d and _appearance_dist(appearance, t.appearance) < APPEARANCE_TOL:
                best_id, best_d = vid, d
        if best_id is not None:
            t = self.active[best_id]
            t.last_centroid, t.last_epoch, t.appearance = centroid, epoch, appearance
            return best_id, False

        # 2) at the threshold: decide re-entry vs new session
        if at_threshold:
            # Sort candidates by recency so the most-recently-exited match wins
            # when multiple exited tracks share a similar appearance.
            candidates = sorted(self.exited, key=lambda t: t.last_epoch, reverse=True)
            for t in candidates:
                if (epoch - t.last_epoch <= self.reentry_window_s   # ← instance var
                        and _appearance_dist(appearance, t.appearance) < APPEARANCE_TOL):
                    # increment re-entry count against the stable physical lineage
                    self._reentries[t.physical_id] = self._reentries.get(t.physical_id, 0) + 1
                    n = self._reentries[t.physical_id]
                    vid = f"{t.physical_id}#{n}"
                    rt = Track(visitor_id=vid, physical_id=t.physical_id,
                               last_centroid=centroid, last_epoch=epoch,
                               appearance=appearance)
                    self.active[vid] = rt
                    self.exited.remove(t)
                    return vid, True

        # 3) brand new visitor
        vid = new_visitor_id()
        self.active[vid] = Track(visitor_id=vid, physical_id=vid,
                                 last_centroid=centroid, last_epoch=epoch,
                                 appearance=appearance)
        return vid, False

    def mark_exit(self, visitor_id: str) -> None:
        t = self.active.pop(visitor_id, None)
        if t:
            t.exited = True
            self.exited.append(t)

    def seq_for(self, visitor_id: str) -> int:
        t = self.active.get(visitor_id)
        return t.next_seq() if t else 1
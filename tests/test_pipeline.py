# PROMPT: "Write pytest tests for the tracking and event emission layer.
#          Cover: track association maintains visitor IDs, threshold crossings
#          open/close sessions, re-entries increment physical visitor suffixes,
#          and emitted events are schema-compliant and unique."
#
# CHANGES MADE:
# - Rewrote tests to directly test Tracker and make_event after removing the
#   generate_synthetic code path.
# - Added deterministic UUID5 event-id tests.
# - Added session-sequence uniqueness tests.
# - Added zone-id uniqueness tests.
# - Removed unused Track import.
# - Made exited-track assertion less brittle.

from datetime import datetime, timezone

from app.models import Event
from pipeline.tracker import Tracker, new_visitor_id
from pipeline.emit import make_event


def test_new_visitor_id_format():
    vid = new_visitor_id()

    assert vid.startswith("VIS_")
    assert len(vid) == 10


def test_tracker_observes_new_and_reentry():
    t = Tracker()
    ts = datetime.now().timestamp()

    # 1. First observation (at doorway threshold) -> New visitor
    centroid = (100, 50)
    appearance = (0.5, 0.5, 0.5)

    vid1, is_reentry1 = t.observe(
        centroid,
        appearance,
        ts,
        at_threshold=True,
    )

    assert vid1.startswith("VIS_")
    assert not is_reentry1
    assert len(t.active) == 1

    # 2. Continue track
    centroid2 = (110, 55)

    vid2, is_reentry2 = t.observe(
        centroid2,
        appearance,
        ts + 1.0,
        at_threshold=False,
    )

    assert vid2 == vid1
    assert not is_reentry2

    # 3. Mark exit
    t.mark_exit(vid1)

    assert len(t.active) == 0
    assert any(
        tr.visitor_id == vid1
        for tr in t.exited
    )

    # 4. Re-entry of same physical person
    vid3, is_reentry3 = t.observe(
        (98, 48),
        appearance,
        ts + 10.0,
        at_threshold=True,
    )

    assert vid3 == f"{vid1}#1"
    assert is_reentry3
    assert len(t.active) == 1


def test_tracker_separates_distinct_people():
    t = Tracker()
    ts = datetime.now().timestamp()

    vid1, _ = t.observe(
        (100, 50),
        (0.1, 0.1, 0.1),
        ts,
        at_threshold=True,
    )

    vid2, _ = t.observe(
        (200, 50),
        (0.9, 0.9, 0.9),
        ts,
        at_threshold=True,
    )

    assert vid1 != vid2
    assert len(t.active) == 2


def test_make_event_schema_compliance():
    ts = datetime.now(timezone.utc)

    raw = make_event(
        store_id="STORE_BLR_002",
        camera_id="CAM_ENTRY_01",
        visitor_id="VIS_test",
        event_type="ENTRY",
        ts=ts,
        confidence=0.95,
    )

    e = Event.model_validate(raw)

    assert e.store_id == "STORE_BLR_002"
    assert e.event_type == "ENTRY"
    assert e.confidence == 0.95
    assert not e.is_staff
    assert e.dwell_ms == 0


def test_make_event_low_confidence_retained():
    ts = datetime.now(timezone.utc)

    raw = make_event(
        store_id="STORE_BLR_002",
        camera_id="CAM_ENTRY_01",
        visitor_id="VIS_test",
        event_type="ZONE_ENTER",
        ts=ts,
        confidence=0.342,
        zone_id="SKINCARE",
    )

    assert raw["confidence"] == 0.342
    assert raw["zone_id"] == "SKINCARE"


def test_make_event_staff_compliance():
    ts = datetime.now(timezone.utc)

    raw = make_event(
        store_id="STORE_BLR_002",
        camera_id="CAM_ENTRY_01",
        visitor_id="VIS_test",
        event_type="ENTRY",
        ts=ts,
        confidence=0.95,
        is_staff=True,
    )

    e = Event.model_validate(raw)

    assert e.is_staff


def test_make_event_is_deterministic():
    ts = datetime(
        2030,
        1,
        1,
        tzinfo=timezone.utc,
    )

    e1 = make_event(
        store_id="STORE_BLR_002",
        camera_id="CAM_ENTRY_01",
        visitor_id="VIS_test",
        event_type="ENTRY",
        ts=ts,
        session_seq=1,
    )

    e2 = make_event(
        store_id="STORE_BLR_002",
        camera_id="CAM_ENTRY_01",
        visitor_id="VIS_test",
        event_type="ENTRY",
        ts=ts,
        session_seq=1,
    )

    assert e1["event_id"] == e2["event_id"]


def test_make_event_unique_when_session_changes():
    ts = datetime(
        2030,
        1,
        1,
        tzinfo=timezone.utc,
    )

    e1 = make_event(
        store_id="STORE_BLR_002",
        camera_id="CAM_ENTRY_01",
        visitor_id="VIS_test",
        event_type="ENTRY",
        ts=ts,
        session_seq=1,
    )

    e2 = make_event(
        store_id="STORE_BLR_002",
        camera_id="CAM_ENTRY_01",
        visitor_id="VIS_test",
        event_type="ENTRY",
        ts=ts,
        session_seq=2,
    )

    assert e1["event_id"] != e2["event_id"]


def test_make_event_unique_when_zone_changes():
    ts = datetime(
        2030,
        1,
        1,
        tzinfo=timezone.utc,
    )

    e1 = make_event(
        store_id="STORE_BLR_002",
        camera_id="CAM_ENTRY_01",
        visitor_id="VIS_test",
        event_type="ZONE_ENTER",
        ts=ts,
        zone_id="SKINCARE",
    )

    e2 = make_event(
        store_id="STORE_BLR_002",
        camera_id="CAM_ENTRY_01",
        visitor_id="VIS_test",
        event_type="ZONE_ENTER",
        ts=ts,
        zone_id="FRAGRANCE",
    )

    assert e1["event_id"] != e2["event_id"]


def test_tracker_sweeps_stale_active_tracks_to_exited():
    # Floor/billing cameras never call mark_exit().
    # Stale tracks should be swept into exited.

    t = Tracker()

    ts = 1000.0
    appearance = (0.4, 0.4, 0.4)

    vid1, _ = t.observe(
        (50, 200),
        appearance,
        ts,
        at_threshold=False,
    )

    assert vid1 in t.active

    t.observe(
        (900, 900),
        (0.9, 0.1, 0.1),
        ts + 30.0,
        at_threshold=False,
    )

    assert vid1 not in t.active

    assert any(
        tr.visitor_id == vid1
        for tr in t.exited
    )


def test_processed_clips_storage():
    from app.storage import Store

    s = Store(":memory:")

    assert s.get_clip_status("test_clip.mp4") is None

    s.insert_or_update_clip(
        "test_clip.mp4",
        "PROCESSING",
    )

    assert (
        s.get_clip_status("test_clip.mp4")
        == "PROCESSING"
    )

    s.insert_or_update_clip(
        "test_clip.mp4",
        "COMPLETED",
    )

    assert (
        s.get_clip_status("test_clip.mp4")
        == "COMPLETED"
    )

    clips = s.get_all_clips()

    assert len(clips) == 1
    assert clips[0]["clip_path"] == "test_clip.mp4"
    assert clips[0]["status"] == "COMPLETED"
# PROMPT: "Write pytest unit tests for two pure helpers in a CCTV store-analytics
#   app (app/main.py):
#     (1) parse_clip_info(filename) -> {store, camera}: maps the supplied clips by
#         number (CAM 1/2 -> floor, CAM 3 -> entry, CAM 4 -> backroom, CAM 5 ->
#         billing) and lets an explicit role keyword in the filename override the map;
#     (2) _rebase_pos_rows(rows): uniformly shifts POS timestamps so the earliest
#         transaction lands on RUN_BASE_TS while preserving every relative gap."
#
# CHANGES MADE:
# - Pinned RUN_BASE_TS for deterministic rebasing.
# - Verified inter-transaction gaps are preserved.
# - Verified malformed timestamps pass through unchanged.
# - Added Store 2 mappings.
# - Added case-insensitive filename tests.
# - Added keyword-vs-camera conflict test.
# - Added Store 2 naming variation tests.

import app.main as m


def test_parse_clip_info_camera_roles():
    cases = {
        "CCTV Footage/CAM 1.mp4": "CAM_FLOOR_01",
        "CCTV Footage/CAM 2.mp4": "CAM_FLOOR_02",
        "CCTV Footage/CAM 3.mp4": "CAM_ENTRY_01",
        "CCTV Footage/CAM 4.mp4": "CAM_BACKROOM_01",
        "CCTV Footage/CAM 5.mp4": "CAM_BILL_01",
    }

    for filename, expected_cam in cases.items():
        info = m.parse_clip_info(filename)

        assert info["store"] == "STORE_BLR_002"
        assert info["camera"] == expected_cam


def test_parse_clip_info_keyword_override():
    # Explicit role keywords override camera-number mapping.
    assert (
        m.parse_clip_info("any_ENTRY_feed.mp4")["camera"]
        == "CAM_ENTRY_01"
    )

    assert (
        m.parse_clip_info("billing_counter.mp4")["camera"]
        == "CAM_BILL_01"
    )

    assert (
        m.parse_clip_info("FLOOR_west.mp4")["camera"]
        == "CAM_FLOOR_01"
    )


def test_keyword_beats_camera_number():
    # ENTRY keyword should override CAM 5 -> BILL mapping
    info = m.parse_clip_info("CAM 5 ENTRY.mp4")

    assert info["camera"] == "CAM_ENTRY_01"


def test_parse_clip_info_case_insensitive():
    cases = [
        "ENTRY 1.MP4",
        "Entry 1.mp4",
        "entry 1.mp4",
    ]

    for filename in cases:
        info = m.parse_clip_info(filename)

        assert info["camera"] == "CAM_ENTRY_01"


def test_rebase_pos_anchors_first_txn_and_preserves_gaps():
    m.RUN_BASE_TS = "2030-01-01T00:00:00Z"

    rows = [
        {
            "transaction_id": "T2",
            "store_id": "S",
            "timestamp": "2026-05-30T02:25:00Z",
            "basket_value_inr": "100",
        },
        {
            "transaction_id": "T1",
            "store_id": "S",
            "timestamp": "2026-05-30T02:20:00Z",
            "basket_value_inr": "200",
        },
        {
            "transaction_id": "Tbad",
            "store_id": "S",
            "timestamp": "not-a-time",
            "basket_value_inr": "0",
        },
    ]

    out = {
        r["transaction_id"]: r["timestamp"]
        for r in m._rebase_pos_rows(rows)
    }

    # Earliest valid txn is rebased to RUN_BASE_TS
    assert out["T1"] == "2030-01-01T00:00:00Z"

    # Relative 5-minute gap preserved
    assert out["T2"] == "2030-01-01T00:05:00Z"

    # Invalid timestamp survives untouched
    assert out["Tbad"] == "not-a-time"


def test_rebase_pos_noop_when_no_valid_rows():
    rows = [
        {
            "transaction_id": "X",
            "store_id": "S",
            "timestamp": "bad",
            "basket_value_inr": "1",
        }
    ]

    assert m._rebase_pos_rows(rows) == rows


def test_parse_clip_info_store2_mapping():
    cases = {
        "CCTV Footage/Store 2/billing_area.mp4":
            ("STORE_DEL_004", "CAM_BILL_01"),

        "CCTV Footage/Store 2/entry 1.mp4":
            ("STORE_DEL_004", "CAM_ENTRY_01"),

        "CCTV Footage/Store 2/entry 2.mp4":
            ("STORE_DEL_004", "CAM_ENTRY_02"),

        "CCTV Footage/Store 2/zone.mp4":
            ("STORE_DEL_004", "CAM_FLOOR_01"),

        "CCTV Footage/Store 1/CAM 1 - zone.mp4":
            ("STORE_BLR_002", "CAM_FLOOR_01"),
    }

    for filename, (expected_store, expected_cam) in cases.items():
        info = m.parse_clip_info(filename)

        assert info["store"] == expected_store
        assert info["camera"] == expected_cam


def test_store2_detection_case_variants():
    variants = [
        "CCTV Footage/Store 2/entry 1.mp4",
        "CCTV Footage/STORE 2/entry 1.mp4",
        "CCTV Footage/store 2/entry 1.mp4",
        "CCTV Footage/Store_2/entry 1.mp4",
    ]

    for filename in variants:
        info = m.parse_clip_info(filename)

        assert info["store"] == "STORE_DEL_004"
        assert info["camera"] == "CAM_ENTRY_01"
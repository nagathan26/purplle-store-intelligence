# CHOICES.md — Three Decisions

Each decision lists the options considered, what an LLM suggested, and what I chose
and why.

---

## 1. Detection model

**Options considered**
- **YOLOv8** (Ultralytics) — mature, one-line `model.track()` with built-in ByteTrack,
  excellent person-class accuracy, CPU-tolerable at 1080p/15fps.
- **RT-DETR** — transformer detector, strong accuracy but heavier and slower to set
  up; tracking not bundled.
- **MediaPipe** — light and fast but tuned for close-range pose, weaker on the
  multi-person, partially-occluded retail scenes the spec describes.
- A **VLM** (Claude/GPT-4V) for whole-frame people-counting — flexible but far too
  slow and expensive per frame for 1 hour × 3 cameras × 5 stores, and non-deterministic.

**What AI suggested.** The LLM recommended YOLOv8 + ByteTrack as the default and,
separately, proposed using a **VLM for staff detection** (classify a cropped person
as staff/customer by uniform) rather than a colour heuristic.

**What I chose and why.** **YOLOv8 + ByteTrack** for detection and in-frame tracking —
it is the best accuracy-per-setup-minute option, runs on CPU, and the bundled tracker
removes a whole dependency. On the VLM-for-staff idea I **partially agreed**: a VLM is
genuinely good at "is this person wearing a store uniform," which is hard for a fixed
colour rule under the spec's mixed lighting. I wired `is_staff` as a classification
hook in the detection path and documented the VLM prompt approach
(*"Does the person in this crop wear the store's staff uniform? Answer staff/customer
and a confidence 0-1."*), but **did not make it the default** because it adds latency
and an API dependency to every detected track; the rule/hook is the baseline and the
VLM is the documented upgrade. For the runnable system, staff are labelled at the
event level so the exclusion logic downstream is fully exercised either way.

**Confidence threshold vs. "do not suppress low-conf events."** These sound
contradictory but operate at different layers. I run YOLO with a modest detection
floor (`--conf 0.25`) so the *tracker* is not fed a stream of phantom boxes — at the
default 0.1, the entry camera invented people from glass reflections and corridor
passers-by, wrecking entry counts. But every event that *is* emitted carries its true
confidence, untouched; nothing is rounded up, dropped after the fact, or hidden. So
the analytics layer still sees and can down-weight low-confidence detections — I just
don't manufacture tracks below a sane detector threshold. The floor is a CLI argument,
so it is tunable and easy to defend.

**Known limit & mitigation.** The tracker runs per detector subprocess, one
process per camera, so a person walking from the entry camera into the floor
camera gets two different `visitor_id`s. Sharing a tracker across processes is a
real engineering effort (serialising state, dealing with overlapping appearance
matches, ordering guarantees) and the appearance signal we have (mean RGB) is
too weak to make cross-process re-ID trustworthy on its own. Instead, the API
side applies a deliberately conservative `link_cross_camera_sessions` step in
`app/sessions.py` that folds floor/billing-only sessions into the temporally
nearest entry-cam session within a 3-minute window on the same store. This keeps
the funnel honest without pretending to be doing real Re-ID; the next-iteration
fix is a proper OSNet embedding feeding the same windowed linker.

---

## 2. Event schema design

**Options considered**
- A **flat schema** with every field (including billing/zone specifics) at top level.
- A **typed envelope + `metadata`** split: universal fields top-level, event-specific
  fields in `metadata`.
- A **polymorphic per-type schema** (a different model per event type).

**What AI suggested.** Promote `queue_depth`/`sku_zone`/`session_seq` to top-level
fields for easier querying.

**What I chose and why.** The **envelope + `metadata`** design (matching the spec's
own example). Universal fields — identity, type, timestamp, confidence, `is_staff` —
are top-level because every consumer needs them and they index cleanly. Event-specific
fields live in `metadata` because they are sparse: `queue_depth` is meaningful only on
billing events, `sku_zone` only on zone events. Promoting them, as the LLM suggested,
would make most events carry null columns and couple the table width to every new
event type. I rejected the polymorphic option as over-engineered for this scale — one
validated `Event` model with an enum `event_type` is easier to ingest, dedup, and
test. Confidence is **never suppressed**: low-confidence detections are stored and
flagged, not dropped, because hiding uncertainty is worse than surfacing it.

---

## 3. API architecture — analytics computed on read vs. pre-aggregated

**Options considered**
- **Compute-on-read**: store raw events, reconstruct sessions and metrics per request.
- **Pre-aggregate on ingest**: maintain rolling counters/materialised sessions as
  events arrive, so reads are O(1).
- **Stream processor** (Kafka + Flink/materialised views) — the "real" production
  shape.

**What AI suggested.** Pre-aggregate on ingest for low read latency, and reach for a
stream processor to "do it properly."

**What I chose and why.** **Compute-on-read**, deliberately, for this challenge. It is
simpler, has one source of truth (the event log), and is **always live** — there is no
risk of a counter drifting out of sync with the events, which is exactly the class of
bug that makes a conversion number untrustworthy. Indexes on `(store_id, ts_epoch)`
and `(store_id, event_type)` keep the queries fast at the dataset's scale. I **noted
where this breaks** (and say so in the follow-up answer the spec anticipates): at 40
live stores streaming continuously, recomputing every session on every dashboard poll
becomes the bottleneck. The migration path is not a rewrite — it is to add a rolling
**materialised session table** updated on ingest while keeping the event log as the
system of record, so reads get fast without giving up the single-source-of-truth
property. The full stream-processor option is the right end state at much larger
scale but is unjustified complexity here, so I explicitly did not build it.

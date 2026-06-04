# DESIGN.md — Architecture

## System overview

The system is a four-stage pipeline with a single contract holding it together: the
**event**. Everything upstream of the event (cameras, models, trackers) exists to
produce well-formed events; everything downstream (ingest, analytics, dashboard)
consumes them. This contract is what lets the detection layer be swapped between a
real YOLOv8 video pipeline and a synthetic generator without the API noticing.

```
Detection layer ──emits──▶ Event schema ──POST /ingest──▶ SQLite ──read──▶ Analytics ──▶ Dashboard
   (pipeline/)              (app/models)                  (app/storage)   (app/metrics)   (static/)
```

### 1. Detection layer (`pipeline/`)
Processes CCTV (YOLOv8 + ByteTrack) or generates synthetic traffic, runs both
through a shared `Tracker` for cross-camera dedup and re-entry, and emits events via
`emit.make_event`. The tracker assigns a per-visit `visitor_id` and a stable
*physical identity*; a returning customer gets a `#n`-suffixed id and a `REENTRY`
event, which is how we solve the vendor "re-entry inflation" problem.

**Camera roles from the footage, not the filenames.** The clips arrive named
`CAM 1`–`CAM 5` with no role hint, and the supplied `store_layout.xlsx` was
effectively empty. I sampled one frame from each clip to assign roles by what the
camera actually sees: `CAM 3` is the glass storefront (entry/exit threshold),
`CAM 1`/`CAM 2` are floor shelving (skincare and the makeup wall), `CAM 5` is the
billing counter, and `CAM 4` is the stockroom. The stockroom is mapped to
`CAM_BACKROOM_01`, a role the detector deliberately has *no* event logic for, so it
runs (proving the watcher processes every clip) but emits zero customer events — a
back-office feed should never inflate store traffic. The mapping lives in one place
(`parse_clip_info`) with a keyword override, and the equivalent of the missing layout
is shipped as `store_layout.json`, which the detector reads for `sku_zone` labels.

### 2. Event stream
Plain JSON events matching `app/models.Event`. The schema carries everything the
analytics layer needs: identity (`visitor_id`), spatial context (`zone_id`,
`camera_id`), timing (`timestamp`, `dwell_ms`), trust (`confidence`, `is_staff`), and
typed metadata (`queue_depth`, `session_seq`). Events are batched (≤500) into ingest.

### 3. Intelligence API (`app/`)
FastAPI over SQLite. Ingest validates each record independently, deduplicates on
`event_id` (idempotency), and stores. Analytics are computed **at request time** from
stored events — never cached from a prior day — so the numbers are always live.

The pivotal abstraction is the **session** (`app/sessions.py`). Raw events are noisy
and repetitive (a `ZONE_DWELL` fires every 30s); a *session* collapses a physical
visitor's whole visit into one object with the zones seen, the billing interaction,
and a converted flag. All session-level analytics (unique visitors, funnel,
conversion) operate on sessions keyed by **physical identity**, so re-entries never
double-count.

**Cross-camera linking.** The detection layer runs one detector subprocess per
clip per camera, so two cameras emit independent `visitor_id`s for the same
physical person walking the floor. `link_cross_camera_sessions` folds floor- and
billing-only sessions into the temporally nearest entry-cam session on the same
store within a 3-minute window. This is conservative: it never merges two
entry-cam sessions (so group entries still count as N visitors), it never
crosses the window, and orphans (floor activity with no nearby entry) are kept
rather than discarded. Without this step, the funnel's ZONE_VISIT stage collapses
to zero because entry-cam sessions never observe a zone.

Conversion is the North Star. The POS file has no `customer_id`, so we correlate by
**time window + store**: a session that was in billing within five minutes before a
transaction timestamp is marked converted, and each transaction converts at most one
session to avoid inflating the numerator.

**Timeline alignment.** Two data realities forced an explicit decision. (1) The
clips are a ~2.5-minute historical sample, but `pos_transactions.csv` is a fixed-date
export whose first transaction sits ~17 minutes after the assumed clip start — so a
literal mapping leaves *no* billing session inside any transaction's 5-minute window
and conversion is a meaningless zero. (2) Metrics use a rolling 24-hour "today"
window, so events stamped with the clips' original date fall out of the window
entirely on any later run, and `/metrics` reads all zeros. Both are solved by
anchoring everything to one boot-time base (`RUN_BASE_TS`): the detector stamps
frame 0 at "now", and the POS export is *uniformly* shifted so its earliest
transaction lands on the same anchor (`POS_REBASE_TO_NOW`, on by default; off for
tests). The shift preserves every relative gap, so the conversion numerator stays a
real, input-dependent computation — only the absolute clock is aligned. The result is
clock-independent: a reviewer running `docker compose up` weeks from now still sees
live numbers and non-zero conversion.

### 4. Live dashboard (`app/static/dashboard.html`)
A dependency-free page served at `/` that polls the analytics endpoints every two
seconds. With `pipeline/stream.py` feeding events on a wall clock, conversion, the
funnel, the heatmap, and anomalies update in place.

## Production concerns (Part C)
- **Containerised**: `docker compose up` builds the API; on startup an in-process
  watcher discovers any `.mp4` under `CCTV Footage/` and spawns a detector
  subprocess per clip, which POSTs events to the same API. CPU-only hosts work
  out of the box (no NVIDIA device is required by compose); the detector falls
  back to CPU automatically.
- **Structured logging**: one JSON line per request via middleware with `trace_id`,
  `store_id`, `endpoint`, `latency_ms`, `event_count`, `status_code`.
- **Idempotency**: enforced at the storage boundary (`event_id` PRIMARY KEY,
  `INSERT OR IGNORE`), so replays are reported as duplicates, not errors.
- **Graceful degradation**: a `DatabaseUnavailable` maps to a structured `503`; a
  catch-all handler ensures no stack trace ever reaches a client.
- **Health**: `/health` reports per-store feed lag and raises `STALE_FEED` past the
  configured threshold — the first thing an on-call engineer checks.

## Key trade-offs
- **SQLite, not Postgres.** Zero-dependency, deterministic for tests, fine at the
  challenge's scale. The `Store` interface is deliberately narrow so the engine can
  be swapped without touching analytics. (See CHOICES.md for the scaling limits.)
- **Compute-on-read analytics.** Simpler and always-fresh; the documented first thing
  to break at 40 live stores is recomputing sessions on every request — the fix is a
  rolling materialised session table, not a schema change.
- **Heuristic Re-ID over a heavyweight Re-ID net.** Transparent, defensible, and good
  enough; a full OSNet model is hard to justify and harder to explain for a take-home.

---

## AI-Assisted Decisions

**1. Event schema shape — agreed, with one override.** I asked an LLM to critique a
draft schema. It suggested promoting `queue_depth`, `sku_zone`, and `session_seq`
into typed top-level fields for query convenience. I **overrode** that and kept them
inside `metadata`, because they are event-type-specific (only billing events carry
`queue_depth`) and promoting them litters every event with mostly-null columns. I did
take its suggestion to add `session_seq` for cheap event ordering within a session.

**2. Conversion correlation strategy — agreed, then tightened.** The model proposed
correlating POS to "any visitor in billing within the window." Taken literally that
double-counts when several people queue near one transaction, inflating conversion. I
**accepted the time-window idea but tightened it**: each transaction converts exactly
one session (the most recent eligible, still-unconverted one). This keeps the
numerator honest, which matters because conversion is the North Star.

**3. Session vs raw-event analytics — agreed, and it shaped the architecture.** When
I described the funnel and re-entry requirements, the LLM strongly recommended
introducing an explicit session abstraction rather than computing metrics directly
over event rows. I **agreed**, and it became the backbone of `app/sessions.py`; it is
the single place re-entry deduplication lives, which kept the funnel, metrics, and
heatmap code small and consistent. The one refinement I made over its draft was
computing per-zone dwell as the **max** of the cumulative `dwell_ms` checkpoints
rather than the sum, since summing every 30s `ZONE_DWELL` event massively
over-counts dwell time (a bug the first version actually had — caught in testing).

**4. Cross-camera dedup — overrode the LLM's first instinct.** When the per-camera
tracker created the obvious "entry-cam session has no zones, floor-cam session has
no entry" funnel collapse, the LLM proposed a shared tracker process with an
appearance gallery serialised across runs. I **overrode** that: serialising a
tracker between subprocesses introduces a coupling the rest of the system does
not need, and the appearance heuristic (mean RGB) is far too weak to be the only
signal across cameras with different lighting and angles. I went with a
post-hoc, time-windowed linker in `app/sessions.py` instead — it is honest about
being a heuristic, leaves the events as the system of record, fails *quietly*
(orphan floor sessions are kept, not discarded), and is trivially testable. The
upgrade path is a real Re-ID embedding plus the appearance signal joining on the
same time window, not a different architecture.

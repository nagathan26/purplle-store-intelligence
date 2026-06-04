"""
Storage layer.

Uses SQLite (file-backed, configurable via DB_PATH) for zero-dependency local
runs and deterministic tests. The interface is deliberately narrow so the engine
could be swapped for PostgreSQL without touching the API layer.

Idempotency is enforced at the storage boundary: event_id is the PRIMARY KEY, so
re-ingesting the same payload is a no-op (INSERT OR IGNORE) and reported as a
duplicate rather than an error.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Iterable, Optional

from .models import Event


class DatabaseUnavailable(Exception):
    """Raised when the underlying store cannot be reached. Maps to HTTP 503."""


SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    event_id     TEXT PRIMARY KEY,
    store_id     TEXT NOT NULL,
    camera_id    TEXT NOT NULL,
    visitor_id   TEXT NOT NULL,
    event_type   TEXT NOT NULL,
    ts           TEXT NOT NULL,      -- ISO-8601 UTC
    ts_epoch     REAL NOT NULL,      -- for fast range queries
    zone_id      TEXT,
    dwell_ms     INTEGER NOT NULL DEFAULT 0,
    is_staff     INTEGER NOT NULL DEFAULT 0,
    confidence   REAL NOT NULL,
    metadata     TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_store_ts   ON events(store_id, ts_epoch);
CREATE INDEX IF NOT EXISTS idx_store_type ON events(store_id, event_type);
CREATE INDEX IF NOT EXISTS idx_visitor    ON events(store_id, visitor_id);
CREATE INDEX IF NOT EXISTS idx_store_staff ON events(store_id, is_staff);

CREATE TABLE IF NOT EXISTS pos_transactions (
    transaction_id TEXT NOT NULL,
    store_id       TEXT NOT NULL,
    ts             TEXT NOT NULL,
    ts_epoch       REAL NOT NULL,
    basket_value   REAL NOT NULL,
    PRIMARY KEY (transaction_id, store_id)
);
CREATE INDEX IF NOT EXISTS idx_pos_store_ts ON pos_transactions(store_id, ts_epoch);

CREATE TABLE IF NOT EXISTS pos_details (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id       TEXT NOT NULL,
    store_id       TEXT NOT NULL,
    product_id     TEXT NOT NULL,
    product_name   TEXT NOT NULL,
    brand_name     TEXT NOT NULL,
    dep_name       TEXT NOT NULL,
    qty            INTEGER NOT NULL,
    total_amount   REAL NOT NULL,
    ts             TEXT NOT NULL,
    ts_epoch       REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_pos_details_store_ts ON pos_details(store_id, ts_epoch);
CREATE UNIQUE INDEX IF NOT EXISTS idx_pos_details_unique ON pos_details(order_id, product_id, store_id);

CREATE TABLE IF NOT EXISTS processed_clips (
    clip_path TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    processed_at TEXT NOT NULL
);
"""


def _iso_to_epoch(ts: datetime | str) -> float:
    if isinstance(ts, str):
        ts = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    return ts.timestamp()


class Store:
    def __init__(self, db_path: str = ":memory:"):
        self.db_path = db_path
        self._lock = threading.Lock()
        # check_same_thread=False because FastAPI may serve from a threadpool;
        # we guard all writes with a process-level lock for correctness.
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._healthy = True
        self._init_schema()

    def _init_schema(self) -> None:
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._conn.commit()

    @contextmanager
    def _cursor(self):
        if not self._healthy:
            raise DatabaseUnavailable("storage marked unhealthy")
        try:
            with self._lock:
                cur = self._conn.cursor()
                yield cur
                self._conn.commit()
        except sqlite3.Error as exc:  # pragma: no cover - defensive
            raise DatabaseUnavailable(str(exc)) from exc

    # --- test/ops hooks -------------------------------------------------
    def simulate_outage(self, down: bool = True) -> None:
        """Flip storage health to exercise graceful-degradation paths."""
        self._healthy = not down

    # --- writes ---------------------------------------------------------
    def insert_event(self, e: Event) -> str:
        """Insert one event. Returns 'inserted' or 'duplicate'."""
        with self._cursor() as cur:
            cur.execute(
                """INSERT OR IGNORE INTO events
                   (event_id, store_id, camera_id, visitor_id, event_type, ts,
                    ts_epoch, zone_id, dwell_ms, is_staff, confidence, metadata)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    e.event_id, e.store_id, e.camera_id, e.visitor_id,
                    e.event_type, e.timestamp.isoformat(),
                    _iso_to_epoch(e.timestamp), e.zone_id, e.dwell_ms,
                    int(e.is_staff), e.confidence,
                    json.dumps(e.metadata.model_dump()),
                ),
            )
            return "inserted" if cur.rowcount == 1 else "duplicate"

    def load_pos(self, rows: Iterable[dict]) -> int:
        n = 0
        with self._cursor() as cur:
            for r in rows:
                cur.execute(
                    """INSERT OR IGNORE INTO pos_transactions
                       (transaction_id, store_id, ts, ts_epoch, basket_value)
                       VALUES (?,?,?,?,?)""",
                    (r["transaction_id"], r["store_id"], r["timestamp"],
                     _iso_to_epoch(r["timestamp"]), float(r["basket_value_inr"])),
                )
                n += cur.rowcount
        return n

    def load_pos_details(self, rows: Iterable[dict]) -> int:
        n = 0
        with self._cursor() as cur:
            for r in rows:
                cur.execute(
                    """INSERT OR IGNORE INTO pos_details
                       (order_id, store_id, product_id, product_name, brand_name, dep_name, qty, total_amount, ts, ts_epoch)
                       VALUES (?,?,?,?,?,?,?,?,?,?)""",
                    (
                        r["order_id"],
                        r["store_id"],
                        r["product_id"],
                        r["product_name"],
                        r["brand_name"],
                        r["dep_name"],
                        int(r["qty"]),
                        float(r["total_amount"]),
                        r["timestamp"],
                        _iso_to_epoch(r["timestamp"])
                    ),
                )
                n += cur.rowcount
        return n

    def get_pos_details_stats(self, store_id: str, since_epoch: Optional[float] = None) -> dict:
        q_base = "FROM pos_details WHERE store_id = ?"
        args = [store_id]
        if since_epoch is not None:
            q_base += " AND ts_epoch >= ?"
            args.append(since_epoch)

        with self._cursor() as cur:
            # 1. Total detailed revenue
            cur.execute("SELECT SUM(total_amount) FROM pos_details WHERE store_id = ?" + 
                        ("" if since_epoch is None else " AND ts_epoch >= ?"), list(args))
            rev_row = cur.fetchone()
            total_revenue = rev_row[0] if rev_row and rev_row[0] is not None else 0.0

            # 2. Top Brands
            cur.execute(
                f"""SELECT brand_name, SUM(total_amount) as revenue, SUM(qty) as volume
                   {q_base}
                   GROUP BY brand_name
                   ORDER BY revenue DESC
                   LIMIT 5""",
                list(args)
            )
            top_brands = [
                {"brand": r["brand_name"], "revenue": round(r["revenue"], 2), "qty": r["volume"]}
                for r in cur.fetchall()
            ]

            # 3. Top Categories/Departments
            cur.execute(
                f"""SELECT dep_name, SUM(total_amount) as revenue
                   {q_base}
                   GROUP BY dep_name
                   ORDER BY revenue DESC
                   LIMIT 5""",
                list(args)
            )
            top_categories = [
                {"category": r["dep_name"], "revenue": round(r["revenue"], 2)}
                for r in cur.fetchall()
            ]

            # 4. Top Products
            cur.execute(
                f"""SELECT product_name, SUM(total_amount) as revenue, SUM(qty) as volume
                   {q_base}
                   GROUP BY product_name
                   ORDER BY revenue DESC
                   LIMIT 5""",
                list(args)
            )
            top_products = [
                {"product": r["product_name"], "revenue": round(r["revenue"], 2), "qty": r["volume"]}
                for r in cur.fetchall()
            ]

            # 5. Average basket value from details
            cur.execute(
                f"""SELECT COUNT(DISTINCT order_id) as txn_count, SUM(qty) as total_qty
                   {q_base}""",
                list(args)
            )
            basket_row = cur.fetchone()
            txn_count = basket_row["txn_count"] if basket_row else 0
            total_qty = basket_row["total_qty"] if basket_row else 0
            avg_items_per_basket = round(total_qty / txn_count, 1) if txn_count else 0.0

            return {
                "total_revenue": round(total_revenue, 2),
                "top_brands": top_brands,
                "top_categories": top_categories,
                "top_products": top_products,
                "avg_items_per_basket": avg_items_per_basket,
                "detailed_txn_count": txn_count
            }

    # --- reads ----------------------------------------------------------
    def events_for(self, store_id: str, since_epoch: Optional[float] = None,
                   include_staff: bool = False) -> list[sqlite3.Row]:
        q = "SELECT * FROM events WHERE store_id = ?"
        args: list = [store_id]
        if since_epoch is not None:
            q += " AND ts_epoch >= ?"
            args.append(since_epoch)
        if not include_staff:
            q += " AND is_staff = 0"
        q += " ORDER BY ts_epoch ASC"
        with self._cursor() as cur:
            cur.execute(q, args)
            return cur.fetchall()

    def pos_for(self, store_id: str, since_epoch: Optional[float] = None) -> list[sqlite3.Row]:
        q = "SELECT * FROM pos_transactions WHERE store_id = ?"
        args: list = [store_id]
        if since_epoch is not None:
            q += " AND ts_epoch >= ?"
            args.append(since_epoch)
        q += " ORDER BY ts_epoch ASC"
        with self._cursor() as cur:
            cur.execute(q, args)
            return cur.fetchall()

    def last_event_per_store(self) -> dict[str, str]:
        with self._cursor() as cur:
            cur.execute(
                "SELECT store_id, MAX(ts) AS last_ts FROM events GROUP BY store_id"
            )
            return {row["store_id"]: row["last_ts"] for row in cur.fetchall()}

    def known_stores(self) -> list[str]:
        with self._cursor() as cur:
            cur.execute(
                """SELECT store_id FROM events
                   UNION
                   SELECT store_id FROM pos_transactions
                   UNION
                   SELECT store_id FROM pos_details
                   ORDER BY store_id"""
            )
            return [r["store_id"] for r in cur.fetchall()]

    def insert_or_update_clip(self, clip_path: str, status: str) -> None:
        with self._cursor() as cur:
            now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            cur.execute(
                """INSERT INTO processed_clips (clip_path, status, processed_at)
                   VALUES (?, ?, ?)
                   ON CONFLICT(clip_path) DO UPDATE SET status = ?, processed_at = ?""",
                (clip_path, status, now_iso, status, now_iso),
            )

    def get_clip_status(self, clip_path: str) -> Optional[str]:
        with self._cursor() as cur:
            cur.execute("SELECT status FROM processed_clips WHERE clip_path = ?", (clip_path,))
            row = cur.fetchone()
            return row["status"] if row else None

    def get_all_clips(self) -> list[dict]:
        with self._cursor() as cur:
            cur.execute("SELECT clip_path, status, processed_at FROM processed_clips ORDER BY processed_at DESC")
            return [
                {"clip_path": r["clip_path"], "status": r["status"], "processed_at": r["processed_at"]}
                for r in cur.fetchall()
            ]
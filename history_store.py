"""Локальное накопление истории продаж.

eAPI отдаёт только 200 последних продаж на предмет, а параметр offset игнорируется.
Чтобы средняя действительно считалась за несколько дней, приложение сохраняет каждую
полученную продажу в SQLite и постепенно накапливает полное окно.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from scanner import _weighted_quantile, parse_dt

SCHEMA = """
CREATE TABLE IF NOT EXISTS sales (
    item       TEXT NOT NULL,
    sold_at    TEXT NOT NULL,
    price      INTEGER NOT NULL,
    amount     INTEGER NOT NULL,
    unit       REAL NOT NULL,
    qlt        INTEGER,
    ptn        INTEGER,
    additional TEXT NOT NULL,
    sig        TEXT NOT NULL,
    fetched_at REAL NOT NULL,
    PRIMARY KEY (item, sig)
);
CREATE INDEX IF NOT EXISTS idx_sales_item_time ON sales(item, sold_at);
CREATE INDEX IF NOT EXISTS idx_sales_time ON sales(sold_at);
"""

_UNSET = object()


class HistoryStore:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False, timeout=30)
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._conn.commit()

    # ------------------------------------------------------------------ запись

    def add_sales(self, item: str, rows) -> int:
        """Сохраняет продажи, возвращает число новых записей."""
        inserted = 0
        now = time.time()
        with self._lock:
            cursor = self._conn.cursor()
            for row in rows or []:
                ts = parse_dt(row.get("time"))
                amount = int(row.get("amount") or 0)
                price = int(row.get("price") or 0)
                if ts is None or amount <= 0 or price <= 0:
                    continue
                additional = row.get("additional")
                if not isinstance(additional, dict):
                    additional = {}
                sold_at = ts.isoformat()
                payload = json.dumps(
                    {"t": sold_at, "p": price, "a": amount, "x": additional},
                    sort_keys=True,
                    ensure_ascii=False,
                )
                sig = hashlib.sha1(payload.encode("utf-8")).hexdigest()
                qlt = additional.get("qlt")
                ptn = additional.get("ptn")
                cursor.execute(
                    "INSERT OR IGNORE INTO sales "
                    "(item, sold_at, price, amount, unit, qlt, ptn, additional, sig, fetched_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        item,
                        sold_at,
                        price,
                        amount,
                        price / amount,
                        int(qlt) if isinstance(qlt, (int, float)) else None,
                        int(ptn) if isinstance(ptn, (int, float)) else None,
                        json.dumps(additional, ensure_ascii=False, sort_keys=True),
                        sig,
                        now,
                    ),
                )
                inserted += cursor.rowcount
            self._conn.commit()
        return inserted

    # ------------------------------------------------------------------ чтение

    def get_sales(self, item: str, cutoff: datetime) -> list[dict]:
        with self._lock:
            cursor = self._conn.execute(
                "SELECT sold_at, price, amount, additional FROM sales "
                "WHERE item = ? AND sold_at >= ? ORDER BY sold_at ASC",
                (item, cutoff.isoformat()),
            )
            rows = cursor.fetchall()
        result = []
        for sold_at, price, amount, additional in rows:
            try:
                extra = json.loads(additional) if additional else {}
            except ValueError:
                extra = {}
            result.append(
                {
                    "time": sold_at,
                    "price": price,
                    "amount": amount,
                    "additional": extra,
                }
            )
        return result

    def variants(self, item: str, cutoff: datetime) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT qlt, ptn, COUNT(*), COALESCE(SUM(amount), 0), MIN(sold_at), MAX(sold_at) "
                "FROM sales WHERE item = ? AND sold_at >= ? "
                "GROUP BY qlt, ptn ORDER BY SUM(amount) DESC",
                (item, cutoff.isoformat()),
            ).fetchall()
        return [
            {
                "qlt": int(qlt) if qlt is not None else None,
                "ptn": int(ptn) if ptn is not None else None,
                "count": int(count),
                "units": int(units),
                "oldest": oldest,
                "newest": newest,
            }
            for qlt, ptn, count, units, oldest, newest in rows
        ]

    def series(
        self,
        item: str,
        cutoff: datetime,
        qlt=_UNSET,
        ptn=_UNSET,
    ) -> list[dict]:
        clauses = ["item = ?", "sold_at >= ?"]
        params: list = [item, cutoff.isoformat()]
        if qlt is not _UNSET:
            if qlt is None:
                clauses.append("qlt IS NULL")
            else:
                clauses.append("qlt = ?")
                params.append(int(qlt))
        if ptn is not _UNSET:
            if ptn is None:
                clauses.append("ptn IS NULL")
            else:
                clauses.append("ptn = ?")
                params.append(int(ptn))
        with self._lock:
            rows = self._conn.execute(
                f"SELECT sold_at, unit, amount FROM sales WHERE {' AND '.join(clauses)} ORDER BY sold_at",
                params,
            ).fetchall()
        buckets: dict[str, list[tuple[float, float]]] = {}
        for sold_at, unit, amount in rows:
            buckets.setdefault(str(sold_at)[:10], []).append((float(unit), float(amount)))
        result = []
        for day in sorted(buckets):
            pairs = buckets[day]
            units = sum(weight for _, weight in pairs)
            prices = [value for value, _ in pairs]
            result.append(
                {
                    "date": day,
                    "count": len(pairs),
                    "units": int(round(units)),
                    "median": round(_weighted_quantile(pairs, 0.5), 2),
                    "p25": round(_weighted_quantile(pairs, 0.25), 2),
                    "p75": round(_weighted_quantile(pairs, 0.75), 2),
                    "min": round(min(prices), 2),
                    "max": round(max(prices), 2),
                }
            )
        return result

    def recent_sales(
        self,
        item: str,
        cutoff: datetime,
        limit: int = 30,
        qlt=_UNSET,
        ptn=_UNSET,
    ) -> list[dict]:
        clauses = ["item = ?", "sold_at >= ?"]
        params: list = [item, cutoff.isoformat()]
        if qlt is not _UNSET:
            if qlt is None:
                clauses.append("qlt IS NULL")
            else:
                clauses.append("qlt = ?")
                params.append(int(qlt))
        if ptn is not _UNSET:
            if ptn is None:
                clauses.append("ptn IS NULL")
            else:
                clauses.append("ptn = ?")
                params.append(int(ptn))
        params.append(max(1, min(200, int(limit))))
        with self._lock:
            rows = self._conn.execute(
                f"SELECT sold_at, price, amount, unit, qlt, ptn FROM sales "
                f"WHERE {' AND '.join(clauses)} ORDER BY sold_at DESC LIMIT ?",
                params,
            ).fetchall()
        return [
            {
                "time": sold_at,
                "price": int(price),
                "amount": int(amount),
                "unit": round(float(unit), 2),
                "qlt": int(qlt) if qlt is not None else None,
                "ptn": int(ptn) if ptn is not None else None,
            }
            for sold_at, price, amount, unit, qlt, ptn in rows
        ]

    def summary(self, item: str, cutoff: datetime, qlt=_UNSET, ptn=_UNSET) -> dict | None:
        clauses = ["item = ?", "sold_at >= ?"]
        params: list = [item, cutoff.isoformat()]
        if qlt is not _UNSET:
            if qlt is None:
                clauses.append("qlt IS NULL")
            else:
                clauses.append("qlt = ?")
                params.append(int(qlt))
        if ptn is not _UNSET:
            if ptn is None:
                clauses.append("ptn IS NULL")
            else:
                clauses.append("ptn = ?")
                params.append(int(ptn))
        with self._lock:
            rows = self._conn.execute(
                f"SELECT sold_at, unit, amount FROM sales WHERE {' AND '.join(clauses)}",
                params,
            ).fetchall()
        if not rows:
            return None
        pairs = [(float(unit), float(amount)) for _, unit, amount in rows if amount > 0]
        if not pairs:
            return None
        volume = sum(weight for _, weight in pairs)
        prices = [value for value, _ in pairs]
        times = [parse_dt(sold_at) for sold_at, _, _ in rows]
        times = [stamp for stamp in times if stamp is not None]
        return {
            "volume": int(round(volume)),
            "sales_count": len(rows),
            "mean": round(sum(value * weight for value, weight in pairs) / volume, 2),
            "median": round(_weighted_quantile(pairs, 0.5), 2),
            "p25": round(_weighted_quantile(pairs, 0.25), 2),
            "p75": round(_weighted_quantile(pairs, 0.75), 2),
            "min": round(min(prices), 2),
            "max": round(max(prices), 2),
            "last_sale": max(times).isoformat() if times else None,
        }

    def coverage(self, item: str, cutoff: datetime) -> dict:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*), COALESCE(SUM(amount), 0), MIN(sold_at), MAX(sold_at) "
                "FROM sales WHERE item = ? AND sold_at >= ?",
                (item, cutoff.isoformat()),
            ).fetchone()
        count, units, oldest, newest = row
        span = 0.0
        if oldest and newest:
            start = parse_dt(oldest)
            end = parse_dt(newest)
            if start and end:
                span = max(0.0, (end - start).total_seconds() / 3600.0)
        return {
            "records": int(count),
            "units": int(units),
            "oldest": oldest,
            "newest": newest,
            "span_hours": round(span, 1),
        }

    def total(self, item: str | None = None) -> int:
        with self._lock:
            if item:
                row = self._conn.execute("SELECT COUNT(*) FROM sales WHERE item = ?", (item,)).fetchone()
            else:
                row = self._conn.execute("SELECT COUNT(*) FROM sales").fetchone()
        return int(row[0] if row else 0)

    # ------------------------------------------------------------------ обслуга

    def prune(self, keep_days: int = 14) -> int:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=max(2, keep_days))).isoformat()
        with self._lock:
            cursor = self._conn.execute("DELETE FROM sales WHERE sold_at < ?", (cutoff,))
            self._conn.commit()
        return cursor.rowcount

    def vacuum(self) -> None:
        """Сжимает файл после массового удаления, чтобы база не росла вечно."""
        with self._lock:
            self._conn.execute("VACUUM")

    def close(self) -> None:
        with self._lock:
            try:
                self._conn.close()
            except sqlite3.Error:
                pass

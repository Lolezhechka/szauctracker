"""Локальный журнал жизненного цикла лотов.

API не отдаёт lotId, поэтому лот идентифицируется стабильной подписью
(предмет, количество, endTime, качество, паттерн, бонусы, spawn_time).
Подпись не включает цену: изменение цены — это событие price у того же лота.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS lots (
    item       TEXT NOT NULL,
    sig        TEXT NOT NULL,
    first_seen REAL NOT NULL,
    last_seen  REAL NOT NULL,
    amount     INTEGER NOT NULL,
    unit       REAL NOT NULL,
    buyout     INTEGER NOT NULL,
    end_ts     REAL,
    qlt        INTEGER,
    ptn        INTEGER,
    additional TEXT NOT NULL,
    status     TEXT NOT NULL,
    closed_at  REAL,
    PRIMARY KEY (item, sig)
);
CREATE INDEX IF NOT EXISTS idx_lots_item_status ON lots(item, status);
CREATE INDEX IF NOT EXISTS idx_lots_last_seen ON lots(last_seen);

CREATE TABLE IF NOT EXISTS events (
    id     INTEGER PRIMARY KEY AUTOINCREMENT,
    item   TEXT NOT NULL,
    sig    TEXT,
    event  TEXT NOT NULL,
    at     REAL NOT NULL,
    amount INTEGER,
    unit   REAL,
    qlt    INTEGER,
    ptn    INTEGER,
    detail TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_item_at ON events(item, at);
CREATE INDEX IF NOT EXISTS idx_events_at ON events(at);
"""


def _sig_base(item: str, lot: dict) -> str:
    additional = lot.get("additional") if isinstance(lot.get("additional"), dict) else {}
    payload = {
        "item": item,
        "amount": int(lot.get("amount") or 0),
        "end": str(lot.get("endTime") or ""),
        "qlt": additional.get("qlt"),
        "ptn": additional.get("ptn"),
        "bp": additional.get("bonus_properties"),
        "sp": additional.get("spawn_time"),
    }
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def _unit(lot: dict) -> float:
    amount = int(lot.get("amount") or 0)
    buyout = int(lot.get("buyoutPrice") or 0)
    return (buyout / amount) if amount > 0 and buyout > 0 else 0.0


def _iso_ts(value) -> float | None:
    if not value:
        return None
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def _sale_ts(row: dict) -> float | None:
    return _iso_ts(row.get("time"))


class MarketStore:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False, timeout=30)
        with self._lock:
            self._conn.executescript(SCHEMA)
            try:
                self._conn.execute("PRAGMA journal_mode=WAL")
                self._conn.execute("PRAGMA synchronous=NORMAL")
            except sqlite3.Error:
                pass
            self._conn.commit()

    # ------------------------------------------------------------------ запись

    def observe(self, item: str, lots: list[dict], now: datetime | float | None = None,
                complete: bool = True, sales: list[dict] | None = None) -> dict:
        """Сохраняет снимок лотов и переходы appeared/price/probable_sold/expired/missing."""
        now_ts = now.timestamp() if isinstance(now, datetime) else (float(now) if now else time.time())
        current: dict[str, dict] = {}
        duplicate: dict[str, int] = {}
        for lot in lots or []:
            buyout = int(lot.get("buyoutPrice") or 0)
            amount = int(lot.get("amount") or 0)
            unit = _unit(lot)
            if buyout <= 0 or amount <= 0 or unit <= 0:
                continue
            base = _sig_base(item, lot)
            count = duplicate.get(base, 0)
            duplicate[base] = count + 1
            sig = base if count == 0 else f"{base}:{count + 1}"
            additional = lot.get("additional") if isinstance(lot.get("additional"), dict) else {}
            current[sig] = {
                "sig": sig,
                "amount": amount,
                "unit": round(unit, 2),
                "buyout": buyout,
                "end_ts": _iso_ts(lot.get("endTime")),
                "qlt": additional.get("qlt") if isinstance(additional.get("qlt"), (int, float)) else None,
                "ptn": additional.get("ptn") if isinstance(additional.get("ptn"), (int, float)) else None,
                "additional": json.dumps(additional, ensure_ascii=False, sort_keys=True),
            }

        counts = {"appeared": 0, "price": 0, "probable_sold": 0, "expired": 0, "missing": 0, "reappeared": 0}
        with self._lock:
            rows = self._conn.execute(
                "SELECT sig, status, unit, buyout, amount, last_seen, first_seen, end_ts "
                "FROM lots WHERE item = ?",
                (item,),
            ).fetchall()
            existing = {row[0]: row for row in rows}
            for sig, row in current.items():
                old = existing.get(sig)
                if old is None:
                    self._conn.execute(
                        "INSERT INTO lots (item, sig, first_seen, last_seen, amount, unit, buyout, "
                        "end_ts, qlt, ptn, additional, status, closed_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', NULL)",
                        (
                            item, sig, now_ts, now_ts, row["amount"], row["unit"], row["buyout"],
                            row["end_ts"], row["qlt"], row["ptn"], row["additional"],
                        ),
                    )
                    self._event(item, sig, "appeared", now_ts, row)
                    counts["appeared"] += 1
                else:
                    status = old[1]
                    old_unit = float(old[2] or 0)
                    if status != "active":
                        self._conn.execute(
                            "UPDATE lots SET status='active', closed_at=NULL, last_seen=?, amount=?, "
                            "unit=?, buyout=?, end_ts=?, qlt=?, ptn=?, additional=? "
                            "WHERE item=? AND sig=?",
                            (
                                now_ts, row["amount"], row["unit"], row["buyout"], row["end_ts"],
                                row["qlt"], row["ptn"], row["additional"], item, sig,
                            ),
                        )
                        self._event(item, sig, "reappeared", now_ts, row)
                        counts["reappeared"] += 1
                    else:
                        self._conn.execute(
                            "UPDATE lots SET last_seen=?, amount=?, unit=?, buyout=?, end_ts=?, "
                            "qlt=?, ptn=?, additional=? WHERE item=? AND sig=?",
                            (
                                now_ts, row["amount"], row["unit"], row["buyout"], row["end_ts"],
                                row["qlt"], row["ptn"], row["additional"], item, sig,
                            ),
                        )
                        if old_unit > 0 and abs(old_unit - row["unit"]) > max(0.01, old_unit * 0.005):
                            self._event(
                                item, sig, "price", now_ts, row,
                                detail={"old": round(old_unit, 2), "new": row["unit"]},
                            )
                            counts["price"] += 1

            if complete:
                used_sales: set[int] = set()
                sale_rows = sales or []
                for sig, old in existing.items():
                    if sig in current or old[1] != "active":
                        continue
                    row = {
                        "sig": sig,
                        "amount": int(old[4] or 0),
                        "unit": float(old[2] or 0),
                    }
                    match = self._match_sale(row, sale_rows, used_sales, now_ts, float(old[6] or now_ts))
                    end_ts = old[7]
                    if match is not None:
                        status = "probable_sold"
                        detail = {"sale_time": match.get("time"), "sale_price": match.get("price")}
                        counts["probable_sold"] += 1
                    elif end_ts is not None and end_ts <= now_ts:
                        status = "expired"
                        detail = {"end_time": datetime.fromtimestamp(end_ts, timezone.utc).isoformat()}
                        counts["expired"] += 1
                    else:
                        status = "missing"
                        detail = {}
                        counts["missing"] += 1
                    self._conn.execute(
                        "UPDATE lots SET status=?, closed_at=? WHERE item=? AND sig=?",
                        (status, now_ts, item, sig),
                    )
                    self._event(item, sig, status, now_ts, row, detail=detail)
            self._conn.commit()
        return counts

    def _match_sale(self, lot: dict, sales: list[dict], used: set[int], now_ts: float,
                    first_seen: float) -> dict | None:
        amount = int(lot.get("amount") or 0)
        unit = float(lot.get("unit") or 0)
        for index, sale in enumerate(sales):
            if index in used:
                continue
            if int(sale.get("amount") or 0) != amount:
                continue
            sale_unit = float(sale.get("price") or 0) / amount if amount else 0.0
            if sale_unit <= 0 or abs(sale_unit - unit) > max(0.01, unit * 0.02):
                continue
            ts = _sale_ts(sale)
            if ts is None or ts < first_seen - 300 or ts > now_ts + 300:
                continue
            used.add(index)
            return sale
        return None

    def _event(self, item: str, sig: str, event: str, at: float, row: dict, detail: dict | None = None) -> None:
        self._conn.execute(
            "INSERT INTO events (item, sig, event, at, amount, unit, qlt, ptn, detail) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                item, sig, event, at, row.get("amount"), row.get("unit"),
                row.get("qlt"), row.get("ptn"),
                json.dumps(detail or {}, ensure_ascii=False, sort_keys=True),
            ),
        )

    # ------------------------------------------------------------------ чтение

    def summary(self, item: str, days: float = 7) -> dict:
        days = max(0.25, min(90.0, float(days)))
        cutoff = time.time() - days * 86400
        with self._lock:
            active = self._conn.execute(
                "SELECT amount, unit FROM lots WHERE item=? AND status='active' AND last_seen>=?",
                (item, cutoff),
            ).fetchall()
            events = self._conn.execute(
                "SELECT event, COUNT(*) FROM events WHERE item=? AND at>=? GROUP BY event",
                (item, cutoff),
            ).fetchall()
            recent = self._conn.execute(
                "SELECT at, event, amount, unit, qlt, ptn, detail FROM events "
                "WHERE item=? AND at>=? ORDER BY at DESC LIMIT 150",
                (item, cutoff),
            ).fetchall()
            closed = self._conn.execute(
                "SELECT first_seen, closed_at, status FROM lots "
                "WHERE item=? AND status!='active' AND closed_at>=?",
                (item, cutoff),
            ).fetchall()
            daily = self._conn.execute(
                "SELECT date(at, 'unixepoch') AS day, event, COUNT(*) FROM events "
                "WHERE item=? AND at>=? GROUP BY day, event ORDER BY day",
                (item, cutoff),
            ).fetchall()

        units = sum(int(row[0] or 0) for row in active)
        prices = sorted(float(row[1]) for row in active if row[1])
        median = prices[len(prices) // 2] if prices else None
        if len(prices) % 2 == 0 and prices:
            median = (prices[len(prices) // 2 - 1] + prices[len(prices) // 2]) / 2
        counts = {event: int(count) for event, count in events}
        sold = counts.get("probable_sold", 0)
        expired = counts.get("expired", 0)
        missing = counts.get("missing", 0)
        disappeared = sold + expired + missing
        lifetimes = [
            (float(closed_at) - float(first_seen)) / 3600.0
            for first_seen, closed_at, _status in closed
            if closed_at and first_seen and closed_at > first_seen
        ]
        series: dict[str, dict] = {}
        for day, event, count in daily:
            row = series.setdefault(str(day), {"date": str(day)})
            row[event] = int(count)
        return {
            "item": item,
            "days": days,
            "supply_lots": len(active),
            "supply_units": units,
            "median_ask": round(median, 2) if median else None,
            "events": counts,
            "disappeared": disappeared,
            "probable_sold_pct": round(100.0 * sold / disappeared, 1) if disappeared else None,
            "avg_lifetime_hours": round(sum(lifetimes) / len(lifetimes), 2) if lifetimes else None,
            "closed": len(closed),
            "series": [series[key] for key in sorted(series)],
            "recent": [
                {
                    "at": datetime.fromtimestamp(float(at), timezone.utc).isoformat(),
                    "event": event,
                    "amount": int(amount or 0),
                    "unit": round(float(unit or 0), 2),
                    "qlt": int(qlt) if qlt is not None else None,
                    "ptn": int(ptn) if ptn is not None else None,
                    "detail": json.loads(detail) if detail else {},
                }
                for at, event, amount, unit, qlt, ptn, detail in recent
            ],
        }

    def items(self) -> list[str]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT item FROM lots GROUP BY item ORDER BY MAX(last_seen) DESC"
            ).fetchall()
        return [row[0] for row in rows]

    def total(self, item: str | None = None) -> int:
        with self._lock:
            if item:
                row = self._conn.execute("SELECT COUNT(*) FROM lots WHERE item=?", (item,)).fetchone()
            else:
                row = self._conn.execute("SELECT COUNT(*) FROM lots").fetchone()
        return int(row[0] if row else 0)

    # ------------------------------------------------------------------ обслуга

    def prune(self, keep_days: int = 30) -> int:
        cutoff = time.time() - max(2, int(keep_days)) * 86400
        with self._lock:
            events = self._conn.execute("DELETE FROM events WHERE at < ?", (cutoff,)).rowcount
            lots = self._conn.execute(
                "DELETE FROM lots WHERE (status!='active' AND closed_at < ?) OR last_seen < ?",
                (cutoff, cutoff),
            ).rowcount
            self._conn.commit()
        return int(events + lots)

    def checkpoint(self) -> None:
        with self._lock:
            try:
                self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                self._conn.commit()
            except sqlite3.Error:
                pass

    def backup_to(self, path: Path | str) -> None:
        with self._lock:
            self.checkpoint()
            target = sqlite3.connect(str(path))
            try:
                self._conn.backup(target)
                target.commit()
            finally:
                target.close()

    def close(self) -> None:
        with self._lock:
            try:
                self.checkpoint()
                self._conn.close()
            except sqlite3.Error:
                pass

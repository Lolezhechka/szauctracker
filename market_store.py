"""Локальный журнал жизненного цикла лотов.

API не отдаёт lotId, поэтому лот идентифицируется стабильной подписью
(предмет, количество, endTime, качество, паттерн, бонусы, spawn_time).
Подпись не включает цену: изменение цены — это событие price у того же лота.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from lot_utils import lot_signature

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

CREATE TABLE IF NOT EXISTS signals (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    item         TEXT NOT NULL,
    sig          TEXT NOT NULL,
    first_at     REAL NOT NULL,
    last_at      REAL NOT NULL,
    name         TEXT NOT NULL,
    amount       INTEGER NOT NULL,
    unit         REAL NOT NULL,
    benchmark    REAL,
    discount_pct REAL,
    score        REAL,
    source       TEXT,
    end_ts       REAL,
    total_net    REAL,
    UNIQUE(item, sig)
);
CREATE INDEX IF NOT EXISTS idx_signals_first ON signals(first_at);
CREATE INDEX IF NOT EXISTS idx_signals_item ON signals(item);
"""


def _sig_base(item: str, lot: dict) -> str:
    return lot_signature(item, lot)


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

    def record_signals(self, deals: list[dict], now: datetime | float | None = None) -> int:
        """Сохраняет рекомендации по конкретным лотам для последующей проверки."""
        now_ts = now.timestamp() if isinstance(now, datetime) else (float(now) if now else time.time())
        inserted = 0
        with self._lock:
            for deal in deals or []:
                item = str(deal.get("item_id") or "")
                if not item:
                    continue
                for lot in deal.get("signal_lots") or []:
                    sig = str(lot.get("sig") or "")
                    if not sig:
                        continue
                    amount = int(lot.get("amount") or 0)
                    unit = float(lot.get("unit") or 0)
                    if amount <= 0 or unit <= 0:
                        continue
                    end_ts = _iso_ts(lot.get("end_time"))
                    cursor = self._conn.execute(
                        "INSERT OR IGNORE INTO signals "
                        "(item, sig, first_at, last_at, name, amount, unit, benchmark, "
                        "discount_pct, score, source, end_ts, total_net) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            item, sig, now_ts, now_ts, str(deal.get("name") or item),
                            amount, unit, deal.get("benchmark"), deal.get("discount_pct"),
                            deal.get("score"), deal.get("source"), end_ts,
                            lot.get("net", deal.get("total_net")),
                        ),
                    )
                    inserted += cursor.rowcount
                    self._conn.execute(
                        "UPDATE signals SET last_at=?, name=?, benchmark=?, "
                        "discount_pct=MAX(COALESCE(discount_pct, 0), ?), "
                        "score=MAX(COALESCE(score, 0), ?), source=?, total_net=MAX(COALESCE(total_net, 0), ?) "
                        "WHERE item=? AND sig=?",
                        (
                            now_ts, str(deal.get("name") or item), deal.get("benchmark"),
                            deal.get("discount_pct") or 0, deal.get("score") or 0,
                            deal.get("source"), lot.get("net", deal.get("total_net")) or 0, item, sig,
                        ),
                    )
            self._conn.commit()
        return inserted

    # ------------------------------------------------------------------ чтение

    def signals(self, days: float = 7) -> dict:
        days = max(0.25, min(90.0, float(days)))
        cutoff = time.time() - days * 86400
        with self._lock:
            rows = self._conn.execute(
                "SELECT s.item, s.sig, s.first_at, s.last_at, s.name, s.amount, s.unit, "
                "s.benchmark, s.discount_pct, s.score, s.source, s.end_ts, s.total_net, "
                "l.status, l.closed_at "
                "FROM signals s LEFT JOIN lots l ON s.item=l.item AND s.sig=l.sig "
                "WHERE s.first_at>=? ORDER BY s.first_at DESC LIMIT 500",
                (cutoff,),
            ).fetchall()
        counts = {"active": 0, "sold": 0, "closed": 0, "unknown": 0}
        sold_profit = 0.0
        active_profit = 0.0
        items = []
        for row in rows:
            (item, sig, first_at, last_at, name, amount, unit, benchmark,
             discount, score, source, end_ts, total_net, lot_status, closed_at) = row
            if lot_status == "probable_sold":
                outcome = "sold"
                sold_profit += float(total_net or 0)
            elif lot_status in ("expired", "missing"):
                outcome = "closed"
            elif lot_status == "active":
                outcome = "active"
                active_profit += float(total_net or 0)
            else:
                outcome = "unknown"
            counts[outcome] += 1
            items.append(
                {
                    "item": item,
                    "sig": sig,
                    "name": name,
                    "first_at": datetime.fromtimestamp(float(first_at), timezone.utc).isoformat(),
                    "last_at": datetime.fromtimestamp(float(last_at), timezone.utc).isoformat(),
                    "amount": int(amount or 0),
                    "unit": round(float(unit or 0), 2),
                    "benchmark": round(float(benchmark), 2) if benchmark else None,
                    "discount_pct": round(float(discount), 1) if discount is not None else None,
                    "score": round(float(score), 1) if score is not None else None,
                    "source": source,
                    "total_net": round(float(total_net or 0), 2),
                    "outcome": outcome,
                    "closed_at": (
                        datetime.fromtimestamp(float(closed_at), timezone.utc).isoformat()
                        if closed_at else None
                    ),
                }
            )
        closed = counts["sold"] + counts["closed"]
        return {
            "days": days,
            "total": len(rows),
            "counts": counts,
            "hit_rate_pct": round(100.0 * counts["sold"] / closed, 1) if closed else None,
            "sold_profit": round(sold_profit, 2),
            "active_profit": round(active_profit, 2),
            "signals": items,
        }

    def overview(self, days: float = 7) -> list[dict]:
        days = max(0.25, min(90.0, float(days)))
        cutoff = time.time() - days * 86400
        with self._lock:
            active = self._conn.execute(
                "SELECT item, amount, unit FROM lots WHERE status='active' AND last_seen>=?",
                (cutoff,),
            ).fetchall()
            events = self._conn.execute(
                "SELECT item, event, COUNT(*) FROM events WHERE at>=? GROUP BY item, event",
                (cutoff,),
            ).fetchall()
            closed = self._conn.execute(
                "SELECT item, first_seen, closed_at FROM lots "
                "WHERE status!='active' AND closed_at>=?",
                (cutoff,),
            ).fetchall()
        rows: dict[str, dict] = {}
        prices: dict[str, list[float]] = {}
        for item, amount, unit in active:
            row = rows.setdefault(item, {"item": item})
            row["supply_lots"] = row.get("supply_lots", 0) + 1
            row["supply_units"] = row.get("supply_units", 0) + int(amount or 0)
            if unit:
                prices.setdefault(item, []).append(float(unit))
        for item, event, count in events:
            row = rows.setdefault(item, {"item": item})
            row[event] = int(count)
        lifetimes: dict[str, list[float]] = {}
        for item, first_seen, closed_at in closed:
            if first_seen and closed_at and closed_at > first_seen:
                lifetimes.setdefault(item, []).append((float(closed_at) - float(first_seen)) / 3600.0)
        for item, values in lifetimes.items():
            row = rows.setdefault(item, {"item": item})
            row["avg_lifetime_hours"] = round(sum(values) / len(values), 2)
        for item, values in prices.items():
            values.sort()
            row = rows.setdefault(item, {"item": item})
            row["median_ask"] = round(values[len(values) // 2], 2)
        result = []
        for item, row in rows.items():
            sold = int(row.get("probable_sold", 0))
            closed_count = int(row.get("expired", 0)) + int(row.get("missing", 0))
            disappeared = sold + closed_count
            row["sold"] = sold
            row["closed"] = closed_count
            row["disappeared"] = disappeared
            row["sold_pct"] = round(100.0 * sold / disappeared, 1) if disappeared else None
            row.setdefault("supply_lots", 0)
            row.setdefault("supply_units", 0)
            row.setdefault("appeared", 0)
            row.setdefault("median_ask", None)
            row.setdefault("avg_lifetime_hours", None)
            result.append(row)
        result.sort(key=lambda row: (-row["sold"], -row["disappeared"], -row["supply_lots"]))
        return result[:200]

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
            signals = self._conn.execute("DELETE FROM signals WHERE first_at < ?", (cutoff,)).rowcount
            self._conn.commit()
        return int(events + lots + signals)

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

"""Портфель сделок: локальный журнал покупок и продаж."""

from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS positions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    item        TEXT NOT NULL,
    name        TEXT NOT NULL,
    qlt         INTEGER,
    ptn         INTEGER,
    buy_price   REAL NOT NULL,
    amount      INTEGER NOT NULL,
    target_price REAL,
    note        TEXT,
    bought_at   TEXT NOT NULL,
    sold_price  REAL,
    sold_at     TEXT
);
CREATE INDEX IF NOT EXISTS idx_positions_item ON positions(item);
"""


class PortfolioStore:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False, timeout=30)
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._conn.commit()

    def add(
        self,
        item: str,
        name: str,
        buy_price: float,
        amount: int,
        qlt: int | None = None,
        ptn: int | None = None,
        target_price: float | None = None,
        note: str = "",
    ) -> int:
        bought_at = datetime.now(timezone.utc).isoformat()
        with self._lock:
            cursor = self._conn.execute(
                "INSERT INTO positions "
                "(item, name, qlt, ptn, buy_price, amount, target_price, note, bought_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (item, name, qlt, ptn, float(buy_price), int(amount), target_price, note, bought_at),
            )
            self._conn.commit()
        return int(cursor.lastrowid)

    def sell(self, position_id: int, price: float) -> bool:
        sold_at = datetime.now(timezone.utc).isoformat()
        with self._lock:
            cursor = self._conn.execute(
                "UPDATE positions SET sold_price = ?, sold_at = ? "
                "WHERE id = ? AND sold_at IS NULL",
                (float(price), sold_at, int(position_id)),
            )
            self._conn.commit()
        return cursor.rowcount > 0

    def remove(self, position_id: int) -> bool:
        with self._lock:
            cursor = self._conn.execute("DELETE FROM positions WHERE id = ?", (int(position_id),))
            self._conn.commit()
        return cursor.rowcount > 0

    def list(self) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, item, name, qlt, ptn, buy_price, amount, target_price, note, "
                "bought_at, sold_price, sold_at FROM positions "
                "ORDER BY (sold_at IS NULL) DESC, bought_at DESC"
            ).fetchall()
        return [
            {
                "id": int(row[0]),
                "item": row[1],
                "name": row[2],
                "qlt": int(row[3]) if row[3] is not None else None,
                "ptn": int(row[4]) if row[4] is not None else None,
                "buy_price": float(row[5]),
                "amount": int(row[6]),
                "target_price": float(row[7]) if row[7] is not None else None,
                "note": row[8] or "",
                "bought_at": row[9],
                "sold_price": float(row[10]) if row[10] is not None else None,
                "sold_at": row[11],
            }
            for row in rows
        ]

    def backup_to(self, path: Path | str) -> None:
        with self._lock:
            target = sqlite3.connect(str(path))
            try:
                self._conn.backup(target)
                target.commit()
            finally:
                target.close()

    def close(self) -> None:
        with self._lock:
            try:
                self._conn.close()
            except sqlite3.Error:
                pass

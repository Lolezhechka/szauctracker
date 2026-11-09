"""Быстрый локальный мониторинг новых лотов по выбранным предметам."""

from __future__ import annotations

import threading
import time
from collections import deque
from datetime import datetime, timezone

from market_store import _sig_base
from stalzone import make_client

FAST_MAX_ITEMS = 8


class FastMonitor:
    def __init__(self, store, catalog, manager, cache_dir, log=None):
        self.store = store
        self.catalog = catalog
        self.manager = manager
        self.cache_dir = cache_dir
        self.log = log
        self.interval = 30
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._seen: dict[str, set[str]] = {}
        self._events = deque(maxlen=100)
        self._seq = 0
        self.last_check: float | None = None
        self.last_error: str | None = None
        self.last_paused: str | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="fast-monitor")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._check()
            except Exception as exc:
                self.last_error = str(exc)
                if self.log:
                    self.log(f"Быстрый мониторинг: {exc}")
            self._stop.wait(max(10, int(self.interval)))

    def _fast_items(self, config: dict) -> list[str]:
        rules = config.get("item_rules") or {}
        items = []
        for item in config.get("watchlist") or []:
            rule = rules.get(item) or {}
            if rule.get("fast"):
                items.append(item)
        return items[:FAST_MAX_ITEMS]

    def _benchmarks(self) -> dict[tuple, float]:
        results = self.manager.results or {}
        bench: dict[tuple, float] = {}
        for item in results.get("items") or []:
            item_id = item.get("id")
            groups = item.get("groups") or []
            for group in groups:
                stats = group.get("stats") or {}
                market = group.get("market") or {}
                values = [stats.get("median"), market.get("p25"), market.get("median")]
                values = [float(v) for v in values if v]
                if not values:
                    continue
                base = min(values)
                key = (item_id, group.get("qlt"), group.get("ptn"))
                bench[key] = base
                bench.setdefault((item_id, None, None), base)
        return bench

    def _check(self) -> None:
        config = self.store.snapshot()
        items = self._fast_items(config)
        if not items:
            with self._lock:
                self.last_paused = "нет предметов с быстрым мониторингом"
            return
        if self.manager.is_running():
            with self._lock:
                self.last_paused = "идёт полный скан"
            return
        if not config.get("demo") and not (config.get("client_id") and config.get("client_secret")):
            with self._lock:
                self.last_paused = "не заданы ключи API"
            return
        self.interval = max(10, min(300, int(config.get("fast_interval_sec", 30) or 30)))
        with self._lock:
            self.last_paused = None
        bench = self._benchmarks()
        threshold = float(config.get("threshold_pct", 10)) / 100.0
        commission = float(config.get("commission_pct", 5)) / 100.0
        min_price = float(config.get("min_price") or 0)
        min_profit = float(config.get("min_profit") or 0)
        client = make_client(config, self.cache_dir)
        now = datetime.now(timezone.utc)
        for item_id in items:
            try:
                data = client.get_lots(
                    item_id, limit=20, offset=0, sort="time_created", order="desc",
                    additional=True, force=True,
                )
            except Exception as exc:
                self.last_error = f"{item_id}: {exc}"
                continue
            sigs = set()
            new_lots = []
            for lot in data.get("lots") or []:
                amount = int(lot.get("amount") or 0)
                buyout = int(lot.get("buyoutPrice") or 0)
                if amount <= 0 or buyout <= 0:
                    continue
                sig = _sig_base(item_id, lot)
                sigs.add(sig)
                if item_id in self._seen and sig not in self._seen[item_id]:
                    new_lots.append(lot)
            # Первый проход задаёт базовую линию, без ложных уведомлений.
            if item_id not in self._seen:
                self._seen[item_id] = sigs
                continue
            self._seen[item_id] = sigs
            for lot in new_lots:
                amount = int(lot.get("amount") or 0)
                buyout = int(lot.get("buyoutPrice") or 0)
                unit = buyout / amount
                additional = lot.get("additional") if isinstance(lot.get("additional"), dict) else {}
                key = (item_id, additional.get("qlt"), additional.get("ptn"))
                base = bench.get(key) or bench.get((item_id, None, None))
                if not base or base <= 0 or unit < min_price:
                    continue
                discount = 1.0 - unit / base
                net = base * (1.0 - commission) - unit
                if discount < threshold - 1e-9 or net <= 0 or net * amount < min_profit:
                    continue
                meta = self.catalog.get(item_id) if self.catalog else None
                name = item_id
                if meta:
                    name = meta["name"].get("ru") or meta["name"].get("en") or item_id
                with self._lock:
                    self._seq += 1
                    self._events.appendleft(
                        {
                            "id": self._seq,
                            "at": now.isoformat(),
                            "item": item_id,
                            "name": name,
                            "amount": amount,
                            "unit": round(unit, 2),
                            "benchmark": round(base, 2),
                            "discount_pct": round(discount * 100.0, 1),
                            "net_total": round(net * amount, 2),
                            "qlt": additional.get("qlt"),
                            "ptn": additional.get("ptn"),
                        }
                    )
        with self._lock:
            self.last_check = time.time()
            self.last_error = None

    def snapshot(self) -> dict:
        config = self.store.snapshot()
        items = self._fast_items(config)
        with self._lock:
            return {
                "running": bool(self._thread and self._thread.is_alive()),
                "enabled": bool(items),
                "items": items,
                "interval": max(10, min(300, int(config.get("fast_interval_sec", 30) or 30))),
                "last_check": (
                    datetime.fromtimestamp(self.last_check, timezone.utc).isoformat()
                    if self.last_check else None
                ),
                "paused": self.last_paused,
                "error": self.last_error,
                "events": list(self._events),
            }

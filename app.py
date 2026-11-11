#!/usr/bin/env python3
"""SZAUCTRACKER — локальный сканер аукциона.

Запуск:  python app.py
Затем откройте http://127.0.0.1:8765

Разовый отчёт без сервера:  python app.py --once
Демо-данные:               python app.py --once --demo
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import os
import re
import shutil
import sys
import tempfile
import threading
import time
import traceback
import urllib.request
import zipfile
import webbrowser
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from barter import BarterBook, calculate_item
from fast_monitor import FastMonitor
from catalog import Catalog
from history_store import HistoryStore
from market_store import MarketStore
from portfolio import PortfolioStore
from scanner import scan, stack_band
from stalzone import ApiError, REGIONS, make_client

FROZEN = bool(getattr(sys, "frozen", False))

if FROZEN:
    # В exe рядом лежат только данные пользователя, ресурсы — внутри бандла.
    BASE_DIR = Path(sys.executable).resolve().parent
    RESOURCE_DIR = Path(getattr(sys, "_MEIPASS", BASE_DIR))
else:
    BASE_DIR = Path(__file__).resolve().parent
    RESOURCE_DIR = BASE_DIR

DATA_DIR = BASE_DIR / "data"
STATIC_DIR = RESOURCE_DIR / "static"
CONFIG_PATH = DATA_DIR / "config.json"
RESULTS_PATH = DATA_DIR / "last_results.json"
CATALOG_PATH = DATA_DIR / "catalog.json"
HISTORY_PATH = DATA_DIR / "history.db"
DEAD_PATH = DATA_DIR / "dead_items.json"
PORTFOLIO_PATH = DATA_DIR / "portfolio.db"
MARKET_PATH = DATA_DIR / "market.db"
BARTER_PATH = DATA_DIR / "barter_recipes.json"
ICONS_DIR = DATA_DIR / "icons"
DISCOVERY_PATH = DATA_DIR / "discovery.json"
CACHE_DIR = DATA_DIR / "cache"
LOG_PATH = DATA_DIR / "app.log"
BUNDLED_CATALOG = RESOURCE_DIR / "data" / "catalog.json"


def seed_bundled_data() -> None:
    """Кладёт справочник из бандла рядом с exe при первом запуске."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if (
        FROZEN
        and not CATALOG_PATH.exists()
        and BUNDLED_CATALOG.exists()
        and BUNDLED_CATALOG.resolve() != CATALOG_PATH.resolve()
    ):
        try:
            shutil.copyfile(BUNDLED_CATALOG, CATALOG_PATH)
        except OSError:
            pass

ITEM_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,24}$")
ALLOWED_REGIONS = {row["id"] for row in REGIONS}

DEFAULT_CONFIG = {
    "client_id": "",
    "client_secret": "",
    "demo": False,
    "region": "RU",
    "days": 2,
    "threshold_pct": 10,
    "commission_pct": 5,
    "benchmark": "adaptive",
    "cap_by_market": True,
    "stack_qty": 100,
    "stack_threshold_pct": 10,
    "stack_min_sales": 2,
    "min_volume": 1,
    "min_price": 5000,
    "max_price": 0,
    "min_profit": 5000,
    "lots_pages": 2,
    "history_pages": 5,
    "discovery_enabled": True,
    "discovery_limit": 10,
    "fast_interval_sec": 30,
    "group_by_qlt": True,
    "group_by_ptn": True,
    "group_by_stack": True,
    "auto_scan": True,
    "scan_interval_min": 5,
    "scan_workers": 10,
    "history_ttl_min": 30,
    "watchlist": ["1kdg", "04yr", "4lml", "g34p", "5rd1"],
    "item_rules": {},
}


# --------------------------------------------------------------------- журнал


LOG_LOCK = threading.Lock()
LOG_MAX_BYTES = 2 * 1024 * 1024


def log_line(message: str) -> None:
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{stamp}] {message}"
    with LOG_LOCK:
        try:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            if LOG_PATH.exists() and LOG_PATH.stat().st_size > LOG_MAX_BYTES:
                backup = LOG_PATH.with_name(LOG_PATH.name + ".1")
                try:
                    backup.unlink(missing_ok=True)
                except OSError:
                    pass
                LOG_PATH.replace(backup)
            with open(LOG_PATH, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except OSError:
            pass
        try:
            # В оконном exe stdout перенаправлен в launcher.log; не дублируем
            # каждую строку скана в двух файлах.
            if sys.stdout is not None and getattr(sys.stdout, "isatty", lambda: False)():
                print(line, flush=True)
        except (OSError, ValueError, AttributeError):
            pass


# --------------------------------------------------------------------- конфиг


def _clamp(value, low, high, default):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, number))


def _clean_rule(rule) -> dict:
    if not isinstance(rule, dict):
        return {}
    cleaned = {}
    if "enabled" in rule:
        cleaned["enabled"] = bool(rule["enabled"])
    if "fast" in rule:
        cleaned["fast"] = bool(rule["fast"])
    for key in ("min_price", "min_profit"):
        if key in rule and rule[key] not in ("", None):
            cleaned[key] = int(_clamp(rule[key], 0, 10**12, 0))
    if "qlt" in rule and rule["qlt"] not in ("", None):
        cleaned["qlt"] = int(_clamp(rule["qlt"], 0, 6, 0))
    if "bonus" in rule and str(rule["bonus"]).strip():
        cleaned["bonus"] = str(rule["bonus"]).strip()[:40]
    return cleaned


def _clean_item_rules(rules) -> dict:
    if not isinstance(rules, dict):
        return {}
    out = {}
    for item, rule in list(rules.items())[:500]:
        item = str(item).strip()
        if not ITEM_ID_RE.match(item):
            continue
        cleaned = _clean_rule(rule)
        if cleaned:
            out[item] = cleaned
    return out


def _clean_watchlist(values) -> list[str]:
    out: list[str] = []
    seen = set()
    for value in values or []:
        item = str(value).strip()
        if not item or item in seen or not ITEM_ID_RE.match(item):
            continue
        seen.add(item)
        out.append(item)
        if len(out) >= 500:
            break
    return out


class Store:
    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.RLock()
        self.config = self._load()

    def _load(self) -> dict:
        config = dict(DEFAULT_CONFIG)
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                saved = json.load(fh)
            if isinstance(saved, dict):
                config.update(saved)
                config["watchlist"] = _clean_watchlist(config.get("watchlist"))
        except (OSError, ValueError):
            pass
        return self._sanitize(config, full=True)

    def _sanitize(self, config: dict, full: bool = False) -> dict:
        out = dict(config)
        out["region"] = str(out.get("region", "RU")).upper()
        if out["region"] not in ALLOWED_REGIONS:
            out["region"] = "RU"
        out["days"] = round(_clamp(out.get("days"), 0.25, 30, 2), 2)
        out["threshold_pct"] = round(_clamp(out.get("threshold_pct"), 0.5, 90, 10), 1)
        out["commission_pct"] = round(_clamp(out.get("commission_pct"), 0, 50, 5), 1)
        out["stack_qty"] = int(_clamp(out.get("stack_qty"), 2, 10000, 100))
        out["stack_threshold_pct"] = round(_clamp(out.get("stack_threshold_pct"), 0.5, 90, 10), 1)
        out["stack_min_sales"] = int(_clamp(out.get("stack_min_sales"), 0, 1000, 2))
        out["min_volume"] = int(_clamp(out.get("min_volume"), 0, 100000, 1))
        out["min_price"] = max(0, int(_clamp(out.get("min_price"), 0, 10**12, 5000)))
        out["max_price"] = max(0, int(_clamp(out.get("max_price"), 0, 10**12, 0)))
        out["min_profit"] = int(_clamp(out.get("min_profit"), 0, 10**12, 5000))
        out["lots_pages"] = int(_clamp(out.get("lots_pages"), 1, 10, 2))
        out["history_pages"] = int(_clamp(out.get("history_pages"), 1, 10, 5))
        out["discovery_limit"] = int(_clamp(out.get("discovery_limit"), 1, 50, 10))
        out["fast_interval_sec"] = int(_clamp(out.get("fast_interval_sec"), 10, 300, 30))
        out["scan_interval_min"] = int(_clamp(out.get("scan_interval_min"), 1, 180, 5))
        out["scan_workers"] = int(_clamp(out.get("scan_workers"), 1, 16, 10))
        out["history_ttl_min"] = int(_clamp(out.get("history_ttl_min"), 1, 1440, 30))
        out.pop("group_by_percent", None)
        out["benchmark"] = str(out.get("benchmark", "median"))
        if out["benchmark"] not in ("mean", "median", "trimmed", "lots", "blend", "adaptive"):
            out["benchmark"] = "adaptive"
        out["cap_by_market"] = bool(out.get("cap_by_market", True))
        out["client_id"] = str(out.get("client_id") or "").strip()[:64]
        out["client_secret"] = str(out.get("client_secret") or "").strip()[:256]
        for key in ("demo", "auto_scan", "discovery_enabled", "group_by_qlt", "group_by_ptn", "group_by_stack"):
            out[key] = bool(out.get(key))
        out["watchlist"] = _clean_watchlist(out.get("watchlist"))
        out["item_rules"] = _clean_item_rules(out.get("item_rules"))
        return out

    def snapshot(self) -> dict:
        with self._lock:
            return json.loads(json.dumps(self.config))

    def save(self) -> None:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(self.config, fh, ensure_ascii=False, indent=2)
            os.replace(tmp, self.path)

    def update(self, patch: dict) -> dict:
        with self._lock:
            if not isinstance(patch, dict):
                raise ValueError("Ожидался JSON-объект")
            updated = dict(self.config)
            for key in (
                "region",
                "days",
                "threshold_pct",
                "commission_pct",
                "benchmark",
                "cap_by_market",
                "stack_qty",
                "stack_threshold_pct",
                "stack_min_sales",
                "min_volume",
                "min_price",
                "max_price",
                "min_profit",
                "lots_pages",
                "history_pages",
                "discovery_enabled",
                "discovery_limit",
                "fast_interval_sec",
                "scan_interval_min",
                "scan_workers",
                "history_ttl_min",
                "demo",
                "auto_scan",
                "group_by_qlt",
                "group_by_ptn",
                "group_by_stack",
                "client_id",
                "item_rules",
            ):
                if key in patch:
                    updated[key] = patch[key]
            if patch.get("clear_secret"):
                updated["client_secret"] = ""
            elif patch.get("client_secret"):
                updated["client_secret"] = patch["client_secret"]
            if "watchlist" in patch:
                updated["watchlist"] = patch["watchlist"]
            self.config = self._sanitize(updated)
            self.save()
            return json.loads(json.dumps(self.config))

    def watch(self, add=None, remove=None, clear=False, add_category=None, limit=100, catalog=None) -> dict:
        with self._lock:
            current = list(self.config.get("watchlist") or [])
            if clear:
                current = []
            if add_category and catalog is not None:
                for item in catalog.by_category(add_category, limit=limit):
                    current.append(item["id"])
            for item in add or []:
                item = str(item).strip()
                if not ITEM_ID_RE.match(item):
                    raise ValueError(f"Некорректный ID предмета: {item!r}")
                current.append(item)
            remove_set = {str(x).strip() for x in (remove or [])}
            current = [item for item in current if item not in remove_set]
            self.config["watchlist"] = _clean_watchlist(current)
            rules = self.config.get("item_rules") or {}
            self.config["item_rules"] = {
                key: value for key, value in rules.items() if key in self.config["watchlist"]
            }
            self.save()
            return {
                "watchlist": list(self.config["watchlist"]),
                "count": len(self.config["watchlist"]),
            }

    def set_rule(self, item: str, rule: dict) -> dict:
        with self._lock:
            item = str(item).strip()
            if not ITEM_ID_RE.match(item):
                raise ValueError("Некорректный ID предмета")
            rules = dict(self.config.get("item_rules") or {})
            cleaned = _clean_rule(rule)
            if cleaned:
                rules[item] = cleaned
            else:
                rules.pop(item, None)
            self.config["item_rules"] = _clean_item_rules(rules)
            self.save()
            return json.loads(json.dumps(self.config["item_rules"]))


# --------------------------------------------------------------------- сканы


class DeadList:
    """ID, которые API считает недействительными: не тратим на них запросы."""

    def __init__(self, path: Path, ttl_days: float = 7.0):
        self.path = path
        self.ttl = ttl_days * 86400
        self._lock = threading.RLock()
        self._items = self._load()

    def _load(self) -> dict:
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            return {}
        now = time.time()
        items = {}
        if isinstance(data, dict):
            for item_id, stamp in data.items():
                try:
                    stamp = float(stamp)
                except (TypeError, ValueError):
                    continue
                if now - stamp <= self.ttl:
                    items[str(item_id)] = stamp
        return items

    def _save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(self._items, fh, ensure_ascii=False)
            os.replace(tmp, self.path)
        except OSError:
            pass

    def _prune(self) -> None:
        now = time.time()
        self._items = {key: stamp for key, stamp in self._items.items() if now - stamp <= self.ttl}

    def ids(self) -> list[str]:
        with self._lock:
            self._prune()
            return sorted(self._items)

    def count(self) -> int:
        return len(self.ids())

    def add(self, item_ids) -> None:
        with self._lock:
            now = time.time()
            for item_id in item_ids or []:
                self._items[str(item_id)] = now
            self._prune()
            self._save()

    def remove(self, item_ids) -> None:
        with self._lock:
            changed = False
            for item_id in item_ids or []:
                if str(item_id) in self._items:
                    self._items.pop(str(item_id), None)
                    changed = True
            if changed:
                self._save()

    def clear(self) -> None:
        with self._lock:
            self._items = {}
            self._save()


class ScanManager:
    def __init__(self, store: Store, catalog: Catalog, history_store=None, dead_list=None, market_store=None):
        self.store = store
        self.catalog = catalog
        self.history_store = history_store
        self.dead_list = dead_list
        self.market_store = market_store
        self.dead_skipped = 0
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._cancel = threading.Event()
        self._client = None
        self.started_at: float | None = None
        self.finished_at: float | None = None
        self.error: str | None = None
        self.phase = "idle"
        self.done = 0
        self.total = 0
        self.current = None
        self.discovery_cursor = self._load_discovery_cursor()
        self.results = self._load_results()
        if self.dead_list is not None:
            invalid = [
                item["id"]
                for item in self.results.get("items", [])
                if item.get("status") == "error" and "Invalid item id" in (item.get("error") or "")
            ]
            if invalid:
                self.dead_list.add(invalid)

    # ------------------------------------------------------------- состояние

    def _load_discovery_cursor(self) -> int:
        try:
            with open(DISCOVERY_PATH, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            return max(0, int(data.get("cursor") or 0))
        except (OSError, ValueError, TypeError):
            return 0

    def _save_discovery_cursor(self) -> None:
        try:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            tmp = DISCOVERY_PATH.with_suffix(".tmp")
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump({"cursor": int(self.discovery_cursor)}, fh)
            tmp.replace(DISCOVERY_PATH)
        except OSError:
            pass

    def _load_results(self) -> dict:
        try:
            with open(RESULTS_PATH, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, dict):
                return data
        except (OSError, ValueError):
            pass
        return {}

    def _save_results(self, results: dict) -> None:
        try:
            RESULTS_PATH.parent.mkdir(parents=True, exist_ok=True)
            tmp = RESULTS_PATH.with_suffix(".tmp")
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(results, fh, ensure_ascii=False)
            os.replace(tmp, RESULTS_PATH)
        except OSError as exc:
            log_line(f"Не удалось сохранить результаты: {exc}")
        except (TypeError, ValueError) as exc:
            log_line(f"Результаты не сериализуются в JSON: {exc}")

    def is_running(self) -> bool:
        with self._lock:
            return bool(self._thread and self._thread.is_alive())

    def last_finished(self) -> float:
        return self.finished_at or 0.0

    def start(self) -> bool:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return False
            self._cancel.clear()
            self._thread = threading.Thread(target=self._run, daemon=True)
            self._thread.start()
            return True

    def cancel(self) -> None:
        self._cancel.set()

    def status(self) -> dict:
        running = self.is_running()
        api = (self.results.get("api") or {}) if self.results else {}
        if running and self._client is not None:
            api = dict(self._client.stats)
        elapsed = time.time() - self.started_at if running and self.started_at else None
        eta = None
        if elapsed is not None and self.done > 0 and self.total > self.done:
            eta = int(elapsed / self.done * (self.total - self.done))
        dead_ids = self.dead_list.ids() if self.dead_list is not None else []
        return {
            "running": running,
            "phase": self.phase,
            "done": self.done,
            "total": self.total,
            "current": self.current,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "elapsed_sec": round(elapsed, 1) if elapsed is not None else None,
            "eta_sec": eta,
            "error": self.error,
            "dead_skipped": self.dead_skipped,
            "dead_count": len(dead_ids),
            "dead_items": dead_ids,
            "results_rev": self.results.get("generated_ts"),
            "results_meta": {
                "generated_at": self.results.get("generated_at"),
                "deals_count": self.results.get("deals_count"),
                "items_total": self.results.get("items_total"),
                "demo": self.results.get("demo"),
            }
            if self.results
            else None,
            "api": api,
        }

    # ------------------------------------------------------------- выполнение

    def _progress(self, done: int, total: int, item_id: str, phase: str) -> None:
        self.done = done
        self.total = total
        self.current = item_id
        self.phase = phase

    def _run(self) -> None:
        config = self.store.snapshot()
        self.started_at = time.time()
        self.finished_at = None
        self.error = None
        self.done = 0
        self.current = None
        self.phase = "подготовка"

        dead_ids = set(self.dead_list.ids()) if self.dead_list is not None else set()
        configured_watch = list(config.get("watchlist") or [])
        config["watchlist"] = [item for item in configured_watch if item not in dead_ids]
        self.dead_skipped = len(configured_watch) - len(config["watchlist"])
        self.total = len(config["watchlist"])
        if not config["watchlist"]:
            self.phase = "ошибка"
            self.error = "Все предметы помечены недействительными. Удалите их из наблюдения."
            self.finished_at = time.time()
            return

        if not self.catalog.ready:
            deadline = time.time() + 25
            while not self.catalog.ready and time.time() < deadline:
                time.sleep(0.3)

        try:
            client = make_client(config, CACHE_DIR)
            self._client = client
            self.phase = "сканирование"
            results = scan(
                client,
                self.catalog,
                config,
                progress=self._progress,
                cancel=self._cancel,
                log=log_line,
                history_store=self.history_store,
                market_store=self.market_store,
                discovery_start=self.discovery_cursor,
                discovery_limit=(
                    int(config.get("discovery_limit", 10))
                    if config.get("discovery_enabled", True) else 0
                ),
                dead_ids=self.dead_list.ids() if self.dead_list is not None else [],
            )
            invalid = [
                item["id"]
                for item in results.get("items", [])
                if item.get("status") == "error" and "Invalid item id" in (item.get("error") or "")
            ]
            if invalid and self.dead_list is not None:
                self.dead_list.add(invalid)
            results["dead_skipped"] = self.dead_skipped
            results["invalid_items"] = invalid
            self.discovery_cursor = int(results.get("discovery_next") or self.discovery_cursor)
            self._save_discovery_cursor()
            self.results = results
            self._save_results(results)
            self.phase = "готово"
            errors = int(results.get("items_errors") or 0)
            total = max(1, int(results.get("items_total") or 0))
            self.error = (
                f"Много ошибок сети/API: {errors} из {total} предметов. Проверьте соединение."
                if errors >= max(3, total // 2) else None
            )
            log_line(
                f"Скан завершён: предметов {results['items_total']}, сделок {results['deals_count']}, "
                f"запросов {results['api'].get('requests')}, {results['elapsed_sec']} с"
            )
        except ApiError as exc:
            self.phase = "ошибка"
            self.error = str(exc)
            log_line(f"Скан прерван: {exc}")
        except Exception as exc:
            self.phase = "ошибка"
            self.error = f"Внутренняя ошибка: {exc}"
            log_line("Скан упал:\n" + traceback.format_exc())
        finally:
            self.finished_at = time.time()
            if self.market_store is not None:
                try:
                    self.market_store.checkpoint()
                except Exception:
                    pass
            if self._cancel.is_set() and self.phase != "ошибка":
                self.phase = "остановлено"
            elif self.phase not in ("готово", "ошибка", "остановлено"):
                self.phase = "остановлено"


def auto_loop(manager: ScanManager, store: Store, stop_event: threading.Event) -> None:
    while not stop_event.wait(5.0):
        try:
            config = store.snapshot()
            if not config.get("auto_scan") or manager.is_running():
                continue
            if not config.get("watchlist"):
                continue
            if not config.get("demo") and not (config.get("client_id") and config.get("client_secret")):
                continue
            # Не запускаем сканы чаще, чем позволяет лимит API: на предмет нужно
            # минимум два запроса, безопасный темп — около 180 запросов/мин.
            base_minutes = max(1, int(config.get("scan_interval_min", 5)))
            items = len(config.get("watchlist") or [])
            required_minutes = (items * 2 + 179) // 180
            interval = max(base_minutes, required_minutes) * 60
            if time.time() - manager.last_finished() >= interval:
                manager.start()
        except Exception as exc:
            log_line(f"Автоскан: {exc}")


# --------------------------------------------------------------------- сервер


class AppState:
    def __init__(self):
        self.store = Store(CONFIG_PATH)
        self.catalog = Catalog(CATALOG_PATH)
        self.dead = DeadList(DEAD_PATH)
        self.history = None
        try:
            self.history = HistoryStore(HISTORY_PATH)
        except Exception as exc:
            log_line(f"Локальная история недоступна: {exc}")
        self.portfolio = None
        try:
            self.portfolio = PortfolioStore(PORTFOLIO_PATH)
        except Exception as exc:
            log_line(f"Портфель недоступен: {exc}")
        self.market = None
        try:
            self.market = MarketStore(MARKET_PATH)
        except Exception as exc:
            log_line(f"Журнал лотов недоступен: {exc}")
        self.barter = BarterBook(BARTER_PATH)
        self.barter.load_async()
        self.barter_prices = {}
        self.barter_lock = threading.Lock()
        self.manager = ScanManager(self.store, self.catalog, self.history, self.dead, self.market)
        self.fast = FastMonitor(self.store, self.catalog, self.manager, CACHE_DIR, log_line, self.history)
        self.fast.start()
        self.stop_event = threading.Event()

    def close(self) -> None:
        try:
            self.fast.stop()
        except Exception:
            pass
        for store in (self.history, self.portfolio, self.market):
            try:
                if store is not None:
                    store.close()
            except Exception:
                pass


def _safe_config(config: dict) -> dict:
    safe = dict(config)
    safe["client_secret"] = ""
    safe["has_secret"] = bool(config.get("client_secret"))
    safe["mode"] = "demo" if config.get("demo") else "live"
    safe["configured"] = bool(config.get("client_id") and config.get("client_secret"))
    return safe


def _compact_results(results: dict) -> dict:
    """Убирает из /api/results тяжёлые группы, оставляя всё нужное интерфейсу."""
    if not results:
        return {}
    items = []
    for item in results.get("items") or []:
        groups = item.get("groups") or []
        volume = 0
        min_lot = None
        best = None
        for group in groups:
            stats = group.get("stats") or {}
            volume += int(stats.get("volume") or 0)
            lot = group.get("min_lot_unit")
            if lot is not None:
                min_lot = lot if min_lot is None else min(min_lot, lot)
            discount = group.get("best_discount_pct")
            if discount is not None:
                best = discount if best is None else max(best, discount)
        items.append(
            {
                "id": item.get("id"),
                "name": item.get("name"),
                "category": item.get("category"),
                "status": item.get("status"),
                "source": item.get("source"),
                "error": item.get("error"),
                "lots_total": item.get("lots_total"),
                "history_in_window": item.get("history_in_window"),
                "coverage": item.get("coverage"),
                "deals_count": len(item.get("deals") or []),
                "volume": volume,
                "min_lot_unit": min_lot,
                "best_discount_pct": best,
            }
        )
    compact = {key: value for key, value in results.items() if key != "items"}
    compact["items"] = items
    return compact


def _market_for_item(results: dict, item: str, qlt, ptn, amount=None):
    """Возвращает группу и текущую цену продажи для портфеля."""
    item_result = next((row for row in results.get("items", []) if row.get("id") == item), None)
    if not item_result:
        return None, None
    group = next(
        (
            row
            for row in item_result.get("groups", [])
            if row.get("qlt") == qlt and row.get("ptn") == ptn
        ),
        None,
    )
    if group is None:
        return item_result, None
    market = group.get("market") or {}
    stats = group.get("stats") or {}
    bands = group.get("amount_bands") or {}
    if amount is not None and bands:
        band_view = bands.get(stack_band(amount))
        if band_view:
            market = band_view.get("market") or market
            stats = band_view.get("stats") or stats
    current = market.get("p25") or market.get("median") or stats.get("median")
    return item_result, {
        "current_price": current,
        "market_min": market.get("min"),
        "market_p25": market.get("p25"),
        "market_median": market.get("median"),
        "market_lots": market.get("count"),
        "sales_median": stats.get("median"),
        "volume": stats.get("volume"),
    }


def _csv_bytes(results: dict) -> bytes:
    reliability_ru = {"high": "высокая", "medium": "средняя", "low": "низкая"}
    buffer = io.StringIO()
    writer = csv.writer(buffer, delimiter=";", lineterminator="\r\n")
    writer.writerow(
        [
            "Скидка, %",
            "Оценка",
            "Предмет",
            "ID",
            "Категория",
            "Качество",
            "Паттерн/уровень",
            "Стак",
            "Тренд, %",
            "Дней до продажи",
            "Цена за шт.",
            "База за шт.",
            "Профит за шт.",
            "Кол-во",
            "Лотов",
            "Профит всего",
            "Продаж, шт.",
            "Продаж/день",
            "Осталось, мин",
            "Надёжность",
        ]
    )
    for deal in results.get("deals") or []:
        writer.writerow(
            [
                deal.get("discount_pct"),
                deal.get("score"),
                deal.get("name"),
                deal.get("item_id"),
                deal.get("category"),
                deal.get("qlt_label") or "",
                deal.get("ptn_label") or "",
                deal.get("amount_band_label") or "",
                deal.get("trend_pct") if deal.get("trend_pct") is not None else "",
                deal.get("expected_days") if deal.get("expected_days") is not None else "",
                round(deal.get("unit_price") or 0),
                round(deal.get("benchmark") or 0),
                round(deal.get("net_unit") or 0),
                deal.get("amount"),
                deal.get("lots_count") or 1,
                round(deal.get("total_net") or 0),
                deal.get("volume"),
                deal.get("sales_per_day"),
                deal.get("time_left_min"),
                reliability_ru.get(deal.get("reliability"), deal.get("reliability") or ""),
            ]
        )
    return buffer.getvalue().encode("utf-8-sig")


def _csv_stacks_bytes(results: dict) -> bytes:
    reliability_ru = {"high": "высокая", "medium": "средняя", "low": "низкая"}
    buffer = io.StringIO()
    writer = csv.writer(buffer, delimiter=";", lineterminator="\r\n")
    writer.writerow(
        [
            "Маржа, %",
            "Направление",
            "Предмет",
            "ID",
            "Категория",
            "Качество",
            "Паттерн/уровень",
            "Кол-во, шт.",
            "Купить всего",
            "Купить за шт.",
            "Продать всего",
            "Продать за шт. (после ком.)",
            "Профит",
            "Лотов куплено",
            "Лотов продано",
            "Продаж одиночных",
            "Продаж стаками",
            "Надёжность",
        ]
    )
    for row in results.get("stacks") or []:
        writer.writerow(
            [
                row.get("margin_pct"),
                row.get("label"),
                row.get("name"),
                row.get("item_id"),
                row.get("category"),
                row.get("qlt_label") or "",
                row.get("ptn_label") or "",
                row.get("qty"),
                round(row.get("buy_cost") or 0),
                round(row.get("buy_unit") or 0),
                round(row.get("sell_net") or 0),
                round(row.get("sell_net_unit") or 0),
                round(row.get("profit") or 0),
                row.get("buy_lots_used"),
                row.get("sell_lots"),
                row.get("sell_sales_count") if row.get("direction") == "bulk_to_singles" else "",
                row.get("sell_sales_count") if row.get("direction") == "singles_to_bulk" else "",
                reliability_ru.get(row.get("reliability"), row.get("reliability") or ""),
            ]
        )
    return buffer.getvalue().encode("utf-8-sig")


def _icon_category(category: str) -> str:
    category = str(category or "other")
    if category == "containers":
        return "backpacks"
    if category.startswith("consumables/"):
        sub = category.split("/", 1)[1]
        return "bullet" if sub == "bullet" else f"supply/{sub}"
    if category == "consumables":
        return "supply"
    return category


def _icon_candidates(item: str, category: str) -> list[str]:
    mapped = _icon_category(category)
    candidates = [f"{mapped}/{item}.png", f"other/{item}.png"]
    if mapped != "supply":
        candidates.append(f"supply/{item}.png")
    candidates.append(f"backpacks/{item}.png")
    return list(dict.fromkeys(candidates))


class Handler(BaseHTTPRequestHandler):
    server_version = "SZAUCTRACKER/2.0"
    state: AppState = None  # задаётся при создании класса

    # ------------------------------------------------------------- ответы

    def _host_allowed(self) -> bool:
        host = (self.headers.get("Host") or "").strip()
        if not host:
            return True
        name = host.rsplit(":", 1)[0].strip("[]").lower()
        return name in ("127.0.0.1", "localhost", "::1")

    def _json(self, code: int, payload) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        try:
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)
        except (ConnectionAbortedError, BrokenPipeError, OSError):
            self.close_connection = True

    def _error(self, code: int, message: str) -> None:
        self._json(code, {"error": message})

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        if length > 2_000_000:
            raise ValueError("Слишком большой запрос")
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except ValueError as exc:
            raise ValueError("Некорректный JSON") from exc
        if not isinstance(data, dict):
            raise ValueError("Ожидался JSON-объект")
        return data

    # ------------------------------------------------------------- маршруты

    def do_GET(self) -> None:
        if not self._host_allowed():
            self._error(403, "Доступ только с localhost")
            return
        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        query = parse_qs(parsed.query)
        try:
            if path == "/":
                self._serve_file(STATIC_DIR / "index.html")
            elif path.startswith("/static/"):
                self._serve_file(STATIC_DIR / path[len("/static/") :])
            elif path == "/api/config":
                config = self.state.store.snapshot()
                self._json(
                    200,
                    {
                        "config": _safe_config(config),
                        "regions": REGIONS,
                        "catalog_ready": self.state.catalog.ready,
                        "catalog_error": self.state.catalog.error,
                        "dead_items": self.state.dead.ids(),
                        "dead_count": self.state.dead.count(),
                    },
                )
            elif path == "/api/status":
                self._json(200, self.state.manager.status())
            elif path == "/api/results":
                self._json(200, _compact_results(self.state.manager.results or {}))
            elif path == "/api/analytics":
                self._analytics(query)
            elif path == "/api/movement":
                self._movement(query)
            elif path == "/api/barter/items":
                self._barter_items()
            elif path == "/api/barter":
                self._barter(query)
            elif path == "/api/fast":
                self._json(200, self.state.fast.snapshot())
            elif path == "/api/icon":
                self._icon(query)
            elif path == "/api/backup":
                self._backup()
            elif path == "/api/portfolio":
                self._portfolio()
            elif path == "/api/regions":
                self._json(200, {"regions": REGIONS})
            elif path == "/api/catalog/categories":
                self._json(
                    200,
                    {
                        "ready": self.state.catalog.ready,
                        "error": self.state.catalog.error,
                        "categories": self.state.catalog.categories() if self.state.catalog.ready else [],
                    },
                )
            elif path == "/api/catalog/search":
                q = (query.get("q") or [""])[0]
                category = (query.get("category") or [""])[0]
                limit = int((query.get("limit") or ["40"])[0] or 40)
                if not self.state.catalog.ready:
                    self._json(
                        200,
                        {
                            "ready": False,
                            "error": self.state.catalog.error,
                            "items": [],
                        },
                    )
                    return
                items = self.state.catalog.search(q, category=category, limit=min(limit, 200))
                self._json(200, {"ready": True, "items": items})
            elif path == "/api/catalog/items":
                raw = (query.get("ids") or [""])[0]
                ids = [part.strip() for part in raw.split(",") if part.strip()][:500]
                items = []
                for item_id in ids:
                    item = self.state.catalog.get(item_id)
                    if item:
                        items.append(item)
                self._json(200, {"items": items})
            elif path == "/api/export.csv":
                results = self.state.manager.results or {}
                if not results.get("deals"):
                    self._error(404, "Нет данных для экспорта")
                    return
                body = _csv_bytes(results)
                self.send_response(200)
                self.send_header("Content-Type", "text/csv; charset=utf-8")
                self.send_header("Content-Disposition", 'attachment; filename="stalzone_deals.csv"')
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif path == "/api/export_stacks.csv":
                results = self.state.manager.results or {}
                if not results.get("stacks"):
                    self._error(404, "Нет данных для экспорта")
                    return
                body = _csv_stacks_bytes(results)
                self.send_response(200)
                self.send_header("Content-Type", "text/csv; charset=utf-8")
                self.send_header("Content-Disposition", 'attachment; filename="stalzone_stacks.csv"')
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif path == "/api/health":
                self._json(200, {"ok": True, "time": time.time()})
            else:
                self._error(404, "Не найдено")
        except (ConnectionAbortedError, BrokenPipeError):
            self.close_connection = True
        except Exception as exc:
            log_line(f"GET {path}: {exc}")
            self._error(500, f"Внутренняя ошибка: {exc}")

    def do_POST(self) -> None:
        if not self._host_allowed():
            self._error(403, "Доступ только с localhost")
            return
        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        try:
            if path == "/api/config":
                patch = self._read_json()
                config = self.state.store.update(patch)
                self._json(
                    200,
                    {
                        "config": _safe_config(config),
                        "catalog_ready": self.state.catalog.ready,
                    },
                )
            elif path == "/api/watch":
                payload = self._read_json()
                added = [str(x).strip() for x in (payload.get("add") or []) if str(x).strip()]
                result = self.state.store.watch(
                    add=added,
                    remove=payload.get("remove") or [],
                    clear=bool(payload.get("clear")),
                    add_category=payload.get("add_category"),
                    limit=int(payload.get("limit") or 100),
                    catalog=self.state.catalog,
                )
                if added:
                    self.state.dead.remove(added)
                result["catalog_ready"] = self.state.catalog.ready
                self._json(200, result)
            elif path == "/api/rules":
                self._set_rule()
            elif path == "/api/portfolio/buy":
                self._portfolio_buy()
            elif path == "/api/portfolio/sell":
                self._portfolio_sell()
            elif path == "/api/portfolio/remove":
                self._portfolio_remove()
            elif path == "/api/dead/cleanup":
                dead_ids = self.state.dead.ids()
                result = self.state.store.watch(remove=dead_ids)
                self.state.dead.clear()
                self._json(
                    200,
                    {
                        "removed": len(dead_ids),
                        "watchlist": result["watchlist"],
                        "count": result["count"],
                    },
                )
            elif path == "/api/scan":
                payload = self._read_json()
                if payload.get("full"):
                    # Полное обновление: чистим кэш истории до старта скана.
                    for file in CACHE_DIR.glob("*.json"):
                        try:
                            file.unlink()
                        except OSError:
                            pass
                started = self.state.manager.start()
                if not started:
                    self._json(409, {"error": "Скан уже выполняется", "status": self.state.manager.status()})
                    return
                self._json(202, {"started": True, "status": self.state.manager.status()})
            elif path == "/api/cancel":
                self.state.manager.cancel()
                self._json(200, {"cancelling": True})
            elif path == "/api/check":
                self._check_credentials()
            else:
                self._error(404, "Не найдено")
        except ValueError as exc:
            self._error(400, str(exc))
        except ApiError as exc:
            self._json(400, {"error": str(exc), "status_code": exc.status})
        except (ConnectionAbortedError, BrokenPipeError):
            self.close_connection = True
        except Exception as exc:
            log_line(f"POST {path}: {exc}")
            self._error(500, f"Внутренняя ошибка: {exc}")

    # ------------------------------------------------------------- правила

    def _set_rule(self) -> None:
        payload = self._read_json()
        item = str(payload.get("item") or "").strip()
        if not ITEM_ID_RE.match(item):
            self._error(400, "Некорректный ID предмета")
            return
        rule = payload.get("rule")
        if not isinstance(rule, dict):
            self._error(400, "Ожидался объект rule")
            return
        rules = self.state.store.set_rule(item, rule)
        self._json(200, {"ok": True, "item_rules": rules})

    # ------------------------------------------------------------- портфель

    @staticmethod
    def _optional_int(value):
        if value is None or value == "":
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    def _backup(self) -> None:
        tmp = Path(tempfile.mkdtemp(prefix="szauctracker-backup-"))
        try:
            for store, name in (
                (self.state.history, "history.db"),
                (self.state.portfolio, "portfolio.db"),
                (self.state.market, "market.db"),
            ):
                if store is not None:
                    try:
                        store.backup_to(tmp / name)
                    except Exception as exc:
                        log_line(f"Backup {name}: {exc}")
            for source, name in (
                (CONFIG_PATH, "config.json"),
                (DEAD_PATH, "dead_items.json"),
                (DISCOVERY_PATH, "discovery.json"),
            ):
                if source.exists():
                    shutil.copy2(source, tmp / name)
            payload = io.BytesIO()
            with zipfile.ZipFile(payload, "w", zipfile.ZIP_DEFLATED) as archive:
                for path in sorted(tmp.iterdir()):
                    if path.is_file():
                        archive.write(path, path.name)
            data = payload.getvalue()
            stamp = datetime.now().strftime("%Y%m%d-%H%M")
            self.send_response(200)
            self.send_header("Content-Type", "application/zip")
            self.send_header("Content-Disposition", f'attachment; filename="szauctracker-backup-{stamp}.zip"')
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except (ConnectionAbortedError, BrokenPipeError, OSError):
            pass
        except Exception as exc:
            self._error(500, f"Не удалось сделать backup: {exc}")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def _icon(self, query) -> None:
        item = (query.get("item") or [""])[0].strip()
        if not ITEM_ID_RE.match(item):
            self._error(400, "Некорректный ID предмета")
            return
        cache_path = ICONS_DIR / f"{item}.png"
        try:
            if cache_path.exists() and cache_path.stat().st_size > 0:
                data = cache_path.read_bytes()
                self._png(data)
                return
        except OSError:
            pass
        meta = self.state.catalog.get(item) or {}
        for candidate in _icon_candidates(item, meta.get("category", "other")):
            url = (
                "https://raw.githubusercontent.com/EXBO-Studio/stalzone-database/"
                f"main/global/icons/{candidate}"
            )
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "SZAUCTRACKER/2.0"})
                with urllib.request.urlopen(req, timeout=20) as resp:
                    data = resp.read()
                if not data:
                    continue
                ICONS_DIR.mkdir(parents=True, exist_ok=True)
                tmp = cache_path.with_suffix(".tmp")
                tmp.write_bytes(data)
                tmp.replace(cache_path)
                self._png(data)
                return
            except Exception as exc:
                log_line(f"Иконка {item}: {type(exc).__name__}: {exc}")
                continue
        self._error(404, "Иконка не найдена")

    def _png(self, data: bytes) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "image/png")
        self.send_header("Cache-Control", "public, max-age=604800")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _movement(self, query) -> None:
        item = (query.get("item") or [""])[0].strip()
        if not ITEM_ID_RE.match(item):
            self._error(400, "Некорректный ID предмета")
            return
        try:
            days = float((query.get("days") or ["7"])[0])
        except ValueError:
            days = 7.0
        days = max(0.25, min(90.0, days))
        market = self.state.market
        if market is None:
            self._json(200, {"item": item, "error": "Журнал лотов недоступен"})
            return
        try:
            summary = market.summary(item, days)
        except Exception as exc:
            self._error(500, f"Не удалось прочитать журнал: {exc}")
            return
        meta = self.state.catalog.get(item)
        name = item
        category = "other"
        if meta:
            name = meta["name"].get("ru") or meta["name"].get("en") or item
            category = meta.get("category", "other")
        summary.update({"name": name, "category": category})
        self._json(200, summary)

    def _barter_items(self) -> None:
        book = self.state.barter
        if not book.ready:
            self._json(200, {"ready": False, "error": book.error, "items": []})
            return
        items = []
        for item_id in book.items():
            meta = self.state.catalog.get(item_id)
            name = item_id
            category = "other"
            if meta:
                name = meta["name"].get("ru") or meta["name"].get("en") or item_id
                category = meta.get("category", "other")
            items.append({"id": item_id, "name": name, "category": category})
        self._json(200, {"ready": True, "error": book.error, "items": items})

    def _barter(self, query) -> None:
        item = (query.get("item") or [""])[0].strip()
        if not ITEM_ID_RE.match(item):
            self._error(400, "Некорректный ID предмета")
            return
        book = self.state.barter
        if not book.ready:
            self._json(200, {"item": item, "error": book.error or "Рецепты ещё загружаются"})
            return
        if not book.for_item(item):
            self._json(200, {"item": item, "error": "Для этого предмета нет бартерного рецепта"})
            return
        config = self.state.store.snapshot()
        try:
            client = make_client(config, CACHE_DIR)
            with self.state.barter_lock:
                result = calculate_item(
                    book, client, item, self.state.catalog,
                    commission_pct=float(config.get("commission_pct", 5)),
                    price_cache=self.state.barter_prices,
                )
        except Exception as exc:
            self._error(500, f"Не удалось рассчитать бартер: {exc}")
            return
        self._json(200, result)

    def _portfolio(self) -> None:
        portfolio = self.state.portfolio
        if portfolio is None:
            self._error(503, "Портфель недоступен")
            return
        config = self.state.store.snapshot()
        commission = float(config.get("commission_pct", 5)) / 100.0
        results = self.state.manager.results or {}
        positions = portfolio.list()
        invested = current_value = unrealized = realized = 0.0
        output = []
        for position in positions:
            market = None
            if not position.get("sold_at"):
                _, market = _market_for_item(
                    results, position["item"], position.get("qlt"), position.get("ptn"),
                    position.get("amount"),
                )
            current = market.get("current_price") if market else None
            position["market"] = market
            position["current_price"] = current
            position["invested"] = round(position["buy_price"] * position["amount"], 2)
            if position.get("sold_at"):
                position["status"] = "sold"
                position["profit"] = round(
                    (position["sold_price"] * (1.0 - commission) - position["buy_price"])
                    * position["amount"],
                    2,
                )
                realized += position["profit"]
            else:
                position["status"] = "open"
                invested += position["invested"]
                if current:
                    position["current_value"] = round(current * position["amount"], 2)
                    position["profit"] = round(
                        (current * (1.0 - commission) - position["buy_price"]) * position["amount"],
                        2,
                    )
                    current_value += position["current_value"]
                    unrealized += position["profit"]
                else:
                    position["current_value"] = None
                    position["profit"] = None
            position["roi_pct"] = (
                round(position["profit"] / position["invested"] * 100, 1)
                if position.get("profit") is not None and position["invested"]
                else None
            )
            output.append(position)
        self._json(
            200,
            {
                "positions": output,
                "summary": {
                    "open_count": len([p for p in output if p["status"] == "open"]),
                    "sold_count": len([p for p in output if p["status"] == "sold"]),
                    "invested": round(invested, 2),
                    "current_value": round(current_value, 2),
                    "unrealized": round(unrealized, 2),
                    "realized": round(realized, 2),
                    "total_profit": round(unrealized + realized, 2),
                },
                "commission_pct": round(commission * 100, 1),
            },
        )

    def _portfolio_buy(self) -> None:
        portfolio = self.state.portfolio
        if portfolio is None:
            self._error(503, "Портфель недоступен")
            return
        payload = self._read_json()
        item = str(payload.get("item") or "").strip()
        if not ITEM_ID_RE.match(item):
            self._error(400, "Некорректный ID предмета")
            return
        try:
            buy_price = float(payload.get("buy_price") or 0)
            amount = int(payload.get("amount") or 0)
            target = float(payload.get("target_price") or 0)
        except (TypeError, ValueError):
            self._error(400, "Некорректная цена или количество")
            return
        if buy_price <= 0 or amount <= 0:
            self._error(400, "Цена и количество должны быть положительными")
            return
        name = str(payload.get("name") or item)[:200]
        position_id = portfolio.add(
            item=item,
            name=name,
            buy_price=buy_price,
            amount=amount,
            qlt=self._optional_int(payload.get("qlt")),
            ptn=self._optional_int(payload.get("ptn")),
            target_price=target if target > 0 else None,
            note=str(payload.get("note") or "")[:500],
        )
        self._json(200, {"ok": True, "id": position_id})

    def _portfolio_sell(self) -> None:
        portfolio = self.state.portfolio
        if portfolio is None:
            self._error(503, "Портфель недоступен")
            return
        payload = self._read_json()
        position_id = self._optional_int(payload.get("id"))
        try:
            price = float(payload.get("price") or 0)
        except (TypeError, ValueError):
            price = 0
        if not position_id or price <= 0:
            self._error(400, "Некорректная позиция или цена")
            return
        if not portfolio.sell(position_id, price):
            self._error(404, "Позиция не найдена или уже продана")
            return
        self._json(200, {"ok": True})

    def _portfolio_remove(self) -> None:
        portfolio = self.state.portfolio
        if portfolio is None:
            self._error(503, "Портфель недоступен")
            return
        payload = self._read_json()
        position_id = self._optional_int(payload.get("id"))
        if not position_id or not portfolio.remove(position_id):
            self._error(404, "Позиция не найдена")
            return
        self._json(200, {"ok": True})

    # ------------------------------------------------------------- аналитика

    def _analytics(self, query) -> None:
        item = (query.get("item") or [""])[0].strip()
        if not ITEM_ID_RE.match(item):
            self._error(400, "Некорректный ID предмета")
            return
        try:
            days = int((query.get("days") or ["7"])[0])
        except ValueError:
            days = 7
        days = max(1, min(90, days))

        raw_variant = (query.get("variant") or [""])[0].strip()
        exact = False
        qlt = ptn = None
        if raw_variant:
            left, _, right = raw_variant.partition(":")
            try:
                qlt = None if left in ("", "null") else int(left)
                ptn = None if right in ("", "null") else int(right)
            except ValueError:
                self._error(400, "Некорректный вариант")
                return
            exact = True

        now = datetime.now(timezone.utc)
        cutoff = now - timedelta(days=days)
        history = self.state.history
        variants = []
        series = []
        recent = []
        history_error = None
        if history is None:
            history_error = "Локальная история недоступна"
        else:
            try:
                variants = history.variants(item, cutoff)
                if not exact and variants:
                    qlt = variants[0]["qlt"]
                    ptn = variants[0]["ptn"]
                    exact = True
                if exact:
                    series = history.series(item, cutoff, qlt=qlt, ptn=ptn)
                    recent = history.recent_sales(item, cutoff, limit=30, qlt=qlt, ptn=ptn)
                else:
                    series = history.series(item, cutoff)
                    recent = history.recent_sales(item, cutoff, limit=30)
            except Exception as exc:
                history_error = str(exc)

        meta = self.state.catalog.get(item)
        name = item
        category = "other"
        if meta:
            name = meta["name"].get("ru") or meta["name"].get("en") or item
            category = meta.get("category", "other")

        results = self.state.manager.results or {}
        item_result = next((row for row in results.get("items", []) if row.get("id") == item), None)
        groups = (item_result or {}).get("groups", [])
        group = None
        if exact:
            group = next((g for g in groups if g.get("qlt") == qlt and g.get("ptn") == ptn), None)
        elif groups:
            group = next((g for g in groups if g.get("market")), None)
        market = group.get("market") if group else None
        stats = None
        if history is not None:
            try:
                if exact:
                    stats = history.summary(item, cutoff, qlt=qlt, ptn=ptn)
                else:
                    stats = history.summary(item, cutoff)
            except Exception as exc:
                history_error = history_error or str(exc)
        coverage = (item_result or {}).get("coverage")

        commission = float(self.state.store.snapshot().get("commission_pct", 5)) / 100.0
        sales_per_day = round(stats["volume"] / days, 2) if stats and stats.get("volume") else None
        recommendation = None
        if market:
            market_min = market.get("min")
            market_price = market.get("p25") or market.get("median")
            fast = round(market_min * 0.99, 2) if market_min else None
            recommendation = {
                "fast": fast,
                "fast_net": round(fast * (1.0 - commission), 2) if fast else None,
                "market": market_price,
                "market_net": round(market_price * (1.0 - commission), 2) if market_price else None,
                "commission_pct": round(commission * 100, 1),
                "sales_per_day": sales_per_day,
                "days_to_sell_one": round(1.0 / sales_per_day, 1) if sales_per_day else None,
            }

        self._json(
            200,
            {
                "item": item,
                "name": name,
                "category": category,
                "days": days,
                "variants": variants,
                "selected": {"qlt": qlt, "ptn": ptn},
                "series": series,
                "recent": recent,
                "market": market,
                "depth": (group.get("depth") if group else []) or [],
                "stats": stats,
                "coverage": coverage,
                "recommendation": recommendation,
                "history_error": history_error,
            },
        )

    # ------------------------------------------------------------- проверка

    def _check_credentials(self) -> None:
        config = self.state.store.snapshot()
        if not config.get("demo") and not (config.get("client_id") and config.get("client_secret")):
            self._json(
                400,
                {
                    "ok": False,
                    "error": "Заполните Client-Id и Client-Secret (или включите демо-режим).",
                },
            )
            return

        client = make_client(config, CACHE_DIR)
        try:
            regions = client.get_regions()
        except ApiError as exc:
            self._json(400, {"ok": False, "error": f"API недоступен: {exc}", "status_code": exc.status})
            return

        # /regions публичный и не доказывает, что ключи рабочие. Настоящая проверка —
        # запрос аукциона, он требует авторизации приложения.
        item = "y1q9" if client.demo else "1kdg"
        try:
            lots = client.get_lots(item, limit=1, additional=True, force=True)
        except ApiError as exc:
            self._json(
                400,
                {
                    "ok": False,
                    "error": f"Ключи не приняты API: {exc}",
                    "status_code": exc.status,
                },
            )
            return

        self._json(
            200,
            {
                "ok": True,
                "mode": "demo" if client.demo else "live",
                "region": client.region,
                "regions": regions,
                "limits": dict(client.stats),
                "sample": {"item": item, "total": lots.get("total"), "ok": True},
            },
        )

    # ------------------------------------------------------------- файлы

    def _serve_file(self, path: Path) -> None:
        try:
            resolved = path.resolve()
            if not resolved.is_relative_to(STATIC_DIR.resolve()):
                self._error(403, "Запрещено")
                return
            if not resolved.is_file():
                self._error(404, "Не найдено")
                return
            content = resolved.read_bytes()
        except OSError:
            self._error(404, "Не найдено")
            return
        content_types = {
            ".html": "text/html; charset=utf-8",
            ".css": "text/css; charset=utf-8",
            ".js": "application/javascript; charset=utf-8",
            ".svg": "image/svg+xml",
            ".ico": "image/x-icon",
            ".png": "image/png",
        }
        self.send_response(200)
        self.send_header("Content-Type", content_types.get(resolved.suffix.lower(), "application/octet-stream"))
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(content)

    def log_message(self, fmt: str, *args) -> None:
        # Не засоряем консоль каждым запросом.
        return


# --------------------------------------------------------------------- CLI


def print_report(results: dict) -> None:
    mode = "ДЕМО" if results.get("demo") else "БОЕВОЙ"
    print()
    print("=" * 78)
    print(
        f"SZAUCTRACKER · {results.get('region')} · окно {results.get('days')} дн. · "
        f"порог {results.get('threshold_pct')}% · {mode}"
    )
    print(
        f"Предметов: {results.get('items_total')} · сделок: {results.get('deals_count')} · "
        f"потенциал: {results.get('potential_profit'):,.0f} · {results.get('elapsed_sec')} с"
    )
    print("=" * 78)
    deals = results.get("deals") or []
    if not deals:
        print("Сделок не найдено.")
    for deal in deals:
        left = deal.get("time_left_min")
        left_text = f"{left // 60} ч {left % 60} мин" if left and left > 0 else "—"
        tags = " ".join(filter(None, [deal.get("qlt_label"), deal.get("ptn_label")]))
        print(
            f"[{deal['discount_pct']:5.1f}%] {deal['name']} ({deal['item_id']}) {tags}\n"
            f"         цена {deal['unit_price']:,.0f}/шт · база {deal['benchmark']:,.0f} · "
            f"профит {deal['net_unit']:,.0f}/шт · {deal['amount']} шт · "
            f"всего {deal['total_net']:,.0f} · продаж {deal['volume']} · осталось {left_text}"
        )
    errors = [item for item in results.get("items") or [] if item.get("status") == "error"]
    if errors:
        print("-" * 78)
        print("Ошибки:")
        for item in errors[:10]:
            print(f"  {item['id']} {item['name']}: {item.get('error')}")
    print()


def run_once(args) -> int:
    seed_bundled_data()
    store = Store(CONFIG_PATH)
    config = store.snapshot()
    if args.demo:
        config["demo"] = True
    if args.items:
        config["watchlist"] = _clean_watchlist(args.items.split(","))
    dead = DeadList(DEAD_PATH)
    if not args.items:
        dead_ids = set(dead.ids())
        config["watchlist"] = [item for item in config.get("watchlist") or [] if item not in dead_ids]
    if not config["watchlist"]:
        print("Все предметы помечены недействительными. Очистите список наблюдения.")
        return 2
    catalog = Catalog(CATALOG_PATH)
    catalog.load_sync()
    if not catalog.ready:
        log_line(f"Справочник не загружен: {catalog.error}")
    history = None
    try:
        history = HistoryStore(HISTORY_PATH)
    except Exception as exc:
        log_line(f"Локальная история недоступна: {exc}")
    market = None
    try:
        market = MarketStore(MARKET_PATH)
    except Exception as exc:
        log_line(f"Журнал лотов недоступен: {exc}")
    try:
        client = make_client(config, CACHE_DIR)
        results = scan(
            client, catalog, config, log=lambda msg: None,
            history_store=history, market_store=market,
        )
        invalid = [
            item["id"]
            for item in results.get("items", [])
            if item.get("status") == "error" and "Invalid item id" in (item.get("error") or "")
        ]
        if invalid:
            dead.add(invalid)
    except ApiError as exc:
        print(f"Ошибка: {exc}")
        return 2
    finally:
        if history is not None:
            history.close()
        if market is not None:
            market.close()
    print_report(results)
    return 0


class AppServer:
    """Сервер, которым можно управлять из GUI: start() / stop()."""

    def __init__(self, port: int = 8765, host: str = "127.0.0.1"):
        seed_bundled_data()
        self.host = host
        self.state = AppState()
        self.state.catalog.load_async()
        handler_class = type("AucTrackerHandler", (Handler,), {"state": self.state})

        self.httpd = None
        self.port = port
        for candidate in range(port, port + 20):
            try:
                self.httpd = ThreadingHTTPServer((host, candidate), handler_class)
                self.port = candidate
                break
            except OSError:
                continue
        if self.httpd is None:
            raise OSError(f"Не удалось занять порт {port}..{port + 19}")
        self.httpd.daemon_threads = True
        self._thread: threading.Thread | None = None
        self._auto_thread: threading.Thread | None = None

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}"

    def start(self) -> "AppServer":
        if self._thread and self._thread.is_alive():
            return self
        self._thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self._thread.start()
        self._auto_thread = threading.Thread(
            target=auto_loop,
            args=(self.state.manager, self.state.store, self.state.stop_event),
            daemon=True,
        )
        self._auto_thread.start()
        log_line(f"Сервер запущен на {self.url}")
        return self

    def stop(self) -> None:
        self.state.stop_event.set()
        self.state.manager.cancel()
        try:
            self.httpd.shutdown()
        except Exception:
            pass
        try:
            self.httpd.server_close()
        except Exception:
            pass
        try:
            self.state.close()
        except Exception:
            pass
        log_line("Сервер остановлен")

    def wait(self) -> None:
        if self._thread:
            self._thread.join()


def serve(args) -> int:
    try:
        server = AppServer(port=args.port)
    except OSError as exc:
        print(exc)
        return 1

    print()
    print("  SZAUCTRACKER")
    print(f"  Интерфейс: {server.url}")
    print("  Остановить: Ctrl+C")
    print()

    server.start()
    if not args.no_browser:
        threading.Timer(1.0, lambda: webbrowser.open(server.url)).start()
    try:
        server.wait()
    except KeyboardInterrupt:
        print("\nОстановка…")
    finally:
        server.stop()
    return 0


def main() -> int:
    # Windows-консоль может быть в cp866/cp1251 — не даём print упасть на «·» и «≈».
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    parser = argparse.ArgumentParser(description="SZAUCTRACKER — сканер выгодных лотов аукциона")
    parser.add_argument("--once", action="store_true", help="один скан и отчёт в консоли, без сервера")
    parser.add_argument("--demo", action="store_true", help="использовать демо-API (без ключей)")
    parser.add_argument("--items", help="ID предметов через запятую (для --once)")
    parser.add_argument("--port", type=int, default=8765, help="порт локального сервера (по умолчанию 8765)")
    parser.add_argument("--no-browser", action="store_true", help="не открывать браузер")
    args = parser.parse_args()
    if args.once:
        return run_once(args)
    return serve(args)


if __name__ == "__main__":
    raise SystemExit(main())

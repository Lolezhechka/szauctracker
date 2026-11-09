"""Тесты логики сканера и конфигурации. Запуск: python -m unittest -v"""

from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from barter import BarterBook, calculate_item
from catalog import Catalog
from history_store import HistoryStore
from market_store import MarketStore
from portfolio import PortfolioStore
from stalzone import BASE_DEMO, BASE_PROD, DiskCache
from scanner import analyze_item, artifact_percent, compute_stats, fetch_history, group_key, percent_band, scan, stack_band
from app import Store, _clean_watchlist, _compact_results, DeadList

NOW = datetime(2026, 10, 29, 12, 0, tzinfo=timezone.utc)
CUTOFF = NOW - timedelta(days=2)


def iso(dt: datetime) -> str:
    return dt.isoformat().replace("+00:00", "Z")


def sale(unit_price, amount=1, hours_ago=5, **additional):
    return {
        "amount": amount,
        "price": unit_price * amount,
        "time": iso(NOW - timedelta(hours=hours_ago)),
        "additional": additional,
    }


def lot(buyout_unit, amount=1, hours_left=10, **additional):
    return {
        "itemId": "test",
        "amount": amount,
        "startPrice": buyout_unit * amount,
        "buyoutPrice": buyout_unit * amount,
        "currentPrice": 0,
        "startTime": iso(NOW - timedelta(hours=1)),
        "endTime": iso(NOW + timedelta(hours=hours_left)),
        "additional": additional,
    }


def cfg(**overrides):
    base = {
        "days": 2,
        "threshold_pct": 10,
        "commission_pct": 5,
        "benchmark": "median",
        "cap_by_market": True,
        "min_volume": 1,
        "min_price": 0,
        "max_price": 0,
        "min_profit": 0,
        "group_by_qlt": True,
        "group_by_ptn": True,
        "stack_qty": 100,
        "stack_threshold_pct": 10,
        "stack_min_sales": 1,
    }
    base.update(overrides)
    return base


def analyze(lots, history, **overrides):
    return analyze_item(
        "test", {"name": "Тестовый предмет", "category": "artefact/test"},
        lots, history, cfg(**overrides), NOW, CUTOFF, history_total=len(history),
    )


class StatsTests(unittest.TestCase):
    def test_weighted_stats(self):
        records = [
            {"unit": 1000.0, "amount": 1, "time": NOW - timedelta(hours=1)},
            {"unit": 2000.0, "amount": 3, "time": NOW - timedelta(hours=2)},
        ]
        stats = compute_stats(records)
        self.assertEqual(stats["volume"], 4)
        self.assertEqual(stats["mean"], 1750.0)
        self.assertEqual(stats["median"], 2000.0)
        self.assertEqual(stats["p25"], 1000.0)

    def test_empty(self):
        self.assertIsNone(compute_stats([]))


class DealDetectionTests(unittest.TestCase):
    def test_cheap_lot_is_deal(self):
        history = [sale(1000) for _ in range(5)]
        result = analyze([lot(800)], history)
        self.assertEqual(len(result["deals"]), 1)
        deal = result["deals"][0]
        self.assertAlmostEqual(deal["discount_pct"], 20.0, places=1)
        self.assertAlmostEqual(deal["net_unit"], 150.0, places=1)  # 1000*0.95 - 800
        self.assertAlmostEqual(deal["total_net"], 150.0, places=1)
        self.assertEqual(deal["qlt_label"], None)
        self.assertEqual(result["status"], "ok")

    def test_threshold_not_met(self):
        history = [sale(1000) for _ in range(5)]
        result = analyze([lot(950)], history)
        self.assertEqual(result["deals"], [])
        self.assertEqual(result["status"], "ok")

    def test_exact_threshold_passes(self):
        history = [sale(1000) for _ in range(5)]
        result = analyze([lot(900)], history)
        self.assertEqual(len(result["deals"]), 1)

    def test_per_unit_normalization(self):
        history = [sale(1000, amount=2) for _ in range(3)]  # 2000 за лот из 2 шт.
        result = analyze([lot(800, amount=3)], history)   # 2400 за лот из 3 шт.
        deal = result["deals"][0]
        self.assertAlmostEqual(deal["unit_price"], 800.0)
        self.assertAlmostEqual(deal["benchmark"], 1000.0)
        self.assertAlmostEqual(deal["total_net"], 450.0)  # 150 * 3

    def test_commission_can_kill_deal(self):
        history = [sale(1000) for _ in range(5)]
        result = analyze([lot(900)], history, commission_pct=15)
        self.assertEqual(result["deals"], [])

    def test_min_volume(self):
        history = [sale(1000) for _ in range(2)]
        result = analyze([lot(500)], history, min_volume=5)
        self.assertEqual(result["deals"], [])

    def test_expired_lot_skipped(self):
        history = [sale(1000) for _ in range(5)]
        result = analyze([lot(500, hours_left=-1)], history)
        self.assertEqual(result["deals"], [])
        self.assertEqual(result["status"], "no_lots")

    def test_bid_only_lot_skipped(self):
        history = [sale(1000) for _ in range(5)]
        bid_lot = lot(0)
        bid_lot["buyoutPrice"] = 0
        bid_lot["currentPrice"] = 500
        result = analyze([bid_lot], history)
        self.assertEqual(result["deals"], [])

    def test_old_sales_ignored(self):
        history = [sale(1000, hours_ago=24 * 3) for _ in range(5)]
        result = analyze([lot(500)], history)
        self.assertEqual(result["deals"], [])
        self.assertEqual(result["status"], "no_sales")

    def test_no_history(self):
        result = analyze([lot(500)], [])
        self.assertEqual(result["status"], "no_data")

    def test_price_filter(self):
        history = [sale(1000) for _ in range(5)]
        result = analyze([lot(500)], history, min_price=600)
        self.assertEqual(result["deals"], [])
        result = analyze([lot(500)], history, max_price=400)
        self.assertEqual(result["deals"], [])

    def test_benchmark_choice_changes_result(self):
        history = [sale(1000) for _ in range(3)] + [sale(5000)]
        # медиана 1000 → лот за 950 дешевле лишь на 5%
        self.assertEqual(analyze([lot(950)], history, benchmark="median")["deals"], [])
        # средняя 2000 → тот же лот дешевле на 52,5%
        self.assertEqual(len(analyze([lot(950)], history, benchmark="mean")["deals"]), 1)


class GroupingTests(unittest.TestCase):
    def test_quality_groups_are_separate(self):
        history = [sale(1000, qlt=3) for _ in range(5)]
        result = analyze([lot(700, qlt=2)], history)
        self.assertEqual(result["deals"], [])
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["history_in_window"], 5)

    def test_same_quality_is_deal(self):
        history = [sale(1000, qlt=3) for _ in range(5)]
        result = analyze([lot(700, qlt=3)], history)
        self.assertEqual(len(result["deals"]), 1)
        self.assertEqual(result["deals"][0]["qlt_label"], "Редкий")

    def test_upgrade_level_groups_are_separate(self):
        history = [sale(1000, ptn=15) for _ in range(5)]
        result = analyze([lot(700, ptn=0)], history)
        self.assertEqual(result["deals"], [])

    def test_grouping_can_be_disabled(self):
        history = [sale(1000, qlt=3) for _ in range(5)]
        result = analyze([lot(700, qlt=2)], history, group_by_qlt=False)
        self.assertEqual(len(result["deals"]), 1)

    def test_group_key(self):
        self.assertEqual(group_key("abc", {"qlt": 3, "ptn": 7}, cfg()), "abc|q3|p7")
        self.assertEqual(group_key("abc", {}, cfg()), "abc")


class StoreTests(unittest.TestCase):
    def test_sanitize_and_watchlist(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "config.json")
            config = store.update(
                {
                    "region": "XX",
                    "days": 999,
                    "threshold_pct": -5,
                    "client_id": "  42  ",
                    "watchlist": ["1kdg", "1kdg", "bad id", "04yr", "x"],
                }
            )
            self.assertEqual(config["region"], "RU")
            self.assertEqual(config["days"], 30)
            self.assertEqual(config["threshold_pct"], 0.5)
            self.assertEqual(config["client_id"], "42")
            self.assertEqual(config["watchlist"], ["1kdg", "04yr", "x"])

    def test_scan_optimization_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "config.json")
            self.assertEqual(store.snapshot()["scan_workers"], 10)
            self.assertEqual(store.snapshot()["history_ttl_min"], 30)
            self.assertEqual(store.snapshot()["min_price"], 5000)
            self.assertEqual(store.snapshot()["min_profit"], 5000)
            self.assertEqual(store.snapshot()["lots_pages"], 2)
            self.assertEqual(store.snapshot()["history_pages"], 5)
            self.assertTrue(store.snapshot()["group_by_stack"])
            self.assertTrue(store.snapshot()["group_by_percent"])
            self.assertTrue(store.snapshot()["discovery_enabled"])
            self.assertEqual(store.snapshot()["discovery_limit"], 10)
            store.update({"scan_workers": 99, "history_ttl_min": 0, "history_pages": 99, "lots_pages": 0, "discovery_limit": 99})
            self.assertEqual(store.snapshot()["scan_workers"], 16)
            self.assertEqual(store.snapshot()["history_ttl_min"], 1)
            self.assertEqual(store.snapshot()["history_pages"], 10)
            self.assertEqual(store.snapshot()["lots_pages"], 1)
            self.assertEqual(store.snapshot()["discovery_limit"], 50)

    def test_cap_flag(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "config.json")
            self.assertTrue(store.snapshot()["cap_by_market"])
            store.update({"cap_by_market": False})
            self.assertFalse(store.snapshot()["cap_by_market"])

    def test_secret_is_kept_when_blank(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "config.json")
            store.update({"client_id": "1", "client_secret": "secret"})
            store.update({"client_id": "2"})
            config = store.snapshot()
            self.assertEqual(config["client_secret"], "secret")
            store.update({"clear_secret": True})
            self.assertEqual(store.snapshot()["client_secret"], "")

    def test_watch_add_remove(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "config.json")
            store.update({"watchlist": []})
            store.watch(add=["1kdg", "04yr"])
            store.watch(remove=["1kdg"])
            self.assertEqual(store.snapshot()["watchlist"], ["04yr"])
            store.watch(clear=True)
            self.assertEqual(store.snapshot()["watchlist"], [])

    def test_invalid_id_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "config.json")
            with self.assertRaises(ValueError):
                store.watch(add=["<script>"])

    def test_watchlist_limit(self):
        ids = [f"id{i}" for i in range(600)]
        self.assertEqual(len(_clean_watchlist(ids)), 500)

    def test_watch_add_category(self):
        with tempfile.TemporaryDirectory() as tmp:
            catalog = Catalog(Path(tmp) / "catalog.json")
            catalog._install(
                [
                    {"id": "a1", "category": "artefact/x", "name": {"ru": "A1"}},
                    {"id": "a2", "category": "artefact/x", "name": {"ru": "A2"}},
                    {"id": "w1", "category": "weapon/x", "name": {"ru": "W1"}},
                ]
            )
            store = Store(Path(tmp) / "config.json")
            store.update({"watchlist": []})
            result = store.watch(add_category="artefact", limit=2, catalog=catalog)
            self.assertEqual(result["count"], 2)
            self.assertIn("a1", result["watchlist"])
            self.assertIn("a2", result["watchlist"])
            self.assertNotIn("w1", result["watchlist"])


class CatalogTests(unittest.TestCase):
    def test_search_and_normalization(self):
        with tempfile.TemporaryDirectory() as tmp:
            catalog = Catalog(Path(tmp) / "catalog.json")
            catalog._install(
                [
                    {"id": "1kdg", "category": "artefact/biochemical", "name": {"ru": "Липкий репях", "en": "Sticky Burr"}},
                    {"id": "g34p", "category": "artefact/biochemical", "name": {"ru": "Юла", "en": "Spinner"}},
                    {"id": "5rd1", "category": "artefact/biochemical", "name": {"ru": "Стальной Ёжик", "en": "Steel Hedgehog"}},
                ]
            )
            self.assertEqual(catalog.search("юла")[0]["id"], "g34p")
            self.assertEqual(catalog.search("ежик")[0]["id"], "5rd1")  # ё → е
            self.assertEqual(catalog.search("1kdg")[0]["id"], "1kdg")
            self.assertEqual(len(catalog.search("", category="artefact")), 3)
            self.assertEqual(catalog.display_name("1kdg"), "Липкий репях")
            self.assertEqual(catalog.display_name("unknown"), "unknown")

    def test_persistence_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "catalog.json"
            first = Catalog(path)
            first._install([{"id": "abc", "category": "other", "name": {"ru": "Тест"}}])
            first._write_cache(first.items)
            second = Catalog(path)
            second.load_sync()
            self.assertTrue(second.ready)
            self.assertEqual(second.display_name("abc"), "Тест")


class SerializationTests(unittest.TestCase):
    def test_result_is_json_serializable(self):
        history = [sale(1000, qlt=3) for _ in range(5)]
        result = analyze([lot(700, qlt=3)], history)
        encoded = json.dumps(result, ensure_ascii=False)
        decoded = json.loads(encoded)
        self.assertEqual(len(decoded["deals"]), 1)
        self.assertIsInstance(decoded["groups"][0]["stats"]["last_sale"], str)


class MarketAndCoverageTests(unittest.TestCase):
    def test_market_stats(self):
        lots = [lot(600), lot(800), lot(1500)]
        result = analyze(lots, [sale(1000) for _ in range(3)])
        group = result["groups"][0]
        self.assertEqual(group["market"]["count"], 3)
        self.assertEqual(group["market"]["min"], 600)
        self.assertEqual(group["market"]["median"], 800)
        self.assertEqual(result["deals"][0]["market_median"], 800)
        self.assertEqual(result["deals"][0]["market_lots"], 3)

    def test_lots_benchmark(self):
        lots = [lot(600), lot(800), lot(1500)]
        result = analyze(lots, [sale(1000) for _ in range(3)], benchmark="lots")
        self.assertEqual(result["deals"][0]["benchmark"], 800)
        self.assertAlmostEqual(result["deals"][0]["discount_pct"], 25.0)
        self.assertAlmostEqual(result["deals"][0]["net_unit"], 160.0)  # 800*0.95 - 600

    def test_blend_benchmark(self):
        lots = [lot(600), lot(800), lot(1500)]
        result = analyze(lots, [sale(1000) for _ in range(3)], benchmark="blend")
        self.assertEqual(result["deals"][0]["benchmark"], 900)
        self.assertAlmostEqual(result["deals"][0]["net_unit"], 255.0)  # 900*0.95 - 600

    def test_lots_benchmark_without_sales(self):
        result = analyze([lot(600), lot(800)], [], benchmark="lots")
        self.assertEqual(len(result["deals"]), 1)
        deal = result["deals"][0]
        self.assertEqual(deal["volume"], 0)
        self.assertEqual(deal["reliability"], "low")
        self.assertIsNone(deal["median"])

    def test_coverage_limited_by_200_sales(self):
        history = [sale(1000, hours_ago=i * 0.01) for i in range(200)]
        result = analyze([lot(500)], history)
        coverage = result["coverage"]
        self.assertTrue(coverage["limited"])
        self.assertEqual(coverage["records"], 200)
        self.assertLess(coverage["span_hours"], 48)

    def test_coverage_sparse_but_full_window(self):
        history = [sale(1000, hours_ago=i * 4) for i in range(10)]  # 36 часов
        result = analyze([lot(500)], history)
        coverage = result["coverage"]
        self.assertFalse(coverage["limited"])
        self.assertEqual(coverage["source"], "api")

    def test_missing_pattern_label(self):
        result = analyze([lot(800)], [sale(1000) for _ in range(3)])
        self.assertEqual(result["deals"][0]["ptn_label"], "без паттерна")


class HistoryStoreTests(unittest.TestCase):
    def _rows(self):
        base = datetime.now(timezone.utc)
        return [
            {"time": iso(base - timedelta(hours=1)), "price": 1000, "amount": 1, "additional": {"qlt": 3}},
            {"time": iso(base - timedelta(hours=2)), "price": 2000, "amount": 2, "additional": {"qlt": 3}},
            {"time": iso(base - timedelta(days=5)), "price": 500, "amount": 1, "additional": {}},
        ]

    def test_insert_and_dedup(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = HistoryStore(Path(tmp) / "h.db")
            try:
                rows = self._rows()
                self.assertEqual(store.add_sales("abc", rows), 3)
                self.assertEqual(store.total("abc"), 3)
                self.assertEqual(store.add_sales("abc", rows), 0)
                self.assertEqual(store.total("abc"), 3)
            finally:
                store.close()

    def test_window_filter(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = HistoryStore(Path(tmp) / "h.db")
            try:
                store.add_sales("abc", self._rows())
                cutoff = datetime.now(timezone.utc) - timedelta(days=2)
                rows = store.get_sales("abc", cutoff)
                self.assertEqual(len(rows), 2)
                self.assertEqual(sum(r["amount"] for r in rows), 3)
                self.assertEqual(rows[0]["additional"]["qlt"], 3)
            finally:
                store.close()

    def test_coverage(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = HistoryStore(Path(tmp) / "h.db")
            try:
                store.add_sales("abc", self._rows())
                cutoff = datetime.now(timezone.utc) - timedelta(days=2)
                coverage = store.coverage("abc", cutoff)
                self.assertEqual(coverage["records"], 2)
                self.assertEqual(coverage["units"], 3)
                self.assertAlmostEqual(coverage["span_hours"], 1.0, places=1)
            finally:
                store.close()

    def test_prune(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = HistoryStore(Path(tmp) / "h.db")
            try:
                store.add_sales("abc", self._rows())
                store.prune(keep_days=2)
                store.vacuum()
                self.assertEqual(store.total("abc"), 2)
            finally:
                store.close()

    def test_variants_series_recent(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = HistoryStore(Path(tmp) / "h.db")
            try:
                store.add_sales("abc", self._rows())
                cutoff = datetime.now(timezone.utc) - timedelta(days=2)
                variants = store.variants("abc", cutoff)
                self.assertEqual(len(variants), 1)
                self.assertEqual(variants[0]["qlt"], 3)
                self.assertEqual(variants[0]["units"], 3)
                series = store.series("abc", cutoff, qlt=3, ptn=None)
                self.assertEqual(len(series), 1)
                self.assertEqual(series[0]["units"], 3)
                self.assertAlmostEqual(series[0]["median"], 1000.0)
                recent = store.recent_sales("abc", cutoff, qlt=3, ptn=None)
                self.assertEqual(len(recent), 2)
                self.assertTrue(all(row["qlt"] == 3 for row in recent))
                summary = store.summary("abc", cutoff, qlt=3, ptn=None)
                self.assertEqual(summary["volume"], 3)
                self.assertEqual(summary["sales_count"], 2)
                self.assertAlmostEqual(summary["median"], 1000.0)
            finally:
                store.close()


class MarketCapTests(unittest.TestCase):
    def test_cap_uses_competing_lots(self):
        history = [sale(1000) for _ in range(5)]
        result = analyze([lot(700), lot(900), lot(950)], history)
        self.assertEqual(len(result["deals"]), 1)
        deal = result["deals"][0]
        self.assertEqual(deal["unit_price"], 700)
        self.assertEqual(deal["sales_base"], 1000)
        self.assertEqual(deal["market_cap"], 900)  # 25-й перцентиль конкурентов
        self.assertEqual(deal["benchmark"], 900)
        self.assertTrue(deal["capped"])
        self.assertAlmostEqual(deal["discount_pct"], 22.2)
        self.assertAlmostEqual(deal["net_unit"], 155.0)  # 900*0.95 - 700

    def test_cap_disabled(self):
        history = [sale(1000) for _ in range(5)]
        result = analyze([lot(700), lot(900), lot(950)], history, cap_by_market=False)
        by_price = {deal["unit_price"]: deal for deal in result["deals"]}
        self.assertIn(700.0, by_price)
        self.assertIn(900.0, by_price)  # ровно 10%
        self.assertEqual(by_price[700.0]["benchmark"], 1000)
        self.assertFalse(by_price[700.0]["capped"])

    def test_cap_ignored_for_thin_market(self):
        history = [sale(1000) for _ in range(5)]
        result = analyze([lot(800)], history)
        deal = result["deals"][0]
        self.assertEqual(deal["benchmark"], 1000)
        self.assertFalse(deal["capped"])
        self.assertIsNone(deal["market_cap"])

    def test_cap_two_competing_lots(self):
        history = [sale(1000) for _ in range(5)]
        result = analyze([lot(800), lot(900)], history)
        deal = next(item for item in result["deals"] if item["unit_price"] == 800)
        self.assertEqual(deal["market_cap"], 900)
        self.assertEqual(deal["benchmark"], 900)
        self.assertTrue(deal["capped"])

    def test_cap_not_applied_to_lots_benchmark(self):
        history = [sale(1000) for _ in range(5)]
        result = analyze([lot(700), lot(900), lot(950)], history, benchmark="lots")
        deal = next(item for item in result["deals"] if item["unit_price"] == 700)
        self.assertEqual(deal["benchmark"], 900)
        self.assertFalse(deal["capped"])


class RuleAndDepthTests(unittest.TestCase):
    def test_quality_filter(self):
        history = [sale(1000, qlt=3) for _ in range(3)] + [sale(500, qlt=2) for _ in range(3)]
        lots = [lot(800, qlt=3), lot(400, qlt=2)]
        result = analyze(lots, history, filter_qlt=3)
        self.assertEqual(len(result["deals"]), 1)
        self.assertEqual(result["deals"][0]["qlt"], 3)

    def test_bonus_filter(self):
        history = [sale(1000, qlt=3, bonus_properties=["SPEED_MOD"]) for _ in range(3)]
        lots = [
            lot(800, qlt=3, bonus_properties=["SPEED_MOD"]),
            lot(700, qlt=3, bonus_properties=["STAMINA_BONUS"]),
        ]
        result = analyze(lots, history, filter_bonus="SPEED_MOD")
        self.assertEqual(len(result["deals"]), 1)
        self.assertEqual(result["deals"][0]["unit_price"], 800)

    def test_depth_levels(self):
        result = analyze([lot(600), lot(800), lot(800, amount=2)], [sale(1000) for _ in range(3)])
        depth = result["groups"][0]["depth"]
        self.assertEqual([row["unit"] for row in depth], [600.0, 800.0])
        self.assertEqual(depth[1]["units"], 3)

    def test_item_rules_sanitized(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "config.json")
            store.update(
                {
                    "item_rules": {
                        "1kdg": {
                            "min_price": 1000,
                            "min_profit": 5000,
                            "qlt": 4,
                            "min_percent": 130,
                            "bonus": "SPEED_MOD",
                            "enabled": True,
                        },
                        "bad id": {"min_price": 1},
                    }
                }
            )
            rules = store.snapshot()["item_rules"]
            self.assertIn("1kdg", rules)
            self.assertNotIn("bad id", rules)
            self.assertEqual(rules["1kdg"]["qlt"], 4)
            store.set_rule("04yr", {"min_price": 2000})
            self.assertIn("04yr", store.snapshot()["item_rules"])
            store.set_rule("04yr", {})
            self.assertNotIn("04yr", store.snapshot()["item_rules"])


class ArtifactPercentTests(unittest.TestCase):
    def test_formula(self):
        self.assertAlmostEqual(artifact_percent({"qlt": 3, "stats_random": -0.5}), 135.6, places=1)
        self.assertAlmostEqual(artifact_percent({"qlt": 4, "stats_random": 1.5390141}), 153.3, places=1)
        self.assertAlmostEqual(artifact_percent({"qlt": 0, "stats_random": 0}), 50.0, places=1)

    def test_missing_or_invalid(self):
        self.assertIsNone(artifact_percent({}))
        self.assertIsNone(artifact_percent({"qlt": 3}))
        self.assertIsNone(artifact_percent({"qlt": 9, "stats_random": 0}))
        self.assertAlmostEqual(artifact_percent({"qlt": 3, "stats_random": 99}), 145.0, places=1)


class DealAggregationTests(unittest.TestCase):
    def test_identical_lots_aggregated(self):
        result = analyze([lot(800), lot(800), lot(1500)], [sale(1000) for _ in range(5)])
        self.assertEqual(len(result["deals"]), 1)
        deal = result["deals"][0]
        self.assertEqual(deal["amount"], 2)
        self.assertEqual(deal["lots_count"], 2)
        self.assertAlmostEqual(deal["total_net"], 300.0)  # 150 * 2
        self.assertTrue(deal["aggregated"])

    def test_all_equal_market_is_not_a_deal(self):
        result = analyze([lot(800), lot(800)], [sale(1000) for _ in range(5)], min_profit=0)
        self.assertEqual(result["deals"], [])

    def test_min_profit_applies_to_aggregate(self):
        lots = [lot(900), lot(900), lot(1500)]
        history = [sale(1000) for _ in range(5)]
        hidden = analyze(lots, history, min_profit=101)
        self.assertEqual(hidden["deals"], [])
        shown = analyze(lots, history, min_profit=100)
        self.assertEqual(len(shown["deals"]), 1)
        self.assertAlmostEqual(shown["deals"][0]["total_net"], 100.0)


class MinProfitTests(unittest.TestCase):
    def test_small_profit_hidden(self):
        history = [sale(1000) for _ in range(5)]
        result = analyze([lot(900)], history, min_profit=100)
        self.assertEqual(result["deals"], [])
        result = analyze([lot(900)], history, min_profit=0)
        self.assertEqual(len(result["deals"]), 1)
        self.assertAlmostEqual(result["deals"][0]["total_net"], 50.0)

    def test_stack_small_profit_hidden(self):
        lots = [lot(120) for _ in range(10)] + [lot(80, amount=10)]
        history = [sale(120) for _ in range(10)] + [sale(80, amount=5) for _ in range(2)]
        result = analyze(lots, history, stack_qty=10, min_profit=500)
        self.assertEqual(result["stacks"], [])
        result = analyze(lots, history, stack_qty=10, min_profit=0)
        self.assertEqual(len(result["stacks"]), 1)


class MarketFilterTests(unittest.TestCase):
    def test_low_outlier_filtered(self):
        result = analyze([lot(100), lot(100), lot(100)], [sale(1), sale(100), sale(100), sale(100)])
        group = result["groups"][0]
        self.assertEqual(group["sales_filtered"], 1)
        self.assertEqual(group["stats"]["volume"], 3)
        self.assertEqual(group["stats"]["median"], 100)

    def test_high_outlier_filtered(self):
        result = analyze(
            [lot(100), lot(100), lot(100)],
            [sale(100), sale(100), sale(100), sale(10000)],
        )
        group = result["groups"][0]
        self.assertEqual(group["sales_filtered"], 1)
        self.assertEqual(group["stats"]["median"], 100)

    def test_no_filter_for_thin_market(self):
        result = analyze([lot(100)], [sale(1), sale(100)])
        group = result["groups"][0]
        self.assertEqual(group["sales_filtered"], 0)
        self.assertEqual(group["stats"]["volume"], 2)


class StackArbitrageTests(unittest.TestCase):
    def test_bulk_cheaper_than_singles(self):
        lots = [lot(120) for _ in range(10)] + [lot(80, amount=10)]
        history = [sale(120) for _ in range(10)] + [sale(80, amount=5) for _ in range(2)]
        result = analyze(lots, history, stack_qty=10)
        self.assertEqual(len(result["stacks"]), 1)
        stack = result["stacks"][0]
        self.assertEqual(stack["direction"], "bulk_to_singles")
        self.assertEqual(stack["qty"], 10)
        self.assertAlmostEqual(stack["buy_cost"], 800.0)
        self.assertAlmostEqual(stack["sell_unit"], 120.0)
        self.assertAlmostEqual(stack["profit"], 340.0)  # 1200*0.95 - 800
        self.assertAlmostEqual(stack["margin_pct"], 42.5)
        self.assertTrue(stack["buy_single_lot"])

    def test_singles_cheaper_than_bulk(self):
        lots = [lot(80) for _ in range(10)] + [lot(120, amount=10)]
        history = [sale(80) for _ in range(10)] + [sale(120, amount=5) for _ in range(2)]
        result = analyze(lots, history, stack_qty=10)
        self.assertEqual(len(result["stacks"]), 1)
        stack = result["stacks"][0]
        self.assertEqual(stack["direction"], "singles_to_bulk")
        self.assertAlmostEqual(stack["buy_cost"], 800.0)
        self.assertAlmostEqual(stack["sell_unit"], 120.0)
        self.assertAlmostEqual(stack["profit"], 340.0)
        self.assertEqual(stack["sell_lots"], 1)
        self.assertEqual(stack["buy_lots_used"], 10)

    def test_insufficient_units(self):
        lots = [lot(80) for _ in range(5)] + [lot(120, amount=10)]
        history = [sale(80) for _ in range(10)] + [sale(120, amount=5) for _ in range(2)]
        result = analyze(lots, history, stack_qty=10)
        self.assertEqual(result["stacks"], [])
        stack = result["groups"][0]["stack"]
        self.assertTrue(any("поштучно доступно только 5" in reason for reason in stack["reasons"]))

    def test_no_sales_no_stack(self):
        lots = [lot(80) for _ in range(10)] + [lot(120, amount=10)]
        result = analyze(lots, [], stack_qty=10)
        self.assertEqual(result["stacks"], [])
        stack = result["groups"][0]["stack"]
        self.assertEqual(len(stack["directions"]), 0)
        self.assertTrue(stack["reasons"])

    def test_commission_kills_stack_deal(self):
        lots = [lot(120) for _ in range(10)] + [lot(108, amount=10)]
        history = [sale(120) for _ in range(10)] + [sale(108, amount=5) for _ in range(2)]
        result = analyze(lots, history, stack_qty=10, commission_pct=15)
        self.assertEqual(result["stacks"], [])

    def test_threshold_respected(self):
        lots = [lot(120) for _ in range(10)] + [lot(115, amount=10)]
        history = [sale(120) for _ in range(10)] + [sale(115, amount=5) for _ in range(2)]
        result = analyze(lots, history, stack_qty=10)
        self.assertEqual(result["stacks"], [])  # 4,2% < 10%

    def test_combined_bulk_lots(self):
        lots = [lot(120) for _ in range(10)] + [lot(70, amount=4) for _ in range(3)]
        history = [sale(120) for _ in range(12)] + [sale(70, amount=4) for _ in range(2)]
        result = analyze(lots, history, stack_qty=10)
        stack = next(item for item in result["stacks"] if item["direction"] == "bulk_to_singles")
        self.assertEqual(stack["qty"], 12)  # докупили 12, потому что лоты целые
        self.assertEqual(stack["buy_lots_used"], 3)
        self.assertFalse(stack["buy_single_lot"])
        self.assertAlmostEqual(stack["buy_cost"], 840.0)  # 3 * 280
        self.assertAlmostEqual(stack["profit"], 528.0)  # 12*120*0.95 - 840

    def test_large_single_lot_extra_units(self):
        lots = [lot(120) for _ in range(10)] + [lot(80, amount=150)]
        history = [sale(120) for _ in range(10)] + [sale(80, amount=50) for _ in range(2)]
        result = analyze(lots, history, stack_qty=100)
        stack = next(item for item in result["stacks"] if item["direction"] == "bulk_to_singles")
        self.assertEqual(stack["qty"], 150)
        self.assertTrue(stack["buy_single_lot"])
        self.assertAlmostEqual(stack["buy_cost"], 12000.0)
        self.assertAlmostEqual(stack["profit"], 5100.0)  # 150*120*0.95 - 12000

    def test_stack_result_is_json_serializable(self):
        lots = [lot(120) for _ in range(10)] + [lot(80, amount=10)]
        history = [sale(120) for _ in range(10)] + [sale(80, amount=5) for _ in range(2)]
        result = analyze(lots, history, stack_qty=10)
        encoded = json.dumps(result, ensure_ascii=False)
        decoded = json.loads(encoded)
        self.assertEqual(decoded["stacks"][0]["direction"], "bulk_to_singles")


class FakeClient:
    demo = False
    client_id = "1"
    client_secret = "secret"
    region = "RU"

    def __init__(self):
        self.stats = {"requests": 0, "units": 0, "remaining": None, "limit": None}
        self.history_items = []
        self.lot_items = []

    def get_lots(self, item, **kwargs):
        self.lot_items.append(item)
        if item == "nolots":
            return {"total": 0, "lots": []}
        now = datetime.now(timezone.utc)
        return {
            "total": 1,
            "lots": [
                {
                    "itemId": item,
                    "amount": 1,
                    "startPrice": 800,
                    "buyoutPrice": 800,
                    "currentPrice": 0,
                    "startTime": iso(now - timedelta(hours=1)),
                    "endTime": iso(now + timedelta(hours=5)),
                    "additional": {},
                }
            ],
        }

    def get_history(self, item, **kwargs):
        self.history_items.append(item)
        now = datetime.now(timezone.utc)
        return {
            "total": 2,
            "prices": [
                {
                    "amount": 1,
                    "price": 1000,
                    "time": iso(now - timedelta(hours=1)),
                    "additional": {},
                },
                {
                    "amount": 1,
                    "price": 1,
                    "time": "2020-01-01T00:00:00Z",
                    "additional": {},
                },
            ],
        }


class ScanIntegrationTests(unittest.TestCase):
    def test_parallel_scan_and_history_skip(self):
        with tempfile.TemporaryDirectory() as tmp:
            catalog = Catalog(Path(tmp) / "catalog.json")
            catalog._install(
                [
                    {"id": "a", "category": "other", "name": {"ru": "A"}},
                    {"id": "b", "category": "other", "name": {"ru": "B"}},
                    {"id": "nolots", "category": "other", "name": {"ru": "No lots"}},
                ]
            )
            history = HistoryStore(Path(tmp) / "history.db")
            client = FakeClient()
            try:
                results = scan(
                    client,
                    catalog,
                    cfg(watchlist=["a", "b", "nolots"], scan_workers=3, history_ttl_min=30, min_profit=0),
                    history_store=history,
                )
                self.assertEqual(results["items_total"], 3)
                self.assertEqual([item["id"] for item in results["items"]], ["a", "b", "nolots"])
                self.assertEqual(results["deals_count"], 2)
                self.assertEqual(results["scan_workers"], 3)
                self.assertEqual(results["history_db"]["new_sales"], 2)
                self.assertEqual(history.total(), 2)
                self.assertEqual(history.total("a"), 1)  # старая продажа 2020 не сохранена
                self.assertNotIn("nolots", client.history_items)
                self.assertTrue(results["items"][2]["history_skipped"])
            finally:
                history.close()


class DiscoveryScanTests(unittest.TestCase):
    def test_discovery_scans_items_outside_watchlist_and_advances_cursor(self):
        with tempfile.TemporaryDirectory() as tmp:
            catalog = Catalog(Path(tmp) / "catalog.json")
            catalog._install(
                [
                    {"id": "a", "category": "other", "name": {"ru": "A"}},
                    {"id": "b", "category": "other", "name": {"ru": "B"}},
                    {"id": "c", "category": "other", "name": {"ru": "C"}},
                    {"id": "d", "category": "other", "name": {"ru": "D"}},
                ]
            )
            client = FakeClient()
            results = scan(
                client, catalog,
                cfg(watchlist=["a"], scan_workers=2, min_profit=0),
                discovery_start=0,
                discovery_limit=2,
                dead_ids=["c"],
            )
            self.assertEqual(results["watch_items"], 1)
            self.assertEqual(results["discovery_items"], 2)
            self.assertEqual([item["id"] for item in results["items"]], ["a", "b", "d"])
            self.assertEqual(results["discovery_next"], 0)
            self.assertEqual(results["items"][0]["source"], "watch")
            self.assertEqual(results["items"][1]["source"], "discovery")
            self.assertEqual(results["discovery_deals_count"], 2)
            self.assertTrue(all(deal["source"] == "discovery" for deal in results["deals"] if deal["item_id"] != "a"))


class CompactResultsTests(unittest.TestCase):
    def test_compact_keeps_only_needed_item_fields(self):
        results = {
            "items": [
                {
                    "id": "a",
                    "name": "A",
                    "category": "other",
                    "status": "ok",
                    "deals": [{}, {}],
                    "groups": [
                        {"stats": {"volume": 5}, "min_lot_unit": 10, "best_discount_pct": 20},
                        {"stats": {"volume": 3}, "min_lot_unit": 7, "best_discount_pct": 10},
                    ],
                }
            ],
            "deals": [{}, {}],
            "stacks": [],
        }
        compact = _compact_results(results)
        item = compact["items"][0]
        self.assertEqual(item["volume"], 8)
        self.assertEqual(item["min_lot_unit"], 7)
        self.assertEqual(item["best_discount_pct"], 20)
        self.assertEqual(item["deals_count"], 2)
        self.assertNotIn("groups", item)
        self.assertEqual(compact["deals"], results["deals"])


class PortfolioTests(unittest.TestCase):
    def test_add_sell_remove(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = PortfolioStore(Path(tmp) / "p.db")
            try:
                position_id = store.add(
                    "1kdg", "Липкий репях", 1000.0, 2, qlt=3, ptn=5, target_price=1500.0
                )
                rows = store.list()
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0]["amount"], 2)
                self.assertEqual(rows[0]["qlt"], 3)
                self.assertEqual(rows[0]["ptn"], 5)
                self.assertTrue(store.sell(position_id, 1400.0))
                self.assertFalse(store.sell(position_id, 1400.0))
                rows = store.list()
                self.assertEqual(rows[0]["sold_price"], 1400.0)
                self.assertIsNotNone(rows[0]["sold_at"])
                self.assertTrue(store.remove(position_id))
                self.assertEqual(store.list(), [])
            finally:
                store.close()

    def test_remove_unknown(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = PortfolioStore(Path(tmp) / "p.db")
            try:
                self.assertFalse(store.remove(999))
            finally:
                store.close()


class DeadListTests(unittest.TestCase):
    def test_add_remove_clear(self):
        with tempfile.TemporaryDirectory() as tmp:
            dead = DeadList(Path(tmp) / "dead.json")
            dead.add(["a", "b"])
            self.assertEqual(dead.count(), 2)
            self.assertEqual(dead.ids(), ["a", "b"])
            dead.remove(["a"])
            self.assertEqual(dead.ids(), ["b"])
            dead.clear()
            self.assertEqual(dead.count(), 0)

    def test_expired_ids_dropped(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "dead.json"
            path.write_text(json.dumps({"old": time.time() - 10 * 86400}), encoding="utf-8")
            dead = DeadList(path)
            self.assertEqual(dead.ids(), [])


class ComparableBandTests(unittest.TestCase):
    def analyze_cat(self, lots, history, category="consumables/food", **overrides):
        return analyze_item(
            "test",
            {"name": "Тестовый предмет", "category": category},
            lots,
            history,
            cfg(**overrides),
            NOW,
            CUTOFF,
            history_total=len(history),
        )

    def test_stack_band_helpers(self):
        self.assertEqual(stack_band(1), "1")
        self.assertEqual(stack_band(2), "2-4")
        self.assertEqual(stack_band(9), "5-9")
        self.assertEqual(stack_band(10), "10-19")
        self.assertEqual(stack_band(49), "20-49")
        self.assertEqual(stack_band(50), "50+")
        self.assertEqual(percent_band(110), "110-119")
        self.assertEqual(percent_band(125), "120-129")
        self.assertIsNone(percent_band(None))

    def test_bulk_lot_is_not_compared_with_singles(self):
        history = [sale(1000) for _ in range(5)] + [sale(600, amount=50) for _ in range(5)]
        result = self.analyze_cat([lot(700, amount=50)], history)
        # Старая общая медиана ~800 дала бы ложную скидку 12,5%.
        self.assertEqual(result["deals"], [])

        result = self.analyze_cat([lot(500, amount=50)], history)
        self.assertEqual(len(result["deals"]), 1)
        deal = result["deals"][0]
        self.assertEqual(deal["benchmark"], 600)
        self.assertEqual(deal["amount_band"], "50+")
        self.assertEqual(deal["comparable_basis"], "amount:50+")

    def test_single_lot_still_uses_singles(self):
        history = [sale(1000) for _ in range(5)] + [sale(600, amount=50) for _ in range(5)]
        result = self.analyze_cat([lot(850)], history)
        self.assertEqual(len(result["deals"]), 1)
        deal = result["deals"][0]
        self.assertEqual(deal["benchmark"], 1000)
        self.assertEqual(deal["amount_band"], "1")
        self.assertFalse(deal["amount_fallback"])

    def test_bulk_without_comparable_band_is_skipped(self):
        history = [sale(1000) for _ in range(5)]
        result = self.analyze_cat([lot(500, amount=3)], history)
        self.assertEqual(result["deals"], [])

    def test_nearest_bulk_band_can_be_used(self):
        history = [sale(800, amount=20) for _ in range(3)]
        result = self.analyze_cat([lot(600, amount=10)], history)
        self.assertEqual(len(result["deals"]), 1)
        deal = result["deals"][0]
        self.assertEqual(deal["benchmark"], 800)
        self.assertEqual(deal["amount_band"], "10-19")
        self.assertTrue(deal["amount_fallback"])
        self.assertEqual(deal["comparable_basis"], "amount-near:10-19")

    def test_artifact_percent_bands_and_per_lot_metadata(self):
        history = [
            sale(1000, qlt=2, ptn=15, stats_random=-2) for _ in range(5)
        ] + [
            sale(1500, qlt=2, ptn=15, stats_random=2) for _ in range(5)
        ]
        lots = [
            lot(900, qlt=2, ptn=15, stats_random=-2, bonus_properties=["A"]),
            lot(900, qlt=2, ptn=15, stats_random=2, bonus_properties=["B"]),
        ]
        result = self.analyze_cat(lots, history, category="artefact/biochemical")
        self.assertEqual(len(result["deals"]), 2)
        by_percent = {deal["artifact_percent"]: deal for deal in result["deals"]}
        self.assertEqual(by_percent[110]["benchmark"], 1000)
        self.assertEqual(by_percent[125]["benchmark"], 1500)
        self.assertEqual(by_percent[110]["percent_band"], "110-119")
        self.assertEqual(by_percent[125]["percent_band"], "120-129")
        self.assertEqual(by_percent[110]["bonus_properties"], ["A"])
        self.assertEqual(by_percent[125]["bonus_properties"], ["B"])
        self.assertEqual(by_percent[110]["comparable_basis"], "percent:110-119")
        self.assertFalse(by_percent[110]["percent_fallback"])

    def test_artifact_without_percent_uses_unknown_only(self):
        history = [sale(1000, qlt=2, ptn=15) for _ in range(5)]
        result = self.analyze_cat(
            [lot(700, qlt=2, ptn=15)], history, category="artefact/biochemical"
        )
        self.assertEqual(len(result["deals"]), 1)
        deal = result["deals"][0]
        self.assertIsNone(deal["artifact_percent"])
        self.assertEqual(deal["comparable_basis"], "percent:unknown")
        self.assertFalse(deal["percent_fallback"])

    def test_missing_variant_and_restrictions_are_flagged(self):
        result = analyze(
            [lot(700, sell_is_blocked=1, personal_on_use=1)],
            [sale(1000) for _ in range(5)],
        )
        deal = result["deals"][0]
        self.assertTrue(deal["variant_unknown"])
        self.assertEqual(set(deal["restrictions"]), {"sell_is_blocked", "personal_on_use"})


class AdaptiveScoreTests(unittest.TestCase):
    def test_adaptive_benchmark_penalizes_falling_market(self):
        history = (
            [sale(1000, hours_ago=2) for _ in range(5)]
            + [sale(1200, hours_ago=30) for _ in range(5)]
        )
        median = analyze([lot(950)], history, benchmark="median")
        self.assertEqual(len(median["deals"]), 1)
        self.assertEqual(median["deals"][0]["benchmark"], 1100)
        adaptive = analyze([lot(950)], history, benchmark="adaptive")
        self.assertEqual(adaptive["deals"], [])
        group = adaptive["groups"][0]
        self.assertLess(group["stats"]["median"] if "stats" in group else 1100, 1200)
        # Поля тренда и адаптивной базы доступны даже без сделки.
        no_deal = analyze([lot(1200)], history, benchmark="adaptive")
        self.assertIsNone(no_deal["deals"][0] if no_deal["deals"] else None)

    def test_trend_and_sell_through_fields(self):
        history = [
            sale(1000, hours_ago=2) for _ in range(3)
        ] + [
            sale(1200, hours_ago=30) for _ in range(3)
        ]
        result = analyze([lot(700), lot(800), lot(900)], history)
        deal = result["deals"][0]
        self.assertEqual(deal["trend_pct"], -16.7)
        self.assertIsNotNone(deal["volatility_pct"])
        self.assertEqual(deal["supply_units"], 3)
        self.assertEqual(deal["queue_units"], 0)
        self.assertGreater(deal["sell_through_pct"], 0)
        self.assertIsNotNone(deal["expected_days"])
        self.assertGreater(deal["score"], 0)
        self.assertEqual(set(deal["score_parts"]), {"profit", "discount", "liquidity", "reliability", "competition", "trend"})
        self.assertIn(deal["score_label"], {"отличная", "хорошая", "средняя", "рискованная"})

    def test_queue_and_undercut_for_expensive_lot(self):
        history = [sale(1000, hours_ago=2) for _ in range(5)]
        result = analyze([lot(700), lot(800), lot(850), lot(950)], history, cap_by_market=False)
        deals = {deal["unit_price"]: deal for deal in result["deals"]}
        expensive = deals[850.0]
        self.assertEqual(expensive["queue_units"], 2)
        self.assertEqual(expensive["queue_lots"], 2)
        self.assertEqual(expensive["undercut_price"], 940.5)
        self.assertLess(deals[700.0]["score"], 100)

    def test_store_accepts_adaptive_benchmark(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "config.json")
            store.update({"benchmark": "adaptive"})
            self.assertEqual(store.snapshot()["benchmark"], "adaptive")
            store.update({"benchmark": "nonsense"})
            self.assertEqual(store.snapshot()["benchmark"], "adaptive")


class ApiHostTests(unittest.TestCase):
    def test_uses_current_net_host(self):
        self.assertTrue(BASE_PROD.endswith(".net"))
        self.assertTrue(BASE_DEMO.endswith(".net"))


class HistoryPaginationTests(unittest.TestCase):
    class FakeClient:
        def __init__(self, pages):
            self.pages = pages
            self.calls = []

        def get_history(self, item, limit=200, offset=0, additional=True, cache_ttl=600):
            self.calls.append(offset)
            index = offset // limit
            return {
                "total": 10,
                "prices": self.pages[index] if index < len(self.pages) else [],
            }

    def row(self, hours_ago):
        return {"time": iso(NOW - timedelta(hours=hours_ago)), "price": 1000, "amount": 1}

    def test_reads_pages_until_cutoff(self):
        page1 = [self.row(1), self.row(2)]
        page2 = [self.row(3), self.row(50)]
        client = self.FakeClient([page1, page2])
        prices, total = fetch_history(
            client, "test", cutoff=CUTOFF, max_pages=5, limit=2, cache_ttl=0
        )
        self.assertEqual(total, 10)
        self.assertEqual(len(prices), 4)
        self.assertEqual(client.calls, [0, 2])

    def test_duplicate_cached_page_does_not_loop(self):
        page = [self.row(1), self.row(2)]
        client = self.FakeClient([page, page])
        prices, total = fetch_history(
            client, "test", cutoff=None, max_pages=5, limit=2, cache_ttl=0
        )
        self.assertEqual(len(prices), 2)
        self.assertEqual(len(client.calls), 2)


class BarterTests(unittest.TestCase):
    class FakeClient:
        def __init__(self, lots, history):
            self.lots = lots
            self.history = history
            self.lot_calls = []
            self.history_calls = []

        def get_lots(self, item, **kwargs):
            self.lot_calls.append(item)
            return {"total": len(self.lots.get(item, [])), "lots": self.lots.get(item, [])}

        def get_history(self, item, **kwargs):
            self.history_calls.append(item)
            return {"total": len(self.history.get(item, [])), "prices": self.history.get(item, [])}

    def make_book(self):
        book = BarterBook("unused.json")
        book._install([
            {
                "settlementTitle": {"lines": {"ru": "Бар"}},
                "recipes": [
                    {
                        "item": "out",
                        "settlementRequiredLevel": 3,
                        "offers": [
                            {
                                "cost": 100,
                                "requiredItems": [{"item": "comp", "amount": 2}],
                            }
                        ],
                    }
                ],
            }
        ])
        return book

    def test_barter_book_and_calculation(self):
        now = datetime.now(timezone.utc)
        lots = {
            "out": [{"amount": 1, "buyoutPrice": 1000, "additional": {}}],
            "comp": [
                {"amount": 1, "buyoutPrice": 100, "additional": {}},
                {"amount": 1, "buyoutPrice": 110, "additional": {}},
            ],
        }
        history = {
            "out": [{"amount": 1, "price": 1000, "time": iso(now), "additional": {}}],
            "comp": [{"amount": 1, "price": 100, "time": iso(now), "additional": {}}],
        }
        client = self.FakeClient(lots, history)
        book = self.make_book()
        self.assertEqual(book.items(), ["out"])
        result = calculate_item(book, client, "out", commission_pct=5, price_cache={})
        self.assertEqual(result["recipes"], 1)
        offer = result["offers"][0]
        self.assertEqual(offer["components_cost"], 210)
        self.assertEqual(offer["total_cost"], 310)
        self.assertEqual(offer["sale_price"], 1000)
        self.assertEqual(offer["profit"], 640)
        self.assertTrue(offer["available"])
        self.assertEqual(result["name"], "out")
        self.assertEqual(client.lot_calls.count("out"), 1)
        self.assertEqual(client.lot_calls.count("comp"), 1)

    def test_barter_flags_not_enough_component(self):
        now = datetime.now(timezone.utc)
        lots = {
            "out": [{"amount": 1, "buyoutPrice": 1000, "additional": {}}],
            "comp": [{"amount": 1, "buyoutPrice": 100, "additional": {}}],
        }
        history = {
            "out": [{"amount": 1, "price": 1000, "time": iso(now), "additional": {}}],
            "comp": [{"amount": 1, "price": 100, "time": iso(now), "additional": {}}],
        }
        result = calculate_item(self.make_book(), self.FakeClient(lots, history), "out", price_cache={})
        offer = result["offers"][0]
        self.assertFalse(offer["available"])
        self.assertFalse(offer["components"][0]["enough"])


class MarketStoreTests(unittest.TestCase):
    def make_lot(self, unit=1000, amount=1, end_hours=2, qlt=2, ptn=15, end_time=None):
        now = datetime.now(timezone.utc)
        return {
            "amount": amount,
            "buyoutPrice": unit * amount,
            "currentPrice": 0,
            "startPrice": unit * amount,
            "startTime": iso(now - timedelta(hours=1)),
            "endTime": end_time or iso(now + timedelta(hours=end_hours)),
            "additional": {"qlt": qlt, "ptn": ptn, "spawn_time": 123},
        }

    def test_lifecycle_sold_expired_missing_and_price(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = MarketStore(Path(tmp) / "market.db")
            now = datetime.now(timezone.utc)
            lot = self.make_lot(unit=1000)
            self.assertEqual(store.observe("item", [lot], now=now)["appeared"], 1)
            self.assertEqual(store.observe("item", [lot], now=now + timedelta(minutes=1))["appeared"], 0)
            self.assertEqual(store.summary("item", 7)["supply_lots"], 1)

            lot2 = dict(lot)
            lot2["buyoutPrice"] = 900
            self.assertEqual(store.observe("item", [lot2], now=now + timedelta(minutes=2))["price"], 1)

            sale = {
                "amount": 1,
                "price": 900,
                "time": iso(now + timedelta(minutes=2, seconds=30)),
                "additional": {"qlt": 2, "ptn": 15},
            }
            counts = store.observe("item", [], now=now + timedelta(minutes=3), sales=[sale])
            self.assertEqual(counts["probable_sold"], 1)
            summary = store.summary("item", 7)
            self.assertEqual(summary["supply_lots"], 0)
            self.assertEqual(summary["events"].get("probable_sold"), 1)
            self.assertGreater(summary["avg_lifetime_hours"], 0)

            expired = self.make_lot(unit=700, end_time=iso(now - timedelta(hours=1)))
            store.observe("item2", [expired], now=now)
            counts = store.observe("item2", [], now=now + timedelta(minutes=1))
            self.assertEqual(counts["expired"], 1)
            self.assertEqual(store.summary("item2", 7)["events"].get("expired"), 1)

            missing = self.make_lot(unit=500)
            store.observe("item3", [missing], now=now)
            counts = store.observe("item3", [], now=now + timedelta(minutes=1))
            self.assertEqual(counts["missing"], 1)

            incomplete = self.make_lot(unit=300)
            store.observe("item4", [incomplete], now=now)
            counts = store.observe("item4", [], now=now + timedelta(minutes=1), complete=False)
            self.assertEqual(counts["missing"], 0)
            self.assertEqual(store.summary("item4", 7)["supply_lots"], 1)

            # Продажа до первого наблюдения не может относиться к этому лоту.
            old_sale = {
                "amount": 1,
                "price": 500,
                "time": iso(now - timedelta(hours=1)),
                "additional": {},
            }
            counts = store.observe("item5", [self.make_lot(unit=500)], now=now)
            counts = store.observe("item5", [], now=now + timedelta(minutes=1), sales=[old_sale])
            self.assertEqual(counts.get("probable_sold", 0), 0)
            self.assertEqual(counts["missing"], 1)
            store.close()

    def test_prune_and_duplicate_signature(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = MarketStore(Path(tmp) / "market.db")
            now = datetime.now(timezone.utc)
            lot = self.make_lot(unit=1000)
            counts = store.observe("item", [lot, dict(lot)], now=now)
            self.assertEqual(counts["appeared"], 2)
            self.assertEqual(store.total("item"), 2)
            store.close()


class CacheEvictionTests(unittest.TestCase):
    def test_old_cache_files_removed(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = DiskCache(Path(tmp) / "cache")
            cache.set("old", {"x": 1})
            cache.set("fresh", {"x": 2})
            old = time.time() - 10 * 86400
            os.utime(cache._path("old"), (old, old))
            removed = cache.evict(max_age_days=7, max_bytes=50 * 1024 * 1024)
            self.assertEqual(removed, 1)
            self.assertIsNone(cache.get("old", 365 * 86400))
            self.assertIsNotNone(cache.get("fresh", 60))


if __name__ == "__main__":
    unittest.main(verbosity=2)

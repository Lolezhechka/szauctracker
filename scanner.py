"""Анализ аукциона: средняя цена за окно дней и поиск лотов дешевле порога."""

from __future__ import annotations

import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone

from stalzone import ApiError

QUALITY_LABELS = {
    0: "Обычный",
    1: "Необычный",
    2: "Особый",
    3: "Редкий",
    4: "Исключительный",
    5: "Легендарный",
    6: "Уникальный",
}

# Диапазоны процента артефакта по качеству (wiki: minValue + range * ((stats_random + 2) / 4)).
ARTIFACT_RANGES = {
    0: (0.0, 100.0),
    1: (100.0, 15.0),
    2: (110.0, 15.0),
    3: (130.0, 15.0),
    4: (140.0, 15.0),
    5: (150.0, 15.0),
}

# Стаки разного размера торгуются по разной цене за штуку: 1 шт. обычно дороже,
# чем 50+. Сравнивать их в одной медиане нельзя.
STACK_BANDS = (
    (1, 1, "1"),
    (2, 4, "2-4"),
    (5, 9, "5-9"),
    (10, 19, "10-19"),
    (20, 49, "20-49"),
    (50, None, "50+"),
)


# --------------------------------------------------------------------- утилиты


def parse_dt(value) -> datetime | None:
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
    return dt.astimezone(timezone.utc)


def _weighted_quantile(pairs: list[tuple[float, float]], q: float) -> float:
    if not pairs:
        return 0.0
    rows = sorted(pairs, key=lambda row: row[0])
    total = sum(weight for _, weight in rows)
    if total <= 0:
        return rows[0][0]
    target = q * total
    cumulative = 0.0
    for index, (value, weight) in enumerate(rows):
        cumulative += weight
        if cumulative >= target - 1e-9:
            # Для медианы на чётном объёме берём среднее двух центральных
            # значений, иначе база систематически занижается.
            if (
                abs(q - 0.5) < 1e-9
                and abs(cumulative - target) < 1e-9
                and index + 1 < len(rows)
            ):
                return (value + rows[index + 1][0]) / 2.0
            return value
    return rows[-1][0]


def _mean(pairs: list[tuple[float, float]]) -> float:
    total = sum(weight for _, weight in pairs)
    if total <= 0:
        return 0.0
    return sum(value * weight for value, weight in pairs) / total


def _trimmed_mean(pairs: list[tuple[float, float]], share: float = 0.1) -> float:
    if not pairs:
        return 0.0
    rows = sorted(pairs, key=lambda row: row[0])
    total = sum(weight for _, weight in rows)
    if total <= 0:
        return 0.0
    cut = total * share
    kept: list[tuple[float, float]] = []
    cumulative = 0.0
    for value, weight in rows:
        start, end = cumulative, cumulative + weight
        cumulative = end
        low = max(start, cut)
        high = min(end, total - cut)
        if high > low:
            kept.append((value, high - low))
    return _mean(kept) if kept else _mean(rows)


def compute_stats(records: list[dict]) -> dict | None:
    """records: [{unit, amount, time, additional}] — только продажи в окне."""
    pairs = [(float(r["unit"]), float(r["amount"])) for r in records if r["amount"] > 0]
    if not pairs:
        return None
    volume = sum(weight for _, weight in pairs)
    if volume <= 0:
        return None
    prices = [value for value, _ in pairs]
    last_sale = max((r["time"] for r in records), default=None)
    return {
        "volume": int(round(volume)),
        "sales_count": len(records),
        "mean": round(_mean(pairs), 2),
        "median": round(_weighted_quantile(pairs, 0.5), 2),
        "trimmed": round(_trimmed_mean(pairs), 2),
        "p25": round(_weighted_quantile(pairs, 0.25), 2),
        "p75": round(_weighted_quantile(pairs, 0.75), 2),
        "min": round(min(prices), 2),
        "max": round(max(prices), 2),
        "last_sale": last_sale.isoformat() if last_sale else None,
    }


def _market_cap(ask_prices: list[float], lot_price: float) -> float | None:
    """Потолок цены продажи по конкурирующим лотам, без самого оцениваемого лота."""
    others = [price for price in ask_prices if abs(price - lot_price) > 1e-9]
    if not others:
        # Все лоты стоят одинаково — это и есть рынок. Если лот единственный,
        # рынка как такового нет, и опираемся на историю продаж.
        return lot_price if len(ask_prices) >= 2 else None
    if len(others) == 1:
        return others[0]
    return _weighted_quantile([(price, 1.0) for price in others], 0.25)


def _filter_sales_by_market(sales: list[dict], group_lots: list[dict]) -> tuple[list[dict], int]:
    """Убирает явный мусор в истории: цены вне диапазона ×10 от текущего рынка.

    В истории встречаются сделки по 1 рублю (переводы/багоюз) и абсурдно высокие
    записи. Если в группе достаточно активных лотов, такие продажи не должны
    определять среднюю.
    """
    prices = [lot["unit_price"] for lot in group_lots if lot["unit_price"]]
    if len(prices) < 3 or not sales:
        return sales, 0
    median = _weighted_quantile([(price, 1.0) for price in prices], 0.5)
    if median <= 0:
        return sales, 0
    low, high = median / 10.0, median * 10.0
    kept = [row for row in sales if low <= row["unit"] <= high]
    return kept, len(sales) - len(kept)


def group_key(item_id: str, additional: dict, config: dict) -> str:
    parts = [item_id]
    qlt = additional.get("qlt") if isinstance(additional, dict) else None
    ptn = additional.get("ptn") if isinstance(additional, dict) else None
    if config.get("group_by_qlt", True) and isinstance(qlt, (int, float)):
        parts.append(f"q{int(qlt)}")
    if config.get("group_by_ptn", True) and isinstance(ptn, (int, float)):
        parts.append(f"p{int(ptn)}")
    return "|".join(parts)


def _quality_label(qlt) -> str | None:
    if not isinstance(qlt, (int, float)):
        return None
    return QUALITY_LABELS.get(int(qlt), f"Качество {int(qlt)}")


def artifact_percent(additional) -> float | None:
    """Процент артефакта по qlt и stats_random; None, если данных нет."""
    if not isinstance(additional, dict):
        return None
    qlt = additional.get("qlt")
    stats_random = additional.get("stats_random")
    if not isinstance(qlt, (int, float)) or not isinstance(stats_random, (int, float)):
        return None
    bounds = ARTIFACT_RANGES.get(int(qlt))
    if bounds is None:
        return None
    minimum, span = bounds
    value = minimum + span * ((float(stats_random) + 2.0) / 4.0)
    value = max(minimum, min(minimum + span, value))
    return round(value, 1)


def _bonus_properties(additional) -> list[str]:
    if not isinstance(additional, dict):
        return []
    bonus = additional.get("bonus_properties")
    if not isinstance(bonus, list):
        return []
    return [str(item) for item in bonus if item]


def _pattern_label(ptn, category: str) -> str | None:
    if isinstance(ptn, (int, float)):
        if str(category).startswith("artefact"):
            return f"паттерн {int(ptn)}"
        return f"+{int(ptn)}"
    # Отсутствие ptn — это не «любой» вариант: у артефактов это лот без
    # раскрытых бонусов, у оружия — базовый уровень без апгрейда.
    if str(category).startswith("artefact"):
        return "без паттерна"
    if str(category).startswith("weapon"):
        return "без апгрейда"
    return None


def stack_band(amount) -> str:
    """Возвращает диапазон размера стака для сравнимых продаж."""
    try:
        value = int(amount)
    except (TypeError, ValueError):
        value = 0
    for low, high, label in STACK_BANDS:
        if value >= low and (high is None or value <= high):
            return label
    return "50+" if value > 50 else "1"


def stack_band_label(band: str | None, amount=None) -> str | None:
    if band is None:
        return None
    if band == "1":
        return "1 шт."
    return f"{band} шт."


def _stack_band_index(band: str) -> int:
    for index, (_, _, label) in enumerate(STACK_BANDS):
        if label == band:
            return index
    return 0


def percent_band(percent, width: float = 10.0) -> str | None:
    if not isinstance(percent, (int, float)):
        return None
    low = int(float(percent) // width) * int(width)
    return f"{low}-{low + int(width) - 1}"


def _restriction_flags(additional) -> list[str]:
    if not isinstance(additional, dict):
        return []
    flags = []
    if additional.get("sell_is_blocked"):
        flags.append("sell_is_blocked")
    if additional.get("personal_on_use"):
        flags.append("personal_on_use")
    return flags


def _build_view(lots: list[dict], sales: list[dict]) -> dict:
    """Считает статистику и рынок для одной сопоставимой выборки."""
    filtered, dropped = _filter_sales_by_market(sales, lots)
    stats = compute_stats(filtered)
    lot_prices = [lot["unit_price"] for lot in lots if lot.get("unit_price")]
    market = None
    if lot_prices:
        pairs = [(price, 1.0) for price in lot_prices]
        market = {
            "count": len(lot_prices),
            "min": round(min(lot_prices), 2),
            "p25": round(_weighted_quantile(pairs, 0.25), 2),
            "median": round(_weighted_quantile(pairs, 0.5), 2),
            "max": round(max(lot_prices), 2),
        }
    depth_map = {}
    for lot in lots:
        price = lot.get("unit_price")
        if price is None:
            continue
        level = round(float(price), 2)
        entry = depth_map.setdefault(level, {"unit": level, "units": 0, "lots": 0})
        entry["units"] += int(lot.get("amount") or 0)
        entry["lots"] += 1
    return {
        "stats": stats,
        "market": market,
        "depth": sorted(depth_map.values(), key=lambda row: row["unit"])[:50],
        "lot_prices": lot_prices,
        "sales": filtered,
        "sales_filtered": dropped,
    }


def _nearest_band_view(views: dict[str, dict], band: str) -> tuple[dict | None, bool]:
    """Ищет ближайший диапазон стака, если в точном нет продаж/лотов."""
    target = _stack_band_index(band)
    best = None
    for distance in range(1, len(STACK_BANDS)):
        for index in (target - distance, target + distance):
            if 0 <= index < len(STACK_BANDS):
                label = STACK_BANDS[index][2]
                if label == "1":
                    # Не подставляем одиночные продажи вместо стака.
                    continue
                view = views.get(label)
                if view and (view["stats"] or view["market"]):
                    if best is None or distance < best[0]:
                        best = (distance, view)
        if best is not None:
            return best[1], True
    return None, False


def build_coverage(
    history: list[dict],
    cutoff: datetime,
    window_hours: float,
    api_sample_size: int | None = None,
    source: str = "api",
) -> dict:
    """Сколько реального времени покрывает история и упёрлись ли мы в лимит API."""
    times = []
    units = 0
    for row in history:
        ts = parse_dt(row.get("time"))
        if ts is None or ts < cutoff:
            continue
        times.append(ts)
        units += int(row.get("amount") or 0)
    span_hours = 0.0
    if len(times) >= 2:
        span_hours = max(0.0, (max(times) - min(times)).total_seconds() / 3600.0)
    sample = api_sample_size if api_sample_size is not None else len(times)
    return {
        "records": len(times),
        "units": units,
        "oldest": min(times).isoformat() if times else None,
        "newest": max(times).isoformat() if times else None,
        "span_hours": round(span_hours, 1),
        "window_hours": round(window_hours, 1),
        "sample_size": int(sample),
        # API возвращает ровно 200 последних продаж; если этого хватает меньше
        # чем на 90% окна, значит выборка обрезана и накапливается локально.
        "limited": bool(sample >= 195 and span_hours < window_hours * 0.9),
        "source": source,
    }


def _reliability(volume: int, sales_count: int | None = None) -> str:
    if sales_count is None:
        if volume >= 10:
            return "high"
        if volume >= 3:
            return "medium"
        return "low"
    if volume >= 10 and sales_count >= 2:
        return "high"
    if sales_count >= 2 and volume >= 3:
        return "medium"
    if sales_count < 2 and volume >= 20:
        # Один крупный стак — это всё ещё одна сделка, но объём заметный.
        return "medium"
    return "low"


# --------------------------------------------------------------------- стаки


def _acquire_bulk(lots: list[dict], qty: int) -> dict | None:
    """Покупка стака: сначала ищем один лот >= qty, иначе собираем целыми лотами."""
    rows = sorted(
        (lot for lot in lots if lot["unit_price"] and lot["amount"] > 0),
        key=lambda lot: lot["unit_price"],
    )
    candidates = [lot for lot in rows if lot["amount"] >= qty]
    if candidates:
        lot = min(candidates, key=lambda item: item["unit_price"])
        return {
            "cost": lot["buyout"],
            "units": lot["amount"],
            "lots_used": 1,
            "unit": lot["unit_price"],
            "single_lot": True,
        }
    units = 0
    cost = 0.0
    used = 0
    for lot in rows:
        cost += lot["buyout"]
        units += lot["amount"]
        used += 1
        if units >= qty:
            break
    if units < qty:
        return None
    return {
        "cost": cost,
        "units": units,
        "lots_used": used,
        "unit": cost / units,
        "single_lot": False,
    }


def _stack_reliability(sales_count: int, available_units: int, qty: int) -> str:
    if sales_count >= 10 and available_units >= qty * 2:
        return "high"
    if sales_count >= 3 and available_units >= qty:
        return "medium"
    return "low"


def analyze_stack_group(group_lots: list[dict], sales: list[dict], config: dict) -> dict | None:
    """Сравнивает цену стака и поштучную цену для одной группы предметов."""
    qty = max(2, int(config.get("stack_qty", 100)))
    threshold = float(config.get("stack_threshold_pct", 10.0)) / 100.0
    commission = float(config.get("commission_pct", 5.0)) / 100.0
    min_sales = max(1, int(config.get("stack_min_sales", 2)))
    min_profit = float(config.get("min_profit", 5000) or 0)

    singles = [lot for lot in group_lots if lot["amount"] == 1 and lot["unit_price"]]
    bulk = [lot for lot in group_lots if lot["amount"] > 1 and lot["unit_price"]]
    if not singles and not bulk:
        return None

    single_sales = [row for row in sales if row["amount"] == 1]
    bulk_sales = [row for row in sales if row["amount"] > 1]
    reference_amount = qty
    reference_band = stack_band(qty)
    bulk_ref = [row for row in bulk_sales if stack_band(row["amount"]) == reference_band]
    bulk_scope = "target_band"
    if len(bulk_ref) < min_sales:
        target_index = _stack_band_index(reference_band)
        for distance in range(1, len(STACK_BANDS)):
            labels = []
            for index in (target_index - distance, target_index + distance):
                if 0 <= index < len(STACK_BANDS):
                    labels.append(STACK_BANDS[index][2])
            candidate = [row for row in bulk_sales if stack_band(row["amount"]) in labels]
            if len(candidate) >= min_sales:
                bulk_ref = candidate
                bulk_scope = "near_band"
                break
    if len(bulk_ref) < min_sales:
        bulk_ref = bulk_sales
        bulk_scope = "all_bulk"

    def median(pairs: list[tuple[float, float]]) -> float | None:
        return _weighted_quantile(pairs, 0.5) if pairs else None

    single_sales_median = (
        median([(row["unit"], 1.0) for row in single_sales]) if len(single_sales) >= min_sales else None
    )
    bulk_sales_median = (
        median([(row["unit"], row["amount"]) for row in bulk_ref]) if len(bulk_ref) >= min_sales else None
    )

    single_ask_prices = [lot["unit_price"] for lot in singles]
    bulk_ask_pairs = [(lot["unit_price"], lot["amount"]) for lot in bulk]
    single_ask_min = round(min(single_ask_prices), 2) if single_ask_prices else None
    single_ask_median = round(median([(price, 1.0) for price in single_ask_prices]), 2) if single_ask_prices else None
    bulk_ask_min = round(min(price for price, _ in bulk_ask_pairs), 2) if bulk_ask_pairs else None
    bulk_ask_median = round(median(bulk_ask_pairs), 2) if bulk_ask_pairs else None

    singles_available = len(singles)
    bulk_available = sum(lot["amount"] for lot in bulk)
    bulk_acq = _acquire_bulk(bulk, qty)

    bulk_single_lot = None
    for lot in bulk:
        if lot["amount"] >= qty and (bulk_single_lot is None or lot["unit_price"] < bulk_single_lot["unit_price"]):
            bulk_single_lot = lot
    bulk_single_entry = None
    if bulk_single_lot:
        bulk_single_entry = {
            "unit": round(bulk_single_lot["unit_price"], 2),
            "amount": bulk_single_lot["amount"],
            "cost": bulk_single_lot["buyout"],
        }

    directions = []
    reasons = []

    # Купить стак -> продать поштучно
    if bulk_acq is None:
        reasons.append(f"в стаках только {bulk_available} шт. из {qty}")
    elif single_sales_median is None:
        reasons.append(f"нет продаж поштучно за окно (нужно минимум {min_sales})")
    else:
        acquired = bulk_acq["units"]
        cost = bulk_acq["cost"]
        revenue = single_sales_median * acquired
        net = revenue * (1.0 - commission)
        profit = net - cost
        margin = profit / cost if cost else 0.0
        directions.append(
            {
                "direction": "bulk_to_singles",
                "label": "Купить стак → продать поштучно",
                "qty_target": qty,
                "qty": acquired,
                "buy_cost": round(cost, 2),
                "buy_unit": round(cost / acquired, 2),
                "buy_available_units": bulk_available,
                "buy_lots_used": bulk_acq["lots_used"],
                "buy_single_lot": bulk_acq["single_lot"],
                "sell_unit": round(single_sales_median, 2),
                "sell_net_unit": round(single_sales_median * (1.0 - commission), 2),
                "sell_revenue": round(revenue, 2),
                "sell_net": round(net, 2),
                "profit": round(profit, 2),
                "margin_pct": round(margin * 100, 1),
                "sell_sales_count": len(single_sales),
                "sell_sales_units": sum(row["amount"] for row in single_sales),
                "sell_lots": acquired,
                "sell_source": "sales",
                "note": f"{acquired} отдельных продаж",
                "reliability": _stack_reliability(len(single_sales), bulk_available, qty),
                "flag": bool(margin >= threshold and profit > 0 and profit >= min_profit),
            }
        )

    # Купить поштучно -> продать стаком
    if singles_available < qty:
        reasons.append(f"поштучно доступно только {singles_available} шт. из {qty}")
    elif bulk_sales_median is None:
        reasons.append(f"нет продаж стаками за окно (нужно минимум {min_sales})")
    else:
        cost = float(sum(lot["unit_price"] for lot in sorted(singles, key=lambda lot: lot["unit_price"])[:qty]))
        revenue = bulk_sales_median * qty
        net = revenue * (1.0 - commission)
        profit = net - cost
        margin = profit / cost if cost else 0.0
        directions.append(
            {
                "direction": "singles_to_bulk",
                "label": "Купить поштучно → продать стаком",
                "qty_target": qty,
                "qty": qty,
                "buy_cost": round(cost, 2),
                "buy_unit": round(cost / qty, 2),
                "buy_available_units": singles_available,
                "buy_lots_used": qty,
                "buy_single_lot": False,
                "sell_unit": round(bulk_sales_median, 2),
                "sell_net_unit": round(bulk_sales_median * (1.0 - commission), 2),
                "sell_revenue": round(revenue, 2),
                "sell_net": round(net, 2),
                "profit": round(profit, 2),
                "margin_pct": round(margin * 100, 1),
                "sell_sales_count": len(bulk_ref),
                "sell_sales_units": sum(row["amount"] for row in bulk_ref),
                "sell_lots": 1,
                "sell_source": "sales",
                "note": f"{qty} отдельных покупок; продажа одним лотом",
                "reliability": _stack_reliability(len(bulk_ref), singles_available, qty),
                "flag": bool(margin >= threshold and profit > 0 and profit >= min_profit),
            }
        )

    if not directions and not reasons:
        return None

    return {
        "qty": qty,
        "threshold_pct": round(threshold * 100, 1),
        "commission_pct": round(commission * 100, 1),
        "singles": {
            "lots": len(singles),
            "available": singles_available,
            "min": single_ask_min,
            "median": single_ask_median,
            "sales_count": len(single_sales),
            "sales_units": sum(row["amount"] for row in single_sales),
            "sales_median": round(single_sales_median, 2) if single_sales_median else None,
        },
        "bulk": {
            "lots": len(bulk),
            "available": bulk_available,
            "min": bulk_ask_min,
            "median": bulk_ask_median,
            "single_lot": bulk_single_entry,
            "reference_amount": reference_amount,
            "reference_band": reference_band,
            "sales_scope": bulk_scope,
            "sales_count": len(bulk_ref),
            "sales_units": sum(row["amount"] for row in bulk_ref),
            "sales_median": round(bulk_sales_median, 2) if bulk_sales_median else None,
        },
        "directions": directions,
        "reasons": reasons,
    }


# --------------------------------------------------------------------- анализ


def _aggregate_deals(deals: list[dict]) -> list[dict]:
    """Склеивает одинаковые лоты (тот же предмет, вариант и цена) в одну возможность."""
    grouped: dict[tuple, dict] = {}
    for deal in deals:
        key = (
            deal["item_id"],
            deal["group_key"],
            round(float(deal["unit_price"]), 2),
            deal.get("amount_band"),
            deal.get("percent_band"),
        )
        current = grouped.get(key)
        if current is None:
            row = dict(deal)
            row["lots_count"] = 1
            row["amount"] = int(deal.get("amount") or 0)
            row["total_net"] = float(deal.get("total_net") or 0)
            row["total_cost"] = float(deal.get("total_cost") or 0)
            row["aggregated"] = False
            grouped[key] = row
        else:
            current["lots_count"] += 1
            current["amount"] += int(deal.get("amount") or 0)
            current["total_net"] += float(deal.get("total_net") or 0)
            current["total_cost"] += float(deal.get("total_cost") or 0)
            deal_left = deal.get("time_left_min")
            current_left = current.get("time_left_min")
            if deal_left is not None and (current_left is None or deal_left < current_left):
                current["time_left_min"] = deal_left
                current["end_time"] = deal.get("end_time")
    result = list(grouped.values())
    for row in result:
        row["total_net"] = round(row["total_net"], 2)
        row["total_cost"] = round(row["total_cost"], 2)
        row["aggregated"] = row["lots_count"] > 1
    return result


def analyze_item(
    item_id: str,
    meta: dict | None,
    lots: list[dict],
    history: list[dict],
    config: dict,
    now: datetime,
    cutoff: datetime,
    history_total: int | None = None,
    coverage: dict | None = None,
    api_sample_size: int | None = None,
) -> dict:
    name = (meta or {}).get("name") or item_id
    category = (meta or {}).get("category") or "other"
    filter_qlt = config.get("filter_qlt")
    filter_percent = config.get("filter_min_percent")
    filter_bonus = config.get("filter_bonus")

    sales_by_group: dict[str, list[dict]] = defaultdict(list)
    group_meta: dict[str, dict] = {}
    history_in_window = 0
    last_sale = None

    for row in history:
        amount = int(row.get("amount") or 0)
        price = int(row.get("price") or 0)
        ts = parse_dt(row.get("time"))
        if amount <= 0 or price <= 0 or ts is None or ts < cutoff:
            continue
        additional = row.get("additional") if isinstance(row.get("additional"), dict) else {}
        if filter_qlt is not None:
            if not isinstance(additional.get("qlt"), (int, float)) or int(additional["qlt"]) != filter_qlt:
                continue
        if filter_percent:
            percent = artifact_percent(additional)
            if percent is None or percent < filter_percent:
                continue
        if filter_bonus and filter_bonus not in _bonus_properties(additional):
            continue
        key = group_key(item_id, additional, config)
        sales_by_group[key].append(
            {"unit": price / amount, "amount": amount, "time": ts, "additional": additional}
        )
        group_meta.setdefault(key, additional)
        history_in_window += 1
        if last_sale is None or ts > last_sale:
            last_sale = ts

    lots_by_group: dict[str, list[dict]] = defaultdict(list)
    lots_total = int(0)
    active_lots = 0
    buyout_lots = 0
    for row in lots:
        amount = int(row.get("amount") or 0)
        if amount <= 0:
            continue
        lots_total += 1
        buyout = int(row.get("buyoutPrice") or 0)
        current = int(row.get("currentPrice") or 0)
        start_price = int(row.get("startPrice") or 0)
        end = parse_dt(row.get("endTime"))
        if end is not None and end <= now:
            continue
        active_lots += 1
        additional = row.get("additional") if isinstance(row.get("additional"), dict) else {}
        key = group_key(item_id, additional, config)
        group_meta.setdefault(key, additional)
        unit = (buyout / amount) if buyout > 0 else None
        if buyout > 0:
            buyout_lots += 1
        lots_by_group[key].append(
            {
                "unit_price": unit,
                "buyout": buyout,
                "current": current,
                "start_price": start_price,
                "amount": amount,
                "end_time": end,
                "additional": additional,
            }
        )

    threshold = float(config.get("threshold_pct", 10.0)) / 100.0
    commission = float(config.get("commission_pct", 5.0)) / 100.0
    benchmark_mode = config.get("benchmark", "median")
    cap_by_market = bool(config.get("cap_by_market", True))
    min_volume = int(config.get("min_volume", 1))
    max_price = float(config.get("max_price") or 0)
    min_price = float(config.get("min_price") or 0)
    min_profit = float(config.get("min_profit", 5000) or 0)
    days = max(float(config.get("days", 2)), 1.0 / 24)

    deals: list[dict] = []
    stack_deals: list[dict] = []
    groups_out: list[dict] = []
    window_hours = days * 24.0
    if coverage is None:
        coverage = build_coverage(history, cutoff, window_hours, api_sample_size)

    group_by_stack = bool(config.get("group_by_stack", True))
    group_by_percent = bool(config.get("group_by_percent", True))
    for key in sorted(set(sales_by_group) | set(lots_by_group)):
        raw_sales = sales_by_group.get(key, [])
        group_lots = lots_by_group.get(key, [])
        if filter_percent or filter_bonus:
            matching_lots = []
            for lot in group_lots:
                lot_additional = lot.get("additional") if isinstance(lot.get("additional"), dict) else {}
                if filter_percent:
                    lot_percent = artifact_percent(lot_additional)
                    if lot_percent is None or lot_percent < filter_percent:
                        continue
                if filter_bonus and filter_bonus not in _bonus_properties(lot_additional):
                    continue
                matching_lots.append(lot)
            group_lots = matching_lots

        additional = group_meta.get(key, {})
        qlt = additional.get("qlt")
        ptn = additional.get("ptn")
        if filter_qlt is not None:
            if not isinstance(qlt, (int, float)) or int(qlt) != filter_qlt:
                continue

        stackable = (
            any(int(lot.get("amount") or 0) > 1 for lot in group_lots)
            or any(int(row.get("amount") or 0) > 1 for row in raw_sales)
        )
        artifact = str(category).startswith("artefact")
        overall = _build_view(group_lots, raw_sales)
        stats = overall["stats"]
        market = overall["market"]
        depth = overall["depth"]
        sales_filtered = overall["sales_filtered"]

        amount_views: dict[str, dict] = {}
        if stackable and group_by_stack:
            for _, _, label in STACK_BANDS:
                band_lots = [lot for lot in group_lots if stack_band(lot.get("amount")) == label]
                band_sales = [row for row in raw_sales if stack_band(row.get("amount")) == label]
                if band_lots or band_sales:
                    amount_views[label] = _build_view(band_lots, band_sales)

        percent_views: dict[str, dict] = {}
        known_view = None
        unknown_view = None
        if artifact and group_by_percent:
            known_lots = []
            known_sales = []
            unknown_lots = []
            unknown_sales = []
            for lot in group_lots:
                pct = artifact_percent(lot.get("additional"))
                (known_lots if pct is not None else unknown_lots).append(lot)
            for row in raw_sales:
                pct = artifact_percent(row.get("additional"))
                (known_sales if pct is not None else unknown_sales).append(row)
            if known_lots or known_sales:
                known_view = _build_view(known_lots, known_sales)
            if unknown_lots or unknown_sales:
                unknown_view = _build_view(unknown_lots, unknown_sales)
            for lot in known_lots:
                label = percent_band(artifact_percent(lot.get("additional")))
                if label is None or label in percent_views:
                    continue
                band_lots = [
                    item for item in known_lots
                    if percent_band(artifact_percent(item.get("additional"))) == label
                ]
                band_sales = [
                    row for row in known_sales
                    if percent_band(artifact_percent(row.get("additional"))) == label
                ]
                percent_views[label] = _build_view(band_lots, band_sales)

        stack = analyze_stack_group(group_lots, overall["sales"], config)
        if stack:
            for direction in stack["directions"]:
                if not direction.get("flag"):
                    continue
                stack_deals.append(
                    {
                        "item_id": item_id,
                        "name": name,
                        "category": category,
                        "group_key": key,
                        "qlt": int(qlt) if isinstance(qlt, (int, float)) else None,
                        "ptn": int(ptn) if isinstance(ptn, (int, float)) else None,
                        "qlt_label": _quality_label(qlt),
                        "ptn_label": _pattern_label(ptn, category),
                        "artifact_percent": artifact_percent(additional),
                        "bonus_properties": _bonus_properties(additional),
                        "additional": additional,
                        "coverage": coverage,
                        "singles_market": stack["singles"],
                        "bulk_market": stack["bulk"],
                        **direction,
                    }
                )

        group_deals = 0
        best_discount = None
        group_sales_base = None
        if stats:
            group_sales_base = stats.get(benchmark_mode) or stats["median"]
        variant_category = str(category).startswith(("artefact", "weapon", "armor"))
        for lot in group_lots:
            unit = lot["unit_price"]
            if unit is None or unit <= 0:
                continue
            if min_price and unit < min_price:
                continue
            if max_price and unit > max_price:
                continue

            lot_additional = lot.get("additional") if isinstance(lot.get("additional"), dict) else {}
            amount_band = stack_band(lot.get("amount")) if stackable else None
            lot_percent = artifact_percent(lot_additional) if artifact else None
            lot_percent_band = percent_band(lot_percent) if lot_percent is not None else None

            view = overall
            comparable_basis = "overall"
            amount_fallback = False
            percent_fallback = False

            if artifact and group_by_percent:
                if lot_percent is not None:
                    candidate = percent_views.get(lot_percent_band)
                    if candidate and (candidate["stats"] or (benchmark_mode == "lots" and candidate["market"])):
                        view = candidate
                        comparable_basis = f"percent:{lot_percent_band}"
                    elif known_view and (known_view["stats"] or (benchmark_mode == "lots" and known_view["market"])):
                        view = known_view
                        comparable_basis = "percent:known"
                        percent_fallback = True
                    else:
                        continue
                else:
                    if unknown_view and (unknown_view["stats"] or (benchmark_mode == "lots" and unknown_view["market"])):
                        view = unknown_view
                        comparable_basis = "percent:unknown"
                    else:
                        continue
            elif stackable and group_by_stack:
                candidate = amount_views.get(amount_band)
                if candidate and (candidate["stats"] or (benchmark_mode == "lots" and candidate["market"])):
                    view = candidate
                    comparable_basis = f"amount:{amount_band}"
                else:
                    candidate, _ = _nearest_band_view(amount_views, amount_band)
                    if amount_band == "1":
                        # Для одиночного лота нет сравнимых одиночных продаж:
                        # не подставляем стаки, даже ближайшие.
                        continue
                    if candidate and (candidate["stats"] or (benchmark_mode == "lots" and candidate["market"])):
                        view = candidate
                        comparable_basis = f"amount-near:{amount_band}"
                        amount_fallback = True
                    else:
                        # Не сравниваем стак с одиночными продажами: это и есть
                        # главный источник ложных скидок.
                        continue

            view_stats = view["stats"]
            view_market = view["market"]
            view_prices = view["lot_prices"]

            sales_base = None
            if view_stats:
                sales_base = view_stats.get(benchmark_mode)
                if sales_base is None:
                    sales_base = view_stats["median"]
            market_base = view_market["median"] if view_market else None

            if benchmark_mode == "lots":
                benchmark = market_base
                eligible = bool(view_market)
            elif benchmark_mode == "blend":
                values = [value for value in (sales_base, market_base) if value]
                benchmark = sum(values) / len(values) if values else None
                eligible = bool(view_stats and view_market)
            else:
                benchmark = sales_base
                eligible = bool(view_stats)

            volume = view_stats["volume"] if view_stats else 0
            volume_ok = True if benchmark_mode == "lots" else volume >= min_volume
            if not (benchmark and benchmark > 0 and eligible and volume_ok):
                continue

            base_for_lot = benchmark
            market_cap = None
            capped = False
            if cap_by_market and benchmark_mode in ("mean", "median", "trimmed"):
                market_cap = _market_cap(view_prices, unit)
                if market_cap and market_cap < base_for_lot:
                    base_for_lot = market_cap
                    capped = True
            discount = 1.0 - unit / base_for_lot
            if discount < threshold - 1e-9:
                continue
            net_unit = base_for_lot * (1.0 - commission) - unit
            if net_unit <= 0:
                continue
            total_net = net_unit * lot["amount"]
            end = lot["end_time"]
            time_left_min = int((end - now).total_seconds() // 60) if end else None
            variant_unknown = bool(
                variant_category
                and config.get("group_by_qlt", True)
                and not isinstance(lot_additional.get("qlt"), (int, float))
            )
            deals.append(
                {
                    "item_id": item_id,
                    "name": name,
                    "category": category,
                    "group_key": key,
                    "qlt": int(qlt) if isinstance(qlt, (int, float)) else None,
                    "ptn": int(ptn) if isinstance(ptn, (int, float)) else None,
                    "qlt_label": _quality_label(qlt),
                    "ptn_label": _pattern_label(ptn, category),
                    "artifact_percent": lot_percent,
                    "bonus_properties": _bonus_properties(lot_additional),
                    "unit_price": round(unit, 2),
                    "buyout": lot["buyout"],
                    "current_price": lot["current"],
                    "start_price": lot["start_price"],
                    "amount": lot["amount"],
                    "amount_band": amount_band,
                    "amount_band_label": stack_band_label(amount_band),
                    "percent_band": lot_percent_band,
                    "comparable_basis": comparable_basis,
                    "amount_fallback": amount_fallback,
                    "percent_fallback": percent_fallback,
                    "variant_unknown": variant_unknown,
                    "restrictions": _restriction_flags(lot_additional),
                    "benchmark": round(base_for_lot, 2),
                    "benchmark_mode": benchmark_mode,
                    "sales_base": round(sales_base, 2) if sales_base else None,
                    "market_cap": round(market_cap, 2) if market_cap else None,
                    "capped": capped,
                    "mean": view_stats["mean"] if view_stats else None,
                    "median": view_stats["median"] if view_stats else None,
                    "p25": view_stats["p25"] if view_stats else None,
                    "p75": view_stats["p75"] if view_stats else None,
                    "market_min": view_market["min"] if view_market else None,
                    "market_p25": view_market["p25"] if view_market else None,
                    "market_median": view_market["median"] if view_market else None,
                    "market_lots": view_market["count"] if view_market else 0,
                    "discount_pct": round(discount * 100, 1),
                    "commission_pct": round(commission * 100, 1),
                    "gross_unit": round(base_for_lot - unit, 2),
                    "net_unit": round(net_unit, 2),
                    "total_net": round(total_net, 2),
                    "total_cost": round(unit * lot["amount"], 2),
                    "volume": volume,
                    "sales_count": view_stats["sales_count"] if view_stats else 0,
                    "sales_filtered": view["sales_filtered"],
                    "sales_per_day": round(volume / days, 2),
                    "reliability": _reliability(volume, view_stats["sales_count"] if view_stats else 0),
                    "time_left_min": time_left_min,
                    "end_time": end.isoformat() if end else None,
                    "additional": lot_additional,
                    "coverage": coverage,
                }
            )
            group_deals += 1
            best_discount = discount if best_discount is None else max(best_discount, discount)

        groups_out.append(
            {
                "key": key,
                "qlt": int(qlt) if isinstance(qlt, (int, float)) else None,
                "ptn": int(ptn) if isinstance(ptn, (int, float)) else None,
                "qlt_label": _quality_label(qlt),
                "ptn_label": _pattern_label(ptn, category),
                "artifact_percent": artifact_percent(additional),
                "bonus_properties": _bonus_properties(additional),
                "stats": stats,
                "sales_filtered": sales_filtered,
                "market": market,
                "depth": depth,
                "sales_base": round(group_sales_base, 2) if group_sales_base else None,
                "market_p25": market["p25"] if market else None,
                "stack": stack,
                "lots": len(group_lots),
                "buyout_lots": sum(1 for lot in group_lots if lot["buyout"] > 0),
                "deals": group_deals,
                "min_lot_unit": round(min(overall["lot_prices"]), 2) if overall["lot_prices"] else None,
                "best_discount_pct": round(best_discount * 100, 1) if best_discount is not None else None,
            }
        )

    if deals:
        status = "ok"
    elif not history:
        status = "no_data"
    elif history_in_window == 0:
        status = "no_sales"
    elif not active_lots:
        status = "no_lots"
    else:
        status = "ok"

    deals = _aggregate_deals(deals)
    deals = [deal for deal in deals if deal["total_net"] >= min_profit]
    deals.sort(key=lambda d: (-d["total_net"], -d["discount_pct"]))
    stack_deals.sort(key=lambda d: (-d["margin_pct"], -d["profit"]))

    return {
        "id": item_id,
        "name": name,
        "category": category,
        "status": status,
        "lots_total": lots_total,
        "lots_buyout": buyout_lots,
        "history_total": history_total if history_total is not None else len(history),
        "history_in_window": history_in_window,
        "last_sale": last_sale.isoformat() if last_sale else None,
        "coverage": coverage,
        "groups": groups_out,
        "deals": deals,
        "stacks": stack_deals,
    }


# --------------------------------------------------------------------- сканер


def fetch_history(
    client,
    item_id: str,
    cutoff: datetime | None = None,
    max_pages: int = 1,
    limit: int = 200,
    cache_ttl: float = 600,
):
    """Читает историю с пагинацией .net, пока не покроет окно или max_pages.

    На старом .com Cloudflare игнорирует offset, поэтому вторая страница
    возвращает те же строки. Дубликаты отбрасываем и останавливаемся.
    """
    prices: list[dict] = []
    total = 0
    seen: set[tuple] = set()
    for page in range(max(1, int(max_pages))):
        data = client.get_history(
            item_id,
            limit=limit,
            offset=page * limit,
            additional=True,
            cache_ttl=cache_ttl,
        )
        chunk = data.get("prices") or []
        total = int(data.get("total") or total)
        new_count = 0
        oldest = None
        for row in chunk:
            key = (row.get("time"), row.get("price"), row.get("amount"))
            ts = parse_dt(row.get("time"))
            if ts is not None and (oldest is None or ts < oldest):
                oldest = ts
            if key in seen:
                continue
            seen.add(key)
            prices.append(row)
            new_count += 1
        if len(chunk) < limit or new_count == 0:
            break
        if cutoff is not None and oldest is not None and oldest <= cutoff:
            break
    return prices, total


def _meta_view(catalog, item_id: str) -> dict:
    meta = catalog.get(item_id) if catalog else None
    if meta:
        name = meta["name"].get("ru") or meta["name"].get("en") or item_id
        return {"name": name, "category": meta.get("category", "other")}
    return {"name": item_id, "category": "other"}


def _error_item(item_id: str, meta_view: dict, message: str, cutoff: datetime, window_hours: float) -> dict:
    return {
        "id": item_id,
        "name": meta_view["name"],
        "category": meta_view["category"],
        "status": "error",
        "error": message,
        "lots_total": 0,
        "lots_buyout": 0,
        "history_total": 0,
        "history_in_window": 0,
        "last_sale": None,
        "groups": [],
        "deals": [],
        "stacks": [],
        "coverage": build_coverage([], cutoff, window_hours, 0, "api"),
    }


def _fetch_lots(client, item_id: str, lots_pages: int) -> tuple[list[dict], int]:
    lots: list[dict] = []
    total = 0
    for page in range(max(1, lots_pages)):
        data = client.get_lots(
            item_id,
            limit=200,
            offset=page * 200,
            sort="buyout_price",
            order="asc",
            additional=True,
            force=True,
        )
        chunk = data.get("lots") or []
        total = int(data.get("total") or 0)
        lots.extend(chunk)
        if len(chunk) < 200 or len(lots) >= total:
            break
    return lots, total


def _scan_one(
    client,
    catalog,
    config: dict,
    item_id: str,
    now: datetime,
    cutoff: datetime,
    window_hours: float,
    history_store,
    history_ttl: float,
    log,
) -> tuple[dict, int]:
    meta_view = _meta_view(catalog, item_id)
    try:
        lots, lots_total = _fetch_lots(client, item_id, max(1, int(config.get("lots_pages", 1))))
        # Если выкупать нечего, история не нужна для поиска сделок. Берём то,
        # что уже накоплено локально, и экономим один запрос к API.
        has_buyout = any(int(lot.get("buyoutPrice") or 0) > 0 for lot in lots)
        fetched: list[dict] = []
        raw_sample = 0
        history_total = 0
        history_skipped = not has_buyout
        if has_buyout:
            fetched, history_total = fetch_history(
                client,
                item_id,
                cutoff=cutoff,
                max_pages=max(1, int(config.get("history_pages", 1))),
                cache_ttl=history_ttl,
            )
            raw_sample = len(fetched)
            # Не храним в SQLite продажи старше горизонта хранения: для неликвидных
            # предметов API отдаёт «последние 200 продаж» из 2021 года, и они
            # возвращались бы в базу после каждой очистки.
            retention_days = max(7, int(window_hours / 24 * 3))
            retention_cutoff = now - timedelta(days=retention_days)
            fetched = [
                row
                for row in fetched
                if (ts := parse_dt(row.get("time"))) is not None and ts >= retention_cutoff
            ]

        source = "api"
        history = fetched
        inserted = 0
        if history_store is not None:
            try:
                if fetched:
                    inserted = history_store.add_sales(item_id, fetched)
                history = history_store.get_sales(item_id, cutoff)
                source = "database"
            except Exception as exc:
                if log:
                    log(f"{item_id}: локальная история недоступна ({exc}), использую ответ API")
                history = fetched

        coverage = build_coverage(history, cutoff, window_hours, raw_sample, source)
        rule = (config.get("item_rules") or {}).get(item_id) or {}
        item_config = dict(config)
        for source_key, target_key in (
            ("min_price", "min_price"),
            ("min_profit", "min_profit"),
            ("qlt", "filter_qlt"),
            ("min_percent", "filter_min_percent"),
            ("bonus", "filter_bonus"),
        ):
            if source_key in rule:
                item_config[target_key] = rule[source_key]
        result = analyze_item(
            item_id,
            meta_view,
            lots,
            history,
            item_config,
            now,
            cutoff,
            history_total=history_total,
            coverage=coverage,
        )
        result["lots_scanned"] = len(lots)
        result["lots_total"] = lots_total or result["lots_total"]
        result["history_skipped"] = history_skipped
        result["inserted_sales"] = inserted
        return result, inserted
    except ApiError as exc:
        if log:
            log(f"{item_id}: {exc}")
        return _error_item(item_id, meta_view, str(exc), cutoff, window_hours), 0
    except Exception as exc:  # не роняем весь скан из-за одного предмета
        if log:
            log(f"{item_id}: {exc}")
        return _error_item(item_id, meta_view, f"Внутренняя ошибка: {exc}", cutoff, window_hours), 0


def scan(
    client,
    catalog,
    config: dict,
    progress=None,
    cancel=None,
    log=None,
    history_store=None,
) -> dict:
    started = time.time()
    now = datetime.now(timezone.utc)
    window_hours = float(config.get("days", 2)) * 24.0
    cutoff = now - timedelta(hours=window_hours)
    history_ttl = max(60.0, float(config.get("history_ttl_min", 30)) * 60.0)
    workers = max(1, min(8, int(config.get("scan_workers", 4))))

    watchlist = [str(x) for x in (config.get("watchlist") or []) if str(x).strip()]
    if client.demo:
        # Демо-база отвечает только на этот предмет.
        watchlist = ["y1q9"]
    if not watchlist:
        raise ApiError("Список наблюдения пуст. Добавьте предметы во вкладке «Наблюдение».")

    if not client.demo and (not client.client_id or not client.client_secret):
        raise ApiError(
            "Не заданы Client-Id и Client-Secret. Получите их у @stalzone_api_bot и укажите в настройках."
        )

    rules = config.get("item_rules") or {}
    configured_watch = list(watchlist)
    watchlist = [item for item in watchlist if (rules.get(item) or {}).get("enabled", True)]
    rules_skipped = len(configured_watch) - len(watchlist)
    if not watchlist:
        raise ApiError("Все предметы отключены в правилах. Включите хотя бы один.")

    if history_store is not None:
        try:
            pruned = history_store.prune(keep_days=max(7, int(window_hours / 24 * 3)))
        except Exception as exc:
            if log:
                log(f"Не удалось почистить локальную историю: {exc}")
        else:
            if pruned:
                try:
                    history_store.vacuum()
                except Exception:
                    # VACUUM может не получить эксклюзивную блокировку, если открыт
                    # второй экземпляр приложения. Это не критично.
                    pass

    results_by_index: list[dict | None] = [None] * len(watchlist)
    inserted_total = 0
    done = 0
    total = len(watchlist)

    if workers <= 1 or total <= 1:
        for index, item_id in enumerate(watchlist):
            if cancel is not None and cancel.is_set():
                break
            if progress:
                progress(index, total, item_id, "сканирование")
            result, inserted = _scan_one(
                client, catalog, config, item_id, now, cutoff, window_hours, history_store, history_ttl, log
            )
            results_by_index[index] = result
            inserted_total += inserted
            if progress:
                progress(index + 1, total, item_id, "готово")
    else:
        with ThreadPoolExecutor(max_workers=min(workers, total), thread_name_prefix="scan") as pool:
            futures = {
                pool.submit(
                    _scan_one,
                    client,
                    catalog,
                    config,
                    item_id,
                    now,
                    cutoff,
                    window_hours,
                    history_store,
                    history_ttl,
                    log,
                ): index
                for index, item_id in enumerate(watchlist)
            }
            for future in as_completed(futures):
                index = futures[future]
                item_id = watchlist[index]
                try:
                    result, inserted = future.result()
                except Exception as exc:
                    if log:
                        log(f"{item_id}: {exc}")
                    result = _error_item(
                        item_id, _meta_view(catalog, item_id), f"Внутренняя ошибка: {exc}", cutoff, window_hours
                    )
                    inserted = 0
                results_by_index[index] = result
                inserted_total += inserted
                done += 1
                if progress:
                    progress(done, total, item_id, "готово")
                if cancel is not None and cancel.is_set():
                    break

    items_out = [result for result in results_by_index if result is not None]
    all_deals = [deal for item in items_out for deal in item.get("deals", [])]
    all_stacks = [stack for item in items_out for stack in item.get("stacks", [])]
    all_deals.sort(key=lambda d: (-d["total_net"], -d["discount_pct"]))
    all_stacks.sort(key=lambda d: (-d["margin_pct"], -d["profit"]))
    limited = [
        item["coverage"]
        for item in items_out
        if isinstance(item.get("coverage"), dict) and item["coverage"].get("limited")
    ]

    return {
        "generated_at": now.isoformat(),
        "generated_ts": now.timestamp(),
        "elapsed_sec": round(time.time() - started, 1),
        "region": client.region,
        "demo": client.demo,
        "days": float(config.get("days", 2)),
        "threshold_pct": float(config.get("threshold_pct", 10)),
        "commission_pct": float(config.get("commission_pct", 5)),
        "min_profit": float(config.get("min_profit", 5000)),
        "min_price": float(config.get("min_price") or 0),
        "benchmark": config.get("benchmark", "median"),
        "scan_workers": workers,
        "history_ttl_min": int(history_ttl / 60),
        "rules_skipped": rules_skipped,
        "items_scanned": len([item for item in items_out if item["status"] != "error"]),
        "items_total": total,
        "items_no_data": len([item for item in items_out if item["status"] in ("no_data", "no_sales")]),
        "items_errors": len([item for item in items_out if item["status"] == "error"]),
        "deals_count": len(all_deals),
        "potential_profit": round(sum(d["total_net"] for d in all_deals), 2),
        "capped_count": len([deal for deal in all_deals if deal.get("capped")]),
        "stacks_count": len(all_stacks),
        "stacks_potential_profit": round(sum(d["profit"] for d in all_stacks), 2),
        "stack_qty": int(config.get("stack_qty", 100)),
        "stack_threshold_pct": float(config.get("stack_threshold_pct", 10)),
        "coverage_limited": len(limited),
        "coverage_min_hours": min((cov["span_hours"] for cov in limited), default=None),
        "history_db": {
            "enabled": history_store is not None,
            "stored": history_store.total() if history_store is not None else 0,
            "new_sales": inserted_total,
        },
        "deals": all_deals,
        "stacks": all_stacks,
        "items": items_out,
        "api": dict(client.stats),
    }

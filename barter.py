"""Бартерный калькулятор по официальным рецептам STALZONE."""

from __future__ import annotations

import json
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

RECIPES_URL = "https://raw.githubusercontent.com/EXBO-Studio/stalzone-database/main/global/barter_recipes.json"
USER_AGENT = "SZAUCTRACKER/2.0 (local tool)"


def _ru(value) -> str:
    if isinstance(value, dict):
        lines = value.get("lines") or {}
        return str(lines.get("ru") or lines.get("en") or value.get("text") or "")
    return str(value or "")


class BarterBook:
    def __init__(self, path: Path | str, url: str = RECIPES_URL, ttl_days: float = 7.0):
        self.path = Path(path)
        self.url = url
        self.ttl_seconds = ttl_days * 86400
        self.ready = False
        self.error: str | None = None
        self.updated_at: float | None = None
        self.recipes: dict[str, list[dict]] = {}
        self._lock = threading.Lock()

    def load_sync(self) -> None:
        cached = self._read_cache()
        fresh = cached is not None and (time.time() - (self.updated_at or 0) < self.ttl_seconds)
        if cached and fresh:
            self._install(cached, self.updated_at)
            return
        try:
            data = self._download()
            self._write_cache(data)
            self._install(data, time.time())
        except Exception as exc:
            if cached:
                self._install(cached, self.updated_at)
                self.error = f"Не удалось обновить рецепты, использую кэш: {exc}"
            else:
                self.error = f"Рецепты недоступны: {exc}"

    def load_async(self) -> None:
        threading.Thread(target=self.load_sync, daemon=True).start()

    def _download(self):
        req = urllib.request.Request(self.url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=45) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def _read_cache(self):
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                payload = json.load(fh)
            if isinstance(payload, dict):
                self.updated_at = float(payload.get("stored", 0)) or None
                payload = payload.get("items")
            else:
                self.updated_at = self.path.stat().st_mtime
            return payload if isinstance(payload, list) else None
        except (OSError, ValueError, TypeError):
            return None

    def _write_cache(self, data: list) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump({"stored": time.time(), "items": data}, fh, ensure_ascii=False)
            tmp.replace(self.path)
        except OSError:
            pass

    def _install(self, data: list, updated_at: float | None = None) -> None:
        recipes: dict[str, list[dict]] = {}
        for settlement in data or []:
            if not isinstance(settlement, dict):
                continue
            title = _ru(settlement.get("settlementTitle")) or "Бартер"
            for recipe in settlement.get("recipes") or []:
                if not isinstance(recipe, dict):
                    continue
                item = str(recipe.get("item") or "").strip()
                if not item:
                    continue
                offers = []
                for offer in recipe.get("offers") or []:
                    if not isinstance(offer, dict):
                        continue
                    required = []
                    for part in offer.get("requiredItems") or []:
                        if not isinstance(part, dict):
                            continue
                        part_item = str(part.get("item") or "").strip()
                        amount = int(part.get("amount") or 0)
                        if part_item and amount > 0:
                            required.append({"item": part_item, "amount": amount})
                    offers.append(
                        {
                            "currency": str(offer.get("currency") or "money"),
                            "cost": int(offer.get("cost") or 0),
                            "required": required,
                        }
                    )
                if offers:
                    recipes.setdefault(item, []).append(
                        {
                            "item": item,
                            "settlement": title,
                            "level": int(recipe.get("settlementRequiredLevel") or 0),
                            "offers": offers,
                        }
                    )
        with self._lock:
            self.recipes = recipes
            self.ready = bool(recipes)
            if self.ready:
                self.error = None
            self.updated_at = updated_at

    def items(self) -> list[str]:
        with self._lock:
            return sorted(self.recipes)

    def for_item(self, item: str) -> list[dict]:
        with self._lock:
            return list(self.recipes.get(str(item)) or [])


def _weighted_quantile(pairs: list[tuple[float, float]], q: float) -> float | None:
    if not pairs:
        return None
    rows = sorted(pairs, key=lambda row: row[0])
    total = sum(weight for _, weight in rows)
    if total <= 0:
        return rows[0][0]
    target = total * q
    cumulative = 0.0
    for index, (value, weight) in enumerate(rows):
        cumulative += weight
        if cumulative >= target - 1e-9:
            if abs(q - 0.5) < 1e-9 and abs(cumulative - target) < 1e-9 and index + 1 < len(rows):
                return (value + rows[index + 1][0]) / 2.0
            return value
    return rows[-1][0]


def quote_item(client, item: str, cache: dict, ttl: float = 300.0) -> dict:
    """Цена компонента/выхода: дешёвые лоты + медиана продаж."""
    now = time.time()
    cached = cache.get(item)
    if cached and now - cached[0] < ttl:
        return cached[1]
    lots_data = client.get_lots(
        item, limit=200, offset=0, sort="buyout_price", order="asc",
        additional=True, force=False,
    )
    history_data = client.get_history(item, limit=200, offset=0, additional=True, cache_ttl=ttl)
    lots = []
    for row in lots_data.get("lots") or []:
        amount = int(row.get("amount") or 0)
        buyout = int(row.get("buyoutPrice") or 0)
        if amount > 0 and buyout > 0:
            lots.append({"unit": buyout / amount, "amount": amount, "buyout": buyout})
    lots.sort(key=lambda row: row["unit"])
    prices = [(float(row["unit"]), float(row["amount"])) for row in lots]
    sales = []
    for row in history_data.get("prices") or []:
        amount = int(row.get("amount") or 0)
        price = int(row.get("price") or 0)
        if amount > 0 and price > 0:
            sales.append({"unit": price / amount, "amount": amount})
    quote = {
        "item": item,
        "lots": lots,
        "available_units": sum(int(row["amount"]) for row in lots),
        "unit_min": round(lots[0]["unit"], 2) if lots else None,
        "unit_p25": round(_weighted_quantile([(row["unit"], 1.0) for row in lots], 0.25), 2) if lots else None,
        "sales_median": round(
            _weighted_quantile([(row["unit"], float(row["amount"])) for row in sales], 0.5) or 0.0, 2
        ) if sales else None,
        "sales_count": len(sales),
        "sales_units": sum(int(row["amount"]) for row in sales),
    }
    cache[item] = (now, quote)
    return quote


def _buy_cost(quote: dict, amount: int) -> tuple[float | None, bool]:
    """Стоимость покупки amount единиц самыми дешёвыми лотами."""
    left = int(amount)
    if left <= 0:
        return 0.0, True
    lots = quote.get("lots") or []
    if not lots:
        return None, False
    cost = 0.0
    for lot in lots:
        take = min(left, int(lot.get("amount") or 0))
        if take <= 0:
            continue
        cost += take * float(lot.get("unit") or 0)
        left -= take
        if left <= 0:
            return cost, True
    # Лотов не хватает: показываем оценку по цене последнего лота, но флаг false.
    last = float(lots[-1].get("unit") or 0)
    return cost + left * last, False


def calculate_item(
    book: BarterBook,
    client,
    item: str,
    catalog=None,
    commission_pct: float = 5.0,
    price_cache: dict | None = None,
    workers: int = 6,
) -> dict:
    recipes = book.for_item(item)
    price_cache = price_cache if price_cache is not None else {}
    all_ids = {item}
    for recipe in recipes:
        for offer in recipe["offers"]:
            all_ids.update(part["item"] for part in offer["required"])

    quotes: dict[str, dict] = {}
    errors: dict[str, str] = {}

    def fetch(part: str):
        try:
            return part, quote_item(client, part, price_cache)
        except Exception as exc:
            return part, {"error": str(exc)}

    with ThreadPoolExecutor(max_workers=max(1, min(workers, len(all_ids)))) as pool:
        for part, data in pool.map(fetch, sorted(all_ids)):
            if data.get("error"):
                errors[part] = data["error"]
            else:
                quotes[part] = data

    def name(part: str) -> str:
        meta = catalog.get(part) if catalog is not None else None
        if meta:
            return meta["name"].get("ru") or meta["name"].get("en") or part
        return part

    output_quote = quotes.get(item) or {}
    output_sale = output_quote.get("sales_median") or output_quote.get("unit_p25") or output_quote.get("unit_min")
    commission = max(0.0, min(50.0, float(commission_pct))) / 100.0
    offers_out = []
    for recipe in recipes:
        for offer in recipe["offers"]:
            components = []
            component_cost = 0.0
            available = True
            for part in offer["required"]:
                q = quotes.get(part["item"]) or {}
                unit = q.get("unit_min")
                amount = int(part["amount"])
                cost, enough = _buy_cost(q, amount)
                if not enough:
                    available = False
                if cost is not None:
                    component_cost += cost
                components.append(
                    {
                        "item": part["item"],
                        "name": name(part["item"]),
                        "amount": amount,
                        "unit": round(unit, 2) if unit else None,
                        "cost": round(cost, 2) if cost is not None else None,
                        "available_units": q.get("available_units", 0),
                        "enough": enough,
                        "sales_median": q.get("sales_median"),
                    }
                )
            money = int(offer.get("cost") or 0)
            total_cost = money + component_cost
            revenue = output_sale * (1.0 - commission) if output_sale else None
            profit = revenue - total_cost if revenue is not None and available else None
            margin = (profit / total_cost * 100.0) if profit is not None and total_cost > 0 else None
            offers_out.append(
                {
                    "settlement": recipe["settlement"],
                    "level": recipe["level"],
                    "money": money,
                    "components": components,
                    "components_cost": round(component_cost, 2),
                    "total_cost": round(total_cost, 2),
                    "sale_price": round(output_sale, 2) if output_sale else None,
                    "revenue": round(revenue, 2) if revenue is not None else None,
                    "profit": round(profit, 2) if profit is not None else None,
                    "margin_pct": round(margin, 1) if margin is not None else None,
                    "available": available and output_sale is not None,
                }
            )
    offers_out.sort(key=lambda row: (-(row["profit"] or -10**18), row["total_cost"]))
    best = offers_out[0] if offers_out else None
    return {
        "item": item,
        "name": name(item),
        "recipes": len(recipes),
        "offers": offers_out,
        "best": best,
        "output": {
            "unit_min": output_quote.get("unit_min"),
            "unit_p25": output_quote.get("unit_p25"),
            "sales_median": output_quote.get("sales_median"),
            "sales_count": output_quote.get("sales_count", 0),
            "available_units": output_quote.get("available_units", 0),
        },
        "errors": errors,
        "commission_pct": round(commission * 100, 1),
    }

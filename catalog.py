"""Справочник предметов STALZONE.

Источник: https://cdn.stalcraft.wiki/exbo_item_parser/listing.json
Формат: [{"id": "1kdg", "category": "artefact/biochemical", "name": {"ru": "...", "en": "..."}}]
"""

from __future__ import annotations

import json
import threading
import time
import urllib.request
from pathlib import Path

CATALOG_URL = "https://cdn.stalcraft.wiki/exbo_item_parser/listing.json"
USER_AGENT = "SZAUCTRACKER/1.3 (local tool)"

CATEGORY_LABELS = {
    "artefact": "Артефакты",
    "weapon": "Оружие",
    "armor": "Броня и костюмы",
    "attachment": "Обвесы",
    "consumables": "Расходники",
    "containers": "Контейнеры",
    "device": "Устройства",
    "backpacks": "Рюкзаки",
    "weapon_modules": "Модули оружия",
    "weapon_style": "Скины оружия",
    "armor_style": "Скины брони",
    "bullet": "Патроны",
    "misc": "Разное",
    "other": "Прочее",
}


def _normalize(text: str) -> str:
    return (text or "").casefold().replace("ё", "е")


class Catalog:
    def __init__(self, path: Path | str, url: str = CATALOG_URL, ttl_days: float = 7.0):
        self.path = Path(path)
        self.url = url
        self.ttl_seconds = ttl_days * 86400
        self.items: list[dict] = []
        self.by_id: dict[str, dict] = {}
        self.ready = False
        self.error: str | None = None
        self.updated_at: float | None = None
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ загрузка

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
        except Exception as exc:  # сеть/диск — не повод падать
            if cached:
                self._install(cached, self.updated_at)
                self.error = f"Не удалось обновить справочник, использую кэш: {exc}"
            else:
                self.error = f"Справочник недоступен: {exc}"

    def load_async(self) -> None:
        threading.Thread(target=self.load_sync, daemon=True).start()

    def _download(self) -> list[dict]:
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
        except (OSError, ValueError):
            return None

    def _write_cache(self, data: list[dict]) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump({"stored": time.time(), "items": data}, fh, ensure_ascii=False)
            tmp.replace(self.path)
        except OSError:
            pass

    def _install(self, data: list[dict], updated_at: float | None = None) -> None:
        cleaned = []
        index = {}
        for raw in data:
            if not isinstance(raw, dict):
                continue
            item_id = str(raw.get("id") or "").strip()
            if not item_id:
                continue
            names = raw.get("name") or {}
            if not isinstance(names, dict):
                names = {"ru": str(names)}
            item = {
                "id": item_id,
                "category": str(raw.get("category") or "other"),
                "name": {k: str(v) for k, v in names.items() if v},
                "color": raw.get("color"),
            }
            cleaned.append(item)
            index[item_id] = item
        with self._lock:
            self.items = cleaned
            self.by_id = index
            self.ready = True
            if updated_at:
                self.updated_at = updated_at

    # ------------------------------------------------------------------ поиск

    def get(self, item_id: str) -> dict | None:
        return self.by_id.get(item_id)

    def display_name(self, item_id: str, lang: str = "ru") -> str:
        item = self.by_id.get(item_id)
        if not item:
            return item_id
        names = item["name"]
        return names.get(lang) or names.get("en") or names.get("ru") or item_id

    def search(self, query: str, category: str = "", limit: int = 40) -> list[dict]:
        q = _normalize(query)
        cat = (category or "").strip()
        results = []
        for item in self.items:
            if cat and not (item["category"] == cat or item["category"].startswith(cat + "/")):
                continue
            name_ru = _normalize(item["name"].get("ru", ""))
            name_en = _normalize(item["name"].get("en", ""))
            id_lower = item["id"].casefold()
            if not q:
                rank = 3
            elif q == id_lower or q == name_ru or q == name_en:
                rank = 0
            elif name_ru.startswith(q) or name_en.startswith(q):
                rank = 1
            elif id_lower.startswith(q):
                rank = 2
            elif q in name_ru or q in name_en:
                rank = 3
            elif q in id_lower:
                rank = 4
            else:
                continue
            display = item["name"].get("ru") or item["name"].get("en") or item["id"]
            results.append((rank, len(display), display.casefold(), item))
        results.sort(key=lambda row: (row[0], row[1], row[2]))
        return [row[3] for row in results[: max(1, min(limit, 200))]]

    def categories(self) -> list[dict]:
        counts: dict[str, int] = {}
        for item in self.items:
            top = item["category"].split("/")[0]
            counts[top] = counts.get(top, 0) + 1
        out = []
        for key, count in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])):
            out.append({"id": key, "label": CATEGORY_LABELS.get(key, key), "count": count})
        return out

    def by_category(self, category: str, limit: int = 100) -> list[dict]:
        cat = (category or "").strip()
        rows = [
            item
            for item in self.items
            if item["category"] == cat or item["category"].startswith(cat + "/")
        ]
        rows.sort(key=lambda item: (item["name"].get("ru") or item["name"].get("en") or item["id"]).casefold())
        return rows[: max(1, min(limit, 500))]

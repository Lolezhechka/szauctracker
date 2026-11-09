"""Клиент STALZONE eAPI (только стандартная библиотека Python).

Документация: https://eapi.stalzone.net/reference
Авторизация приложения: заголовки Client-Id / Client-Secret.
Лимит: 400 токенов в минуту, запрос аукциона стоит 2 токена.
"""

from __future__ import annotations

import hashlib
import http.client
import json
import os
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

# .net — актуальный хост. На .com Cloudflare с 19.10.2026 кэширует ответы
# без query-строки: offset/limit/sort игнорируются, пагинация не работает.
BASE_PROD = "https://eapi.stalzone.net"
BASE_DEMO = "https://dapi.stalzone.net"

# Публичные демо-ключи из документации. Работают только с dapi и ограниченным
# набором предметов — нужны, чтобы проверить интерфейс без регистрации.
DEMO_CLIENT_ID = "1"
DEMO_CLIENT_SECRET = "E98cm6J9NNjTQopph0c2eIXNKafg4R1Cjz0TZh2D"

USER_AGENT = "SZAUCTRACKER/1.5 (local tool)"

REGIONS = [
    {"id": "RU", "name": "Россия"},
    {"id": "EU", "name": "Европа"},
    {"id": "NA", "name": "Северная Америка"},
    {"id": "SEA", "name": "Юго-Восточная Азия"},
    {"id": "NEA", "name": "Северо-Восточная Азия"},
]


class ApiError(Exception):
    def __init__(self, message: str, status: int | None = None, details=None):
        super().__init__(message)
        self.status = status
        self.details = details


def _as_int(value):
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


class RateLimiter:
    """Общий для процесса бюджет токенов в минуту + минимальный интервал."""

    def __init__(self, budget: int = 380, window: float = 60.0, min_gap: float = 0.33):
        self.budget = budget
        self.window = window
        self.min_gap = min_gap
        self._lock = threading.Lock()
        self._events: list[tuple[float, int]] = []
        self._last = 0.0

    def acquire(self, cost: int = 1) -> None:
        while True:
            wait = 0.0
            with self._lock:
                now = time.monotonic()
                if self._events:
                    self._events = [e for e in self._events if now - e[0] < self.window]
                used = sum(c for _, c in self._events)
                if used + cost > self.budget:
                    need = used + cost - self.budget
                    freed = 0
                    for ts, c in self._events:
                        freed += c
                        if freed >= need:
                            wait = max(wait, ts + self.window - now + 0.02)
                            break
                if self._last and now - self._last < self.min_gap:
                    wait = max(wait, self.min_gap - (now - self._last))
                if wait <= 0:
                    self._events.append((now, cost))
                    self._last = now
                    return
            time.sleep(min(wait, 5.0))


class DiskCache:
    """Простой файловый кэш ответов API."""

    def __init__(self, root: Path | None):
        self.root = Path(root) if root else None
        if self.root:
            self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        digest = hashlib.sha1(key.encode("utf-8")).hexdigest()
        return self.root / f"{digest}.json"

    def get(self, key: str, ttl: float):
        if not self.root:
            return None
        path = self._path(key)
        try:
            with open(path, "r", encoding="utf-8") as fh:
                payload = json.load(fh)
            if time.time() - float(payload.get("stored", 0)) <= ttl:
                return payload.get("data")
        except (OSError, ValueError, TypeError):
            return None
        return None

    def set(self, key: str, data) -> None:
        if not self.root:
            return
        path = self._path(key)
        tmp = path.with_suffix(".tmp")
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump({"stored": time.time(), "data": data}, fh, ensure_ascii=False)
            os.replace(tmp, path)
        except OSError:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass


    def evict(self, max_age_days: float = 7.0, max_bytes: int = 50 * 1024 * 1024) -> int:
        """Удаляет устаревшие файлы кэша и ограничивает его размер."""
        if not self.root:
            return 0
        now = time.time()
        removed = 0
        for path in self.root.glob("*.json"):
            try:
                if now - path.stat().st_mtime > max_age_days * 86400:
                    path.unlink()
                    removed += 1
            except OSError:
                pass
        entries = []
        for path in self.root.glob("*.json"):
            try:
                stat = path.stat()
            except OSError:
                continue
            entries.append((stat.st_mtime, stat.st_size, path))
        total = sum(size for _, size, _ in entries)
        for _, size, path in sorted(entries):
            if total <= max_bytes:
                break
            try:
                path.unlink()
                total -= size
                removed += 1
            except OSError:
                pass
        return removed


class HttpSession:
    """Keep-alive GET: одно TLS-соединение на поток, без рукопожатия на каждый запрос."""

    def __init__(self, base_url: str, timeout: float):
        parts = urllib.parse.urlsplit(base_url)
        self.scheme = parts.scheme or "https"
        self.host = parts.hostname or ""
        self.port = parts.port
        self.timeout = timeout
        self._local = threading.local()
        proxies = urllib.request.getproxies()
        self._use_proxy = any(key in proxies for key in ("http", "https", "all"))
        port = f":{self.port}" if self.port else ""
        self._origin = f"{self.scheme}://{self.host}{port}"

    def _new_conn(self):
        if self.scheme == "https":
            return http.client.HTTPSConnection(
                self.host,
                self.port,
                timeout=self.timeout,
                context=ssl.create_default_context(),
            )
        return http.client.HTTPConnection(self.host, self.port, timeout=self.timeout)

    def request(self, target: str, headers: dict):
        if self._use_proxy:
            req = urllib.request.Request(self._origin + target, headers=headers, method="GET")
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    return resp.status, resp.headers, resp.read()
            except urllib.error.HTTPError as exc:
                return exc.code, exc.headers, exc.read()

        last_error: Exception | None = None
        for _ in range(2):
            conn = getattr(self._local, "conn", None)
            try:
                if conn is None:
                    conn = self._new_conn()
                    self._local.conn = conn
                conn.request("GET", target, headers=headers)
                response = conn.getresponse()
                body = response.read()
                status = response.status
                if response.will_close:
                    conn.close()
                    self._local.conn = None
                return status, response.headers, body
            except (http.client.HTTPException, OSError) as exc:
                last_error = exc
                try:
                    if conn is not None:
                        conn.close()
                except OSError:
                    pass
                self._local.conn = None
        raise last_error or OSError("HTTP request failed")


class StalzoneClient:
    def __init__(
        self,
        client_id: str,
        client_secret: str,
        region: str = "RU",
        base_url: str = BASE_PROD,
        cache_dir: Path | str | None = None,
        timeout: float = 25.0,
        demo: bool = False,
    ):
        self.client_id = (client_id or "").strip()
        self.client_secret = (client_secret or "").strip()
        self.region = (region or "RU").upper()
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.demo = demo
        self.limiter = RateLimiter()
        self.cache = DiskCache(cache_dir)
        self.cache.evict(max_age_days=7, max_bytes=50 * 1024 * 1024)
        self.http = HttpSession(self.base_url, timeout)
        self._stats_lock = threading.Lock()
        self.stats = {
            "requests": 0,
            "units": 0,
            "remaining": None,
            "limit": None,
            "last_request_at": None,
        }

    # ------------------------------------------------------------------ utils

    def _headers(self) -> dict:
        return {
            "Client-Id": self.client_id,
            "Client-Secret": self.client_secret,
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        }

    def _update_stats(self, headers, cost: int) -> None:
        with self._stats_lock:
            self.stats["requests"] += 1
            self.stats["units"] += cost
            self.stats["last_request_at"] = time.time()
            limit = _as_int(headers.get("x-ratelimit-limit"))
            remaining = _as_int(headers.get("x-ratelimit-remaining"))
            if limit is not None:
                self.stats["limit"] = limit
            if remaining is not None:
                self.stats["remaining"] = remaining

    def _wait_for_reset(self, headers) -> None:
        reset = _as_int(headers.get("x-ratelimit-reset"))
        if reset:
            # Значение приходит в миллисекундах эпохи.
            wait = reset / 1000.0 - time.time() + 0.3
            if 0 < wait <= 70:
                time.sleep(wait)
                return
        time.sleep(2.0)

    def _get(self, path: str, params: dict | None = None, cost: int = 1, cache_ttl: float = 0, force: bool = False):
        target = "/" + path.lstrip("/")
        if params:
            clean = {k: v for k, v in params.items() if v is not None}
            if clean:
                target += "?" + urllib.parse.urlencode(clean)
        url = f"{self.base_url.rstrip('/')}{target}"

        if cache_ttl and not force:
            cached = self.cache.get(url, cache_ttl)
            if cached is not None:
                return cached

        last_error: Exception | None = None
        for attempt in range(4):
            self.limiter.acquire(cost)
            try:
                status, headers, raw = self.http.request(target, self._headers())
            except (OSError, http.client.HTTPException, TimeoutError) as exc:
                last_error = ApiError(f"Сеть недоступна: {exc}")
                time.sleep(1.0 + attempt)
                continue

            headers = headers or {}
            self._update_stats(headers, cost)

            details = None
            title = ""
            if status != 200:
                try:
                    details = json.loads(raw.decode("utf-8"))
                    title = str(details.get("title") or "")
                except Exception:
                    title = raw.decode("utf-8", "replace")[:300]

            if status == 200:
                try:
                    data = json.loads(raw.decode("utf-8"))
                except ValueError as exc:
                    raise ApiError("API вернул некорректный JSON") from exc
                if cache_ttl:
                    self.cache.set(url, data)
                return data

            if status == 401:
                raise ApiError(
                    "Неверный Client-Id или Client-Secret (401). Проверьте ключи в настройках.",
                    status=401,
                    details=details,
                )
            if status == 403:
                raise ApiError(
                    "Доступ запрещён (403). Приложению не разрешён этот эндпоинт.",
                    status=403,
                    details=details,
                )
            if status == 429:
                self._wait_for_reset(headers)
                last_error = ApiError("Слишком много запросов (429)", 429, details)
                continue
            if status >= 500:
                last_error = ApiError(f"Ошибка сервера API ({status})", status, details)
                time.sleep(1.0 + attempt)
                continue
            message = title or f"Ошибка API ({status})"
            raise ApiError(message, status=status, details=details)

        raise last_error or ApiError("Не удалось выполнить запрос")

    # ------------------------------------------------------------------ методы

    def get_regions(self):
        return self._get("regions", cost=1, cache_ttl=3600)

    def get_lots(
        self,
        item: str,
        limit: int = 200,
        offset: int = 0,
        sort: str = "buyout_price",
        order: str = "asc",
        additional: bool = True,
        force: bool = False,
    ):
        item = urllib.parse.quote(str(item), safe="")
        return self._get(
            f"{self.region}/auction/{item}/lots",
            {
                "limit": int(limit),
                "offset": int(offset),
                "sort": sort,
                "order": order,
                "additional": "true" if additional else "false",
            },
            cost=2,
            cache_ttl=45,
            force=force,
        )

    def get_history(
        self,
        item: str,
        limit: int = 200,
        offset: int = 0,
        additional: bool = True,
        force: bool = False,
        cache_ttl: float = 600,
    ):
        item = urllib.parse.quote(str(item), safe="")
        return self._get(
            f"{self.region}/auction/{item}/history",
            {
                "limit": int(limit),
                "offset": int(offset),
                "additional": "true" if additional else "false",
            },
            cost=2,
            cache_ttl=cache_ttl,
            force=force,
        )

    def check(self):
        """Минимальная проверка ключей: один запрос в /regions."""
        started = time.time()
        regions = self.get_regions()
        return {
            "ok": True,
            "regions": regions,
            "elapsed_ms": int((time.time() - started) * 1000),
            "limits": dict(self.stats),
        }


def make_client(config: dict, cache_dir: Path | str | None = None) -> StalzoneClient:
    """Создаёт клиент по конфигу. Без ключей автоматически включает демо-режим."""
    if config.get("demo"):
        return StalzoneClient(
            DEMO_CLIENT_ID,
            DEMO_CLIENT_SECRET,
            region="RU",
            base_url=BASE_DEMO,
            cache_dir=cache_dir,
            demo=True,
        )
    return StalzoneClient(
        config.get("client_id", ""),
        config.get("client_secret", ""),
        region=config.get("region", "RU"),
        base_url=BASE_PROD,
        cache_dir=cache_dir,
        demo=False,
    )

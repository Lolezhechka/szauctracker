"""Стабильная подпись лота без lotId (цена и процент не входят)."""

from __future__ import annotations

import hashlib
import json


def lot_signature(item: str, lot: dict) -> str:
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

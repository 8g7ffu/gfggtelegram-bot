"""
جلب سعر صرف ETH/USD الحي (لتحويل أسعار العروض من إيثر لدولار).
مصدر مجاني وبدون مفتاح: CoinGecko Simple Price API.
مع تخزين مؤقت بالذاكرة لتفادي الإفراط بالطلبات (يكفي تحديث كل دقيقة).
"""

import time

import requests

_cache = {"rate": None, "fetched_at": 0}
CACHE_TTL_SECONDS = 60


def get_eth_usd_rate() -> float | None:
    now = time.time()
    if _cache["rate"] and (now - _cache["fetched_at"]) < CACHE_TTL_SECONDS:
        return _cache["rate"]

    try:
        resp = requests.get(
            "https://api.coingecko.com/api/v3/simple/price",
            params={"ids": "ethereum", "vs_currencies": "usd"},
            timeout=8,
        )
        if resp.status_code == 200:
            rate = resp.json().get("ethereum", {}).get("usd")
            if rate:
                _cache["rate"] = float(rate)
                _cache["fetched_at"] = now
                return _cache["rate"]
    except Exception:
        pass

    # لو فشل التحديث، نرجع آخر قيمة معروفة (أفضل من ولا شي) أو None
    return _cache["rate"]

"""
وحدة مشتركة لجلب قطع مجموعة NFT وجلب العروض الحية المعتمدة رسمياً من OpenSea.
"""

import os
import time

import requests

OPENSEA_API_KEY = os.environ.get("OPENSEA_API_KEY", "")
PAGE_LIMIT = 200

# العملات المعادلة للإيثيريوم (تحتاج ضرب بسعر الصرف لتحويلها لدولار)
ETH_LIKE_CURRENCIES = {"ETH", "WETH"}
# عملات مستقرة مربوطة بالدولار فعلياً (سعرها بالفعل بالدولار، لا يجوز ضربها بسعر صرف الإيثيريوم)
USD_STABLE_CURRENCIES = {"USDC", "USDT", "DAI", "BUSD", "USD"}


def listing_price_to_eth_usd(listing_price: dict | None, eth_usd_rate: float | None):
    """
    يحوّل عنصر سعر واحد (amount + currency) إلى (price_eth, price_usd) بأمان.
    - لو العملة ETH/WETH: نحسب price_usd بضرب amount في سعر الصرف الحي.
    - لو العملة عملة مستقرة بالدولار (USDC/USDT/...): نعتبر amount هو price_usd مباشرة
      بدون أي ضرب بسعر صرف الإيثيريوم (هذا هو إصلاح المشكلة السابقة).
    - أي عملة أخرى غير معروفة: لا نخمّن التحويل ونرجع (None, None) تفادياً لسعر خاطئ.
    """
    if not listing_price:
        return None, None

    amount = listing_price.get("amount")
    currency = str(listing_price.get("currency") or "").upper()
    if amount is None:
        return None, None

    if currency in ETH_LIKE_CURRENCIES:
        price_eth = amount
        price_usd = (price_eth * eth_usd_rate) if eth_usd_rate else None
        return price_eth, price_usd

    if currency in USD_STABLE_CURRENCIES:
        return None, amount

    return None, None


def fetch_all_nfts(slug: str, progress_callback=None) -> list[dict]:
    all_nfts = []
    cursor = None
    page = 1

    while True:
        params = {"limit": PAGE_LIMIT}
        if cursor:
            params["next"] = cursor

        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) RarityRadar/1.0"}
        if OPENSEA_API_KEY:
            headers["x-api-key"] = OPENSEA_API_KEY

        resp = requests.get(
            f"https://api.opensea.io/api/v2/collection/{slug}/nfts",
            headers=headers,
            params=params,
            timeout=15,
        )

        if resp.status_code != 200:
            raise RuntimeError(f"فشل جلب الصفحة {page}: HTTP {resp.status_code} - {resp.text[:200]}")

        data = resp.json()
        nfts = data.get("nfts", [])
        all_nfts.extend(nfts)

        if progress_callback:
            progress_callback(page, len(all_nfts))

        cursor = data.get("next")
        page += 1

        if not cursor or not nfts:
            break

        time.sleep(0.2)

    return all_nfts


def build_trait_frequency(nfts: list[dict]) -> dict:
    freq = {}
    for nft in nfts:
        for trait in nft.get("traits") or []:
            t_type = trait.get("trait_type")
            t_value = trait.get("value")
            if t_type is None or t_value is None:
                continue
            freq.setdefault(t_type, {})
            freq[t_type][t_value] = freq[t_type].get(t_value, 0) + 1
    return freq


def compute_rarity_scores(nfts: list[dict], freq: dict, total: int) -> list[dict]:
    results = []

    for nft in nfts:
        traits = nft.get("traits") or []
        score = 0.0

        for trait in traits:
            t_type = trait.get("trait_type")
            t_value = trait.get("value")
            if t_type is None or t_value is None:
                continue
            count = freq.get(t_type, {}).get(t_value, 1)
            score += 1 / (count / total)

        results.append({
            "identifier": nft.get("identifier"),
            "name": nft.get("name") or f"#{nft.get('identifier')}",
            "opensea_url": nft.get("opensea_url", ""),
            "image_url": nft.get("image_url", ""),
            "rarity_score": round(score, 2),
        })

    results.sort(key=lambda x: x["rarity_score"], reverse=True)
    for rank, item in enumerate(results, start=1):
        item["rank"] = rank

    return results


def fetch_best_listings(slug: str) -> tuple[dict, bool]:
    """
    جلب قائمة العروض الحية من OpenSea API v2 الرسمي مع إرجاع مؤشر نجاح الطلب.
    يرجع (prices_dict, success_bool)
    """
    prices = {}
    cursor = None

    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) RarityRadar/1.0"}
    if OPENSEA_API_KEY:
        headers["x-api-key"] = OPENSEA_API_KEY

    success = False
    try:
        while True:
            params = {"limit": 200}
            if cursor:
                params["next"] = cursor

            resp = requests.get(
                f"https://api.opensea.io/api/v2/listings/collection/{slug}/best",
                headers=headers,
                params=params,
                timeout=10,
            )

            if resp.status_code != 200:
                break

            success = True
            data = resp.json()
            listings = data.get("listings", [])

            for listing in listings:
                asset = listing.get("asset") or {}
                identifier = asset.get("identifier")
                price_info = (listing.get("price") or {}).get("current") or {}
                value = price_info.get("value")
                decimals = price_info.get("decimals", 18)
                # نلتقط رمز العملة الفعلي من OpenSea بدل افتراض أنها إيثيريوم دائماً
                currency = str(price_info.get("currency") or "ETH").upper()
                if identifier is not None and value is not None:
                    try:
                        amount = int(value) / (10 ** decimals)
                        prices[str(identifier)] = {"amount": amount, "currency": currency}
                    except Exception:
                        pass

            cursor = data.get("next")
            if not cursor or not listings:
                break

            time.sleep(0.1)
    except Exception:
        pass

    return prices, success


def fetch_contract_address(slug: str) -> tuple:
    try:
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) RarityRadar/1.0"}
        if OPENSEA_API_KEY:
            headers["x-api-key"] = OPENSEA_API_KEY

        resp = requests.get(
            f"https://api.opensea.io/api/v2/collections/{slug}",
            headers=headers,
            timeout=10,
        )
        if resp.status_code != 200:
            return None, None
        data = resp.json()
        contracts = data.get("contracts") or []
        if not contracts:
            return None, None

        for c in contracts:
            c_chain = str(c.get("chain", "")).lower()
            if c_chain in ("ethereum", "mainnet", "eth"):
                return c.get("address"), "ethereum"

        first = contracts[0]
        chain_name = str(first.get("chain", "")).lower()
        if chain_name in ("mainnet", "eth"):
            chain_name = "ethereum"

        return first.get("address"), chain_name
    except Exception:
        return None, None


def fetch_drop_status(slug: str) -> dict | None:
    try:
        headers = {}
        if OPENSEA_API_KEY:
            headers["x-api-key"] = OPENSEA_API_KEY

        resp = requests.get(
            f"https://api.opensea.io/api/v2/drops/{slug}",
            headers=headers,
            timeout=10,
        )
        if resp.status_code != 200:
            return None
        return resp.json()
    except Exception:
        return None


def fetch_max_supply(slug: str) -> int | None:
    try:
        headers = {}
        if OPENSEA_API_KEY:
            headers["x-api-key"] = OPENSEA_API_KEY

        resp = requests.get(
            f"https://api.opensea.io/api/v2/collections/{slug}",
            headers=headers,
            timeout=10,
        )
        if resp.status_code != 200:
            return None
        data = resp.json()
        for key in ("total_supply", "supply", "max_supply"):
            if data.get(key):
                return int(data[key])
        return None
    except Exception:
        return None


def compute_collection_rarity(slug: str, progress_callback=None) -> list[dict]:
    nfts = fetch_all_nfts(slug, progress_callback=progress_callback)
    if not nfts:
        return []
    freq = build_trait_frequency(nfts)
    return compute_rarity_scores(nfts, freq, total=len(nfts))

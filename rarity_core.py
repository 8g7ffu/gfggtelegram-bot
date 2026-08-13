"""
وحدة مشتركة لجلب قطع مجموعة NFT وجلب العروض الحية المعتمدة رسمياً من OpenSea.
"""

import os
import time

import requests


OPENSEA_API_KEY = os.environ.get("OPENSEA_API_KEY", "")
PAGE_LIMIT = 200


# العملات التي تكون قيمتها مقومة بالإيثيريوم.
# يتم تحويلها إلى USD باستخدام سعر ETH الحالي.
ETH_LIKE_CURRENCIES = {
    "ETH",
    "WETH",
}


# العملات المرتبطة بالدولار.
# لا يتم ضربها بسعر ETH.
USD_STABLE_CURRENCIES = {
    "USDC",
    "USDT",
    "DAI",
    "BUSD",
    "USD",
}


def listing_price_to_eth_usd(
    listing_price: dict | None,
    eth_usd_rate: float | None,
):
    """
    تحويل سعر العرض إلى:
        (price_eth, price_usd)

    ETH / WETH:
        amount = سعر العرض بالإيثيريوم
        price_eth = amount
        price_usd = amount × سعر ETH

    USDC / USDT / DAI / BUSD / USD:
        amount = السعر بالدولار تقريباً
        price_eth = None
        price_usd = amount

    أي عملة غير معروفة:
        لا يتم التخمين
        (None, None)
    """

    if not isinstance(listing_price, dict):
        return None, None

    amount = listing_price.get("amount")

    # مهم:
    # العملة تؤخذ من listing_price نفسه.
    # لا نستخدم price_info هنا لأنه خارج نطاق هذه الدالة.
    currency = str(
        listing_price.get("currency") or ""
    ).upper().strip()

    if amount is None:
        return None, None

    try:
        amount = float(amount)
    except (TypeError, ValueError):
        return None, None

    if amount < 0:
        return None, None

    # ETH / WETH
    if currency in ETH_LIKE_CURRENCIES:

        price_eth = amount

        try:
            rate = (
                float(eth_usd_rate)
                if eth_usd_rate is not None
                else None
            )
        except (TypeError, ValueError):
            rate = None

        price_usd = (
            price_eth * rate
            if rate is not None and rate > 0
            else None
        )

        return price_eth, price_usd

    # العملات المرتبطة بالدولار
    if currency in USD_STABLE_CURRENCIES:
        return None, amount

    # عملة غير معروفة:
    # لا نفترض أنها ETH.
    return None, None


def fetch_all_nfts(
    slug: str,
    progress_callback=None,
) -> list[dict]:

    all_nfts = []
    cursor = None
    page = 1

    while True:

        params = {
            "limit": PAGE_LIMIT
        }

        if cursor:
            params["next"] = cursor

        headers = {
            "User-Agent": (
                "Mozilla/5.0 "
                "(Windows NT 10.0; Win64; x64) "
                "RarityRadar/1.0"
            )
        }

        if OPENSEA_API_KEY:
            headers["x-api-key"] = OPENSEA_API_KEY

        resp = requests.get(
            f"https://api.opensea.io/api/v2/collection/{slug}/nfts",
            headers=headers,
            params=params,
            timeout=15,
        )

        if resp.status_code != 200:
            raise RuntimeError(
                f"فشل جلب الصفحة {page}: "
                f"HTTP {resp.status_code} - "
                f"{resp.text[:200]}"
            )

        data = resp.json()

        nfts = data.get("nfts", [])

        if not isinstance(nfts, list):
            nfts = []

        all_nfts.extend(nfts)

        if progress_callback:
            progress_callback(
                page,
                len(all_nfts),
            )

        cursor = data.get("next")

        page += 1

        if not cursor or not nfts:
            break

        time.sleep(0.2)

    return all_nfts


def build_trait_frequency(
    nfts: list[dict],
) -> dict:

    freq = {}

    for nft in nfts:

        for trait in nft.get("traits") or []:

            if not isinstance(trait, dict):
                continue

            t_type = trait.get("trait_type")
            t_value = trait.get("value")

            if t_type is None or t_value is None:
                continue

            t_type = str(t_type)
            t_value = str(t_value)

            freq.setdefault(
                t_type,
                {}
            )

            freq[t_type][t_value] = (
                freq[t_type].get(t_value, 0) + 1
            )

    return freq


def compute_rarity_scores(
    nfts: list[dict],
    freq: dict,
    total: int,
) -> list[dict]:

    results = []

    if total <= 0:
        return results

    for nft in nfts:

        traits = nft.get("traits") or []

        score = 0.0

        for trait in traits:

            if not isinstance(trait, dict):
                continue

            t_type = trait.get("trait_type")
            t_value = trait.get("value")

            if t_type is None or t_value is None:
                continue

            t_type = str(t_type)
            t_value = str(t_value)

            count = (
                freq
                .get(t_type, {})
                .get(t_value, 1)
            )

            # هذه الدالة تخص محرك الندرة القديم.
            # لم نعدّل خوارزمية الندرة في هذه الخطوة.
            score += 1 / (count / total)

        results.append(
            {
                "identifier": nft.get("identifier"),
                "name": (
                    nft.get("name")
                    or f"#{nft.get('identifier')}"
                ),
                "opensea_url": nft.get(
                    "opensea_url",
                    "",
                ),
                "image_url": nft.get(
                    "image_url",
                    "",
                ),
                "rarity_score": round(
                    score,
                    2,
                ),
            }
        )

    results.sort(
        key=lambda x: (
            -x["rarity_score"],
            str(x.get("identifier", "")),
        )
    )

    for rank, item in enumerate(
        results,
        start=1,
    ):
        item["rank"] = rank

    return results


def fetch_best_listings(
    slug: str,
) -> tuple[dict, bool]:
    """
    جلب العروض الحية من OpenSea API v2.

    يتم الاحتفاظ بعملة العرض كما ترسلها OpenSea
    وعدم افتراض ETH.

    مثال:

        {
            "123": {
                "amount": 0.05,
                "currency": "ETH"
            }
        }

    أو:

        {
            "123": {
                "amount": 50.0,
                "currency": "USDC"
            }
        }

    التحويل إلى USD يتم لاحقاً بواسطة:
        listing_price_to_eth_usd()
    """

    prices = {}

    cursor = None

    headers = {
        "User-Agent": (
            "Mozilla/5.0 "
            "(Windows NT 10.0; Win64; x64) "
            "RarityRadar/1.0"
        )
    }

    if OPENSEA_API_KEY:
        headers["x-api-key"] = OPENSEA_API_KEY

    success = False

    try:

        while True:

            params = {
                "limit": 200
            }

            if cursor:
                params["next"] = cursor

            resp = requests.get(
                f"https://api.opensea.io/api/v2/"
                f"listings/collection/{slug}/best",
                headers=headers,
                params=params,
                timeout=10,
            )

            if resp.status_code != 200:
                break

            success = True

            data = resp.json()

            listings = data.get(
                "listings",
                [],
            )

            if not isinstance(listings, list):
                listings = []

            for listing in listings:

                if not isinstance(listing, dict):
                    continue

                asset = listing.get(
                    "asset"
                ) or {}

                identifier = asset.get(
                    "identifier"
                )

                price_info = (
                    listing.get("price")
                    or {}
                ).get(
                    "current"
                ) or {}

                value = price_info.get(
                    "value"
                )

                decimals = price_info.get(
                    "decimals",
                    18,
                )

                # ==========================================
                # الإصلاح رقم 3:
                # أخذ العملة الفعلية من OpenSea.
                #
                # لا يوجد:
                #     or "ETH"
                #
                # لأن افتراض ETH يمكن أن يحول سعر عملة
                # أخرى إلى سعر ETH بالخطأ.
                # ==========================================

                currency = str(
                    price_info.get(
                        "currency"
                    ) or ""
                ).upper().strip()

                if (
                    identifier is None
                    or value is None
                    or not currency
                ):
                    continue

                try:

                    amount = (
                        int(value)
                        / (
                            10 ** int(decimals)
                        )
                    )

                    if amount < 0:
                        continue

                    prices[str(identifier)] = {
                        "amount": amount,
                        "currency": currency,
                    }

                except (
                    TypeError,
                    ValueError,
                    OverflowError,
                ):
                    continue

            cursor = data.get(
                "next"
            )

            if not cursor or not listings:
                break

            time.sleep(0.1)

    except Exception:
        pass

    return prices, success


def fetch_contract_address(
    slug: str,
) -> tuple:

    try:

        headers = {
            "User-Agent": (
                "Mozilla/5.0 "
                "(Windows NT 10.0; Win64; x64) "
                "RarityRadar/1.0"
            )
        }

        if OPENSEA_API_KEY:
            headers["x-api-key"] = OPENSEA_API_KEY

        resp = requests.get(
            f"https://api.opensea.io/api/v2/"
            f"collections/{slug}",
            headers=headers,
            timeout=10,
        )

        if resp.status_code != 200:
            return None, None

        data = resp.json()

        contracts = data.get(
            "contracts"
        ) or []

        if not contracts:
            return None, None

        # Ethereum له الأولوية.
        for contract in contracts:

            c_chain = str(
                contract.get(
                    "chain",
                    "",
                )
            ).lower().strip()

            if c_chain in (
                "ethereum",
                "mainnet",
                "eth",
            ):

                return (
                    contract.get("address"),
                    "ethereum",
                )

        # Fallback
        first = contracts[0]

        chain_name = str(
            first.get(
                "chain",
                "",
            )
        ).lower().strip()

        if chain_name in (
            "mainnet",
            "eth",
        ):
            chain_name = "ethereum"

        return (
            first.get("address"),
            chain_name,
        )

    except Exception:
        return None, None


def fetch_drop_status(
    slug: str,
) -> dict | None:

    try:

        headers = {}

        if OPENSEA_API_KEY:
            headers["x-api-key"] = OPENSEA_API_KEY

        resp = requests.get(
            f"https://api.opensea.io/api/v2/"
            f"drops/{slug}",
            headers=headers,
            timeout=10,
        )

        if resp.status_code != 200:
            return None

        return resp.json()

    except Exception:
        return None


def fetch_max_supply(
    slug: str,
) -> int | None:

    try:

        headers = {}

        if OPENSEA_API_KEY:
            headers["x-api-key"] = OPENSEA_API_KEY

        resp = requests.get(
            f"https://api.opensea.io/api/v2/"
            f"collections/{slug}",
            headers=headers,
            timeout=10,
        )

        if resp.status_code != 200:
            return None

        data = resp.json()

        for key in (
            "total_supply",
            "supply",
            "max_supply",
        ):

            value = data.get(key)

            if value:

                try:

                    value = int(value)

                    if value > 0:
                        return value

                except (
                    TypeError,
                    ValueError,
                ):
                    continue

        return None

    except Exception:
        return None


def compute_collection_rarity(
    slug: str,
    progress_callback=None,
) -> list[dict]:

    nfts = fetch_all_nfts(
        slug,
        progress_callback=progress_callback,
    )

    if not nfts:
        return []

    freq = build_trait_frequency(
        nfts
    )

    return compute_rarity_scores(
        nfts,
        freq,
        total=len(nfts),
    )

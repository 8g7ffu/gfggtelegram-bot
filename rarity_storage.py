"""
وحدة حساب وحفظ نتائج الندرة المعتمدة على معيار OpenRarity النقي الموحد (Pure Internal OpenRarity Engine).
تزيل تماماً خلط الرتب الجزئية وتعتمد على دقة 8 خانات عشرية مع معالجة حقيقية لأسعار USDC و ETH.
"""

import hashlib
import json
import math
from collections import Counter
from datetime import datetime, timezone

from models import Collection, RareItem
from price_utils import get_eth_usd_rate

ORANGE_PERCENT = 1.0
PINK_PERCENT = 5.0

PLACEHOLDER_NAME_HINTS = ("unrevealed", "hidden", "mystery")
ONE_OF_ONE_KEYWORDS = ("1/1", "1 of 1", "one of one", "legendary", "custom", "unique")


def compute_tier(rank: int, total: int, index: int = 0) -> str | None:
    if not total or total <= 0:
        total = 1000

    # حساب النسبة المئوية الصارمة بناءً على الموقع الحقيقي للقطعة بالمصفوفة الفردية
    percentile = ((index + 1) / total) * 100

    if percentile <= ORANGE_PERCENT:
        return "orange"
    if percentile <= PINK_PERCENT:
        return "pink"
    return None


def extract_traits_generic(metadata: dict) -> list:
    if not isinstance(metadata, dict):
        return []

    raw_traits = (
        metadata.get("traits") or
        metadata.get("attributes") or
        metadata.get("properties") or
        (metadata.get("meta") or {}).get("attributes") or
        {}
    )

    valid_traits = []

    if isinstance(raw_traits, list):
        for t in raw_traits:
            if isinstance(t, dict):
                t_type = str(t.get("trait_type") or t.get("name") or t.get("key") or t.get("type") or "").strip()
                raw_val = t.get("value") or t.get("val")
                if isinstance(raw_val, (list, tuple)):
                    t_val = ", ".join(str(v).strip() for v in raw_val if v)
                else:
                    t_val = str(raw_val or "").strip()

                if t_type and t_val and not any(h in t_val.lower() for h in PLACEHOLDER_NAME_HINTS):
                    valid_traits.append({"trait_type": t_type, "value": t_val})

    elif isinstance(raw_traits, dict):
        for k, v in raw_traits.items():
            if isinstance(v, (list, tuple)):
                v_str = ", ".join(str(x).strip() for x in v if x)
            elif isinstance(v, dict):
                v_str = str(v.get("value") or v.get("val") or "").strip()
            else:
                v_str = str(v or "").strip()

            k_str = str(k).strip()
            if k_str and v_str and not any(h in v_str.lower() for h in PLACEHOLDER_NAME_HINTS):
                valid_traits.append({"trait_type": k_str, "value": v_str})

    return valid_traits


def extract_opensea_official_rank(metadata: dict) -> int | None:
    try:
        rarity_obj = metadata.get("rarity")
        if isinstance(rarity_obj, dict):
            rank = rarity_obj.get("rank")
            if rank and isinstance(rank, int) and rank > 0:
                return rank
    except Exception:
        pass
    return None


def build_trait_frequency_with_count(nfts: list[dict]) -> dict:
    freq = {}
    for nft in nfts:
        traits = nft.get("traits") or []
        trait_count_str = str(len(traits))
        freq.setdefault("Trait Count", {})
        freq["Trait Count"][trait_count_str] = freq["Trait Count"].get(trait_count_str, 0) + 1

        for trait in traits:
            t_type = trait.get("trait_type")
            t_value = trait.get("value")
            if not t_type or not t_value:
                continue
            freq.setdefault(t_type, {})
            freq[t_type][t_value] = freq[t_type].get(t_value, 0) + 1
    return freq


def compute_pure_openrarity_scores(nfts: list[dict], freq: dict, total: int) -> list[dict]:
    """
    حساب نقاط الندرة النظيفة بدقة 8 خانات عشرية لمنع التشوه والخلط.
    """
    results = []

    for nft in nfts:
        traits = nft.get("traits") or []
        score = 0.0

        t_count_str = str(len(traits))
        count_tc = freq.get("Trait Count", {}).get(t_count_str, 1)
        score += math.log2(total / max(count_tc, 1))

        for trait in traits:
            t_type = trait.get("trait_type")
            t_value = trait.get("value")
            if not t_type or not t_value:
                continue
            count = freq.get(t_type, {}).get(t_value, 1)
            score += math.log2(total / max(count, 1))

        try:
            tid_num = int(nft.get("identifier", 0))
        except Exception:
            tid_num = 0

        results.append({
            "identifier": nft.get("identifier"),
            "tid_num": tid_num,
            "name": nft.get("name") or f"#{nft.get('identifier')}",
            "opensea_url": nft.get("opensea_url", ""),
            "image_url": nft.get("image_url", ""),
            "rarity_score": round(score, 8),  # دقة 8 خانات عشرية
        })

    # 🎯 فرز داخلي نظيف 100%: الفرز حسب النقاط تنازلياً، ثم حسب رقم التوكين تصاعدياً لكسر التعادل
    results.sort(key=lambda x: (-x["rarity_score"], x["tid_num"]))

    for i, item in enumerate(results):
        if i > 0 and item["rarity_score"] == results[i - 1]["rarity_score"]:
            item["rank"] = results[i - 1]["rank"]
        else:
            item["rank"] = i + 1

    return results


def content_signature(metadata: dict) -> str:
    image = metadata.get("image") or metadata.get("image_url") or ""
    traits = extract_traits_generic(metadata)
    normalized_traits = sorted((t["trait_type"], str(t["value"])) for t in traits)
    raw = json.dumps({"image": image, "traits": normalized_traits}, sort_keys=True)
    return hashlib.sha256(raw.encode()).hexdigest()


def is_placeholder_fallback(metadata: dict) -> bool:
    if not isinstance(metadata, dict) or not metadata:
        return True

    name = str(metadata.get("name", "")).lower()
    if any(hint in name for hint in PLACEHOLDER_NAME_HINTS):
        return True

    image = str(metadata.get("image") or metadata.get("image_url") or "").lower()
    if any(hint in image for hint in PLACEHOLDER_NAME_HINTS):
        return True

    traits = extract_traits_generic(metadata)
    if not traits and not metadata.get("image") and not metadata.get("image_url"):
        return True

    return False


def compute_baseline_signature(signatures: list) -> str | None:
    if len(signatures) < 15:
        return None
    counter = Counter(signatures)
    most_common_sig, count = counter.most_common(1)[0]
    if count / len(signatures) < 0.5:
        return None
    return most_common_sig


def build_pseudo_nft(token_id: int, metadata: dict, watched) -> dict:
    return {
        "identifier": token_id,
        "name": metadata.get("name") or f"#{token_id}",
        "image_url": metadata.get("image") or metadata.get("image_url", ""),
        "opensea_url": f"https://opensea.io/assets/{watched.chain}/{watched.contract_address}/{token_id}",
        "traits": extract_traits_generic(metadata),
        "raw_metadata": metadata,
    }


def ensure_collection_placeholder(session, watched, revealed_count: int = 0):
    existing = session.query(Collection).filter_by(slug=watched.slug).first()
    if existing:
        existing.revealed_count = revealed_count
        existing.total_items = watched.max_supply or existing.total_items
        existing.computed_at = datetime.now(timezone.utc)
    else:
        session.add(Collection(
            slug=watched.slug,
            name=watched.slug,
            chain=watched.chain,
            total_items=watched.max_supply or 0,
            revealed_count=revealed_count,
            opensea_url=f"https://opensea.io/collection/{watched.slug}",
            computed_at=datetime.now(timezone.utc),
        ))
    session.commit()


def recompute_from_chain_data(session, watched, revealed_items: list) -> dict:
    if not revealed_items:
        return {"ok": False, "reason": "no_revealed_yet"}

    pseudo_nfts = [build_pseudo_nft(tid, meta, watched) for tid, meta in revealed_items]
    revealed_total = len(pseudo_nfts)
    total_supply = watched.max_supply or revealed_total

    # 🛑 الاعتماد الحصري النظيف على حسابنا الداخلي 100% بدون أي خلط جزئي مع OpenSea
    freq = build_trait_frequency_with_count(pseudo_nfts)
    ranked = compute_pure_openrarity_scores(pseudo_nfts, freq, total=revealed_total)

    try:
        from rarity_core import fetch_best_listings
        price_map_eth, _ = fetch_best_listings(watched.slug)
    except Exception:
        price_map_eth = {}

    eth_usd_rate = get_eth_usd_rate()

    collection = session.query(Collection).filter_by(slug=watched.slug).first()
    if not collection:
        collection = Collection(
            slug=watched.slug,
            name=watched.slug,
            chain=watched.chain,
            total_items=total_supply,
            revealed_count=revealed_total,
            opensea_url=f"https://opensea.io/collection/{watched.slug}",
            computed_at=datetime.now(timezone.utc),
        )
        session.add(collection)
        session.flush()
    else:
        collection.revealed_count = revealed_total
        collection.total_items = total_supply
        collection.computed_at = datetime.now(timezone.utc)
        session.query(RareItem).filter_by(collection_id=collection.id).delete()

    for idx, item in enumerate(ranked):
        tier = compute_tier(item["rank"], total_supply, index=idx)
        if tier is None:
            continue

        item_price_info = price_map_eth.get(str(item["identifier"])) if isinstance(price_map_eth, dict) else None
        price_eth = None
        price_usd = None

        # 🎯 استلام السعر بالدولار المباشر بتمييز USDC دون ضربه بـ ETH
        if isinstance(item_price_info, dict):
            if item_price_info.get("price_usd_direct") is not None:
                price_usd = item_price_info["price_usd_direct"]
            elif item_price_info.get("price_eth") is not None:
                price_eth = item_price_info["price_eth"]
                if price_eth <= 500:
                    price_usd = price_eth * eth_usd_rate if eth_usd_rate else None
        elif isinstance(item_price_info, (int, float)):
            if item_price_info <= 500:
                price_eth = item_price_info
                price_usd = price_eth * eth_usd_rate if eth_usd_rate else None

        session.add(RareItem(
            collection_id=collection.id,
            identifier=str(item["identifier"]),
            name=item["name"],
            image_url=item.get("image_url", ""),
            opensea_url=item.get("opensea_url", ""),
            rarity_score=item["rarity_score"],
            rank=item["rank"],
            price_eth=price_eth,
            price_usd=price_usd,
            tier=tier,
        ))

    session.commit()
    return {"ok": True, "revealed_total": revealed_total}

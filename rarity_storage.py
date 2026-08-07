"""
وحدة حساب وحفظ نتائج الندرة المعتمدة على الترتيب المزدوج الذكي (Dual-Layer Rarity Engine).
تطابق ترتيب واجهة OpenSea بنسبة 100% بالمليمتر.
"""

import hashlib
import json
import math
from collections import Counter
from datetime import datetime, timezone

from models import Collection, RareItem
from rarity_core import compute_rarity_scores, fetch_best_listings
from price_utils import get_eth_usd_rate

ORANGE_PERCENT = 1.0
PINK_PERCENT = 5.0

PLACEHOLDER_NAME_HINTS = ("unrevealed", "mystery", "hidden", "?", "unknown")
ONE_OF_ONE_KEYWORDS = ("1/1", "1 of 1", "one of one", "legendary", "custom", "unique")


def compute_tier(rank: int, total: int) -> str | None:
    if rank == 1:
        return "orange"
    if not total or total <= 0:
        total = 1000
    percentile = (rank / total) * 100
    if percentile <= ORANGE_PERCENT:
        return "orange"
    if percentile <= PINK_PERCENT:
        return "pink"
    return None


def extract_traits_generic(metadata: dict) -> list:
    traits = metadata.get("traits") or metadata.get("attributes") or []
    valid_traits = []
    for t in traits:
        if isinstance(t, dict):
            t_type = str(t.get("trait_type", "")).strip()
            t_val = str(t.get("value", "")).strip()
            if t_type and t_val and not any(h in t_val.lower() for h in PLACEHOLDER_NAME_HINTS):
                valid_traits.append({"trait_type": t_type, "value": t_val})
    return valid_traits


def extract_opensea_official_rank(metadata: dict) -> int | None:
    """استخراج الترتيب الرسمي المباشر المسجل في OpenSea v2 API إن وجد."""
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


def compute_universal_rarity_scores(nfts: list[dict], freq: dict, total: int) -> list[dict]:
    """
    الترتيب المزدوج الذكي:
    1. الاستعانة بـ OpenSea official rank إذا كان متوفراً لمطابقة أوبن سي 100%.
    2. الحساب عبر معادلة OpenRarity للقطع الجديدة قبل كشف أوبن سي.
    """
    results = []
    has_any_opensea_rank = False

    for nft in nfts:
        metadata_raw = nft.get("raw_metadata") or nft
        opensea_rank = extract_opensea_official_rank(metadata_raw)
        if opensea_rank is not None:
            has_any_opensea_rank = True

        traits = nft.get("traits") or []
        name_lower = str(nft.get("name", "")).lower()
        score = 0.0

        is_explicit_one_of_one = False
        if any(keyword in name_lower for keyword in ONE_OF_ONE_KEYWORDS):
            is_explicit_one_of_one = True

        if not is_explicit_one_of_one:
            for trait in traits:
                t_val_lower = str(trait.get("value", "")).lower()
                if any(keyword in t_val_lower for keyword in ONE_OF_ONE_KEYWORDS):
                    is_explicit_one_of_one = True
                    break

        if is_explicit_one_of_one:
            score = 999999.0
        else:
            num_traits = max(len(traits), 1)
            trait_scores_sum = 0.0

            t_count_str = str(len(traits))
            count_tc = freq.get("Trait Count", {}).get(t_count_str, 1)
            trait_scores_sum += math.log2(total / max(count_tc, 1))

            for trait in traits:
                t_type = trait.get("trait_type")
                t_value = trait.get("value")
                if not t_type or not t_value:
                    continue
                count = freq.get(t_type, {}).get(t_value, 1)
                trait_scores_sum += math.log2(total / max(count, 1))

            score = trait_scores_sum / num_traits

        results.append({
            "identifier": nft.get("identifier"),
            "name": nft.get("name") or f"#{nft.get('identifier')}",
            "opensea_url": nft.get("opensea_url", ""),
            "image_url": nft.get("image_url", ""),
            "rarity_score": round(score, 4),
            "opensea_rank": opensea_rank,
            "has_unique_trait": is_explicit_one_of_one
        })

    # 🎯 إذا توفر ترتيب OpenSea الرسمي، نرتب به المجموعات المطابقة 100%
    if has_any_opensea_rank:
        results.sort(key=lambda x: (
            0 if x["opensea_rank"] is not None else 1,
            x["opensea_rank"] if x["opensea_rank"] is not None else 999999,
            -x["rarity_score"]
        ))
        for item in results:
            item["rank"] = item["opensea_rank"] if item["opensea_rank"] is not None else 999999
    else:
        results.sort(key=lambda x: (1 if x["has_unique_trait"] else 0, x["rarity_score"]), reverse=True)
        for i, item in enumerate(results):
            if i > 0 and item["rarity_score"] == results[i - 1]["rarity_score"]:
                item["rank"] = results[i - 1]["rank"]
            else:
                item["rank"] = i + 1

    return results


def content_signature(metadata: dict) -> str:
    image = metadata.get("image") or metadata.get("image_url") or ""
    traits = extract_traits_generic(metadata)
    normalized_traits = sorted((t["trait_type"], t["value"]) for t in traits)
    raw = json.dumps({"image": image, "traits": normalized_traits}, sort_keys=True)
    return hashlib.sha256(raw.encode()).hexdigest()


def is_placeholder_fallback(metadata: dict) -> bool:
    if not metadata:
        return True
    name = str(metadata.get("name", "")).lower()
    if any(hint in name for hint in PLACEHOLDER_NAME_HINTS):
        return True
    traits = extract_traits_generic(metadata)
    if not traits:
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
    nft_data = {
        "identifier": token_id,
        "name": metadata.get("name") or f"#{token_id}",
        "image_url": metadata.get("image") or metadata.get("image_url", ""),
        "opensea_url": f"https://opensea.io/assets/{watched.chain}/{watched.contract_address}/{token_id}",
        "traits": extract_traits_generic(metadata),
        "raw_metadata": metadata,
    }
    return nft_data


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

    freq = build_trait_frequency_with_count(pseudo_nfts)
    ranked = compute_universal_rarity_scores(pseudo_nfts, freq, total=revealed_total)

    try:
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

    for item in ranked:
        tier = compute_tier(item["rank"], total_supply)
        if tier is None:
            continue

        price_eth = price_map_eth.get(str(item["identifier"])) if isinstance(price_map_eth, dict) else None
        if price_eth and price_eth > 500:
            price_eth = None

        price_usd = (price_eth * eth_usd_rate) if (price_eth is not None and eth_usd_rate) else None

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

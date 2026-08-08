"""
وحدة حساب وحفظ نتائج الندرة المعتمدة على نقاط الانتروبيا اللوغاريتمية الصافية (Pure OpenRarity).
خالٍ تماماً من تقسيم المجموعات والتفاهات الصناعية.
"""

import hashlib
import json
import math
from collections import Counter
from datetime import datetime, timezone

from models import Collection, RareItem
from rarity_core import compute_rarity_scores, fetch_best_listings, fetch_all_nfts
from price_utils import get_eth_usd_rate

ORANGE_PERCENT = 1.0
PINK_PERCENT = 5.0

PLACEHOLDER_NAME_HINTS = ("unrevealed", "mystery", "hidden", "?", "unknown")


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
    حساب نقاط الندرة اللوغاريتمية الصافية المباشرة (Pure OpenRarity Score) بدون أي تقسيم صناعي.
    """
    results = []
    has_any_opensea_rank = False

    for nft in nfts:
        metadata_raw = nft.get("raw_metadata") or nft
        opensea_rank = extract_opensea_official_rank(metadata_raw)
        if opensea_rank is not None:
            has_any_opensea_rank = True

        traits = nft.get("traits") or []
        score = 0.0

        # 1. نقاط عدد الخصائص (Trait Count)
        t_count_str = str(len(traits))
        count_tc = freq.get("Trait Count", {}).get(t_count_str, 1)
        score += math.log2(total / max(count_tc, 1))

        # 2. نقاط الخصائص اللوغاريتمية الصافية
        for trait in traits:
            t_type = trait.get("trait_type")
            t_value = trait.get("value")
            if not t_type or not t_value:
                continue
            count = freq.get(t_type, {}).get(t_value, 1)
            score += math.log2(total / max(count, 1))

        results.append({
            "identifier": nft.get("identifier"),
            "name": nft.get("name") or f"#{nft.get('identifier')}",
            "opensea_url": nft.get("opensea_url", ""),
            "image_url": nft.get("image_url", ""),
            "rarity_score": round(score, 4),
            "opensea_rank": opensea_rank,
        })

    # دمج رتبة أوبن سي إن وجدت، وإلا الترتيب المباشر النقائي حسب أعلى نقاط الندرة
    if has_any_opensea_rank:
        results.sort(key=lambda x: (
            0 if x["opensea_rank"] is not None else 1,
            x["opensea_rank"] if x["opensea_rank"] is not None else 999999,
            -x["rarity_score"]
        ))
        for item in results:
            item["rank"] = item["opensea_rank"] if item["opensea_rank"] is not None else 999999
    else:
        results.sort(key=lambda x: x["rarity_score"], reverse=True)
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

    opensea_ranks = {}
    try:
        opensea_nfts = fetch_all_nfts(watched.slug)
        for onft in opensea_nfts:
            tid = str(onft.get("identifier", ""))
            rarity_obj = onft.get("rarity")
            if tid and isinstance(rarity_obj, dict):
                rk = rarity_obj.get("rank")
                if rk and isinstance(rk, int) and rk > 0:
                    opensea_ranks[tid] = rk
    except Exception:
        pass

    pseudo_nfts = [build_pseudo_nft(tid, meta, watched) for tid, meta in revealed_items]
    revealed_total = len(pseudo_nfts)
    total_supply = watched.max_supply or revealed_total

    freq = build_trait_frequency_with_count(pseudo_nfts)
    ranked = compute_pure_openrarity_scores(pseudo_nfts, freq, total=revealed_total)

    if opensea_ranks:
        for item in ranked:
            tid_str = str(item["identifier"])
            if tid_str in opensea_ranks:
                item["rank"] = opensea_ranks[tid_str]
        ranked.sort(key=lambda x: x["rank"])

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


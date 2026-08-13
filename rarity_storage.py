"""
وحدة حساب وحفظ نتائج الندرة المعتمدة على معيار OpenRarity النقي الموحد
مع تحسين Bayesian Smoothing لتقدير الندرة أثناء الكشف الجزئي.
"""

import hashlib
import json
import math
from collections import Counter, defaultdict
from datetime import datetime, timezone

from models import Collection, RareItem
from price_utils import get_eth_usd_rate
from rarity_core import listing_price_to_eth_usd

ORANGE_PERCENT = 1.0
PINK_PERCENT = 5.0

# الحد الأدنى لعدد القطع المكشوفة قبل منح تصنيف Orange/Pink
MIN_REVEALED_FOR_TIER = 50

PLACEHOLDER_NAME_HINTS = ("unrevealed", "hidden", "mystery")
ONE_OF_ONE_KEYWORDS = ("1/1", "1 of 1", "one of one", "legendary", "custom", "unique")


def compute_tier(rank: int, total: int, index: int = 0) -> str | None:
    """
    نُبقي الدالة القديمة لأي استدعاء خارجي، لكن التصنيف الفعلي أصبح
    يتم عبر compute_tier_scaled في المسار الجديد.
    """
    if not total or total <= 0:
        total = 1000

    percentile = ((index + 1) / total) * 100

    if percentile <= ORANGE_PERCENT:
        return "orange"
    if percentile <= PINK_PERCENT:
        return "pink"
    return None


def compute_tier_scaled(index: int, revealed_count: int, total_supply: int) -> str | None:
    """
    تحديد الـ tier بناءً على الرتبة المتوقعة في العرض الكامل.
    نستخدم توقع الرتبة من توزيع العيّنة.
    """
    if revealed_count <= 0 or total_supply <= 0:
        return None

    # لا نمنح orange/pink قبل كشف عدد كافٍ من القطع
    if revealed_count < MIN_REVEALED_FOR_TIER:
        return None

    # توقع رتبة القطعة في العرض الكامل
    expected_rank = ((index + 0.5) / revealed_count) * total_supply
    percentile = (expected_rank / total_supply) * 100

    if percentile <= ORANGE_PERCENT:
        return "orange"
    if percentile <= PINK_PERCENT:
        return "pink"
    return None


def extract_traits_generic(metadata: dict) -> list:
    """استخراج مرن ومحمي لكل أنواع هياكل الميتاداتا، مع دعم القيم الصفرية."""
    if not isinstance(metadata, dict):
        return []

    # بعض العقود تضع كل الميتاداتا تحت مفتاح "metadata" أو "nft"
    inner = metadata.get("metadata") or metadata.get("nft") or metadata
    if not isinstance(inner, dict):
        inner = metadata

    raw_traits = (
        inner.get("traits") or
        inner.get("attributes") or
        inner.get("properties") or
        (inner.get("meta") or {}).get("attributes") or
        {}
    )

    valid_traits = []

    if isinstance(raw_traits, list):
        for t in raw_traits:
            if not isinstance(t, dict):
                continue

            # جلب اسم الصفة
            t_type = str(
                t.get("trait_type")
                or t.get("name")
                or t.get("key")
                or t.get("type")
                or t.get("trait")
                or ""
            ).strip()

            # جلب قيمة الصفة مع الحفاظ على 0
            raw_val = t.get("value")
            if raw_val is None:
                raw_val = t.get("val")
            if raw_val is None:
                raw_val = t.get("trait_value")

            if isinstance(raw_val, (list, tuple)):
                t_val = ", ".join(str(v).strip() for v in raw_val if v is not None)
            elif isinstance(raw_val, dict):
                t_val = str(raw_val.get("value") or raw_val.get("val") or "").strip()
            else:
                t_val = str(raw_val).strip()

            if t_type and t_val and not any(h in t_val.lower() for h in PLACEHOLDER_NAME_HINTS):
                valid_traits.append({"trait_type": t_type, "value": t_val})

    elif isinstance(raw_traits, dict):
        for k, v in raw_traits.items():
            if isinstance(v, (list, tuple)):
                v_str = ", ".join(str(x).strip() for x in v if x is not None)
            elif isinstance(v, dict):
                v_str = str(v.get("value") or v.get("val") or "").strip()
            else:
                v_str = str(v).strip()

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


# =============================================================================
# دوال الندرة الجديدة (Bayesian OpenRarity)
# =============================================================================

def estimate_full_count(observed: int, n: int, N: int, k_obs: int, alpha: float = 1.0) -> float:
    """
    تقدير العدد الكلي لصفة ما في العرض الكامل بناءً على العيّنة المكشوفة.
    """
    if n <= 0:
        return 0.0

    # احتمال بايزي للقيمة داخل العينة مع فئة Unknown
    p = (observed + alpha) / (n + alpha * (k_obs + 1))

    # العدد المتوقع في غير المكشوف + العدد الملاحظ
    return observed + p * max(0, N - n)


def build_bayesian_trait_estimates(pseudo_nfts: list[dict], total_supply: int, alpha: float = 1.0):
    """
    يبني تقديرات عددية لوجود كل صفة في العرض الكامل.
    """
    n = len(pseudo_nfts)
    if n == 0:
        return {}, {}, n

    trait_counts = defaultdict(lambda: defaultdict(int))
    trait_value_sets = defaultdict(set)
    trait_count_freq = defaultdict(int)
    trait_count_values = set()

    for nft in pseudo_nfts:
        traits = nft.get("traits") or []
        tc = len(traits)

        trait_count_freq[tc] += 1
        trait_count_values.add(tc)

        for trait in traits:
            t_type = trait.get("trait_type")
            t_value = trait.get("value")
            if not t_type or not t_value:
                continue
            t_type = str(t_type)
            t_value = str(t_value)

            trait_counts[t_type][t_value] += 1
            trait_value_sets[t_type].add(t_value)

    # تقدير أعداد الصفات
    estimated_traits = {}
    for t_type, value_counts in trait_counts.items():
        k_obs = len(trait_value_sets[t_type])
        estimated_traits[t_type] = {}
        for value, observed in value_counts.items():
            estimated_traits[t_type][value] = estimate_full_count(
                observed, n, total_supply, k_obs, alpha
            )

    # تقدير أعداد Trait Count
    k_tc_obs = len(trait_count_values)
    estimated_trait_counts = {}
    for tc, observed in trait_count_freq.items():
        estimated_trait_counts[tc] = estimate_full_count(
            observed, n, total_supply, k_tc_obs, alpha
        )

    return estimated_traits, estimated_trait_counts, n


def compute_bayesian_openrarity_scores(pseudo_nfts: list[dict], total_supply: int, alpha: float = 1.0) -> list[dict]:
    """
    حساب نقاط الندرة بطريقة OpenRarity + Bayesian.
    total_supply هنا هو العرض الكلي للكولكشن وليس عدد القطع المكشوفة.
    """
    if total_supply <= 0:
        total_supply = len(pseudo_nfts) or 1

    estimated_traits, estimated_trait_counts, n = build_bayesian_trait_estimates(
        pseudo_nfts, total_supply, alpha
    )

    results = []

    for nft in pseudo_nfts:
        traits = nft.get("traits") or []
        score = 0.0

        # 1) نقاط Trait Count
        tc = len(traits)
        est_tc = estimated_trait_counts.get(tc, 0.0)
        if est_tc <= 0:
            est_tc = 1e-9
        score += -math.log2(est_tc / total_supply)

        # 2) نقاط كل صفة
        for trait in traits:
            t_type = str(trait.get("trait_type", ""))
            t_value = str(trait.get("value", ""))
            if not t_type or not t_value:
                continue

            est_value = estimated_traits.get(t_type, {}).get(t_value, 0.0)
            if est_value <= 0:
                est_value = 1e-9

            score += -math.log2(est_value / total_supply)

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
            "rarity_score": round(score, 8),
        })

    # ترتيب دقيق حسب النقاط ثم معرف التوكين
    results.sort(key=lambda x: (-x["rarity_score"], x["tid_num"]))
    return results


# =============================================================================
# الدوال الأخرى تبقى كما هي
# =============================================================================

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
    # لو الميتاداتا الحقيقية تحت مفتاح "metadata" أو "nft"
    inner = metadata.get("metadata") or metadata.get("nft") or metadata
    if not isinstance(inner, dict):
        inner = metadata

    traits = extract_traits_generic(inner)

    return {
        "identifier": token_id,
        "name": inner.get("name") or metadata.get("name") or f"#{token_id}",
        "image_url": inner.get("image") or inner.get("image_url", ""),
        "opensea_url": f"https://opensea.io/assets/{watched.chain}/{watched.contract_address}/{token_id}",
        "traits": traits,
        "raw_metadata": inner,
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

    from rarity_core import fetch_best_listings

    pseudo_nfts = [build_pseudo_nft(tid, meta, watched) for tid, meta in revealed_items]
    revealed_total = len(pseudo_nfts)

    # سطر تشخيص مؤقت
    if pseudo_nfts:
        sample_traits = pseudo_nfts[0]["traits"]
        print(f"[تشخيص] عدد الصفات في أول قطعة: {len(sample_traits)}")
        print(f"[تشخيص] الصفات: {sample_traits}")

    # العرض الكلي الحقيقي من العقد أو من إعداد المراقبة
    total_supply = watched.max_supply or revealed_total

    # ========== استخدم الخوارزمية الجديدة ==========
    ranked = compute_bayesian_openrarity_scores(
        pseudo_nfts,
        total_supply=total_supply,
        alpha=1.0
    )
    # =============================================

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

    for idx, item in enumerate(ranked):
        # التصنيف الجديد يعتمد على الرتبة المتوقعة
        tier = compute_tier_scaled(
            index=idx,
            revealed_count=revealed_total,
            total_supply=total_supply
        )

        if tier is None:
            continue

        listing_price = price_map_eth.get(str(item["identifier"])) if isinstance(price_map_eth, dict) else None
        price_eth, price_usd = listing_price_to_eth_usd(listing_price, eth_usd_rate)

        if price_eth and price_eth > 500:
            price_eth = None
            price_usd = None

        session.add(RareItem(
            collection_id=collection.id,
            identifier=str(item["identifier"]),
            name=item["name"],
            image_url=item.get("image_url", ""),
            opensea_url=item.get("opensea_url", ""),
            rarity_score=item["rarity_score"],
            rank=idx + 1,
            price_eth=price_eth,
            price_usd=price_usd,
            tier=tier,
        ))

    session.commit()
    return {"ok": True, "revealed_total": revealed_total}

"""
OpenRarity-compatible rarity engine.

يعتمد على مبادئ المرجع الرسمي لـ OpenRarity:
- Information Content
- Collection Entropy normalization
- Null attributes
- Meta Trait Count
- Double Sort:
    1) unique_attribute_count
    2) rarity score
- Competition ranking (RANK)
- عدم استخدام 999999 أو كلمات legendary/unique كعامل رياضي

مهم:
الـFinal OpenRarity rank لا يكون صالحاً إلا عندما تكون مجموعة
البيانات المقدمة كاملة بالنسبة إلى الـcollection المراد ترتيبها.
"""

import hashlib
import json
import math
from collections import Counter
from datetime import datetime, timezone

from models import Collection, RareItem
from price_utils import get_eth_usd_rate
from rarity_core import listing_price_to_eth_usd


ORANGE_PERCENT = 1.0
PINK_PERCENT = 5.0

TRAIT_COUNT_ATTRIBUTE = "meta_trait:trait_count"

PLACEHOLDER_NAME_HINTS = (
    "unrevealed",
    "hidden",
    "mystery",
)


# ============================================================
# Basic normalization
# ============================================================

def normalize_attribute_string(value) -> str:
    """
    OpenRarity normalizes string attribute names/values
    so case and surrounding whitespace do not create
    separate attributes.
    """
    return str(value).strip().lower()


# ============================================================
# Tier
# ============================================================

def compute_tier(
    rank: int,
    total: int,
) -> str | None:
    """
    Final tier only.

    Orange = top 1%
    Pink   = top 5%

    Uses the actual rank rather than array index.
    """

    if rank is None or total is None:
        return None

    if total <= 0 or rank <= 0:
        return None

    orange_limit = max(
        1,
        math.ceil(total * (ORANGE_PERCENT / 100.0)),
    )

    pink_limit = max(
        1,
        math.ceil(total * (PINK_PERCENT / 100.0)),
    )

    if rank <= orange_limit:
        return "orange"

    if rank <= pink_limit:
        return "pink"

    return None


# ============================================================
# Metadata / Traits
# ============================================================

def extract_traits_generic(metadata: dict) -> list[dict]:
    """
    Extract string traits from supported metadata structures.

    Returns canonical:
        [
            {
                "trait_type": "...",
                "value": "..."
            }
        ]

    Numeric/date attributes are intentionally not treated as
    standard OpenRarity attributes because OpenRarity's current
    reference scorer rejects numeric/date collections.
    """

    if not isinstance(metadata, dict):
        return []

    raw_traits = (
        metadata.get("traits")
        or metadata.get("attributes")
        or metadata.get("properties")
        or (metadata.get("meta") or {}).get("attributes")
        or {}
    )

    valid_traits = []

    if isinstance(raw_traits, list):

        for item in raw_traits:

            if not isinstance(item, dict):
                continue

            trait_type = (
                item.get("trait_type")
                or item.get("name")
                or item.get("key")
                or item.get("type")
            )

            raw_value = (
                item.get("value")
                if "value" in item
                else item.get("val")
            )

            # OpenRarity standard scorer is based on string attrs.
            if not isinstance(trait_type, str):
                continue

            if not isinstance(raw_value, str):
                continue

            trait_type = normalize_attribute_string(trait_type)
            value = normalize_attribute_string(raw_value)

            if not trait_type or not value:
                continue

            if any(
                hint in value
                for hint in PLACEHOLDER_NAME_HINTS
            ):
                continue

            valid_traits.append(
                {
                    "trait_type": trait_type,
                    "value": value,
                }
            )

    elif isinstance(raw_traits, dict):

        for raw_type, raw_value in raw_traits.items():

            if not isinstance(raw_type, str):
                continue

            # Only string values for OpenRarity-compatible scoring.
            if not isinstance(raw_value, str):
                continue

            trait_type = normalize_attribute_string(
                raw_type
            )

            value = normalize_attribute_string(
                raw_value
            )

            if not trait_type or not value:
                continue

            if any(
                hint in value
                for hint in PLACEHOLDER_NAME_HINTS
            ):
                continue

            valid_traits.append(
                {
                    "trait_type": trait_type,
                    "value": value,
                }
            )

    return valid_traits


# ============================================================
# Trait data model
# ============================================================

def build_attribute_maps(
    nfts: list[dict],
) -> tuple[
    list[dict[str, str]],
    dict[str, dict[str, int]],
]:
    """
    Build canonical per-token attributes and collection
    frequency counts.

    Every token receives the synthetic:
        meta_trait:trait_count

    exactly like the reference implementation.
    """

    token_attributes = []
    frequency = {}

    for nft in nfts:

        traits = extract_traits_generic(
            nft.get("raw_metadata")
            if isinstance(nft.get("raw_metadata"), dict)
            else {
                "attributes": nft.get("traits") or []
            }
        )

        attributes = {}

        # Real string traits.
        for trait in traits:

            trait_type = trait["trait_type"]
            value = trait["value"]

            # Same behavior as a dictionary-backed metadata model:
            # one value per trait type.
            attributes[trait_type] = value

        # Meta Trait Count.
        trait_count = len(attributes)

        attributes[TRAIT_COUNT_ATTRIBUTE] = str(
            trait_count
        )

        token_attributes.append(
            attributes
        )

        # Collection frequencies.
        for name, value in attributes.items():

            frequency.setdefault(
                name,
                {}
            )

            frequency[name][value] = (
                frequency[name].get(value, 0)
                + 1
            )

    return token_attributes, frequency


# ============================================================
# Null attributes
# ============================================================

def build_null_attribute_counts(
    frequency: dict[str, dict[str, int]],
    total: int,
) -> dict[str, int]:
    """
    For every attribute category:

        Null = total - sum(all explicit values)

    Exactly the behavior used by OpenRarity's Collection model.
    """

    null_counts = {}

    for attribute_name, value_counts in frequency.items():

        explicit_count = sum(
            value_counts.values()
        )

        missing = total - explicit_count

        if missing > 0:
            null_counts[
                attribute_name
            ] = missing

    return null_counts


# ============================================================
# Collection entropy
# ============================================================

def compute_collection_entropy(
    frequency: dict[str, dict[str, int]],
    null_counts: dict[str, int],
    total: int,
) -> float:
    """
    H = -Σ p log2(p)

    Includes explicit attribute values and Null values.
    """

    if total <= 0:
        return 0.0

    entropy = 0.0

    for attribute_name, value_counts in frequency.items():

        counts = list(
            value_counts.values()
        )

        if attribute_name in null_counts:
            counts.append(
                null_counts[attribute_name]
            )

        for count in counts:

            if count <= 0:
                continue

            probability = (
                count / total
            )

            entropy -= (
                probability
                * math.log2(probability)
            )

    return entropy


# ============================================================
# Token score
# ============================================================

def compute_token_information_score(
    attributes: dict[str, str],
    frequency: dict[str, dict[str, int]],
    null_counts: dict[str, int],
    total: int,
) -> float:
    """
    Raw Information Content:

        Σ log2(N / count(attribute))
    """

    if total <= 0:
        return 0.0

    score = 0.0

    # Iterate through every attribute category known
    # to the collection.

    for attribute_name, value_counts in frequency.items():

        if attribute_name in attributes:

            value = attributes[attribute_name]

            count = (
                value_counts.get(
                    value,
                    0,
                )
            )

        else:

            count = null_counts.get(
                attribute_name,
                0,
            )

        if count <= 0:
            continue

        score += math.log2(
            total / count
        )

    return score


# ============================================================
# Unique attribute count
# ============================================================

def compute_unique_attribute_count(
    attributes: dict[str, str],
    frequency: dict[str, dict[str, int]],
) -> int:
    """
    OpenRarity Double Sort feature.

    Count how many attribute name/value pairs are globally
    unique in the collection.
    """

    unique_count = 0

    for name, value in attributes.items():

        count = (
            frequency
            .get(name, {})
            .get(value, 0)
        )

        if count == 1:
            unique_count += 1

    return unique_count


# ============================================================
# Main OpenRarity score engine
# ============================================================

def compute_pure_openrarity_scores(
    nfts: list[dict],
    freq: dict | None = None,
    total: int | None = None,
) -> list[dict]:
    """
    OpenRarity-compatible scoring and ranking.

    Important:
    The supplied nfts must represent the complete collection
    if the caller wants a final rank matching OpenSea.

    This function intentionally does NOT:
    - use 999999
    - inspect "legendary"
    - inspect "1/1"
    - use token name as rarity
    - use token ID as a rarity signal
    - divide by number of traits
    """

    if not nfts:
        return []

    actual_total = len(nfts)

    # OpenRarity derives collection total from its token set.
    if total is None or total != actual_total:
        total = actual_total

    token_attributes, derived_frequency = (
        build_attribute_maps(nfts)
    )

    frequency = derived_frequency

    null_counts = build_null_attribute_counts(
        frequency,
        total,
    )

    collection_entropy = compute_collection_entropy(
        frequency,
        null_counts,
        total,
    )

    if collection_entropy <= 0:
        collection_entropy = 1.0

    results = []

    for index, nft in enumerate(nfts):

        attributes = token_attributes[index]

        raw_ic = compute_token_information_score(
            attributes,
            frequency,
            null_counts,
            total,
        )

        normalized_score = (
            raw_ic / collection_entropy
        )

        unique_attribute_count = (
            compute_unique_attribute_count(
                attributes,
                frequency,
            )
        )

        try:
            tid_num = int(
                nft.get("identifier", 0)
            )
        except (
            TypeError,
            ValueError,
        ):
            tid_num = 0

        results.append(
            {
                "identifier": nft.get(
                    "identifier"
                ),
                "tid_num": tid_num,
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
                # Keep full precision internally.
                "rarity_score": float(
                    normalized_score
                ),
                "raw_information_content": float(
                    raw_ic
                ),
                "unique_attribute_count": (
                    unique_attribute_count
                ),
            }
        )

    # ========================================================
    # OpenRarity Double Sort
    #
    # Primary:
    #   unique_attribute_count
    #
    # Secondary:
    #   score
    #
    # Both descending.
    # ========================================================

    results.sort(
        key=lambda item: (
            item[
                "unique_attribute_count"
            ],
            item[
                "rarity_score"
            ],
        ),
        reverse=True,
    )

    # ========================================================
    # Competition ranking (RANK)
    #
    # 1, 2, 2, 2, 5
    #
    # OpenRarity uses math.isclose for score equality.
    # ========================================================

    previous = None

    for index, item in enumerate(
        results
    ):

        if previous is None:

            rank = 1

        else:

            same_unique_count = (
                item[
                    "unique_attribute_count"
                ]
                ==
                previous[
                    "unique_attribute_count"
                ]
            )

            scores_equal = math.isclose(
                item["rarity_score"],
                previous["rarity_score"],
            )

            if (
                same_unique_count
                and scores_equal
            ):
                rank = previous["rank"]
            else:
                rank = index + 1

        item["rank"] = rank
        previous = item

    # Round only AFTER ranking.
    for item in results:

        item["rarity_score"] = round(
            item["rarity_score"],
            8,
        )

    return results


# ============================================================
# Metadata signature
# ============================================================

def content_signature(
    metadata: dict,
) -> str:

    image = (
        metadata.get("image")
        or metadata.get("image_url")
        or ""
    )

    traits = extract_traits_generic(
        metadata
    )

    normalized_traits = sorted(
        (
            t["trait_type"],
            t["value"],
        )
        for t in traits
    )

    raw = json.dumps(
        {
            "image": image,
            "traits": normalized_traits,
        },
        sort_keys=True,
        ensure_ascii=False,
    )

    return hashlib.sha256(
        raw.encode()
    ).hexdigest()


# ============================================================
# Placeholder detection
# ============================================================

def is_placeholder_fallback(
    metadata: dict,
) -> bool:

    if not isinstance(
        metadata,
        dict,
    ) or not metadata:

        return True

    name = str(
        metadata.get(
            "name",
            "",
        )
    ).lower()

    if any(
        hint in name
        for hint in PLACEHOLDER_NAME_HINTS
    ):
        return True

    image = str(
        metadata.get("image")
        or metadata.get("image_url")
        or ""
    ).lower()

    if any(
        hint in image
        for hint in PLACEHOLDER_NAME_HINTS
    ):
        return True

    traits = extract_traits_generic(
        metadata
    )

    if (
        not traits
        and not metadata.get("image")
        and not metadata.get("image_url")
    ):
        return True

    return False


# ============================================================
# Baseline
# ============================================================

def compute_baseline_signature(
    signatures: list,
) -> str | None:

    if len(signatures) < 15:
        return None

    counter = Counter(
        signatures
    )

    most_common_sig, count = (
        counter.most_common(1)[0]
    )

    if (
        count / len(signatures)
        < 0.5
    ):
        return None

    return most_common_sig


# ============================================================
# Pseudo NFT
# ============================================================

def build_pseudo_nft(
    token_id: int,
    metadata: dict,
    watched,
) -> dict:

    return {
        "identifier": token_id,
        "name": (
            metadata.get("name")
            or f"#{token_id}"
        ),
        "image_url": (
            metadata.get("image")
            or metadata.get(
                "image_url",
                "",
            )
        ),
        "opensea_url": (
            f"https://opensea.io/assets/"
            f"{watched.chain}/"
            f"{watched.contract_address}/"
            f"{token_id}"
        ),
        "traits": extract_traits_generic(
            metadata
        ),
        "raw_metadata": metadata,
    }


# ============================================================
# Collection persistence
# ============================================================

def ensure_collection_placeholder(
    session,
    watched,
    revealed_count: int = 0,
):

    existing = (
        session.query(Collection)
        .filter_by(
            slug=watched.slug
        )
        .first()
    )

    if existing:

        existing.revealed_count = (
            revealed_count
        )

        existing.total_items = (
            watched.max_supply
            or existing.total_items
        )

        existing.computed_at = (
            datetime.now(timezone.utc)
        )

    else:

        session.add(
            Collection(
                slug=watched.slug,
                name=watched.slug,
                chain=watched.chain,
                total_items=(
                    watched.max_supply
                    or 0
                ),
                revealed_count=(
                    revealed_count
                ),
                opensea_url=(
                    "https://opensea.io/"
                    f"collection/{watched.slug}"
                ),
                computed_at=(
                    datetime.now(timezone.utc)
                ),
            )
        )

    session.commit()


# ============================================================
# Main recompute
# ============================================================

def recompute_from_chain_data(
    session,
    watched,
    revealed_items: list,
) -> dict:

    if not revealed_items:
        return {
            "ok": False,
            "reason": "no_revealed_yet",
        }

    from rarity_core import (
        fetch_best_listings,
    )

    pseudo_nfts = [
        build_pseudo_nft(
            tid,
            meta,
            watched,
        )
        for tid, meta in revealed_items
    ]

    revealed_total = len(
        pseudo_nfts
    )

    total_supply = (
        watched.max_supply
        or revealed_total
    )

    # IMPORTANT:
    # The OpenRarity final rank requires the full
    # collection dataset.
    is_complete = (
        total_supply > 0
        and revealed_total >= total_supply
    )

    ranked = compute_pure_openrarity_scores(
        pseudo_nfts,
        total=revealed_total,
    )

    try:

        price_map, _ = (
            fetch_best_listings(
                watched.slug
            )
        )

    except Exception:

        price_map = {}

    eth_usd_rate = (
        get_eth_usd_rate()
    )

    collection = (
        session.query(Collection)
        .filter_by(
            slug=watched.slug
        )
        .first()
    )

    if not collection:

        collection = Collection(
            slug=watched.slug,
            name=watched.slug,
            chain=watched.chain,
            total_items=total_supply,
            revealed_count=revealed_total,
            opensea_url=(
                "https://opensea.io/"
                f"collection/{watched.slug}"
            ),
            computed_at=(
                datetime.now(timezone.utc)
            ),
        )

        session.add(
            collection
        )

        session.flush()

    else:

        collection.revealed_count = (
            revealed_total
        )

        collection.total_items = (
            total_supply
        )

        collection.computed_at = (
            datetime.now(timezone.utc)
        )

        session.query(RareItem).filter_by(
            collection_id=collection.id
        ).delete()

    saved_count = 0

    for item in ranked:

        # NEVER present a partial ranking as a final
        # OpenSea-compatible tier.
        tier = (
            compute_tier(
                item["rank"],
                total_supply,
            )
            if is_complete
            else None
        )

        # During partial reveal we do not create
        # final-tier RareItems.
        if not is_complete:
            continue

        listing_price = (
            price_map.get(
                str(item["identifier"])
            )
            if isinstance(
                price_map,
                dict,
            )
            else None
        )

        (
            price_eth,
            price_usd,
        ) = listing_price_to_eth_usd(
            listing_price,
            eth_usd_rate,
        )

        if (
            price_eth is not None
            and price_eth > 500
        ):
            price_eth = None
            price_usd = None

        session.add(
            RareItem(
                collection_id=collection.id,
                identifier=str(
                    item["identifier"]
                ),
                name=item["name"],
                image_url=item.get(
                    "image_url",
                    "",
                ),
                opensea_url=item.get(
                    "opensea_url",
                    "",
                ),
                rarity_score=item[
                    "rarity_score"
                ],
                rank=item["rank"],
                price_eth=price_eth,
                price_usd=price_usd,
                tier=tier,
            )
        )

        saved_count += 1

    session.commit()

    if not is_complete:

        return {
            "ok": False,
            "reason": "partial_collection",
            "revealed_total": revealed_total,
            "total_supply": total_supply,
            "message": (
                "Metadata is incomplete; "
                "final OpenRarity rank cannot be "
                "claimed yet."
            ),
        }

    return {
        "ok": True,
        "revealed_total": revealed_total,
        "total_supply": total_supply,
        "saved_count": saved_count,
        "ranking_method": (
            "openrarity_reference_compatible"
        ),
    }

"""
محرك المراقبة المباشر من البلوكشين لمتابعة Reveal وMetadata
وجمع البيانات الكاملة للمجموعة قبل تشغيل محرك Rarity.

المسؤوليات:
1) تحديد Max Supply من المصادر المناسبة.
2) فصل Max Supply عن Total Supply.
3) إنشاء/توسيع سجلات تتبع Tokens.
4) مراقبة tokenURI مباشرة.
5) دعم data:, IPFS, HTTP/HTTPS.
6) استخدام Base URI inference فقط عندما يكون النمط قابلاً للاختبار.
7) جلب Metadata مباشرة وعدم الاعتماد على OpenSea لإثبات الـReveal.
8) تحديد Placeholder مقابل Revealed.
9) حفظ جميع الـRevealed Metadata تراكميًا.
10) تشغيل rarity_storage عند وجود بيانات جديدة أو عدم وجود نتائج سابقة.
"""

import asyncio
import logging
import re
import time
from datetime import datetime, timezone

from eth_abi import decode as eth_abi_decode
from web3 import Web3

from models import (
    RevealTrack,
    WatchedCollection,
    SessionLocal,
    init_db,
    Collection,
)

from chain_reader import (
    async_batch_get_token_uris,
    async_batch_resolve_metadata,
    detect_global_reveal_flag,
    get_web3,
)

from rarity_core import (
    fetch_max_supply,
    fetch_drop_status,
)

from rarity_storage import (
    recompute_from_chain_data,
    ensure_collection_placeholder,
    content_signature,
    is_placeholder_fallback,
    compute_baseline_signature,
)


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    datefmt="%H:%M:%S",
)

log = logging.getLogger("reveal-watcher")


# ============================================================
# Configuration
# ============================================================

POLL_INTERVAL = 2

BATCH_SIZE = 500

DEFAULT_START_TOKEN_ID = 1

PATTERN_VALIDATION_SAMPLE_SIZE = 12

# watched_id -> {token_id: metadata}
COLLECTION_METADATA_CACHE = {}

# watched_id -> {token_id: uri}
COLLECTION_URI_CACHE = {}

# watched_id -> [baseline signatures]
BASELINE_SAMPLE_CACHE = {}

# watched_id -> last known totalSupply
COLLECTION_TOTAL_SUPPLY_CACHE = {}


# ============================================================
# Helpers
# ============================================================

def is_dynamic_url(uri: str) -> bool:

    if not uri:
        return False

    uri = str(uri).lower()

    return (
        uri.startswith("http://")
        or uri.startswith("https://")
    )


def is_data_uri(uri: str) -> bool:

    if not uri:
        return False

    return str(uri).lower().startswith("data:")


def normalize_uri(uri: str | None) -> str:

    if uri is None:
        return ""

    return str(uri).strip()


# ============================================================
# On-chain Total Supply
# ============================================================

def resolve_total_supply(
    watched: WatchedCollection,
) -> int | None:

    """
    يقرأ totalSupply() مباشرة من العقد.

    مهم:
    totalSupply != maxSupply

    totalSupply:
        عدد الـNFTs الموجودة/المصكوكة حاليًا.

    maxSupply:
        الحد الأقصى النهائي للمجموعة.

    لا نستخدم totalSupply لتغيير watched.max_supply.
    """

    chain = watched.chain or "ethereum"

    selector = bytes.fromhex(
        "18160ddd"
    )

    try:

        w3 = get_web3(chain)

        checksum_addr = (
            Web3.to_checksum_address(
                watched.contract_address
            )
        )

        result = w3.eth.call(
            {
                "to": checksum_addr,
                "data": selector,
            }
        )

        if not result:
            return None

        if len(result) != 32:
            return None

        (
            total_supply,
        ) = eth_abi_decode(
            ["uint256"],
            result,
        )

        if (
            total_supply is not None
            and int(total_supply) >= 0
        ):

            return int(
                total_supply
            )

    except Exception:
        pass

    return None


# ============================================================
# Max Supply Resolver
# ============================================================

def resolve_max_supply(
    watched: WatchedCollection,
) -> int:

    """
    محاولة الحصول على Max Supply الحقيقي.

    لا نستخدم totalSupply() هنا.

    الأولوية:

    1. maxSupply()
    2. MAX_SUPPLY()
    3. maxTokens()
    4. OpenSea collection
    5. OpenSea drop
    6. القيمة المخزنة مسبقًا
    """

    chain = watched.chain or "ethereum"

    selectors = [

        # maxSupply()
        bytes.fromhex(
            "d5abeb01"
        ),

        # MAX_SUPPLY()
        bytes.fromhex(
            "d368b122"
        ),

        # maxTokens()
        bytes.fromhex(
            "3a4b66f1"
        ),
    ]

    try:

        w3 = get_web3(chain)

        checksum_addr = (
            Web3.to_checksum_address(
                watched.contract_address
            )
        )

        for selector in selectors:

            try:

                result = w3.eth.call(
                    {
                        "to": checksum_addr,
                        "data": selector,
                    }
                )

                if not result:
                    continue

                if len(result) != 32:
                    continue

                (
                    value,
                ) = eth_abi_decode(
                    ["uint256"],
                    result,
                )

                if (
                    value
                    and int(value) > 0
                ):

                    return int(value)

            except Exception:
                continue

    except Exception:
        pass

    # --------------------------------------------------------
    # OpenSea fallback
    # --------------------------------------------------------

    try:

        supply = fetch_max_supply(
            watched.slug
        )

        if (
            supply
            and supply > 0
        ):

            return int(supply)

        drop_status = (
            fetch_drop_status(
                watched.slug
            )
        )

        if drop_status:

            for key in (
                "max_supply",
                "total_supply",
            ):

                value = (
                    drop_status.get(
                        key
                    )
                )

                if value:

                    value = int(
                        value
                    )

                    if value > 0:
                        return value

    except Exception:
        pass

    # --------------------------------------------------------
    # Existing stored value
    # --------------------------------------------------------

    if (
        watched.max_supply
        and watched.max_supply > 0
    ):

        return int(
            watched.max_supply
        )

    return 10000


# ============================================================
# Tracking
# ============================================================

def ensure_tracks(
    session,
    watched: WatchedCollection,
) -> bool:

    latest_max_supply = (
        resolve_max_supply(
            watched
        )
    )

    # --------------------------------------------------------
    # لا ننقص max_supply بسبب totalSupply
    # --------------------------------------------------------

    if (
        not watched.max_supply
        or latest_max_supply
        > watched.max_supply
    ):

        old_supply = (
            watched.max_supply
            or 0
        )

        watched.max_supply = (
            latest_max_supply
        )

        watched.failed_attempts = 0

        session.commit()

        if old_supply > 0:

            log.info(
                f"[{watched.slug}] "
                f"تحديث Max Supply: "
                f"{old_supply} -> "
                f"{latest_max_supply}"
            )

        else:

            log.info(
                f"[{watched.slug}] "
                f"Max Supply: "
                f"{latest_max_supply}"
            )

        ensure_collection_placeholder(
            session,
            watched,
            revealed_count=0,
        )

    # --------------------------------------------------------
    # Total Supply الحالي
    # --------------------------------------------------------

    total_supply = (
        resolve_total_supply(
            watched
        )
    )

    if (
        total_supply is not None
    ):

        COLLECTION_TOTAL_SUPPLY_CACHE[
            watched.id
        ] = total_supply

        log.info(
            f"[{watched.slug}] "
            f"On-chain Total Supply="
            f"{total_supply} | "
            f"Max Supply="
            f"{watched.max_supply}"
        )

    # --------------------------------------------------------
    # إنشاء Tracks حتى Max Supply
    #
    # لا نحذف الموجود.
    # --------------------------------------------------------

    existing_ids = {
        row.token_id
        for row in (
            session.query(
                RevealTrack.token_id
            )
            .filter_by(
                watched_id=watched.id
            )
            .all()
        )
    }

    existing_count = len(
        existing_ids
    )

    if (
        existing_count
        >= watched.max_supply
    ):

        return True

    new_tracks = []

    end_token_id = (
        DEFAULT_START_TOKEN_ID
        + watched.max_supply
    )

    for token_id in range(
        DEFAULT_START_TOKEN_ID,
        end_token_id,
    ):

        if (
            token_id
            not in existing_ids
        ):

            new_tracks.append(
                RevealTrack(
                    watched_id=watched.id,
                    token_id=token_id,
                )
            )

    if new_tracks:

        session.bulk_save_objects(
            new_tracks
        )

        session.commit()

        log.info(
            f"[{watched.slug}] "
            f"تم إنشاء "
            f"{len(new_tracks)} "
            f"سجل Token جديد."
        )

    return True


# ============================================================
# Global Reveal Flag
# ============================================================

def check_global_flag(
    session,
    watched: WatchedCollection,
):

    chain = (
        watched.chain
        or "ethereum"
    )

    flag = (
        detect_global_reveal_flag(
            watched.contract_address,
            chain=chain,
        )
    )

    if (
        flag
        != watched.global_revealed_flag
    ):

        watched.global_revealed_flag = (
            flag
        )

        session.commit()

        if flag is not None:

            log.info(
                f"[{watched.slug}] "
                f"On-chain reveal flag: "
                f"{'REVEALED' if flag else 'PLACEHOLDER'}"
            )


# ============================================================
# Base URI Pattern Detection
# ============================================================

def detect_base_uri_pattern_smart(
    sample_uris: dict[int, str],
) -> tuple[str | None, int]:

    valid_uris = {

        int(token_id): normalize_uri(
            uri
        )

        for token_id, uri
        in (
            sample_uris or {}
        ).items()

        if normalize_uri(uri)
    }

    if not valid_uris:
        return None, 0

    if any(
        is_data_uri(uri)
        for uri in valid_uris.values()
    ):

        return None, 0

    if (
        len(
            set(
                valid_uris.values()
            )
        )
        == 1
        and len(valid_uris) > 1
    ):

        return None, 0

    candidates = []

    for token_id, uri in (
        valid_uris.items()
    ):

        token_str = str(
            token_id
        )

        match = re.search(
            r"(0*)"
            + re.escape(token_str)
            + r"(\.[A-Za-z0-9]+)?$",
            uri,
        )

        if match:

            zeros = (
                match.group(1)
            )

            extension = (
                match.group(2)
                or ""
            )

            padding_width = (
                len(zeros)
                + len(token_str)
            )

            full_suffix = (
                zeros
                + token_str
                + extension
            )

            prefix = uri.rsplit(
                full_suffix,
                1,
            )[0]

            pattern = (
                prefix
                + "{id}"
                + extension
            )

            candidates.append(
                (
                    pattern,
                    padding_width,
                )
            )

            continue

        if token_str in uri:

            prefix, suffix = (
                uri.rsplit(
                    token_str,
                    1,
                )
            )

            pattern = (
                prefix
                + "{id}"
                + suffix
            )

            candidates.append(
                (
                    pattern,
                    0,
                )
            )

    if not candidates:
        return None, 0

    counter = {}

    for candidate in candidates:

        counter[candidate] = (
            counter.get(
                candidate,
                0,
            )
            + 1
        )

    best_pattern = max(
        counter,
        key=counter.get,
    )

    return best_pattern


# ============================================================
# Validate URI Pattern
# ============================================================

async def validate_uri_pattern(
    watched: WatchedCollection,
    pattern: str,
    padding_width: int,
    sample_ids: list[int],
    actual_sample_uris: dict[int, str],
    chain: str,
) -> bool:

    if not pattern:
        return False

    if not sample_ids:
        return False

    checked = 0
    matches = 0

    for token_id in sample_ids:

        formatted = (
            str(token_id).zfill(
                padding_width
            )
            if padding_width > 0
            else str(token_id)
        )

        expected_uri = (
            pattern.replace(
                "{id}",
                formatted,
            )
        )

        actual_uri = normalize_uri(
            (
                actual_sample_uris
                or {}
            ).get(
                token_id
            )
        )

        if not actual_uri:
            continue

        checked += 1

        if (
            actual_uri
            == expected_uri
        ):

            matches += 1

    return (
        checked >= 3
        and matches == checked
    )


# ============================================================
# Main Collection Processor
# ============================================================

async def process_collection_async(
    watched_id: int,
):

    session = SessionLocal()

    cycle_start = (
        time.monotonic()
    )

    try:

        watched = (
            session.query(
                WatchedCollection
            )
            .filter_by(
                id=watched_id,
                active=True,
            )
            .first()
        )

        if not watched:
            return

        # ====================================================
        # 1. Supply + Tracks
        # ====================================================

        ok = ensure_tracks(
            session,
            watched,
        )

        if not ok:
            return

        chain = (
            watched.chain
            or "ethereum"
        )

        # ====================================================
        # 2. Global Reveal Flag
        # ====================================================

        check_global_flag(
            session,
            watched,
        )

        if (
            watched.global_revealed_flag
            is False
        ):

            ensure_collection_placeholder(
                session,
                watched,
                revealed_count=0,
            )

            return

        # ====================================================
        # 3. Initialize Caches
        # ====================================================

        if (
            watched.id
            not in COLLECTION_METADATA_CACHE
        ):

            COLLECTION_METADATA_CACHE[
                watched.id
            ] = {}

        if (
            watched.id
            not in COLLECTION_URI_CACHE
        ):

            COLLECTION_URI_CACHE[
                watched.id
            ] = {}

        if (
            watched.id
            not in BASELINE_SAMPLE_CACHE
        ):

            BASELINE_SAMPLE_CACHE[
                watched.id
            ] = []

        # ====================================================
        # 4. Load Tracks
        # ====================================================

        tracks = (
            session.query(
                RevealTrack
            )
            .filter_by(
                watched_id=watched.id
            )
            .all()
        )

        if not tracks:
            return

        token_ids = [
            track.token_id
            for track in tracks
        ]

        tracks_by_id = {
            track.token_id: track
            for track in tracks
        }

        # ====================================================
        # 5. Real Total Supply
        # ====================================================

        current_total_supply = (
            COLLECTION_TOTAL_SUPPLY_CACHE.get(
                watched.id
            )
        )

        if (
            current_total_supply is not None
            and current_total_supply > 0
        ):

            log.info(
                f"[{watched.slug}] "
                f"Minted/Existing="
                f"{current_total_supply} | "
                f"Max="
                f"{watched.max_supply}"
            )

        # ====================================================
        # 6. URI Samples
        # ====================================================

        sample_ids = [
            token_id
            for token_id in (
                1,
                2,
                5,
                10,
                20,
                50,
                100,
            )
            if token_id in tracks_by_id
        ]

        if not sample_ids:

            sample_ids = token_ids[
                :min(
                    5,
                    len(token_ids),
                )
            ]

        sample_uris = (
            await async_batch_get_token_uris(
                watched.contract_address,
                sample_ids,
                chain,
            )
        )

        for token_id, uri in (
            sample_uris or {}
        ).items():

            if uri:

                COLLECTION_URI_CACHE[
                    watched.id
                ][token_id] = (
                    normalize_uri(uri)
                )

        # ====================================================
        # 7. URI Pattern
        # ====================================================

        (
            detected_pattern,
            padding_width,
        ) = detect_base_uri_pattern_smart(
            sample_uris or {}
        )

        pattern_valid = False

        if detected_pattern:

            validation_ids = list(
                dict.fromkeys(
                    sample_ids
                )
            )

            for candidate in token_ids:

                if (
                    candidate
                    not in validation_ids
                    and len(validation_ids)
                    < PATTERN_VALIDATION_SAMPLE_SIZE
                ):

                    validation_ids.append(
                        candidate
                    )

            validation_real_uris = (
                await async_batch_get_token_uris(
                    watched.contract_address,
                    validation_ids,
                    chain,
                )
            )

            pattern_valid = (
                await validate_uri_pattern(
                    watched,
                    detected_pattern,
                    padding_width,
                    validation_ids,
                    validation_real_uris,
                    chain,
                )
            )

        # ====================================================
        # 8. Fetch URIs
        # ====================================================

        uris_to_fetch = {}

        now = datetime.now(
            timezone.utc
        )

        if (
            detected_pattern
            and pattern_valid
        ):

            log.info(
                f"[{watched.slug}] "
                f"URI pattern validated: "
                f"{detected_pattern}"
            )

            for token_id in token_ids:

                track = (
                    tracks_by_id[
                        token_id
                    ]
                )

                formatted_token_id = (
                    str(token_id).zfill(
                        padding_width
                    )
                    if padding_width > 0
                    else str(token_id)
                )

                computed_uri = (
                    detected_pattern.replace(
                        "{id}",
                        formatted_token_id,
                    )
                )

                # ------------------------------------------------
                # مهم:
                # لا نستخدم URI المولد كدليل Reveal.
                # نستخدمه فقط لتحديد مكان Metadata.
                # ------------------------------------------------

                if (
                    not track.revealed
                    or track.last_uri
                    != computed_uri
                ):

                    track.last_uri = (
                        computed_uri
                    )

                    track.content_checked_at = (
                        now
                    )

                    uris_to_fetch[
                        token_id
                    ] = computed_uri

        else:

            log.info(
                f"[{watched.slug}] "
                f"URI pattern غير موثوق؛ "
                f"استخدام tokenURI الحقيقي."
            )

            for start in range(
                0,
                len(token_ids),
                BATCH_SIZE,
            ):

                chunk = token_ids[
                    start:
                    start + BATCH_SIZE
                ]

                try:

                    uri_results = (
                        await async_batch_get_token_uris(
                            watched.contract_address,
                            chunk,
                            chain,
                        )
                    )

                except Exception as exc:

                    log.warning(
                        f"[{watched.slug}] "
                        f"فشل جلب URI batch: "
                        f"{exc}"
                    )

                    continue

                for token_id, uri in (
                    uri_results or {}
                ).items():

                    uri = normalize_uri(
                        uri
                    )

                    if not uri:
                        continue

                    COLLECTION_URI_CACHE[
                        watched.id
                    ][token_id] = uri

                    track = (
                        tracks_by_id.get(
                            token_id
                        )
                    )

                    if not track:
                        continue

                    if (
                        not track.revealed
                        or track.last_uri
                        != uri
                    ):

                        track.last_uri = (
                            uri
                        )

                        track.content_checked_at = (
                            now
                        )

                        uris_to_fetch[
                            token_id
                        ] = uri

        try:

            session.commit()

        except Exception:

            session.rollback()

        # ====================================================
        # 9. Nothing Changed
        # ====================================================

        if not uris_to_fetch:

            # حتى لو لم توجد Metadata جديدة،
            # لا نخرج قبل التأكد من وجود نتائج rarity.
            #
            # هذا مهم عند إعادة تشغيل البوت.
            metadata_cache = (
                COLLECTION_METADATA_CACHE[
                    watched.id
                ]
            )

            if not metadata_cache:
                return

            cumulative_revealed_items = list(
                metadata_cache.items()
            )

        else:

            # =================================================
            # 10. Resolve Metadata
            # =================================================

            t0 = time.monotonic()

            metadata_map = (
                await async_batch_resolve_metadata(
                    uris_to_fetch
                )
            )

            elapsed = round(
                time.monotonic()
                - t0,
                3,
            )

            fetched_this_cycle = []

            for (
                token_id,
                metadata,
            ) in (
                metadata_map or {}
            ).items():

                if metadata is None:
                    continue

                track = (
                    tracks_by_id.get(
                        token_id
                    )
                )

                if not track:
                    continue

                try:

                    signature = (
                        content_signature(
                            metadata
                        )
                    )

                except Exception:

                    continue

                fetched_this_cycle.append(
                    (
                        track,
                        metadata,
                        signature,
                    )
                )

            if fetched_this_cycle:

                log.info(
                    f"[{watched.slug}] "
                    f"جلب Metadata لـ "
                    f"{len(fetched_this_cycle)} "
                    f"Token خلال "
                    f"{elapsed}s."
                )

            # =================================================
            # 11. Baseline Learning
            # =================================================

            baseline_samples = (
                BASELINE_SAMPLE_CACHE[
                    watched.id
                ]
            )

            for (
                _track,
                _metadata,
                signature,
            ) in fetched_this_cycle:

                baseline_samples.append(
                    signature
                )

            if len(
                baseline_samples
            ) > 5000:

                del baseline_samples[
                    :-5000
                ]

            if (
                not watched.baseline_locked
                and len(
                    baseline_samples
                ) >= 15
            ):

                baseline = (
                    compute_baseline_signature(
                        baseline_samples
                    )
                )

                if baseline:

                    watched.baseline_signature = (
                        baseline
                    )

                    watched.baseline_locked = (
                        True
                    )

                    log.info(
                        f"[{watched.slug}] "
                        f"تم تثبيت Baseline."
                    )

                    try:
                        session.commit()
                    except Exception:
                        session.rollback()

            # =================================================
            # 12. Determine Reveal State
            # =================================================

            changed_count = 0

            metadata_cache = (
                COLLECTION_METADATA_CACHE[
                    watched.id
                ]
            )

            for (
                track,
                metadata,
                signature,
            ) in fetched_this_cycle:

                was_revealed = bool(
                    track.revealed
                )

                is_placeholder = (
                    is_placeholder_fallback(
                        metadata
                    )
                )

                if (
                    watched.baseline_locked
                    and watched.baseline_signature
                ):

                    now_revealed = (
                        signature
                        != watched.baseline_signature
                        and not is_placeholder
                    )

                else:

                    now_revealed = (
                        not is_placeholder
                    )

                track.revealed = (
                    now_revealed
                )

                if (
                    now_revealed
                    and not was_revealed
                ):

                    changed_count += 1

                if now_revealed:

                    metadata_cache[
                        track.token_id
                    ] = metadata

                else:

                    metadata_cache.pop(
                        track.token_id,
                        None,
                    )

            try:

                session.commit()

            except Exception:

                session.rollback()

            cumulative_revealed_items = list(
                metadata_cache.items()
            )

        # ====================================================
        # 13. Current Revealed Count
        # ====================================================

        cumulative_count = len(
            cumulative_revealed_items
        )

        total_supply = (
            watched.max_supply
            or 0
        )

        minted_supply = (
            COLLECTION_TOTAL_SUPPLY_CACHE.get(
                watched.id
            )
        )

        # ====================================================
        # 14. IMPORTANT FIX
        #
        # اقرأ Collection قبل تحديث revealed_count.
        #
        # النسخة السابقة كانت:
        #
        # ensure_collection_placeholder()
        # existing_collection = query(...)
        #
        # وهذا يجعل previous_count == cumulative_count
        # وبالتالي لا يتم تشغيل rarity.
        # ====================================================

        existing_collection = (
            session.query(
                Collection
            )
            .filter_by(
                slug=watched.slug
            )
            .first()
        )

        previous_count = (
            existing_collection.revealed_count
            if existing_collection
            else 0
        )

        has_rare_items = bool(
            existing_collection
            and len(
                existing_collection.rare_items
            ) > 0
        )

        # ====================================================
        # 15. Update Collection Statistics
        # ====================================================

        ensure_collection_placeholder(
            session,
            watched,
            revealed_count=cumulative_count,
        )

        # ====================================================
        # 16. Rarity Computation
        # ====================================================

        should_compute_rarity = (

            cumulative_count > 0

            and (
                cumulative_count
                > previous_count

                or not has_rare_items
            )

            and cumulative_revealed_items
        )

        if should_compute_rarity:

            t0 = time.monotonic()

            result = (
                recompute_from_chain_data(
                    session,
                    watched,
                    cumulative_revealed_items,
                )
            )

            rarity_elapsed = round(
                time.monotonic()
                - t0,
                3,
            )

            if result.get("ok"):

                log.info(
                    f"[{watched.slug}] "
                    f"Rarity محسوبة بنجاح: "
                    f"{result.get('revealed_total')}"
                    f"/"
                    f"{result.get('total_supply', total_supply)} "
                    f"خلال "
                    f"{rarity_elapsed}s."
                )

            else:

                log.info(
                    f"[{watched.slug}] "
                    f"Rarity لم تكتمل: "
                    f"{result.get('reason', 'unknown')}"
                )

        # ====================================================
        # 17. Diagnostics
        # ====================================================

        total_cycle_time = round(
            time.monotonic()
            - cycle_start,
            3,
        )

        if minted_supply is not None:

            log.info(
                f"[{watched.slug}] "
                f"Reveal="
                f"{cumulative_count}/"
                f"{total_supply} | "
                f"Minted="
                f"{minted_supply} | "
                f"new="
                f"{locals().get('changed_count', 0)} | "
                f"cycle="
                f"{total_cycle_time}s"
            )

        else:

            log.info(
                f"[{watched.slug}] "
                f"Reveal="
                f"{cumulative_count}/"
                f"{total_supply} | "
                f"new="
                f"{locals().get('changed_count', 0)} | "
                f"cycle="
                f"{total_cycle_time}s"
            )

    except Exception as exc:

        log.exception(
            f"[{watched_id}] "
            f"فشل تنفيذ دورة المراقبة: "
            f"{exc}"
        )

    finally:

        try:
            session.close()

        except Exception:
            pass


# ============================================================
# Timeout Wrapper
# ============================================================

async def process_collection_with_timeout(
    watched_id: int,
):

    try:

        await asyncio.wait_for(
            process_collection_async(
                watched_id
            ),
            timeout=30.0,
        )

    except asyncio.TimeoutError:

        log.warning(
            f"[{watched_id}] "
            f"تجاوزت دورة المراقبة "
            f"30 ثانية."
        )

    except Exception as exc:

        log.exception(
            f"[{watched_id}] "
            f"خطأ في wrapper: "
            f"{exc}"
        )


# ============================================================
# Main Loop
# ============================================================

async def main_async_loop():

    init_db()

    log.info(
        "بدأ محرك المراقبة المباشر من البلوكشين."
    )

    while True:

        try:

            session = SessionLocal()

            try:

                watched_list = (
                    session.query(
                        WatchedCollection.id
                    )
                    .filter_by(
                        active=True
                    )
                    .all()
                )

            finally:

                session.close()

            if watched_list:

                tasks = [

                    process_collection_with_timeout(
                        row.id
                    )

                    for row
                    in watched_list[:3]
                ]

                await asyncio.gather(
                    *tasks,
                    return_exceptions=True,
                )

            else:

                log.info(
                    "لا توجد Collections تحت المراقبة."
                )

        except Exception as exc:

            log.exception(
                f"فشل loop الرئيسي: "
                f"{exc}"
            )

            await asyncio.sleep(4)

            continue

        await asyncio.sleep(
            POLL_INTERVAL
        )


if __name__ == "__main__":

    asyncio.run(
        main_async_loop()
    )

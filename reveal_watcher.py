"""
محرك المراقبة الجارف المزود بجلب مباشر متوازٍ من IPFS/HTTP ومحرك استعلام السقف الأقصى من البلوكشين.
"""

import asyncio
import json
import logging
import time
from datetime import datetime, timezone

from eth_abi import decode as eth_abi_decode
from web3 import Web3

from models import RevealTrack, WatchedCollection, SessionLocal, init_db
from chain_reader import (async_batch_get_token_uris, resolve_metadata,
                          async_batch_resolve_metadata, detect_global_reveal_flag, get_web3)
from rarity_core import fetch_max_supply, fetch_drop_status
from rarity_storage import (recompute_from_chain_data, ensure_collection_placeholder,
                             content_signature, is_placeholder_fallback,
                             compute_baseline_signature)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("reveal-watcher")

POLL_INTERVAL = 2
BATCH_SIZE = 500
DEFAULT_START_TOKEN_ID = 1

COLLECTION_METADATA_CACHE = {}


def is_dynamic_url(uri: str) -> bool:
    return uri.startswith("http://") or uri.startswith("https://")


def resolve_max_supply(watched: WatchedCollection) -> int:
    """
    استعلام السقف الأقصى الحقيقي للمجموعة مباشرة من عقد البلوكشين الذكي أولاً،
    ثم الاستعلام من OpenSea API كاحتياطي ثانٍ.
    """
    chain = watched.chain or "ethereum"

    # 1. فحص دوال السقف الأقصى المعيارية على البلوكشين مباشرة (On-Chain Call)
    # selectors: maxSupply() = 0xd5abeb01, MAX_SUPPLY() = 0xd368b122, maxTokens() = 0x3a4b66f1, totalSupply() = 0x18160ddd
    selectors = [
        bytes.fromhex("d5abeb01"),  # maxSupply()
        bytes.fromhex("d368b122"),  # MAX_SUPPLY()
        bytes.fromhex("3a4b66f1"),  # maxTokens()
        bytes.fromhex("18160ddd"),  # totalSupply()
    ]

    try:
        w3 = get_web3(chain)
        checksum_addr = Web3.to_checksum_address(watched.contract_address)

        for sel in selectors:
            try:
                res = w3.eth.call({"to": checksum_addr, "data": sel})
                if res and len(res) == 32:
                    (on_chain_sp,) = eth_abi_decode(["uint256"], res)
                    if on_chain_sp and on_chain_sp > 0:
                        return int(on_chain_sp)
            except Exception:
                continue
    except Exception:
        pass

    # 2. الاستعلام الاحتياطي من OpenSea API
    try:
        supply = fetch_max_supply(watched.slug)
        if supply and supply > 0:
            return supply
        drop_status = fetch_drop_status(watched.slug)
        if drop_status:
            for key in ("max_supply", "total_supply"):
                if drop_status.get(key) and int(drop_status[key]) > 0:
                    return int(drop_status[key])
    except Exception:
        pass

    return watched.max_supply or 10000


def ensure_tracks(session, watched: WatchedCollection) -> bool:
    """تتبع وتوسيع ديناميكي لصفوف المينت فور زيادة السك بالبلوكشين."""
    latest_supply = resolve_max_supply(watched)

    if not watched.max_supply or latest_supply > watched.max_supply:
        old_supply = watched.max_supply or 0
        watched.max_supply = latest_supply
        watched.failed_attempts = 0
        session.commit()
        if old_supply > 0:
            log.info(f"[{watched.slug}] ⚡ تحديث السقف الأقصى من البلوكشين: {old_supply} 👈 {latest_supply}")
        else:
            log.info(f"[{watched.slug}] ⚡ السقف الأقصى المحدد من البلوكشين: {latest_supply}")
        ensure_collection_placeholder(session, watched, revealed_count=0)

    existing_count = session.query(RevealTrack).filter_by(watched_id=watched.id).count()
    if existing_count >= watched.max_supply:
        return True

    existing_ids = {t.token_id for t in session.query(RevealTrack.token_id)
                    .filter_by(watched_id=watched.id).all()}

    new_tracks = []
    for token_id in range(DEFAULT_START_TOKEN_ID, DEFAULT_START_TOKEN_ID + watched.max_supply):
        if token_id not in existing_ids:
            new_tracks.append(RevealTrack(watched_id=watched.id, token_id=token_id))

    if new_tracks:
        session.bulk_save_objects(new_tracks)
        session.commit()
        log.info(f"[{watched.slug}] ⚡ تم إدخال وتوسيع {len(new_tracks)} صف تتبع جديد للقطع المصكوكة.")

    return True


def check_global_flag(session, watched: WatchedCollection):
    chain = watched.chain or "ethereum"
    flag = detect_global_reveal_flag(watched.contract_address, chain=chain)
    if flag != watched.global_revealed_flag:
        watched.global_revealed_flag = flag
        session.commit()
        if flag is not None:
            log.info(f"[{watched.slug}] 📡 مؤشر معلن من العقد نفسه ({chain}): "
                      f"{'انكشفت بالكامل' if flag else 'لسا ما انكشفت'}.")


def detect_base_uri_pattern_smart(sample_uris: dict[int, str]) -> str | None:
    valid_uris = [uri for uri in sample_uris.values() if uri]
    if not valid_uris:
        return None

    if len(set(valid_uris)) == 1 and len(valid_uris) > 1:
        return None

    for tid, uri in sample_uris.items():
        if not uri:
            continue
        str_tid = str(tid)
        if str_tid in uri:
            pattern_parts = uri.rsplit(str_tid, 1)
            candidate_pattern = "{id}".join(pattern_parts)
            for other_tid, other_uri in sample_uris.items():
                if other_tid != tid and other_uri:
                    if candidate_pattern.replace("{id}", str(other_tid)) == other_uri:
                        return candidate_pattern
    return None


async def process_collection_async(watched_id: int):
    session = SessionLocal()
    try:
        watched = session.query(WatchedCollection).filter_by(id=watched_id, active=True).first()
        if not watched:
            return

        ok = ensure_tracks(session, watched)
        if not ok:
            return

        chain = watched.chain or "ethereum"
        check_global_flag(session, watched)

        if watched.global_revealed_flag is False:
            ensure_collection_placeholder(session, watched, revealed_count=0)
            return

        if watched.id not in COLLECTION_METADATA_CACHE:
            COLLECTION_METADATA_CACHE[watched.id] = {}

        tracks = session.query(RevealTrack).filter_by(watched_id=watched.id).all()
        if not tracks:
            return

        token_ids = [t.token_id for t in tracks]
        tracks_by_id = {t.token_id: t for t in tracks}

        uris_to_fetch = {}
        now = datetime.now(timezone.utc)

        sample_ids = [tid for tid in [1, 2, 5, 10, 20, 50, 100] if tid in tracks_by_id]
        if not sample_ids:
            sample_ids = token_ids[:5]

        sample_uris = await async_batch_get_token_uris(watched.contract_address, sample_ids, chain)

        detected_pattern = detect_base_uri_pattern_smart(sample_uris)

        if detected_pattern:
            for tid in token_ids:
                track = tracks_by_id[tid]
                computed_uri = detected_pattern.replace("{id}", str(tid))
                if not track.revealed or track.last_uri != computed_uri:
                    track.last_uri = computed_uri
                    track.content_checked_at = now
                    uris_to_fetch[tid] = computed_uri
        else:
            for i in range(0, len(token_ids), BATCH_SIZE):
                chunk = token_ids[i:i + BATCH_SIZE]
                try:
                    uri_results = await async_batch_get_token_uris(watched.contract_address, chunk, chain)
                except Exception:
                    continue

                for token_id, uri in uri_results.items():
                    if not uri:
                        continue
                    track = tracks_by_id[token_id]
                    if not track.revealed or uri != track.last_uri:
                        track.last_uri = uri
                        track.content_checked_at = now
                        uris_to_fetch[token_id] = uri

        session.commit()

        if not uris_to_fetch:
            return

        # مصدر البيانات الوحيد دائمًا: قراءة مباشرة متوازية من IPFS/HTTP.
        fetched_this_cycle = []
        start_time = time.time()
        metadata_map = await async_batch_resolve_metadata(uris_to_fetch)
        elapsed = round(time.time() - start_time, 2)

        for token_id, metadata in metadata_map.items():
            if metadata is not None:
                track = tracks_by_id[token_id]
                sig = content_signature(metadata)
                fetched_this_cycle.append((track, metadata, sig))

        session.commit()

        if fetched_this_cycle:
            log.info(f"[{watched.slug}] 🚀 تم جلب ميتاداتا {len(fetched_this_cycle)} قطعة مباشرة "
                      f"من IPFS/HTTP بالتوازي خلال {elapsed} ثانية (بدون المرور عبر OpenSea).")

        if not watched.baseline_locked and fetched_this_cycle:
            signatures = [sig for _, _, sig in fetched_this_cycle]
            baseline = compute_baseline_signature(signatures)
            if baseline:
                watched.baseline_signature = baseline
                watched.baseline_locked = True
                session.commit()
                log.info(f"[{watched.slug}] 🔒 حُدّد الشكل الموحّد (baseline) من {len(signatures)} عيّنة.")
            elif len(signatures) >= 15:
                watched.baseline_locked = True
                watched.baseline_signature = None
                session.commit()
                log.info(f"[{watched.slug}] 🔓 المجموعة منكشفة بالكامل (جميع القطع فريدة).")

        changed_count = 0
        for track, metadata, sig in fetched_this_cycle:
            was_revealed = track.revealed

            is_placeholder = is_placeholder_fallback(metadata)
            if watched.baseline_locked and watched.baseline_signature:
                now_revealed = (sig != watched.baseline_signature) and not is_placeholder
            else:
                now_revealed = not is_placeholder

            track.revealed = now_revealed
            if now_revealed and not was_revealed:
                changed_count += 1

            if now_revealed:
                COLLECTION_METADATA_CACHE[watched.id][track.token_id] = metadata
            elif track.token_id in COLLECTION_METADATA_CACHE[watched.id]:
                del COLLECTION_METADATA_CACHE[watched.id][track.token_id]

        session.commit()

        cumulative_revealed_items = list(COLLECTION_METADATA_CACHE[watched.id].items())
        cumulative_count = len(cumulative_revealed_items)

        ensure_collection_placeholder(session, watched, revealed_count=cumulative_count)

        from models import Collection
        existing_collection = session.query(Collection).filter_by(slug=watched.slug).first()
        has_rare_items = existing_collection and len(existing_collection.rare_items) > 0

        if (changed_count > 0 or (cumulative_count > 0 and not has_rare_items)) and cumulative_revealed_items:
            result = recompute_from_chain_data(session, watched, cumulative_revealed_items)
            if result.get("ok"):
                log.info(f"[{watched.slug}] 🎯 [الترتيب النهائي] تم حساب ندرة وترتيب {result['revealed_total']} قطعة منكشفة على شبكة ({chain}) بالكامل!")

    except Exception as e:
        log.error(f"[خطأ معالجة]: {e}")
    finally:
        session.close()


async def main_async_loop():
    init_db()
    log.info("🚀 بدأ محرك المراقبة الجارف السريع الذكي (مصدر واحد: البلوكشين/IPFS مباشرة + On-Chain MaxSupply).")

    while True:
        try:
            session = SessionLocal()
            try:
                watched_list = session.query(WatchedCollection.id).filter_by(active=True).all()
                if watched_list:
                    tasks = [process_collection_async(w.id) for w in watched_list[:3]]
                    await asyncio.gather(*tasks, return_exceptions=True)
                else:
                    log.info("لا توجد مجموعات تحت المراقبة حاليًا.")
            finally:
                session.close()
        except Exception as e:
            log.warning(f"بانتظار استجابة خادم Postgres: {e}")
            await asyncio.sleep(4)
            continue

        await asyncio.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    asyncio.run(main_async_loop())


"""
محرك المراقبة الجارف - مصدر واحد فقط: البلوكشين/IPFS مباشرة، مع تشخيص كامل واستعلام On-Chain MaxSupply وتوسيع حقيقي.
"""

import asyncio
import json
import logging
import re
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
    """استعلام المعروض الحقيقي المباشر من البلوكشين أولاً."""
    chain = watched.chain or "ethereum"

    selectors = [
        bytes.fromhex("18160ddd"),  # totalSupply()
        bytes.fromhex("d5abeb01"),  # maxSupply()
        bytes.fromhex("d368b122"),  # MAX_SUPPLY()
        bytes.fromhex("3a4b66f1"),  # maxTokens()
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
    latest_supply = resolve_max_supply(watched)

    if not watched.max_supply or latest_supply > watched.max_supply:
        old_supply = watched.max_supply or 0
        watched.max_supply = latest_supply
        watched.failed_attempts = 0
        session.commit()
        if old_supply > 0:
            log.info(f"[{watched.slug}] ⚡ تحديث السقف الأقصى من البلوكشين: {old_supply} 👈 {latest_supply}")
        else:
            log.info(f"[{watched.slug}] الحد الأقصى للعرض: {latest_supply}")
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
            log.info(f"[{watched.slug}] 📡 [إعلامي فقط، غير مُعتمَد كحاجز] مؤشر من العقد ({chain}): "
                      f"{'انكشفت' if flag else 'لسا ما انكشفت'}.")


def detect_base_uri_pattern_smart(sample_uris: dict[int, str]) -> tuple[str | None, int]:
    valid_uris = {tid: uri for tid, uri in sample_uris.items() if uri}
    if not valid_uris:
        return None, 0

    first_uri = next(iter(valid_uris.values()))
    if first_uri.startswith("data:"):
        return None, 0

    if len(set(valid_uris.values())) == 1 and len(valid_uris) > 1:
        return None, 0

    for tid, uri in valid_uris.items():
        if not uri or uri.startswith("data:"):
            continue
        str_tid = str(tid)
        match = re.search(r'(0*)' + re.escape(str_tid) + r'(\.[a-zA-Z0-9]+)?$', uri)
        if match:
            zeros = match.group(1)
            ext = match.group(2) or ""
            padding_width = len(zeros) + len(str_tid)

            full_match = zeros + str_tid + ext
            pattern_parts = uri.rsplit(full_match, 1)
            candidate_pattern = "{id}" + ext
            pattern_template = pattern_parts[0] + candidate_pattern

            return pattern_template, padding_width

        elif str_tid in uri:
            pattern_parts = uri.rsplit(str_tid, 1)
            return "{id}".join(pattern_parts), 0

    return None, 0


async def process_collection_async(watched_id: int):
    session = SessionLocal()
    cycle_start = time.monotonic()
    try:
        watched = session.query(WatchedCollection).filter_by(id=watched_id, active=True).first()
        if not watched:
            return

        ok = ensure_tracks(session, watched)
        if not ok:
            return

        chain = watched.chain or "ethereum"
        check_global_flag(session, watched)

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

        t0 = time.monotonic()
        sample_uris = await async_batch_get_token_uris(watched.contract_address, sample_ids, chain)
        log.info(f"[{watched.slug}] [تشخيص توقيت] جلب عيّنة الأنماط ({len(sample_ids)} قطعة): "
                  f"{round(time.monotonic() - t0, 3)} ثانية.")

        detected_pattern, padding_width = detect_base_uri_pattern_smart(sample_uris)
        if detected_pattern:
            log.info(f"[{watched.slug}] [تشخيص نمط] اكتُشف نمط متسلسل: {detected_pattern[:80]} (الأصفار: {padding_width})")

        t0 = time.monotonic()
        if detected_pattern:
            for tid in token_ids:
                track = tracks_by_id[tid]
                formatted_tid = str(tid).zfill(padding_width) if padding_width > 0 else str(tid)
                computed_uri = detected_pattern.replace("{id}", formatted_tid)
                if not track.revealed or track.last_uri != computed_uri:
                    track.last_uri = computed_uri
                    track.content_checked_at = now
                    uris_to_fetch[tid] = computed_uri
        else:
            for i in range(0, len(token_ids), BATCH_SIZE):
                chunk = token_ids[i:i + BATCH_SIZE]
                try:
                    uri_results = await async_batch_get_token_uris(watched.contract_address, chunk, chain)
                except Exception as e:
                    log.error(f"[{watched.slug}] خطأ بجلب دفعة tokenURI: {e}")
                    continue

                for token_id, uri in uri_results.items():
                    if not uri:
                        continue
                    track = tracks_by_id[token_id]
                    if not track.revealed or uri != track.last_uri:
                        track.last_uri = uri
                        track.content_checked_at = now
                        uris_to_fetch[token_id] = uri

        try:
            session.commit()
        except Exception as e:
            log.error(f"[{watched.slug}] فشل حفظ روابط tokenURI (تعارض توقيت محتمل): {e}")
            session.rollback()

        if not uris_to_fetch:
            log.info(f"[{watched.slug}] [تشخيص] لا يوجد أي رابط جديد يحتاج فحص هذي الدورة.")
            return

        t0 = time.monotonic()
        fetched_this_cycle = []
        metadata_map = await async_batch_resolve_metadata(uris_to_fetch)
        elapsed = round(time.monotonic() - t0, 3)

        success_count = sum(1 for v in metadata_map.values() if v is not None)
        log.info(f"[{watched.slug}] [تشخيص توقيت] جلب المحتوى الفعلي (IPFS/HTTP): "
                  f"{elapsed} ثانية | نجح {success_count}/{len(uris_to_fetch)}.")

        for token_id, metadata in metadata_map.items():
            if metadata is not None:
                track = tracks_by_id[token_id]
                sig = content_signature(metadata)
                fetched_this_cycle.append((track, metadata, sig))
                COLLECTION_METADATA_CACHE[watched.id][token_id] = metadata

        try:
            session.commit()
        except Exception as e:
            log.error(f"[{watched.slug}] فشل حفظ الميتاداتا المجلوبة: {e}")
            session.rollback()

        if fetched_this_cycle:
            log.info(f"[{watched.slug}] 🚀 تم جلب ميتاداتا {len(fetched_this_cycle)} قطعة حقيقية خلال {elapsed} ثانية!")

        if not watched.baseline_locked and fetched_this_cycle:
            signatures = [sig for _, _, sig in fetched_this_cycle]
            baseline = compute_baseline_signature(signatures)
            if baseline:
                watched.baseline_signature = baseline
                watched.baseline_locked = True
                try:
                    session.commit()
                except Exception as e:
                    log.error(f"[{watched.slug}] فشل حفظ baseline: {e}")
                    session.rollback()
                log.info(f"[{watched.slug}] 🔒 [تشخيص كشف] حُدّد الشكل الموحّد (baseline) من {len(signatures)} عيّنة.")
            elif len(signatures) >= 15:
                watched.baseline_locked = True
                watched.baseline_signature = None
                try:
                    session.commit()
                except Exception as e:
                    log.error(f"[{watched.slug}] فشل حفظ حالة baseline: {e}")
                    session.rollback()
                log.info(f"[{watched.slug}] 🔓 [تشخيص كشف] المجموعة منكشفة بالكامل (كل القطع فريدة، ولا baseline).")

        changed_count = 0
        conflict_count = 0
        diag_samples = []

        for track, metadata, sig in fetched_this_cycle:
            was_revealed = track.revealed
            is_placeholder = is_placeholder_fallback(metadata)

            if watched.baseline_locked and watched.baseline_signature:
                differs_from_baseline = sig != watched.baseline_signature
                now_revealed = differs_from_baseline and not is_placeholder
                method = "baseline+fallback"
                if differs_from_baseline and is_placeholder:
                    conflict_count += 1
                    if conflict_count <= 5:
                        log.warning(
                            f"[{watched.slug}] ⚠️ [تعارض كشف] القطعة #{track.token_id}: "
                            f"البصمة تختلف عن baseline (يفترض منكشفة) لكن "
                            f"is_placeholder_fallback رفضها. الاسم: '{metadata.get('name','')}' | "
                            f"عدد الصفات: {len(metadata.get('traits') or metadata.get('attributes') or [])}"
                        )
            else:
                now_revealed = not is_placeholder
                method = "fallback-only (لسا ما تحدد baseline)"

            track.revealed = now_revealed
            if now_revealed and not was_revealed:
                changed_count += 1
                if len(diag_samples) < 5:
                    diag_samples.append(f"#{track.token_id}({method})")

            if now_revealed:
                COLLECTION_METADATA_CACHE[watched.id][track.token_id] = metadata
            elif track.token_id in COLLECTION_METADATA_CACHE[watched.id]:
                del COLLECTION_METADATA_CACHE[watched.id][track.token_id]

        try:
            session.commit()
        except Exception as e:
            log.error(f"[{watched.slug}] فشل حفظ حالة الانكشاف: {e}")
            session.rollback()

        if changed_count:
            log.info(f"[{watched.slug}] 🔔 [تشخيص كشف] {changed_count} قطعة انكشفت هذي الدورة. "
                      f"أمثلة: {', '.join(diag_samples)}")
        if conflict_count:
            log.warning(f"[{watched.slug}] ⚠️ [تشخيص كشف] إجمالي {conflict_count} حالة تعارض "
                        f"بين الإشارتين هذي الدورة (راجع التحذيرات فوق للتفاصيل).")

        cumulative_revealed_items = list(COLLECTION_METADATA_CACHE[watched.id].items())
        cumulative_count = len(cumulative_revealed_items)

        ensure_collection_placeholder(session, watched, revealed_count=cumulative_count)

        from models import Collection
        existing_collection = session.query(Collection).filter_by(slug=watched.slug).first()
        has_rare_items = existing_collection and len(existing_collection.rare_items) > 0

        if (changed_count > 0 or (cumulative_count > 0 and not has_rare_items)) and cumulative_revealed_items:
            t0 = time.monotonic()
            result = recompute_from_chain_data(session, watched, cumulative_revealed_items)
            if result.get("ok"):
                log.info(f"[{watched.slug}] 🎯 [تشخيص توقيت] حساب الترتيب الكامل لـ "
                          f"{result['revealed_total']} قطعة: {round(time.monotonic() - t0, 3)} ثانية.")

        total_cycle_time = round(time.monotonic() - cycle_start, 3)
        log.info(f"[{watched.slug}] ⏱️ [تشخيص] إجمالي وقت هذي الدورة كاملة: {total_cycle_time} ثانية.")

    except Exception as e:
        log.error(f"[معالجة watched_id={watched_id}] خطأ غير متوقع: {e}")
        try:
            session.rollback()
        except Exception:
            pass
    finally:
        try:
            session.close()
        except Exception:
            pass


async def process_collection_with_timeout(watched_id: int):
    session = SessionLocal()
    max_sup = 10000
    try:
        watched = session.query(WatchedCollection).filter_by(id=watched_id, active=True).first()
        if watched and watched.max_supply:
            max_sup = watched.max_supply
    except Exception:
        pass
    finally:
        session.close()

    dynamic_timeout = max(35.0, float(max_sup / 1000.0) * 10.0)

    try:
        await asyncio.wait_for(process_collection_async(watched_id), timeout=dynamic_timeout)
    except asyncio.TimeoutError:
        log.warning(f"[watched_id={watched_id}] [Timeout] فحص الكولكشن استغرق أكثر من {dynamic_timeout} ثانية — للانتقال للدورة التالية.")
    except Exception as e:
        log.error(f"[watched_id={watched_id}] خطأ غير متوقع بمهمة المعالجة: {e}")


async def main_async_loop():
    init_db()
    log.info("🚀 بدأ محرك المراقبة الجارف السريع اللحظي المباشر.")

    while True:
        try:
            session = SessionLocal()
            try:
                watched_list = session.query(WatchedCollection.id).filter_by(active=True).all()
                if watched_list:
                    tasks = [process_collection_with_timeout(w.id) for w in watched_list[:3]]
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

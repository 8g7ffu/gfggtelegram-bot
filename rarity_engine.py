"""
محرك حساب الندرة (Rarity Engine) - المرحلة 1 من مشروع "رادار الندرة".

يجيب كل قطع مجموعة معينة من OpenSea (مع الترقيم الصفحي الكامل)، يحسب
Rarity Score لكل قطعة بالمعادلة المعيارية المستخدمة من أدوات الصناعة
(Rarity Tools / Trait Sniper / HowRare):

    Rarity Score للصفة = 1 / (عدد القطع اللي فيها هذي القيمة / إجمالي القطع)
    Rarity Score الكلي للقطعة = مجموع Rarity Score لكل صفاتها

كل ما زاد الرقم، كل ما كانت القطعة أندر. يرتب النتائج من الأندر للأقل ندرة.
"""

import json
import os
import time

import requests
from dotenv import load_dotenv

load_dotenv()

OPENSEA_API_KEY = os.environ["OPENSEA_API_KEY"]

TEST_COLLECTION_SLUG = os.environ.get("TEST_COLLECTION_SLUG", "doodles-official")
PAGE_LIMIT = 200  # أقصى حد يسمح به OpenSea لكل صفحة
TOP_N_TO_SHOW = 20  # عدد أندر القطع نعرضها بالتفصيل بالنهاية


def fetch_all_nfts(slug: str) -> list[dict]:
    """يجيب كل قطع المجموعة عبر الترقيم الصفحي الكامل (pagination)."""
    all_nfts = []
    cursor = None
    page = 1

    while True:
        params = {"limit": PAGE_LIMIT}
        if cursor:
            params["next"] = cursor

        resp = requests.get(
            f"https://api.opensea.io/api/v2/collection/{slug}/nfts",
            headers={"x-api-key": OPENSEA_API_KEY},
            params=params,
            timeout=15,
        )

        if resp.status_code != 200:
            print(f"[خطأ] فشل جلب الصفحة {page}: HTTP {resp.status_code} - {resp.text[:200]}")
            break

        data = resp.json()
        nfts = data.get("nfts", [])
        all_nfts.extend(nfts)

        print(f"[تقدم] صفحة {page}: جُلبت {len(nfts)} قطعة (الإجمالي حتى الآن: {len(all_nfts)})")

        cursor = data.get("next")
        page += 1

        if not cursor or not nfts:
            break

        time.sleep(0.2)  # مجاملة بسيطة لتفادي أي حد إرسال (rate limit)

    return all_nfts


def build_trait_frequency(nfts: list[dict]) -> dict:
    """يحسب: كم مرة تكررت كل قيمة (value) داخل كل فئة صفة (trait_type)."""
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
    """يحسب Rarity Score لكل قطعة حسب المعادلة المعيارية، ويرجع قائمة مرتبة."""
    results = []

    for nft in nfts:
        traits = nft.get("traits") or []
        score = 0.0
        trait_breakdown = []

        for trait in traits:
            t_type = trait.get("trait_type")
            t_value = trait.get("value")
            if t_type is None or t_value is None:
                continue
            count = freq.get(t_type, {}).get(t_value, 1)
            trait_score = 1 / (count / total)
            score += trait_score
            trait_breakdown.append({
                "trait_type": t_type,
                "value": t_value,
                "count": count,
                "percentage": round(count / total * 100, 2),
                "trait_score": round(trait_score, 2),
            })

        results.append({
            "identifier": nft.get("identifier"),
            "name": nft.get("name") or f"#{nft.get('identifier')}",
            "opensea_url": nft.get("opensea_url", ""),
            "image_url": nft.get("image_url", ""),
            "rarity_score": round(score, 2),
            "traits": trait_breakdown,
        })

    results.sort(key=lambda x: x["rarity_score"], reverse=True)

    for rank, item in enumerate(results, start=1):
        item["rank"] = rank

    return results


def main():
    print(f"[بدء] جاري حساب الندرة لمجموعة '{TEST_COLLECTION_SLUG}'...\n")

    nfts = fetch_all_nfts(TEST_COLLECTION_SLUG)
    total = len(nfts)

    if total == 0:
        print("[خطأ] لم يتم جلب أي قطع. تأكد من صحة اسم المجموعة (slug).")
        return

    print(f"\n[معلومة] إجمالي القطع المجلوبة: {total}")

    freq = build_trait_frequency(nfts)
    print(f"[معلومة] عدد فئات الصفات المكتشفة: {len(freq)}")
    for t_type, values in freq.items():
        print(f"   - {t_type}: {len(values)} قيمة مختلفة")

    ranked = compute_rarity_scores(nfts, freq, total)

    print(f"\n{'=' * 70}")
    print(f"أندر {TOP_N_TO_SHOW} قطعة بالمجموعة (الأعلى Rarity Score):")
    print(f"{'=' * 70}")
    for item in ranked[:TOP_N_TO_SHOW]:
        print(f"#{item['rank']:>4} | {item['name']:<20} | Score: {item['rarity_score']:>8} | {item['opensea_url']}")

    # حفظ النتيجة الكاملة بملف JSON (سيُستخدم لاحقًا لتغذية صفحة الويب بالمرحلة 2)
    output_path = "/tmp/rarity_results.json"
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump({
            "collection_slug": TEST_COLLECTION_SLUG,
            "total_items": total,
            "computed_at": time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime()),
            "ranked_items": ranked,
        }, f, ensure_ascii=False, indent=2)

    print(f"\n[تم] حُفظت النتيجة الكاملة ({len(ranked)} قطعة مرتبة) في: {output_path}")


if __name__ == "__main__":
    main()



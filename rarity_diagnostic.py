"""
سكربت تشخيصي - الخطوة الأولى فقط من مشروع "رادار الندرة".

الهدف: التأكد من الشكل الحقيقي لبيانات الصفات (Traits) اللي يرجعها OpenSea
لكل قطعة NFT، قبل ما نبني محرك حساب الندرة الكامل. توثيق OpenSea ما يعطي
مثال كامل واضح لهذا الـ endpoint، فبدل ما نخمن أسماء الحقول، نطبعها فعليًا
من استجابة حقيقية ونبني عليها بثقة.

يجيب أول عدد محدود من القطع لمجموعة اخترتها (يفضل مجموعة منكشفة بالكامل
حتى تكون الصفات ظاهرة فعليًا)، ويطبع أول قطعة كاملة + قائمة أسماء الصفات
لكل القطع المجلوبة، حتى نتأكد الحقول ثابتة ومتوقعة.
"""

import json
import os

import requests
from dotenv import load_dotenv

load_dotenv()

OPENSEA_API_KEY = os.environ["OPENSEA_API_KEY"]

# غيّر هذي القيم لاختبار مجموعة مختلفة عبر متغيرات البيئة
TEST_COLLECTION_SLUG = os.environ.get("TEST_COLLECTION_SLUG", "doodles-official")
TEST_LIMIT = int(os.environ.get("TEST_LIMIT", "5"))


def main():
    print(f"[تشخيص] جاري جلب {TEST_LIMIT} قطع من مجموعة '{TEST_COLLECTION_SLUG}'...")

    resp = requests.get(
        f"https://api.opensea.io/api/v2/collection/{TEST_COLLECTION_SLUG}/nfts",
        headers={"x-api-key": OPENSEA_API_KEY},
        params={"limit": TEST_LIMIT},
        timeout=15,
    )

    print(f"[تشخيص] رمز الاستجابة: {resp.status_code}")

    if resp.status_code != 200:
        print(f"[تشخيص] فشل الطلب: {resp.text[:500]}")
        return

    data = resp.json()
    nfts = data.get("nfts", [])
    print(f"[تشخيص] عدد القطع المُستلمة: {len(nfts)}")

    if not nfts:
        print("[تشخيص] لم يرجع أي قطع — تأكد من صحة اسم المجموعة (slug).")
        return

    print("\n" + "=" * 60)
    print("أول قطعة كاملة (لمعرفة كل الحقول المتاحة):")
    print("=" * 60)
    print(json.dumps(nfts[0], indent=2, ensure_ascii=False))

    print("\n" + "=" * 60)
    print("ملخص الصفات (Traits) لكل قطعة مجلوبة:")
    print("=" * 60)
    for i, nft in enumerate(nfts, start=1):
        name = nft.get("name") or nft.get("identifier", "بدون اسم")
        traits = nft.get("traits")
        print(f"\n{i}) {name}")
        if traits is None:
            print("   ⚠️ حقل 'traits' غير موجود في هذه القطعة!")
        elif not traits:
            print("   (قائمة صفات فارغة)")
        else:
            for t in traits:
                print(f"   - {t}")


if __name__ == "__main__":
    main()


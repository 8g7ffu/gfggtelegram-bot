"""
تطبيق ويب (Flask) لمشروع "رادار الندرة" مع استجابة لحظية مانعة لتعليق الصفحة.
"""

import os
import re
import time

from flask import Flask, redirect, render_template, request, url_for, jsonify
from sqlalchemy.orm import joinedload

from models import Collection, WatchedCollection, RareItem, SessionLocal, init_db
from rarity_core import fetch_contract_address, fetch_best_listings
from price_utils import get_eth_usd_rate

app = Flask(__name__)

ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "")

_prices_cache = {"data": {}, "fetched_at": 0}
PRICES_CACHE_TTL = 2


def slug_from_input(raw: str) -> str:
    raw = raw.strip()
    match = re.search(r"opensea\.io/collection/([a-zA-Z0-9\-]+)", raw)
    if match:
        return match.group(1)
    return raw


def normalize_chain(chain_str: str | None) -> str:
    if not chain_str:
        return "ethereum"
    c = chain_str.lower().strip()
    if c in ("mainnet", "eth"):
        return "ethereum"
    return c


def check_token(form_or_args) -> bool:
    if not ADMIN_TOKEN:
        return True
    return form_or_args.get("token") == ADMIN_TOKEN


@app.route("/")
def dashboard():
    session = SessionLocal()
    try:
        # استعلام فائق السرعة مدمج يفتح الصفحة في ميكروثانية
        collections = (session.query(Collection)
                       .options(joinedload(Collection.rare_items))
                       .order_by(Collection.computed_at.desc())
                       .all())
        watched = (session.query(WatchedCollection)
                   .order_by(WatchedCollection.added_at.desc())
                   .all())
        token = request.args.get("token", "")
        return render_template("index.html", collections=collections,
                                watched=watched, token=token,
                                admin_token_set=bool(ADMIN_TOKEN))
    finally:
        session.close()


@app.route("/api/prices")
def api_prices():
    session = SessionLocal()
    try:
        now = time.time()
        if _prices_cache["data"] and (now - _prices_cache["fetched_at"]) < PRICES_CACHE_TTL:
            return jsonify(_prices_cache["data"])

        watched_collections = session.query(WatchedCollection).filter_by(active=True).all()
        eth_usd_rate = get_eth_usd_rate()
        updated_data = {}

        for watched in watched_collections:
            col = session.query(Collection).filter_by(slug=watched.slug).first()
            if not col or not col.rare_items:
                continue

            price_map_eth, success = fetch_best_listings(watched.slug)

            if not success and not price_map_eth:
                for item in col.rare_items:
                    formatted_usd = f"${item.price_usd:.2f}" if item.price_usd is not None else "غير مسعّر"
                    updated_data[item.id] = {
                        "price_eth": item.price_eth,
                        "price_usd": formatted_usd,
                        "is_listed": item.price_usd is not None
                    }
                continue

            for item in col.rare_items:
                price_eth = price_map_eth.get(str(item.identifier)) if isinstance(price_map_eth, dict) else None
                if price_eth and price_eth > 500:
                    price_eth = None

                price_usd = (price_eth * eth_usd_rate) if (price_eth is not None and eth_usd_rate) else None

                item.price_eth = price_eth
                item.price_usd = price_usd

                formatted_usd = f"${price_usd:.2f}" if price_usd is not None else "غير مسعّر"
                updated_data[item.id] = {
                    "price_eth": price_eth,
                    "price_usd": formatted_usd,
                    "is_listed": price_usd is not None
                }

        session.commit()
        _prices_cache["data"] = updated_data
        _prices_cache["fetched_at"] = now
        return jsonify(updated_data)
    except Exception:
        return jsonify(_prices_cache.get("data", {}))
    finally:
        session.close()


@app.route("/add")
def add_form():
    token_ok = bool(ADMIN_TOKEN) and request.args.get("token") == ADMIN_TOKEN
    return render_template("add.html", token_ok=token_ok, admin_token_set=bool(ADMIN_TOKEN))


@app.route("/submit", methods=["POST"])
def submit():
    if not check_token(request.form):
        return "غير مصرح - تأكد من الرابط الصحيح.", 403

    slug = slug_from_input(request.form.get("link", ""))
    if not slug:
        return redirect(url_for("add_form"))

    address, chain = fetch_contract_address(slug)
    if not address:
        return render_template(
            "add.html", token_ok=True, admin_token_set=bool(ADMIN_TOKEN),
            error=f"تعذر إيجاد عنوان العقد تلقائيًا لـ '{slug}'. تأكد من صحة الرابط أو الاسم."
        )

    clean_chain = normalize_chain(chain)

    session = SessionLocal()
    try:
        existing = session.query(WatchedCollection).filter_by(slug=slug).first()
        if existing:
            existing.contract_address = address
            existing.chain = clean_chain
            existing.active = True
            existing.failed_attempts = 0
            existing.baseline_locked = False
            existing.baseline_signature = None
        else:
            session.add(WatchedCollection(slug=slug, contract_address=address, chain=clean_chain))
        session.commit()
    finally:
        session.close()

    return redirect(url_for("dashboard"))


@app.route("/delete_collection/<int:collection_id>", methods=["POST"])
def delete_collection(collection_id):
    if not check_token(request.form):
        return "غير مصرح.", 403
    session = SessionLocal()
    try:
        col = session.query(Collection).filter_by(id=collection_id).first()
        if col:
            slug = col.slug.strip()
            session.delete(col)
            session.flush()
            watched_matches = session.query(WatchedCollection).filter(
                WatchedCollection.slug == slug
            ).all()
            for w in watched_matches:
                session.delete(w)
            session.commit()
    finally:
        session.close()
    return redirect(url_for("dashboard"))


@app.route("/delete_watch/<int:watched_id>", methods=["POST"])
def delete_watch(watched_id):
    if not check_token(request.form):
        return "غير مصرح.", 403
    session = SessionLocal()
    try:
        w = session.query(WatchedCollection).filter_by(id=watched_id).first()
        if w:
            session.delete(w)
            session.commit()
    finally:
        session.close()
    return redirect(url_for("dashboard"))


if __name__ == "__main__":
    init_db()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
else:
    init_db()


"""
CS2 Skin Tracker — REST API для Telegram Mini App
Порт: 8001
Запуск: python3 api.py
"""

import sqlite3
import requests
import re
import time
import threading
from flask import Flask, jsonify, request
from flask_cors import CORS

try:
    from config import ADMIN_CHAT_ID as ADMIN_ID
except ImportError:
    try:
        from config import ADMIN_ID
    except ImportError:
        ADMIN_ID = 0

app = Flask(__name__)
CORS(app)  # разрешаем запросы из Telegram WebApp

DB_FILE = "bot_data.db"

# ── Кэш цен (30 минут) ────────────────────────────────────────────────────────
_price_cache: dict = {}
CACHE_TTL = 1800  # секунд

CURRENCIES = {
    "RUB": {"code": 5,  "symbol": "руб."},
    "USD": {"code": 1,  "symbol": "$"},
    "EUR": {"code": 3,  "symbol": "€"},
    "UAH": {"code": 18, "symbol": "₴"},
    "KZT": {"code": 37, "symbol": "₸"},
}

# Топ популярных скинов (статичный список для /api/top)
TOP_EXPENSIVE = [
    "AK-47 | Fire Serpent (Field-Tested)",
    "AWP | Medusa (Factory New)",
    "Karambit | Fade (Factory New)",
    "Karambit | Tiger Tooth (Factory New)",
    "Butterfly Knife | Fade (Factory New)",
    "M9 Bayonet | Fade (Factory New)",
    "Karambit | Marble Fade (Factory New)",
    "Desert Eagle | Blaze (Factory New)",
]

TOP_POPULAR = [
    "AK-47 | Redline (Field-Tested)",
    "AWP | Asiimov (Field-Tested)",
    "M4A4 | Asiimov (Field-Tested)",
    "AK-47 | Vulcan (Field-Tested)",
    "AWP | Hyper Beast (Field-Tested)",
    "Glock-18 | Water Elemental (Factory New)",
    "M4A1-S | Hyper Beast (Field-Tested)",
    "USP-S | Kill Confirmed (Field-Tested)",
]

# Кэш изображений — заполняется при первом поиске каждого скина
_image_cache: dict = {}

# Кэш главной страницы (TTL 30 мин)
_home_cache: dict = {}
HOME_CACHE_TTL = 1800


def _build_home_data(currency: str) -> dict:
    """Строит данные главной страницы — последовательно, с кэшем Steam."""
    expensive = []
    for name in TOP_EXPENSIVE[:8]:
        p = get_price_steam(name, currency)
        if p and p.get("lowest_price", 0) > 0:
            expensive.append({
                "name": name, "lowest_price": p["lowest_price"],
                "median_price": p["median_price"], "volume": p["volume"],
                "image": fetch_skin_image(name), "change": None,
            })

    popular = []
    for name in TOP_POPULAR[:8]:
        p = get_price_steam(name, currency)
        if p and p.get("lowest_price", 0) > 0:
            popular.append({
                "name": name, "lowest_price": p["lowest_price"],
                "median_price": p["median_price"], "volume": p["volume"],
                "image": fetch_skin_image(name), "change": None,
            })

    trending = []
    try:
        with sqlite3.connect(DB_FILE) as conn:
            rows = conn.execute("""
                SELECT skin_name, MIN(price) as price_old, MAX(price) as price_new
                FROM price_history
                WHERE recorded_at > datetime('now', '-25 hours')
                GROUP BY skin_name
                HAVING COUNT(*) >= 2 AND MIN(price) > 0
                ORDER BY (MAX(price) - MIN(price)) / MIN(price) DESC
                LIMIT 8
            """).fetchall()
            for row in rows:
                name, old_p, new_p = row
                change = round(((new_p - old_p) / old_p) * 100, 1)
                if abs(change) < 0.5:
                    continue
                p = get_price_steam(name, currency)
                trending.append({
                    "name": name,
                    "lowest_price": p["lowest_price"] if p else new_p,
                    "median_price": p["median_price"] if p else new_p,
                    "volume": p["volume"] if p else "—",
                    "image": fetch_skin_image(name),
                    "change": change,
                })
    except Exception as e:
        print(f"Trending error: {e}")

    return {"trending": trending, "expensive": expensive, "popular": popular}


def _prewarm_cache():
    """Прогрев кэша при старте сервера — в фоновом потоке."""
    time.sleep(5)  # ждём пока Flask поднимется
    print("Prewarming home cache...")
    try:
        data = _build_home_data("RUB")
        exp_count = len(data['expensive'])
        pop_count = len(data['popular'])
        print(f"Cache warmed: {exp_count} expensive, {pop_count} popular")
        # Кэшируем только если достаточно данных
        if exp_count >= 3 and pop_count >= 3:
            _home_cache["RUB"] = (data, time.time())
        else:
            print(f"Not enough data to cache (exp={exp_count}, pop={pop_count}), will retry on request")
    except Exception as e:
        print(f"Prewarm error: {e}")

threading.Thread(target=_prewarm_cache, daemon=True).start()


def fetch_skin_image(name: str) -> str:
    """Получает URL изображения скина из Steam search (с кэшем)."""
    if name in _image_cache:
        return _image_cache[name]
    try:
        url = "https://steamcommunity.com/market/search/render/"
        params = {"appid": 730, "query": name, "count": 1, "norender": 1, "currency": 5}
        r = requests.get(url, params=params, timeout=5, headers={"User-Agent": "Mozilla/5.0"})
        time.sleep(0.3)
        data = r.json()
        results = data.get("results", [])
        if results:
            asset = results[0].get("asset_description", {})
            if asset.get("icon_url"):
                image = f"https://steamcommunity-a.akamaihd.net/economy/image/{asset['icon_url']}/128x96"
                _image_cache[name] = image
                return image
    except Exception:
        pass
    return ""


# ── Helpers ───────────────────────────────────────────────────────────────────

def parse_price_value(price_str: str) -> float:
    """Парсит цену из строки Steam (напр. '2 993,73 руб.' -> 2993.73)"""
    if not price_str:
        return 0.0
    cleaned = re.sub(r'[^\d,.]', '', price_str).strip('.,')
    if not cleaned:
        return 0.0
    if ',' in cleaned and '.' in cleaned:
        if cleaned.rfind(',') > cleaned.rfind('.'):
            cleaned = cleaned.replace('.', '').replace(',', '.')
        else:
            cleaned = cleaned.replace(',', '')
    elif ',' in cleaned:
        parts = cleaned.split(',')
        if len(parts) == 2 and len(parts[1]) <= 2:
            cleaned = cleaned.replace(',', '.')
        else:
            cleaned = cleaned.replace(',', '')
    try:
        return float(cleaned)
    except ValueError:
        return 0.0


def get_price_steam(name: str, currency: str = "RUB") -> dict | None:
    """Получает цену из Steam Market API с кэшированием."""
    cache_key = f"{name}:{currency}"
    now = time.time()
    if cache_key in _price_cache:
        data, ts = _price_cache[cache_key]
        if now - ts < CACHE_TTL:
            return data

    cur_code = CURRENCIES.get(currency, {}).get("code", 5)
    symbol   = CURRENCIES.get(currency, {}).get("symbol", "руб.")
    url = "https://steamcommunity.com/market/priceoverview/"
    params = {"appid": 730, "currency": cur_code, "market_hash_name": name}

    try:
        r = requests.get(url, params=params, timeout=8,
                         headers={"User-Agent": "Mozilla/5.0"})
        time.sleep(0.4)  # rate limit protection
        if r.status_code == 429:
            return _price_from_db(name)
        data = r.json()
        if not data.get("success"):
            return None

        result = {
            "name":          name,
            "lowest_price":  parse_price_value(data.get("lowest_price", "")),
            "median_price":  parse_price_value(data.get("median_price", "")),
            "volume":        data.get("volume", "0").replace(",", ""),
            "symbol":        symbol,
            "currency":      currency,
        }
        _price_cache[cache_key] = (result, now)
        # сохраняем в историю цен
        _save_price_history(name, result["lowest_price"])
        return result
    except Exception as e:
        print(f"Steam API error: {e}")
        return _price_from_db(name)


def _price_from_db(name: str) -> dict | None:
    """Fallback: последняя известная цена из price_history."""
    try:
        with sqlite3.connect(DB_FILE) as conn:
            row = conn.execute(
                "SELECT price FROM price_history WHERE skin_name=? ORDER BY recorded_at DESC LIMIT 1",
                (name,)
            ).fetchone()
            if row:
                return {"name": name, "lowest_price": row[0], "median_price": row[0],
                        "volume": "—", "symbol": "руб.", "currency": "RUB", "from_cache": True}
    except Exception:
        pass
    return None


def _save_price_history(name: str, price: float):
    try:
        with sqlite3.connect(DB_FILE) as conn:
            conn.execute(
                "INSERT INTO price_history (skin_name, price) VALUES (?, ?)",
                (name, price)
            )
    except Exception:
        pass


def search_steam(query: str, currency: str = "RUB") -> list:
    """Поиск скинов по названию через Steam Market search."""
    cur_code = CURRENCIES.get(currency, {}).get("code", 5)
    url = "https://steamcommunity.com/market/search/render/"
    params = {
        "appid": 730,
        "query": query,
        "count": 8,
        "search_descriptions": 0,
        "norender": 1,
        "currency": cur_code,
    }
    try:
        r = requests.get(url, params=params, timeout=8,
                         headers={"User-Agent": "Mozilla/5.0"})
        time.sleep(0.4)
        if r.status_code != 200:
            return []
        data = r.json()
        results = []
        for item in data.get("results", []):
            name  = item.get("name", "")
            image = ""
            asset = item.get("asset_description", {})
            if asset.get("icon_url"):
                image = f"https://steamcommunity-a.akamaihd.net/economy/image/{asset['icon_url']}/128x96"
                # Кэшируем изображение по имени скина
                if name:
                    _image_cache[name] = image
            # Цену из поиска НЕ используем (US IP возвращает USD центы)
            # Получаем через priceoverview
            price_data = get_price_steam(name, currency)
            results.append({
                "name":          name,
                "image":         image,
                "lowest_price":  price_data["lowest_price"] if price_data else 0,
                "median_price":  price_data["median_price"] if price_data else 0,
                "volume":        price_data["volume"] if price_data else "—",
            })
        return results
    except Exception as e:
        print(f"Search error: {e}")
        return []


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/api/search")
def route_search():
    q        = request.args.get("q", "").strip()
    currency = request.args.get("currency", "RUB").upper()
    if len(q) < 2:
        return jsonify({"items": []})
    items = search_steam(q, currency)
    return jsonify({"items": items})


@app.route("/api/price")
def route_price():
    name     = request.args.get("name", "").strip()
    currency = request.args.get("currency", "RUB").upper()
    if not name:
        return jsonify({"error": "name required"}), 400
    data = get_price_steam(name, currency)
    if not data:
        return jsonify({"error": "not found"}), 404
    return jsonify(data)


@app.route("/api/portfolio")
def route_portfolio():
    user_id  = request.args.get("user_id", 0, type=int)
    currency = request.args.get("currency", "RUB").upper()
    if not user_id:
        return jsonify({"items": []})
    try:
        with sqlite3.connect(DB_FILE) as conn:
            rows = conn.execute(
                "SELECT id, skin_name, purchase_price, added_at FROM portfolio WHERE user_id=? ORDER BY added_at DESC",
                (user_id,)
            ).fetchall()
        items = []
        for row in rows:
            item_id, name, buy_price, added_at = row
            price_data = get_price_steam(name, currency)
            items.append({
                "id":            item_id,
                "name":          name,
                "buy_price":     buy_price,
                "current_price": price_data["lowest_price"] if price_data else buy_price,
                "added_at":      added_at,
                "image":         fetch_skin_image(name),
            })
        return jsonify({"items": items})
    except Exception as e:
        print(f"Portfolio error: {e}")
        return jsonify({"items": []})


@app.route("/api/portfolio/add")
def route_portfolio_add():
    user_id   = request.args.get("user_id", 0, type=int)
    name      = request.args.get("name", "").strip()
    buy_price = request.args.get("buy_price", 0, type=float)
    if not user_id or not name or buy_price <= 0:
        return jsonify({"ok": False, "error": "invalid params"}), 400
    try:
        with sqlite3.connect(DB_FILE) as conn:
            conn.execute(
                "INSERT INTO portfolio (user_id, skin_name, purchase_price) VALUES (?,?,?)",
                (user_id, name, buy_price)
            )
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/watchlist")
def route_watchlist():
    user_id = request.args.get("user_id", 0, type=int)
    if not user_id:
        return jsonify({"items": []})
    try:
        with sqlite3.connect(DB_FILE) as conn:
            rows = conn.execute(
                "SELECT id, skin_name, price_below, price_above FROM watches WHERE user_id=?",
                (user_id,)
            ).fetchall()
        items = []
        for r in rows:
            if r[2] is not None:
                condition, threshold = "below", r[2]
            else:
                condition, threshold = "above", r[3] or 0
            items.append({"id": r[0], "name": r[1], "condition": condition, "threshold": threshold, "image": fetch_skin_image(r[1])})
        return jsonify({"items": items})
    except Exception as e:
        return jsonify({"items": []})


@app.route("/api/watchlist/add")
def route_watchlist_add():
    user_id   = request.args.get("user_id", 0, type=int)
    name      = request.args.get("name", "").strip()
    condition = request.args.get("condition", "below")
    threshold = request.args.get("threshold", 0, type=float)
    if not user_id or not name:
        return jsonify({"ok": False}), 400
    price_below = threshold if condition == "below" else None
    price_above = threshold if condition == "above" else None
    try:
        with sqlite3.connect(DB_FILE) as conn:
            conn.execute(
                "INSERT INTO watches (user_id, skin_name, price_below, price_above) VALUES (?,?,?,?)",
                (user_id, name, price_below, price_above)
            )
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/watchlist/remove")
def route_watchlist_remove():
    user_id = request.args.get("user_id", 0, type=int)
    item_id = request.args.get("id", 0, type=int)
    if not user_id or not item_id:
        return jsonify({"ok": False}), 400
    try:
        with sqlite3.connect(DB_FILE) as conn:
            conn.execute("DELETE FROM watches WHERE id=? AND user_id=?", (item_id, user_id))
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False}), 500


@app.route("/api/top")
def route_top():
    top_type = request.args.get("type", "expensive")
    currency = request.args.get("currency", "RUB").upper()
    names = TOP_EXPENSIVE if top_type == "expensive" else TOP_POPULAR
    items = []
    for name in names[:10]:
        price_data = get_price_steam(name, currency)
        # Картинки: Steam Market icon URL
        image_name = name.replace(" ", "%20")
        items.append({
            "name":         name,
            "lowest_price": price_data["lowest_price"] if price_data else 0,
            "median_price": price_data["median_price"] if price_data else 0,
            "volume":       price_data["volume"] if price_data else "—",
            "image":        "",  # заполнится при поиске
        })
    return jsonify({"items": items})


@app.route("/api/home")
def route_home():
    """Данные для главной страницы: trending, expensive, popular."""
    currency = request.args.get("currency", "RUB").upper()
    now = time.time()

    # Возвращаем из кэша если свежий (30 мин)
    if currency in _home_cache:
        data, ts = _home_cache[currency]
        if now - ts < HOME_CACHE_TTL:
            return jsonify(data)

    # Строим данные (может занять 10-20 сек при холодном старте)
    data = _build_home_data(currency)
    # Кэшируем только если получили хотя бы 3 скина — иначе повторим при следующем запросе
    if len(data.get("expensive", [])) >= 3:
        _home_cache[currency] = (data, now)
    return jsonify(data)


@app.route("/api/user/status")
def route_user_status():
    """Статус пользователя: premium, referrals, bonus_views."""
    user_id = request.args.get("user_id", 0, type=int)
    if not user_id:
        return jsonify({"premium": False, "referrals": 0, "bonus_views": 0})
    # Администратор всегда premium
    if ADMIN_ID and user_id == ADMIN_ID:
        return jsonify({"premium": True, "referrals": 0, "bonus_views": 999})
    try:
        with sqlite3.connect(DB_FILE) as conn:
            row = conn.execute(
                "SELECT premium, bonus_compares FROM user_settings WHERE user_id=?",
                (user_id,)
            ).fetchone()
            premium = bool(row[0]) if row else False
            bonus = row[1] if row else 0
            ref_count = conn.execute(
                "SELECT COUNT(*) FROM referrals WHERE referrer_id=?",
                (user_id,)
            ).fetchone()[0]
        return jsonify({
            "premium": premium,
            "referrals": ref_count,
            "bonus_views": bonus * 5,
        })
    except Exception:
        return jsonify({"premium": False, "referrals": 0, "bonus_views": 0})


@app.route("/api/health")
def health():
    return jsonify({"ok": True, "service": "cs2-api"})


if __name__ == "__main__":
    print("CS2 API запущен на порту 8001")
    app.run(host="0.0.0.0", port=8001, debug=False)

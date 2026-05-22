# =============================================================
# CS2 SKIN PRICE CHECKER — твой самый первый рабочий скрипт
# =============================================================
# Что делает: берёт список скинов и показывает их текущие
# цены на Steam Market (минимальную, медианную и объём продаж).
#
# Это "проверка связи" — если этот скрипт у тебя запустится
# и покажет цены, значит всё готово, и можно двигаться дальше
# к настоящему Telegram-боту.
# =============================================================

# requests — библиотека для отправки запросов в интернет.
# Steam Market имеет специальный адрес ("API"), который
# возвращает данные о ценах в виде структурированного текста,
# а не красивой веб-страницы. Мы будем спрашивать у этого API.
import re
import json
import requests
import urllib.parse

# Brotli — алгоритм сжатия который требует Skinport.
# Устанавливается отдельно: pip install brotli
# Если не установлен — Skinport работать не будет (вернёт 406).
try:
    import brotli
    BROTLI_AVAILABLE = True
except ImportError:
    BROTLI_AVAILABLE = False


def parse_price_value(price_str: str) -> float | None:
    """
    Извлекает числовое значение из строки цены Steam.

    Steam возвращает цены в разных форматах в зависимости от валюты:
      Рубли:  "3 300 руб."   → 3300.0   (пробел = разделитель тысяч)
      Доллары: "$44.00"      → 44.0     (точка = десятичный разделитель)
      Доллары: "$1,234.56"   → 1234.56  (запятая = тысячи, точка = дробь)

    Алгоритм:
      1. Убираем всё кроме цифр, точки и запятой
      2. Определяем роль каждого разделителя по контексту
      3. Приводим к стандартному виду с точкой и конвертируем в float
    """
    if not price_str:
        return None

    # Убираем всё кроме цифр, точки и запятой.
    # .strip('.,') в конце убирает точку от "руб." — иначе "2993,73."
    # воспринималась бы как "есть и запятая, и точка" и давала неверный результат.
    cleaned = re.sub(r'[^\d,.]', '', price_str).strip('.,')
    if not cleaned:
        return None

    has_comma = ',' in cleaned
    has_dot   = '.' in cleaned

    if has_comma and has_dot:
        # Присутствуют оба символа — последний из них десятичный разделитель.
        # "$1,234.56" → точка последняя → запятые убираем (тысячи)
        # "1.234,56"  → запятая последняя → точки убираем (тысячи), запятую → точка
        if cleaned.rfind('.') > cleaned.rfind(','):
            cleaned = cleaned.replace(',', '')            # убираем тысячные запятые
        else:
            cleaned = cleaned.replace('.', '').replace(',', '.')  # тысячные точки
    elif has_comma:
        # Только запятая.
        # Если после запятой ровно 3 цифры — это тысячный разделитель ("3,300").
        # Иначе — десятичный ("3,50" → 3.50).
        after_comma = cleaned.split(',')[-1]
        if len(after_comma) == 3:
            cleaned = cleaned.replace(',', '')     # тысячный: "3,300" → "3300"
        else:
            cleaned = cleaned.replace(',', '.')    # десятичный: "3,50" → "3.50"
    # Если только точка или только цифры — оставляем как есть

    try:
        return float(cleaned)
    except ValueError:
        return None


def get_top_skins(sort_by: str = "popular", count: int = 5, currency: int = 5, country: str = "RU") -> list:
    """
    Получает топ скинов CS2 с Steam Market.

    sort_by:  "popular" — по популярности, "price" — по цене
    count:    сколько скинов вернуть
    currency: код валюты Steam (5 = рубли, 1 = доллары)
    country:  код страны для корректных региональных цен ("RU", "US" и т.д.)
              Без него Steam может вернуть цены в евро вместо рублей.
    """
    url = (
        f"https://steamcommunity.com/market/search/render/"
        f"?appid=730&sort_column={sort_by}&sort_dir=desc"
        f"&count={count}&norender=1&currency={currency}&country={country}"
    )
    try:
        response = requests.get(url, timeout=10)
        data = response.json()
        if data.get("success") and data.get("results"):
            # Обрезаем до нужного количества — API иногда возвращает больше
            return data["results"][:count]
    except Exception:
        pass
    return []


def resolve_skin_name(query: str) -> str | None:
    """
    Ищет скин по запросу через Steam Market и возвращает
    точное английское название (market_hash_name).

    Работает с русскими названиями — Steam понимает их в поиске.
    Например: "красная линия" → "AK-47 | Redline (Field-Tested)"

    Возвращает None если ничего не найдено.
    """
    encoded = urllib.parse.quote(query)
    url = (
        f"https://steamcommunity.com/market/search/render/"
        f"?appid=730&query={encoded}&count=5&norender=1"
    )

    try:
        response = requests.get(url, timeout=10)
        data = response.json()

        if data.get("success") and data.get("results"):
            # Берём название первого найденного скина.
            # Это и есть точное английское название для API цен.
            return data["results"][0]["name"]
    except Exception:
        pass

    return None


def get_skin_image_url(skin_name: str) -> str | None:
    """
    Ищет изображение скина через поиск Steam Market.

    Возвращает прямую ссылку на картинку скина (330x192 пикселей),
    или None если изображение не найдено.
    """

    # Кодируем название для URL — пробелы и спецсимволы нужно превратить
    # в специальный формат, который понимает браузер и серверы Steam.
    encoded_name = urllib.parse.quote(skin_name)

    # Адрес поиска Steam Market — возвращает список найденных скинов в JSON.
    # norender=1 — просим сырые данные, без HTML-страницы
    # count=1 — нам достаточно первого результата
    url = (
        f"https://steamcommunity.com/market/search/render/"
        f"?appid=730&query={encoded_name}&count=1&norender=1"
    )

    try:
        response = requests.get(url, timeout=10)
        data = response.json()

        # Проверяем, что поиск вернул хотя бы один результат.
        if data.get("success") and data.get("results"):
            # Берём icon_url из первого найденного скина.
            # Это короткий хэш-путь к картинке на серверах Steam.
            icon_url = data["results"][0]["asset_description"]["icon_url"]

            # Собираем полный URL картинки.
            # /330x192 в конце — это размер изображения (ширина x высота).
            return (
                f"https://community.cloudflare.steamstatic.com"
                f"/economy/image/{icon_url}/330x192"
            )
    except Exception:
        # Если что-то пошло не так — просто возвращаем None.
        # Бот продолжит работу, только без картинки.
        pass

    return None


def get_skin_price(skin_name: str, currency: int = 5) -> dict:
    """
    Получает информацию о цене скина с Steam Market.

    skin_name — полное название скина, например: "AK-47 | Redline (Field-Tested)"
    currency  — код валюты Steam (5 = рубли, 1 = доллары США)
    """
    encoded_name = urllib.parse.quote(skin_name)

    # country=RU нужен для корректных региональных цен
    url = (
        "https://steamcommunity.com/market/priceoverview/"
        f"?country=RU&currency={currency}&appid=730"
        f"&market_hash_name={encoded_name}"
    )

    # Делаем GET-запрос (просим у сайта данные).
    # timeout=10 — если сайт молчит больше 10 секунд, прекращаем ждать.
    try:
        response = requests.get(url, timeout=10)
    except requests.exceptions.RequestException as error:
        return {"success": False, "error": f"Не удалось связаться со Steam: {error}"}

    # Проверяем, что сайт ответил успешно (HTTP-код 200 = "всё ок").
    if response.status_code != 200:
        return {
            "success": False,
            "error": f"Steam ответил с кодом {response.status_code}",
        }

    # Steam возвращает данные в формате JSON — превращаем в словарь Python.
    data = response.json()

    # Если success=False, значит Steam не нашёл такой скин
    # (опечатка в названии, неправильный формат и т.п.).
    if not data.get("success"):
        return {
            "success": False,
            "error": "Steam не нашёл скин. Проверь точность названия.",
        }

    lowest  = data.get("lowest_price")
    median  = data.get("median_price")
    volume  = data.get("volume", "0")

    # Если priceoverview вернул пустые цены — пробуем запасной вариант:
    # поиск Steam Market. Он почти всегда имеет sell_price_text даже для
    # редких скинов с низким объёмом торгов.
    if not lowest and not median:
        try:
            search_url = (
                "https://steamcommunity.com/market/search/render/"
                f"?appid=730&query={encoded_name}&count=1&norender=1"
                f"&currency={currency}&country=RU"
            )
            sr = requests.get(search_url, timeout=10)
            sdata = sr.json()
            if sdata.get("success") and sdata.get("results"):
                item = sdata["results"][0]
                fallback_price = item.get("sell_price_text")
                if fallback_price:
                    lowest = fallback_price
                    # Медианная цена недоступна через поиск — показываем прочерк
                    median = "нет данных"
                    volume = str(item.get("sell_listings", 0))
        except Exception:
            pass

    return {
        "success": True,
        "lowest_price": lowest or "нет данных",
        "median_price": median or "нет данных",
        "volume": volume,
    }


# =============================================================
# ГЛАВНАЯ ЧАСТЬ — что выполняется при запуске скрипта
# =============================================================

# =============================================================
# СРАВНЕНИЕ ЦЕН НА РАЗНЫХ ПЛОЩАДКАХ
# =============================================================

# Сколько секунд ждём ответа от сторонних площадок.
# Steam обычно быстрее, DMarket и Skinport чуть медленнее.
THIRD_PARTY_TIMEOUT = 12

# Сколько бесплатных сравнений в неделю даём пользователю.
FREE_COMPARES_PER_WEEK = 5


def get_dmarket_price(skin_name: str) -> dict:
    """
    Получает минимальную цену скина на DMarket.

    DMarket — крупная международная площадка CS2 скинов.
    Цены обычно на 15-30% ниже чем на Steam Market.

    Как работает:
      Обращаемся к публичному API DMarket, запрашиваем список
      лотов по точному названию скина, сортируем по цене (cheapest first).

    Возвращает словарь:
      success   — True если нашли цену, False если ошибка
      price_usd — цена в долларах (float), например 12.34
      price_str — строка для показа, например "$12.34"
      listings  — сколько лотов доступно
      url       — ссылка на страницу скина на DMarket
      error     — текст ошибки (только если success=False)
    """
    encoded = urllib.parse.quote(skin_name)
    url = (
        "https://api.dmarket.com/exchange/v1/market/items"
        f"?title={encoded}&gameId=a8db&currency=USD&limit=1&orderBy=price&orderDir=asc"
    )
    try:
        response = requests.get(
            url, timeout=THIRD_PARTY_TIMEOUT,
            headers={"User-Agent": "Mozilla/5.0"}
        )
        data = response.json()

        objects = data.get("objects", [])
        if not objects:
            return {"success": False, "error": "Нет лотов на DMarket"}

        item = objects[0]
        # Цена приходит в центах как строка — "1234" означает $12.34
        price_cents_str = item.get("price", {}).get("USD", "0")
        price_usd = int(price_cents_str) / 100

        total_listings = int(data.get("total", {}).get("offers", 0))

        return {
            "success": True,
            "price_usd": price_usd,
            "price_str": f"${price_usd:.2f}",
            "listings": total_listings,
            "url": (
                f"https://dmarket.com/ingame-items/item-list/csgo-skins"
                f"?title={encoded}&sort=Price_asc"
            ),
        }
    except Exception as e:
        return {"success": False, "error": f"DMarket: {e}"}


def get_skinport_price(skin_name: str, _debug: bool = False) -> dict:
    """
    Получает минимальную цену скина на Skinport.

    Skinport — европейская площадка CS2 скинов.
    Цены обычно на 10-25% ниже чем на Steam Market.

    Пробуем три варианта запроса:
      1. С фильтром market_hash_name
      2. Со всем каталогом + поиск по имени внутри
      3. Если всё не работает — возвращаем ошибку с диагнозом
    """
    encoded = urllib.parse.quote(skin_name)

    if not BROTLI_AVAILABLE:
        return {
            "success": False,
            "error": "Установи пакет: pip install brotli",
        }

    # Skinport ТРЕБУЕТ заголовок Accept-Encoding: br (Brotli).
    # Без него возвращает HTTP 406. Мы вручную декодируем ответ пакетом brotli.
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "application/json",
        "Accept-Encoding": "br",
    }

    def _decode_response(r) -> list | dict | None:
        """
        Декодирует ответ Skinport.
        Когда пакет brotli установлен, библиотека requests декодирует
        Brotli-сжатие автоматически — r.json() возвращает готовый результат.
        Ручное brotli.decompress() здесь не нужно и вызывает ошибку.
        """
        return r.json()

    def _parse_skinport_item(item: dict) -> dict | None:
        """Извлекает нужные поля из одного элемента ответа Skinport."""
        min_price = item.get("min_price")
        if min_price is None:
            return None
        return {
            "success": True,
            "price_usd": float(min_price),
            "price_str": f"${float(min_price):.2f}",
            "listings": item.get("quantity", 0),
            "url": f"https://skinport.com/market?search={encoded}",
        }

    last_error = "Skinport не ответил"

    # Запрос с фильтром по конкретному скину
    url = (
        f"https://api.skinport.com/v1/items"
        f"?app_id=730&currency=USD&market_hash_name={encoded}"
    )
    try:
        r = requests.get(url, timeout=THIRD_PARTY_TIMEOUT, headers=headers)
        if _debug:
            print(f"[Skinport] status={r.status_code} encoding={r.headers.get('Content-Encoding')} len={len(r.content)}")
        if r.status_code == 200:
            data = _decode_response(r)
            if isinstance(data, list) and data:
                result = _parse_skinport_item(data[0])
                if result:
                    return result
                if _debug:
                    print(f"[Skinport] item data: {data[0]}")
        elif r.status_code == 403:
            # 403 — Skinport блокирует запросы с серверных IP (дата-центры).
            # Это ограничение на стороне Skinport, обойти без прокси нельзя.
            # Возвращаем тихую ошибку — пользователю не показываем технические детали.
            return {"success": False, "error": "unavailable"}
        else:
            last_error = f"Skinport ответил кодом {r.status_code}"
    except Exception as e:
        last_error = str(e)

    # Запасной вариант — весь каталог (медленнее, ~2-5 сек)
    url_all = "https://api.skinport.com/v1/items?app_id=730&currency=USD"
    try:
        r = requests.get(url_all, timeout=40, headers=headers)
        if _debug:
            print(f"[Skinport all] status={r.status_code} len={len(r.content)}")
        if r.status_code == 200:
            data = _decode_response(r)
            if isinstance(data, list):
                skin_lower = skin_name.lower()
                for item in data:
                    if item.get("market_hash_name", "").lower() == skin_lower:
                        result = _parse_skinport_item(item)
                        if result:
                            return result
                return {"success": False, "error": "Скин не найден в каталоге Skinport"}
        elif r.status_code == 403:
            return {"success": False, "error": "unavailable"}
    except Exception as e:
        last_error = f"Skinport каталог: {e}"

    return {"success": False, "error": last_error}


def get_price_comparison(skin_name: str, steam_price_usd: float = None) -> dict:
    """
    Собирает цены с нескольких площадок и возвращает готовое сравнение.

    steam_price_usd — цена Steam в долларах. Если передать, Steam будет
    включён в список для сравнения экономии. Если None — только сторонние.

    Возвращает словарь:
      platforms — список площадок, отсортированный от дешёвой к дорогой.
          Каждая площадка: {name, price_usd, price_str, listings, url, savings_pct}
          savings_pct — на сколько процентов дешевле Steam (0 если нет данных Steam)
      cheapest  — название самой дешёвой площадки (или None)
      errors    — список площадок которые не ответили
    """
    results = []
    errors = []

    # Добавляем Steam если передана цена
    if steam_price_usd is not None:
        encoded = urllib.parse.quote(skin_name)
        results.append({
            "name": "Steam",
            "price_usd": steam_price_usd,
            "price_str": f"${steam_price_usd:.2f}",
            "listings": None,
            "url": f"https://steamcommunity.com/market/listings/730/{encoded}",
            "savings_pct": 0,
        })

    # Запрашиваем DMarket
    dmarket = get_dmarket_price(skin_name)
    if dmarket["success"]:
        savings = 0
        if steam_price_usd and steam_price_usd > 0:
            savings = round((1 - dmarket["price_usd"] / steam_price_usd) * 100)
        results.append({
            "name": "DMarket",
            "price_usd": dmarket["price_usd"],
            "price_str": dmarket["price_str"],
            "listings": dmarket["listings"],
            "url": dmarket["url"],
            "savings_pct": savings,
        })
    else:
        errors.append(f"DMarket: {dmarket['error']}")

    # Запрашиваем Skinport
    skinport = get_skinport_price(skin_name)
    if skinport["success"]:
        savings = 0
        if steam_price_usd and steam_price_usd > 0:
            savings = round((1 - skinport["price_usd"] / steam_price_usd) * 100)
        results.append({
            "name": "Skinport",
            "price_usd": skinport["price_usd"],
            "price_str": skinport["price_str"],
            "listings": skinport["listings"],
            "url": skinport["url"],
            "savings_pct": savings,
        })
    else:
        errors.append(f"Skinport: {skinport['error']}")

    # Сортируем по цене — самая дешёвая первой
    results.sort(key=lambda x: x["price_usd"])
    cheapest = results[0]["name"] if results else None

    return {
        "platforms": results,
        "cheapest": cheapest,
        "errors": errors,
    }


if __name__ == "__main__":
    # Список скинов для проверки.
    # Можешь добавить свои — название копируй ТОЧНО как оно
    # написано на странице скина в Steam Market.
    skins_to_check = [
        "AK-47 | Redline (Field-Tested)",
        "AWP | Asiimov (Field-Tested)",
        "M4A1-S | Hyper Beast (Minimal Wear)",
        "Glock-18 | Fade (Factory New)",
    ]

    print("=" * 55)
    print("  ПРОВЕРКА ЦЕН CS2 СКИНОВ НА STEAM MARKET")
    print("=" * 55)

    for skin in skins_to_check:
        print(f"\n  {skin}")
        result = get_skin_price(skin)

        if result["success"]:
            print(f"    Минимальная цена: {result['lowest_price']}")
            print(f"    Медианная цена:   {result['median_price']}")
            print(f"    Продано за сутки: {result['volume']} шт.")
        else:
            print(f"    [ошибка] {result['error']}")

    print("\n" + "=" * 55)
    print("  Готово! Если ты видишь цены выше — всё работает.")
    print("=" * 55)

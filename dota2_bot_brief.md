# Техническое задание: Dota 2 Item Price Tracker Bot

## Контекст
Уже есть рабочий CS2 бот (@cs2skinprice_bot) на Python + python-telegram-bot v22.7 + SQLite.
Нужно сделать аналогичный бот для отслеживания цен на предметы Dota 2 в Steam Market.
Архитектура, логика и структура — полностью копируются с CS2 бота, меняются только API запросы и специфика предметов.

---

## Стек технологий
- Python 3.11
- python-telegram-bot 22.7
- SQLite (через модуль sqlite3)
- requests (HTTP запросы к API)
- APScheduler (через job_queue в PTB для фоновых задач)
- Сервер: Oracle Cloud Always Free (Ubuntu 22.04), IP: 138.2.228.123
- Systemd сервис для автозапуска
- GitHub для деплоя

## Подключение к серверу
```
ssh -i C:\Users\mik-p\Documents\ssh-key.key ubuntu@138.2.228.123
```

---

## Структура файлов

```
dota2-bot/
├── bot.py          # основной файл, handlers, логика
├── database.py     # все функции работы с SQLite
├── skin_checker.py # фоновая проверка цен для вотчлиста
├── config.py       # токен бота и ADMIN_ID (не в GitHub!)
├── requirements.txt
└── dota2bot.service  # systemd unit
```

### config.py (создаётся вручную на сервере, не в GitHub)
```python
BOT_TOKEN = "токен от BotFather"
ADMIN_ID = 123456789  # твой Telegram user_id
```

---

## API для получения цен

### 1. Steam Market — основной источник цен

**Цена конкретного предмета:**
```
GET https://steamcommunity.com/market/priceoverview/
    ?appid=570
    &currency=5
    &market_hash_name=НАЗВАНИЕ_ПРЕДМЕТА
```
- appid=570 — это Dota 2 (CS2 был 730)
- currency=5 — рубли (1=USD, 3=EUR, 5=RUB, 18=UAH)
- market_hash_name — точное название предмета из Steam Market

**Ответ:**
```json
{
  "success": true,
  "lowest_price": "150,00 руб.",
  "median_price": "160,00 руб.",
  "volume": "42"
}
```

**Поиск предмета по названию:**
```
GET https://steamcommunity.com/market/search/render/
    ?appid=570
    &query=ПОИСКОВЫЙ_ЗАПРОС
    &count=5
    &search_descriptions=0
    &norender=1
    &currency=5
```

**ВАЖНО про сервер:** IP Oracle Cloud (US датацентр) — Steam игнорирует параметр currency в search/render и возвращает цены в USD центах. Поэтому:
- Для отображения цен всегда используй priceoverview (он надёжен)
- Из результатов поиска бери только названия предметов, не цены

---

## Валюты (те же что в CS2 боте)

```python
CURRENCIES = {
    "RUB": {"code": 5,  "symbol": "руб.", "name": "Рубли"},
    "USD": {"code": 1,  "symbol": "$",    "name": "Доллары"},
    "EUR": {"code": 3,  "symbol": "€",    "name": "Евро"},
    "UAH": {"code": 18, "symbol": "₴",    "name": "Гривны"},
    "KZT": {"code": 37, "symbol": "₸",    "name": "Тенге"},
}
```

---

## База данных (database.py)

Таблицы — аналогично CS2 боту:

```sql
-- Настройки пользователя
CREATE TABLE IF NOT EXISTS user_settings (
    user_id INTEGER PRIMARY KEY,
    currency TEXT DEFAULT 'RUB',
    premium INTEGER DEFAULT 0,
    compare_count INTEGER DEFAULT 0,
    week_start TEXT DEFAULT '',
    bonus_compares INTEGER DEFAULT 0
);

-- Список отслеживания
CREATE TABLE IF NOT EXISTS watchlist (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    item_name TEXT,
    condition TEXT,
    threshold REAL
);

-- История активности
CREATE TABLE IF NOT EXISTS user_activity (
    user_id INTEGER PRIMARY KEY,
    last_seen TEXT
);

-- История цен
CREATE TABLE IF NOT EXISTS price_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    item_name TEXT,
    price REAL,
    recorded_at TEXT DEFAULT (datetime('now'))
);

-- Реферальная программа
CREATE TABLE IF NOT EXISTS referrals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    referrer_id INTEGER,
    referred_id INTEGER UNIQUE NOT NULL,
    created_at TEXT DEFAULT (datetime('now'))
);

-- Портфель
CREATE TABLE IF NOT EXISTS portfolio (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    item_name TEXT,
    buy_price REAL,
    added_at TEXT DEFAULT (datetime('now'))
);

-- Статистика событий
CREATE TABLE IF NOT EXISTS daily_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type TEXT,
    created_at TEXT DEFAULT (datetime('now'))
);
```

---

## Парсинг цены (skin_checker.py)

Критически важная функция — Steam возвращает цены в формате "2 993,73 руб." или "$12.34":

```python
import re

def parse_price_value(price_str: str) -> float:
    """
    Парсит цену из строки Steam Market.
    Примеры: "2 993,73 руб." -> 2993.73
             "$12.34" -> 12.34
             "1.234,56 руб." -> 1234.56
    """
    if not price_str:
        return 0.0
    # Оставляем только цифры, запятые и точки
    cleaned = re.sub(r'[^\d,.]', '', price_str).strip('.,')
    if not cleaned:
        return 0.0
    # Определяем формат: если есть и запятая и точка
    if ',' in cleaned and '.' in cleaned:
        # Последний разделитель — десятичный
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
```

**ВАЖНО:** `.strip('.,')` в конце первого re.sub обязателен — Steam пишет "руб." и точка от сокращения попадает в regex как десятичный разделитель, давая 100x ошибку в ценах.

---

## Основная функция получения цены

```python
def get_item_price(item_name: str, currency: str = "RUB") -> dict | None:
    """
    Возвращает словарь с ценами или None если предмет не найден.
    """
    currency_code = CURRENCIES.get(currency, {}).get("code", 5)
    symbol = CURRENCIES.get(currency, {}).get("symbol", "руб.")
    
    url = "https://steamcommunity.com/market/priceoverview/"
    params = {
        "appid": 570,  # Dota 2
        "currency": currency_code,
        "market_hash_name": item_name,
    }
    
    try:
        resp = requests.get(url, params=params, timeout=10)
        data = resp.json()
        
        if not data.get("success"):
            return None
        
        lowest = parse_price_value(data.get("lowest_price", ""))
        median = parse_price_value(data.get("median_price", ""))
        volume = data.get("volume", "0").replace(",", "")
        
        return {
            "lowest": lowest,
            "median": median,
            "volume": int(volume) if volume.isdigit() else 0,
            "symbol": symbol,
            "currency": currency,
            "item_name": item_name,
        }
    except Exception:
        return None
```

---

## Особенности предметов Dota 2

В отличие от CS2 скинов, у Dota 2 есть нюансы:

**Категории предметов:**
- Обычные предметы (Common, Uncommon, Rare)
- Immortal — самые популярные для торговли
- Arcana — самые дорогие
- Сеты предметов (Sets)
- Курьеры (Couriers)
- Варды (Wards)

**Важно для поиска:**
- Некоторые предметы имеют суффикс `(Unusual)` — они значительно дороже
- Предметы могут быть `Inscribed` (с счётчиком убийств) — другая цена
- Точное название нужно копировать из Steam Market

**Хорошие примеры для тестирования бота:**
```
Dragonclaw Hook
Genuine Dragonclaw Hook
Dragonclaw Hook (Unusual)
Collector's Imperial Flame Pack
Tempest Helm of the Thundergod
```

---

## Лимиты и монетизация (те же что в CS2 боте)

```python
FREE_COMPARES_PER_WEEK = 5  # бесплатных сравнений в неделю
```

- Просмотр цены — бесплатно и без лимита
- Добавить в вотчлист — бесплатно
- Сравнение Steam vs DMarket — лимит 5 в неделю
- Реферальная программа: +3 сравнения за каждого приглашённого

---

## DMarket API (опционально)

DMarket тоже поддерживает Dota 2:
```
GET https://api.dmarket.com/exchange/v1/market/items
    ?gameId=dota2
    &title=НАЗВАНИЕ_ПРЕДМЕТА
    &currency=USD
    &limit=1
```
Внимание: DMarket блокирует датацентровые IP (HTTP 403) — показывать ошибку пользователю тихо.

---

## Клавиатура бота (ReplyKeyboardMarkup)

```
[ 🔍 Проверить цену ]
[ ⭐ Мой вотчлист  ] [ 📊 Портфель    ]
[ 🔀 Сравнить цены ] [ 🏆 Топ Dota 2  ]
[ ⚙️ Настройки     ] [ 👥 Пригласить  ]
```

---

## Команды для BotFather

```
start - Главное меню
price - Узнать цену предмета
watch - Добавить в отслеживание
list - Мой вотчлист
portfolio - Мой портфель предметов
settings - Настройки (валюта)
share - Пригласить друга
```

**Описание бота (до 120 символов):**
```
Бот для трейдеров Dota 2 — отслеживает цены предметов на Steam Market и уведомляет при изменении
```

---

## Деплой на сервер

### Первый раз:
```bash
# На сервере
cd ~
git clone https://github.com/ТВОЙ_РЕПО/dota2-bot.git
cd dota2-bot
pip install -r requirements.txt

# Создать config.py вручную
nano config.py

# Создать systemd сервис
sudo nano /etc/systemd/system/dota2bot.service
sudo systemctl enable dota2bot
sudo systemctl start dota2bot
```

### systemd unit файл:
```ini
[Unit]
Description=Dota 2 Price Bot
After=network.target

[Service]
User=ubuntu
WorkingDirectory=/home/ubuntu/dota2-bot
ExecStart=/usr/bin/python3 /home/ubuntu/dota2-bot/bot.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

### Обновление после изменений:
```bash
# Локально (PowerShell)
cd Z:\dota2-bot
git add .
git commit -m "описание изменений"
git push

# На сервере
cd ~/dota2-bot && git pull && sudo systemctl restart dota2bot

# Проверить логи
sudo journalctl -u dota2bot -n 50 -f
```

---

## Что реализовать в первую очередь (MVP)

1. `/start` — приветствие с кнопками
2. Проверка цены — ввод названия предмета → ответ с ценой
3. Добавить в вотчлист — задать порог, фоновая проверка каждые 30 минут
4. `/list` — список отслеживаемых предметов
5. Смена валюты в настройках

Всё остальное (портфель, история цен, рефералы, сравнение с DMarket) — добавить после MVP.

---

## Ссылки
- Steam Market Dota 2: https://steamcommunity.com/market/search?appid=570
- appid Dota 2 в Steam: 570
- DMarket Dota 2: https://dmarket.com/ingame-items/item-list/dota2-skins

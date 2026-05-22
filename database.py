# =============================================================
# DATABASE.PY — работа с базой данных SQLite
# =============================================================
# SQLite — это простая база данных, которая хранится в одном файле
# на диске (у нас это будет файл bot_data.db).
# Никакого отдельного сервера не нужно — Python работает с ней напрямую.
# =============================================================

import sqlite3
from datetime import datetime, timedelta

DB_FILE = "bot_data.db"

# Сколько бесплатных сравнений площадок даём в неделю
FREE_COMPARES_PER_WEEK = 5


def get_week_start() -> str:
    """
    Возвращает дату начала текущей недели (понедельник) в формате YYYY-MM-DD.
    Используется как ключ для подсчёта недельных лимитов.
    """
    today = datetime.now().date()
    monday = today - timedelta(days=today.weekday())
    return monday.isoformat()


def init_db():
    """
    Создаёт таблицы если они ещё не существуют,
    затем выполняет миграцию — добавляет новые колонки если их нет.
    """
    with sqlite3.connect(DB_FILE) as conn:
        # Таблица отслеживаемых скинов
        conn.execute("""
            CREATE TABLE IF NOT EXISTS watches (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id          INTEGER NOT NULL,
                skin_name        TEXT    NOT NULL,
                price_below      REAL,
                price_above      REAL,
                created_at       TEXT DEFAULT (datetime('now')),
                last_notified_at TEXT
            )
        """)
        # Таблица настроек пользователя.
        # Здесь хранится выбранная валюта и другие настройки.
        # currency: 5 = рубли (по умолчанию), 1 = доллары США
        conn.execute("""
            CREATE TABLE IF NOT EXISTS user_settings (
                user_id  INTEGER PRIMARY KEY,
                currency INTEGER DEFAULT 5
            )
        """)
        # Таблица счётчика сравнений площадок.
        # Каждая строка — один пользователь за одну неделю.
        # PRIMARY KEY (user_id, week_start) — не может быть двух строк
        # для одного пользователя за одну неделю.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS compare_usage (
                user_id    INTEGER NOT NULL,
                week_start TEXT    NOT NULL,
                count      INTEGER DEFAULT 0,
                PRIMARY KEY (user_id, week_start)
            )
        """)
        # Таблица активности пользователей.
        # first_seen — когда пользователь впервые запустил бота.
        # last_seen  — когда последний раз что-то делал (обновляется при каждом действии).
        conn.execute("""
            CREATE TABLE IF NOT EXISTS user_activity (
                user_id    INTEGER PRIMARY KEY,
                first_seen TEXT DEFAULT (datetime('now')),
                last_seen  TEXT DEFAULT (datetime('now'))
            )
        """)
        # Таблица событий для ежедневной статистики.
        # Каждое важное действие пользователя записывается сюда.
        # event_type: price_check, watch_added, compare_done, limit_hit, new_user
        conn.execute("""
            CREATE TABLE IF NOT EXISTS daily_events (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                event_type  TEXT NOT NULL,
                recorded_at TEXT DEFAULT (datetime('now'))
            )
        """)
        # Таблица истории цен.
        # Бот записывает цену раз в час (во время check_prices) и когда пользователь
        # открывает "История цен". Это позволяет показать мин/макс/среднюю за 30 дней.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS price_history (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                skin_name   TEXT NOT NULL,
                price       REAL NOT NULL,
                recorded_at TEXT DEFAULT (datetime('now'))
            )
        """)
        # Таблица рефералов.
        # referrer_id — кто пригласил, referred_id — кто пришёл по ссылке.
        # referred_id UNIQUE — один пользователь не может быть приглашён дважды.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS referrals (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                referrer_id INTEGER NOT NULL,
                referred_id INTEGER UNIQUE NOT NULL,
                created_at  TEXT DEFAULT (datetime('now'))
            )
        """)
        # Таблица портфеля скинов.
        # Пользователь вносит скины с ценой покупки.
        # Бот показывает текущую цену и прибыль/убыток.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS portfolio (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id        INTEGER NOT NULL,
                skin_name      TEXT NOT NULL,
                purchase_price REAL NOT NULL,
                added_at       TEXT DEFAULT (datetime('now'))
            )
        """)
        conn.commit()

    # Миграция — безопасно добавляем колонки если их нет
    with sqlite3.connect(DB_FILE) as conn:
        for sql in [
            "ALTER TABLE watches ADD COLUMN last_notified_at TEXT",
            # is_premium: 0 = обычный пользователь, 1 = Premium (безлимитные сравнения)
            "ALTER TABLE user_settings ADD COLUMN is_premium INTEGER DEFAULT 0",
            # percent_drop / percent_rise — отслеживание по % изменению цены.
            # Например: percent_drop=10 → уведомить когда цена упадёт на 10% от текущей.
            # base_price — цена скина в момент добавления отслеживания (для расчёта %).
            "ALTER TABLE watches ADD COLUMN percent_drop REAL",
            "ALTER TABLE watches ADD COLUMN percent_rise REAL",
            "ALTER TABLE watches ADD COLUMN base_price REAL",
            # bonus_compares — бонусные сравнения за приглашённых друзей.
            # Каждый приглашённый друг даёт +3 к еженедельному лимиту навсегда.
            "ALTER TABLE user_settings ADD COLUMN bonus_compares INTEGER DEFAULT 0",
        ]:
            try:
                conn.execute(sql)
                conn.commit()
            except sqlite3.OperationalError:
                pass  # Колонка уже существует — это нормально


def add_watch(user_id: int, skin_name: str, price_below: float = None, price_above: float = None):
    """
    Добавляет скин в список отслеживания для пользователя.
    """
    with sqlite3.connect(DB_FILE) as conn:
        conn.execute(
            "INSERT INTO watches (user_id, skin_name, price_below, price_above) VALUES (?, ?, ?, ?)",
            (user_id, skin_name, price_below, price_above)
        )
        conn.commit()


def upsert_watch(user_id: int, skin_name: str, price_below: float = None, price_above: float = None) -> str:
    """
    Умное добавление скина — без дублей.

    Логика:
    - Если у пользователя уже есть этот скин с тем же направлением
      (price_below или price_above) — обновляем порог, не создаём новую запись.
    - Если такой записи нет — создаём новую.

    Возвращает строку "updated" или "created" — чтобы бот мог написать нужный ответ.

    Направление определяется тем, какой параметр передан:
      price_below=3000 → отслеживание "уведомить если цена упадёт ниже 3000"
      price_above=5000 → отслеживание "уведомить если цена вырастет выше 5000"
    """
    with sqlite3.connect(DB_FILE) as conn:
        if price_below is not None:
            # Ищем существующую запись "ниже порога" для этого скина
            cursor = conn.execute(
                "SELECT id FROM watches WHERE user_id = ? AND skin_name = ? AND price_below IS NOT NULL",
                (user_id, skin_name)
            )
            row = cursor.fetchone()
            if row:
                # Запись найдена — обновляем порог
                conn.execute(
                    "UPDATE watches SET price_below = ?, last_notified_at = NULL WHERE id = ?",
                    (price_below, row[0])
                )
                conn.commit()
                return "updated"
        elif price_above is not None:
            # Ищем существующую запись "выше порога" для этого скина
            cursor = conn.execute(
                "SELECT id FROM watches WHERE user_id = ? AND skin_name = ? AND price_above IS NOT NULL",
                (user_id, skin_name)
            )
            row = cursor.fetchone()
            if row:
                # Запись найдена — обновляем порог
                conn.execute(
                    "UPDATE watches SET price_above = ?, last_notified_at = NULL WHERE id = ?",
                    (price_above, row[0])
                )
                conn.commit()
                return "updated"

        # Новая запись — добавляем как обычно
        conn.execute(
            "INSERT INTO watches (user_id, skin_name, price_below, price_above) VALUES (?, ?, ?, ?)",
            (user_id, skin_name, price_below, price_above)
        )
        conn.commit()
        return "created"


def upsert_watch_pct(user_id: int, skin_name: str, base_price: float,
                     percent_drop: float = None, percent_rise: float = None) -> str:
    """
    Добавляет или обновляет отслеживание по процентному изменению цены.

    base_price    — текущая цена скина (зафиксируем как точку отсчёта)
    percent_drop  — уведомить когда цена упадёт на X% от base_price
    percent_rise  — уведомить когда цена вырастет на X% от base_price

    Возвращает "updated" или "created".
    """
    with sqlite3.connect(DB_FILE) as conn:
        if percent_drop is not None:
            cursor = conn.execute(
                "SELECT id FROM watches WHERE user_id=? AND skin_name=? AND percent_drop IS NOT NULL",
                (user_id, skin_name)
            )
            row = cursor.fetchone()
            if row:
                conn.execute(
                    "UPDATE watches SET percent_drop=?, base_price=?, last_notified_at=NULL WHERE id=?",
                    (percent_drop, base_price, row[0])
                )
                conn.commit()
                return "updated"
        elif percent_rise is not None:
            cursor = conn.execute(
                "SELECT id FROM watches WHERE user_id=? AND skin_name=? AND percent_rise IS NOT NULL",
                (user_id, skin_name)
            )
            row = cursor.fetchone()
            if row:
                conn.execute(
                    "UPDATE watches SET percent_rise=?, base_price=?, last_notified_at=NULL WHERE id=?",
                    (percent_rise, base_price, row[0])
                )
                conn.commit()
                return "updated"

        conn.execute(
            "INSERT INTO watches (user_id, skin_name, base_price, percent_drop, percent_rise) VALUES (?,?,?,?,?)",
            (user_id, skin_name, base_price, percent_drop, percent_rise)
        )
        conn.commit()
        return "created"


def get_watches(user_id: int) -> list:
    """
    Возвращает список отслеживаемых скинов конкретного пользователя.
    """
    with sqlite3.connect(DB_FILE) as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.execute(
            "SELECT * FROM watches WHERE user_id = ? ORDER BY created_at DESC",
            (user_id,)
        )
        return [dict(row) for row in cursor.fetchall()]


def get_all_watches() -> list:
    """
    Возвращает ВСЕ записи из базы — для всех пользователей.
    Используется фоновой задачей проверки цен.
    """
    with sqlite3.connect(DB_FILE) as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.execute("SELECT * FROM watches")
        return [dict(row) for row in cursor.fetchall()]


def update_last_notified(watch_id: int):
    """
    Записывает текущее время как момент последнего уведомления.
    Нужно чтобы не спамить одним и тем же уведомлением каждый час.
    """
    with sqlite3.connect(DB_FILE) as conn:
        conn.execute(
            "UPDATE watches SET last_notified_at = datetime('now') WHERE id = ?",
            (watch_id,)
        )
        conn.commit()


def get_user_currency(user_id: int) -> int:
    """
    Возвращает код валюты пользователя.
    Если пользователь не менял настройки — возвращает 5 (рубли).
    """
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.execute(
            "SELECT currency FROM user_settings WHERE user_id = ?",
            (user_id,)
        )
        row = cursor.fetchone()
        # Если записи нет — пользователь ещё не менял валюту, возвращаем рубли
        return row[0] if row else 5


def set_user_currency(user_id: int, currency: int):
    """
    Сохраняет выбор валюты пользователя.
    INSERT OR REPLACE — создаёт запись если нет, обновляет если есть.
    """
    with sqlite3.connect(DB_FILE) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO user_settings (user_id, currency) VALUES (?, ?)",
            (user_id, currency)
        )
        conn.commit()


def remove_watch(watch_id: int, user_id: int):
    """
    Удаляет запись из отслеживания.
    user_id передаём для безопасности — нельзя удалить чужую запись.
    """
    with sqlite3.connect(DB_FILE) as conn:
        conn.execute(
            "DELETE FROM watches WHERE id = ? AND user_id = ?",
            (watch_id, user_id)
        )
        conn.commit()


def get_compare_count(user_id: int) -> int:
    """
    Возвращает сколько раз пользователь использовал сравнение площадок
    за текущую неделю.
    """
    week = get_week_start()
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.execute(
            "SELECT count FROM compare_usage WHERE user_id = ? AND week_start = ?",
            (user_id, week)
        )
        row = cursor.fetchone()
        return row[0] if row else 0


def increment_compare_count(user_id: int):
    """
    Увеличивает счётчик сравнений на 1.
    Если записи за эту неделю нет — создаёт новую со значением 1.
    """
    week = get_week_start()
    with sqlite3.connect(DB_FILE) as conn:
        conn.execute("""
            INSERT INTO compare_usage (user_id, week_start, count)
            VALUES (?, ?, 1)
            ON CONFLICT(user_id, week_start) DO UPDATE SET count = count + 1
        """, (user_id, week))
        conn.commit()


def is_premium(user_id: int) -> bool:
    """
    Проверяет есть ли у пользователя Premium-доступ.
    Premium даёт безлимитные сравнения площадок.
    """
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.execute(
            "SELECT is_premium FROM user_settings WHERE user_id = ?",
            (user_id,)
        )
        row = cursor.fetchone()
        return bool(row and row[0])


def set_premium(user_id: int, value: bool = True):
    """
    Выдаёт или отбирает Premium у пользователя.
    INSERT OR REPLACE — создаёт запись если нет, обновляет если есть.
    """
    with sqlite3.connect(DB_FILE) as conn:
        conn.execute("""
            INSERT INTO user_settings (user_id, currency, is_premium)
            VALUES (?, 5, ?)
            ON CONFLICT(user_id) DO UPDATE SET is_premium = ?
        """, (user_id, int(value), int(value)))
        conn.commit()


def is_new_user(user_id: int) -> bool:
    """Возвращает True если пользователь ещё не был в боте (нет записи в user_activity)."""
    with sqlite3.connect(DB_FILE) as conn:
        row = conn.execute(
            "SELECT 1 FROM user_activity WHERE user_id = ?", (user_id,)
        ).fetchone()
        return row is None


def record_activity(user_id: int):
    """
    Фиксирует активность пользователя.

    Если пользователь новый — создаёт запись с first_seen = сейчас.
    Если уже есть — только обновляет last_seen.

    Вызывать при каждом осмысленном действии пользователя:
    /start, проверка цены, добавление отслеживания и т.д.
    """
    with sqlite3.connect(DB_FILE) as conn:
        conn.execute("""
            INSERT INTO user_activity (user_id, first_seen, last_seen)
            VALUES (?, datetime('now'), datetime('now'))
            ON CONFLICT(user_id) DO UPDATE SET last_seen = datetime('now')
        """, (user_id,))
        conn.commit()


def get_users_who_hit_limit_last_week() -> list:
    """
    Возвращает список user_id пользователей, которые исчерпали
    бесплатный лимит сравнений на прошлой неделе.
    Используется для отправки уведомления о восстановлении лимита в понедельник.
    """
    today = datetime.now().date()
    monday_this_week = today - timedelta(days=today.weekday())
    monday_last_week = (monday_this_week - timedelta(weeks=1)).isoformat()

    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.execute(
            "SELECT user_id FROM compare_usage WHERE week_start = ? AND count >= ?",
            (monday_last_week, FREE_COMPARES_PER_WEEK)
        )
        return [row[0] for row in cursor.fetchall()]


def get_stats() -> dict:
    """
    Собирает статистику использования бота.

    Возвращает словарь:
      total_users   — сколько уникальных пользователей вообще запускало бота
      active_today  — активны сегодня (last_seen за последние 24 часа)
      active_week   — активны за последние 7 дней
      active_watches — сколько активных отслеживаний в базе
      top_skins     — список (skin_name, count) топ-5 самых отслеживаемых скинов
    """
    with sqlite3.connect(DB_FILE) as conn:
        # Общее число уникальных пользователей
        total_users = conn.execute(
            "SELECT COUNT(*) FROM user_activity"
        ).fetchone()[0]

        # Активных за последние 24 часа
        active_today = conn.execute(
            "SELECT COUNT(*) FROM user_activity WHERE last_seen >= datetime('now', '-1 day')"
        ).fetchone()[0]

        # Активных за последние 7 дней
        active_week = conn.execute(
            "SELECT COUNT(*) FROM user_activity WHERE last_seen >= datetime('now', '-7 days')"
        ).fetchone()[0]

        # Количество активных отслеживаний
        active_watches = conn.execute(
            "SELECT COUNT(*) FROM watches"
        ).fetchone()[0]

        # Топ-5 самых популярных скинов для отслеживания
        top_skins_rows = conn.execute("""
            SELECT skin_name, COUNT(*) as cnt
            FROM watches
            GROUP BY skin_name
            ORDER BY cnt DESC
            LIMIT 5
        """).fetchall()
        top_skins = [(row[0], row[1]) for row in top_skins_rows]

    return {
        "total_users": total_users,
        "active_today": active_today,
        "active_week": active_week,
        "active_watches": active_watches,
        "top_skins": top_skins,
    }


# =============================================================
# ИСТОРИЯ ЦЕН
# =============================================================

def record_price_history(skin_name: str, price: float):
    """
    Записывает текущую цену скина в историю.
    Вызывается раз в час из check_prices и когда пользователь
    открывает кнопку "История цен".
    """
    with sqlite3.connect(DB_FILE) as conn:
        conn.execute(
            "INSERT INTO price_history (skin_name, price) VALUES (?, ?)",
            (skin_name, price)
        )
        conn.commit()


def get_price_history_stats(skin_name: str, days: int = 30) -> dict:
    """
    Возвращает статистику цен за последние N дней:
      min   — минимальная цена
      max   — максимальная цена
      avg   — средняя цена
      count — сколько записей в базе (показываем пользователю)

    Если данных нет — возвращает count=0.
    """
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.execute("""
            SELECT MIN(price), MAX(price), AVG(price), COUNT(*)
            FROM price_history
            WHERE skin_name = ? AND recorded_at >= datetime('now', ?)
        """, (skin_name, f"-{days} days"))
        row = cursor.fetchone()
        if not row or not row[3]:
            return {"min": None, "max": None, "avg": None, "count": 0}
        return {
            "min": row[0],
            "max": row[1],
            "avg": row[2],
            "count": row[3],
        }


# =============================================================
# РЕФЕРАЛЬНАЯ ПРОГРАММА
# =============================================================

def add_referral(referrer_id: int, referred_id: int) -> bool:
    """
    Записывает нового реферала.
    Начисляет рефереру +3 бонусных сравнения за неделю.

    Возвращает True если реферал новый, False если уже был
    (один пользователь не может быть приглашён дважды — UNIQUE).
    Также возвращает False если пользователь пытается пригласить сам себя.
    """
    if referrer_id == referred_id:
        return False
    with sqlite3.connect(DB_FILE) as conn:
        try:
            conn.execute(
                "INSERT INTO referrals (referrer_id, referred_id) VALUES (?, ?)",
                (referrer_id, referred_id)
            )
            conn.commit()
        except sqlite3.IntegrityError:
            # referred_id уже есть — пользователь уже был приглашён
            return False

        # Начисляем +3 бонусных сравнения рефереру
        conn.execute("""
            INSERT INTO user_settings (user_id, currency, bonus_compares)
            VALUES (?, 5, 3)
            ON CONFLICT(user_id) DO UPDATE SET bonus_compares = bonus_compares + 3
        """, (referrer_id,))
        conn.commit()
        return True


def get_referral_stats(user_id: int) -> dict:
    """
    Возвращает статистику рефералов пользователя:
      count         — сколько человек пришло по его ссылке
      bonus_compares — сколько бонусных сравнений накоплено
    """
    with sqlite3.connect(DB_FILE) as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM referrals WHERE referrer_id = ?",
            (user_id,)
        ).fetchone()[0]
        bonus_row = conn.execute(
            "SELECT bonus_compares FROM user_settings WHERE user_id = ?",
            (user_id,)
        ).fetchone()
        bonus = bonus_row[0] if bonus_row else 0
    return {"count": count, "bonus_compares": bonus}


def get_bonus_compares(user_id: int) -> int:
    """
    Возвращает количество бонусных сравнений пользователя.
    Используется при проверке лимита сравнений площадок.
    """
    with sqlite3.connect(DB_FILE) as conn:
        row = conn.execute(
            "SELECT bonus_compares FROM user_settings WHERE user_id = ?",
            (user_id,)
        ).fetchone()
        return row[0] if row else 0


# =============================================================
# ПОРТФЕЛЬ СКИНОВ
# =============================================================

def add_portfolio_item(user_id: int, skin_name: str, purchase_price: float) -> int:
    """
    Добавляет скин в портфель пользователя.
    Возвращает id новой записи.
    """
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.execute(
            "INSERT INTO portfolio (user_id, skin_name, purchase_price) VALUES (?, ?, ?)",
            (user_id, skin_name, purchase_price)
        )
        conn.commit()
        return cursor.lastrowid


def get_portfolio(user_id: int) -> list:
    """
    Возвращает список скинов в портфеле пользователя.
    Каждая запись — словарь с полями: id, skin_name, purchase_price, added_at.
    """
    with sqlite3.connect(DB_FILE) as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.execute(
            "SELECT * FROM portfolio WHERE user_id = ? ORDER BY added_at DESC",
            (user_id,)
        )
        return [dict(row) for row in cursor.fetchall()]


def remove_portfolio_item(item_id: int, user_id: int):
    """
    Удаляет скин из портфеля.
    user_id передаём для безопасности — нельзя удалить чужую запись.
    """
    with sqlite3.connect(DB_FILE) as conn:
        conn.execute(
            "DELETE FROM portfolio WHERE id = ? AND user_id = ?",
            (item_id, user_id)
        )
        conn.commit()


# =============================================================
# ЕЖЕДНЕВНАЯ СТАТИСТИКА
# =============================================================

def log_event(event_type: str):
    """
    Записывает событие для ежедневной статистики.
    event_type может быть:
      price_check  — пользователь проверил цену скина
      watch_added  — добавлено новое отслеживание
      compare_done — выполнено сравнение площадок
      limit_hit    — пользователь упёрся в лимит сравнений
      new_user     — новый пользователь запустил бота
    """
    with sqlite3.connect(DB_FILE) as conn:
        conn.execute(
            "INSERT INTO daily_events (event_type) VALUES (?)",
            (event_type,)
        )
        conn.commit()


def get_daily_stats(hours: int = 24) -> dict:
    """
    Возвращает статистику за последние N часов.
    Используется для ежедневного отчёта администратору.
    """
    with sqlite3.connect(DB_FILE) as conn:
        since = f"-{hours} hours"

        def count_event(event_type):
            return conn.execute(
                "SELECT COUNT(*) FROM daily_events WHERE event_type = ? AND recorded_at >= datetime('now', ?)",
                (event_type, since)
            ).fetchone()[0]

        price_checks  = count_event("price_check")
        watches_added = count_event("watch_added")
        compares_done = count_event("compare_done")
        limit_hits    = count_event("limit_hit")
        new_users     = count_event("new_user")

        # Активные пользователи за сутки — из таблицы активности
        active_today = conn.execute(
            "SELECT COUNT(*) FROM user_activity WHERE last_seen >= datetime('now', ?)",
            (since,)
        ).fetchone()[0]

        # Всего пользователей в базе
        total_users = conn.execute(
            "SELECT COUNT(*) FROM user_activity"
        ).fetchone()[0]

        # Всего активных отслеживаний
        total_watches = conn.execute(
            "SELECT COUNT(*) FROM watches"
        ).fetchone()[0]

        # Всего записей в портфелях
        total_portfolio = conn.execute(
            "SELECT COUNT(*) FROM portfolio"
        ).fetchone()[0]

    return {
        "price_checks":  price_checks,
        "watches_added": watches_added,
        "compares_done": compares_done,
        "limit_hits":    limit_hits,
        "new_users":     new_users,
        "active_today":  active_today,
        "total_users":   total_users,
        "total_watches": total_watches,
        "total_portfolio": total_portfolio,
    }

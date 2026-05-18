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
        conn.commit()

    # Миграция — безопасно добавляем колонки если их нет
    with sqlite3.connect(DB_FILE) as conn:
        for sql in [
            "ALTER TABLE watches ADD COLUMN last_notified_at TEXT",
            # is_premium: 0 = обычный пользователь, 1 = Premium (безлимитные сравнения)
            "ALTER TABLE user_settings ADD COLUMN is_premium INTEGER DEFAULT 0",
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

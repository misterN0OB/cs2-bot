# -*- coding: utf-8 -*-
# =============================================================
# BOT.PY — основной файл Telegram-бота CS2 Skin Price
# =============================================================
# Команды и кнопки:
#   /start   — приветствие с картинкой и главное меню
#   /price   — проверить цену скина
#   /watch   — добавить отслеживание
#   /list    — список отслеживаемых скинов
#
# Фоновые задачи:
#   check_prices — раз в час проверяет цены и шлёт уведомления
# =============================================================

import os
import re
import logging
from datetime import datetime, timedelta

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    LabeledPrice,
)
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    PreCheckoutQueryHandler,
    ContextTypes,
    filters,
)
from telegram.request import HTTPXRequest
from telegram.error import TimedOut, NetworkError

logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s",
    level=logging.WARNING
)

from config import BOT_TOKEN, ADMIN_CHAT_ID

# Временное хранилище данных для кнопок "Упадёт ниже / Вырастет выше".
# Проблема: Telegram ограничивает callback_data 64 байтами.
# Длинные названия скинов + цена не влезают.
# Решение: сохраняем данные здесь по user_id, в кнопку пишем только короткий код.
# Ключ: user_id (число), значение: {"skin_name": "...", "threshold": 3000.0}
_pending_watches: dict = {}

# Хранилище для кнопки "Сравнить площадки" — аналогичная причина.
# Ключ: user_id, значение: skin_name (строка)
_pending_compares: dict = {}
from skin_checker import (
    get_skin_price, get_skin_image_url, resolve_skin_name,
    parse_price_value, get_top_skins, get_price_comparison,
    FREE_COMPARES_PER_WEEK,
)
from database import (
    init_db, add_watch, upsert_watch,
    get_watches, get_all_watches, remove_watch,
    update_last_notified, get_user_currency, set_user_currency,
    record_activity, get_stats,
    get_compare_count, increment_compare_count, is_premium, set_premium,
    FREE_COMPARES_PER_WEEK as DB_FREE_COMPARES,
    get_users_who_hit_limit_last_week,
)

# Поддерживаемые валюты.
# Ключ — код валюты Steam, значение — словарь с отображаемым названием и символом.
CURRENCIES = {
    5: {"name": "Рубли",  "symbol": "руб.", "country": "RU"},
    1: {"name": "Доллары", "symbol": "$",    "country": "US"},
}


# =============================================================
# КОНСТАНТЫ
# =============================================================

# Путь к картинке приветствия.
# Положи файл welcome.jpg в папку Z:\cs2-bot — бот найдёт его автоматически.
# os.path.dirname(__file__) — папка где лежит этот файл (cs2-bot).
WELCOME_IMAGE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "welcome.jpg")

# Как часто проверять цены в секундах.
# 3600 = 1 час. Для тестирования можно поставить 60 (1 минута).
PRICE_CHECK_INTERVAL = 3600

# Стоимость Premium в Telegram Stars.
# 1 Star ≈ $0.013. 199 Stars ≈ $2.60 — разовая покупка навсегда.
PREMIUM_PRICE_STARS = 199

# Минимальный промежуток между уведомлениями по одному скину (в часах).
# Защищает от спама если цена держится ниже порога несколько дней подряд.
NOTIFICATION_COOLDOWN_HOURS = 24


# =============================================================
# КЛАВИАТУРЫ
# =============================================================

MAIN_KEYBOARD = ReplyKeyboardMarkup(
    [
        ["Проверить цену", "Мои отслеживания"],
        ["Добавить отслеживание", "Топ скины"],
        ["Настройки", "Написать нам"],
        ["Поделиться с другом"],
    ],
    resize_keyboard=True
)

# Клавиатура в режиме диалога — только кнопка отмены
DIALOG_KEYBOARD = ReplyKeyboardMarkup(
    [["Вернуться в меню"]],
    resize_keyboard=True
)


# =============================================================
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# =============================================================

def has_cyrillic(text: str) -> bool:
    """Возвращает True если в тексте есть русские буквы."""
    return bool(re.search("[а-яёА-ЯЁ]", text))


def fmt(value: float) -> str:
    """
    Форматирует число с пробелом как разделителем тысяч — по-русски.
    Например: 145000 → "145 000", 3300 → "3 300"
    Python по умолчанию использует запятую (145,000) — это путает пользователей.
    """
    return f"{value:,.0f}".replace(",", " ")


def build_price_card(skin_name: str, result: dict) -> str:
    """Формирует текст карточки скина в HTML для Telegram."""
    return (
        f"<b>{skin_name}</b>\n\n"
        f"Минимальная цена:  <b>{result['lowest_price']}</b>\n"
        f"Средняя цена:      <b>{result['median_price']}</b>\n"
        f"Продано за сутки: <b>{result['volume']} шт.</b>"
    )


def build_watch_keyboard(user_id: int, skin_name: str, threshold: float, symbol: str) -> InlineKeyboardMarkup:
    """
    Сохраняет данные отслеживания во временное хранилище и возвращает
    кнопки выбора направления с коротким callback_data.

    Почему не пишем данные прямо в кнопку:
    Telegram ограничивает callback_data 64 байтами. Длинные названия скинов
    (особенно StatTrak™) плюс цена не умещаются. Поэтому данные храним
    отдельно по user_id, а в кнопку пишем только "sw_below:{user_id}".
    """
    _pending_watches[user_id] = {"skin_name": skin_name, "threshold": threshold}
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"Упадёт ниже {fmt(threshold)} {symbol}", callback_data=f"sw_below:{user_id}")],
        [InlineKeyboardButton(f"Вырастет выше {fmt(threshold)} {symbol}", callback_data=f"sw_above:{user_id}")],
    ])


def build_price_keyboard(skin_name: str) -> InlineKeyboardMarkup:
    """
    Кнопки под карточкой цены.
    Кнопка 'Сравнить площадки' открывает сравнение Steam vs DMarket vs Skinport.
    Если название скина длиннее 55 символов — кнопка compare использует короткий
    ключ через _pending_compares, иначе пишет имя прямо в callback_data.
    """
    # Проверяем длину: "cmp:" = 4 символа, остаток = 60 для имени
    if len(skin_name) <= 60:
        cmp_data = f"cmp:{skin_name}"
    else:
        # Для очень длинных имён используем временное хранилище
        cmp_data = f"cmp:__long__"

    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("Обновить цену", callback_data=f"refresh:{skin_name[:55]}"),
            InlineKeyboardButton("Отследить", callback_data=f"watchmenu:{skin_name[:53]}"),
        ],
        [
            InlineKeyboardButton("Сравнить площадки", callback_data=cmp_data),
        ]
    ])


async def show_price_for_skin(update: Update, skin_name: str, currency: int = 5) -> bool:
    """
    Ищет цену скина и отправляет карточку пользователю.
    currency — код валюты Steam (берётся из настроек пользователя).
    Возвращает True если успешно, False если не найдено.
    """
    if has_cyrillic(skin_name):
        await update.message.reply_text(
            f"Ищу скин по запросу: <b>{skin_name}</b>...\n\n"
            f"Поиск может занять несколько секунд.",
            parse_mode="HTML"
        )
        resolved = resolve_skin_name(skin_name)
        if resolved:
            await update.message.reply_text(f"Нашёл: <b>{resolved}</b>\nЗагружаю цену...", parse_mode="HTML")
            skin_name = resolved
        else:
            await update.message.reply_text(
                "Не удалось найти скин с таким названием.\n\n"
                "Попробуй написать по-другому или на английском.\n"
                "Например: <code>redline</code> вместо <code>красная линия</code>",
                parse_mode="HTML"
            )
            return False
    else:
        await update.message.reply_text(
            f"Ищу цену для: <b>{skin_name}</b>\n\n"
            f"Обычно 2-3 секунды. Если дольше — Steam немного подвисает, это нормально.",
            parse_mode="HTML"
        )

    result = get_skin_price(skin_name, currency=currency)

    if not result["success"]:
        await update.message.reply_text(f"Не удалось получить цену.\n{result['error']}")
        return False

    card_text = build_price_card(skin_name, result)

    # Если имя длинное — сохраняем в хранилище для кнопки сравнения
    if update.effective_user and len(skin_name) > 60:
        _pending_compares[update.effective_user.id] = skin_name

    keyboard = build_price_keyboard(skin_name)
    image_url = get_skin_image_url(skin_name)

    if image_url:
        await update.message.reply_photo(photo=image_url, caption=card_text, parse_mode="HTML", reply_markup=keyboard)
    else:
        await update.message.reply_text(card_text, parse_mode="HTML", reply_markup=keyboard)

    return True


# =============================================================
# ФОНОВАЯ ЗАДАЧА — проверка цен каждый час
# =============================================================
async def check_prices(context: ContextTypes.DEFAULT_TYPE):
    """
    Запускается автоматически раз в час.
    Проверяет все скины в базе данных и шлёт уведомления если
    цена пересекла заданный порог.

    context.bot.send_message — отправляет сообщение пользователю
    даже без его запроса (push-уведомление).
    """
    watches = get_all_watches()

    if not watches:
        return

    print(f"[проверка цен] Проверяю {len(watches)} скинов...")

    for w in watches:
        # Проверяем кулдаун — не беспокоим пользователя чаще раз в 24 часа
        # по одному и тому же скину.
        if w["last_notified_at"]:
            last = datetime.fromisoformat(w["last_notified_at"])
            if datetime.now() - last < timedelta(hours=NOTIFICATION_COOLDOWN_HOURS):
                continue

        # Запрашиваем текущую цену в валюте пользователя
        currency = get_user_currency(w["user_id"])
        result = get_skin_price(w["skin_name"], currency=currency)
        if not result["success"]:
            continue

        # Превращаем строку "3 300 руб." в число 3300.0 для сравнения
        current_price = parse_price_value(result["lowest_price"])
        if current_price is None:
            continue

        # Проверяем условие уведомления
        triggered = False
        condition_text = ""

        if w["price_below"] and current_price <= w["price_below"]:
            triggered = True
            condition_text = f"упала ниже {fmt(w['price_below'])} руб."

        elif w["price_above"] and current_price >= w["price_above"]:
            triggered = True
            condition_text = f"выросла выше {fmt(w['price_above'])} руб."

        if triggered:
            message = (
                f"Сработало отслеживание!\n\n"
                f"Скин: <b>{w['skin_name']}</b>\n"
                f"Текущая цена: <b>{result['lowest_price']}</b>\n"
                f"Цена {condition_text}\n\n"
                f"Смотри в <b>Мои отслеживания</b>"
            )
            try:
                await context.bot.send_message(
                    chat_id=w["user_id"],
                    text=message,
                    parse_mode="HTML"
                )
                # Записываем время уведомления чтобы не спамить
                update_last_notified(w["id"])
                print(f"[уведомление] Пользователь {w['user_id']} — {w['skin_name']}")
            except Exception as e:
                print(f"[ошибка уведомления] {e}")


# =============================================================
# ОБРАБОТЧИК /start
# =============================================================
async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Команда /stats — только для администратора.
    Показывает статистику использования бота:
    сколько пользователей, сколько активных отслеживаний, топ скины.
    """
    if update.effective_user.id != ADMIN_CHAT_ID:
        await update.message.reply_text("Нет доступа.")
        return

    data = get_stats()

    top_lines = ""
    for i, (name, count) in enumerate(data["top_skins"], 1):
        top_lines += f"  {i}. {name} — {count} чел.\n"
    if not top_lines:
        top_lines = "  Пока нет данных\n"

    text = (
        f"<b>Статистика бота CS2 Skin Tracker</b>\n\n"
        f"<b>Пользователи:</b>\n"
        f"  Всего запускали бота: <b>{data['total_users']}</b>\n"
        f"  Активны сегодня (24ч): <b>{data['active_today']}</b>\n"
        f"  Активны за неделю: <b>{data['active_week']}</b>\n\n"
        f"<b>Отслеживания:</b>\n"
        f"  Активных записей в базе: <b>{data['active_watches']}</b>\n\n"
        f"<b>Топ-5 отслеживаемых скинов:</b>\n"
        f"{top_lines}"
    )
    await update.message.reply_text(text, parse_mode="HTML")


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    record_activity(update.effective_user.id)

    # Сначала отправляем короткое сообщение о подключении к Steam.
    # Пользователь сразу видит что бот работает, пока грузится картинка.
    await update.message.reply_text(
        "Подключаюсь к торговой площадке Steam...\n"
        "Может потребоваться несколько секунд."
    )

    text = (
        "Привет! Я бот для отслеживания цен CS2 скинов на Steam Market.\n\n"
        "Используй кнопки внизу:\n"
        "<b>Проверить цену</b> — текущая цена любого скина\n"
        "<b>Добавить отслеживание</b> — уведомление при изменении цены\n"
        "<b>Мои отслеживания</b> — список активных отслеживаний\n"
        "<b>Топ скины</b> — самые популярные и дорогие скины\n\n"
        "Названия скинов можно вводить на русском или английском."
    )

    # Если картинка welcome.jpg лежит в папке — отправляем её
    if os.path.exists(WELCOME_IMAGE_PATH):
        with open(WELCOME_IMAGE_PATH, "rb") as img:
            await update.message.reply_photo(photo=img, caption=text, parse_mode="HTML", reply_markup=MAIN_KEYBOARD)
    else:
        await update.message.reply_text(text, parse_mode="HTML", reply_markup=MAIN_KEYBOARD)


# =============================================================
# ОБРАБОТЧИК /price
# =============================================================
async def price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    skin_name = " ".join(context.args) if context.args else ""
    if not skin_name:
        await update.message.reply_text(
            "Укажи название скина после команды.\n\n"
            "<b>Примеры:</b>\n"
            "<code>/price красная линия</code>\n"
            "<code>/price AK-47 | Redline (Field-Tested)</code>",
            parse_mode="HTML"
        )
        return
    await show_price_for_skin(update, skin_name)


# =============================================================
# ОБРАБОТЧИК /watch
# =============================================================
async def watch(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args if context.args else []
    if len(args) < 2:
        await update.message.reply_text(
            "Укажи название скина и цену.\n\n"
            "<b>Примеры:</b>\n"
            "<code>/watch красная линия 3000</code>\n"
            "<code>/watch AK-47 | Redline (Field-Tested) 3000</code>",
            parse_mode="HTML"
        )
        return

    price_str = args[-1]
    skin_name = " ".join(args[:-1])

    try:
        threshold = float(price_str.replace(",", "."))
    except ValueError:
        await update.message.reply_text(f"Цена <b>{price_str}</b> не похожа на число.", parse_mode="HTML")
        return

    if has_cyrillic(skin_name):
        await update.message.reply_text(f"Ищу скин: <b>{skin_name}</b>...", parse_mode="HTML")
        resolved = resolve_skin_name(skin_name)
        if resolved:
            skin_name = resolved
        else:
            await update.message.reply_text("Не удалось найти скин. Попробуй на английском.")
            return

    symbol = CURRENCIES.get(get_user_currency(update.effective_user.id), {}).get("symbol", "руб.")
    await update.message.reply_text(
        f"Скин: <b>{skin_name}</b>\nПорог: <b>{fmt(threshold)} {symbol}</b>\n\nУведомить когда цена...",
        parse_mode="HTML",
        reply_markup=build_watch_keyboard(update.effective_user.id, skin_name, threshold, symbol)
    )


# =============================================================
# ОБРАБОТЧИК /list
# =============================================================
async def list_watches(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    watches = get_watches(user_id)

    if not watches:
        await update.message.reply_text(
            "У тебя пока нет отслеживаемых скинов.\n\n"
            "Добавь через кнопку <b>Добавить отслеживание</b>",
            parse_mode="HTML"
        )
        return

    await update.message.reply_text(f"<b>Твои отслеживаемые скины: {len(watches)} шт.</b>", parse_mode="HTML")

    for w in watches:
        if w["price_below"]:
            condition = f"Уведомить когда упадёт ниже <b>{fmt(w['price_below'])} руб.</b>"
        else:
            condition = f"Уведомить когда вырастет выше <b>{fmt(w['price_above'])} руб.</b>"

        caption = f"<b>{w['skin_name']}</b>\n\n{condition}"
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("Удалить из отслеживания", callback_data=f"delwatch:{w['id']}")
        ]])
        image_url = get_skin_image_url(w["skin_name"])

        if image_url:
            await update.message.reply_photo(photo=image_url, caption=caption, parse_mode="HTML", reply_markup=keyboard)
        else:
            await update.message.reply_text(caption, parse_mode="HTML", reply_markup=keyboard)


# =============================================================
# ОБРАБОТЧИК ТЕКСТОВЫХ СООБЩЕНИЙ (кнопки и диалоговый ввод)
# =============================================================
async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    state = context.user_data.get("state")

    # --- Кнопка "Отмена" ---
    if text == "Вернуться в меню":
        context.user_data.clear()
        await update.message.reply_text("Возвращаемся в главное меню.", reply_markup=MAIN_KEYBOARD)

    # --- Кнопка "Проверить цену" ---
    elif text == "Проверить цену":
        record_activity(update.effective_user.id)
        context.user_data["state"] = "waiting_price"
        await update.message.reply_text(
            "Введи название скина (можно на русском или английском):\n\n"
            "<b>Примеры:</b>\n"
            "<code>красная линия</code>\n"
            "<code>AK-47 | Redline (Field-Tested)</code>",
            parse_mode="HTML",
            reply_markup=DIALOG_KEYBOARD
        )

    # --- Кнопка "Мои отслеживания" ---
    elif text == "Мои отслеживания":
        await list_watches(update, context)

    # --- Кнопка "Добавить отслеживание" ---
    elif text == "Добавить отслеживание":
        record_activity(update.effective_user.id)
        context.user_data["state"] = "watch_waiting_name"
        await update.message.reply_text(
            "Введи название скина для отслеживания (можно на русском):\n\n"
            "<b>Примеры:</b>\n"
            "<code>красная линия</code>\n"
            "<code>AWP | Asiimov (Field-Tested)</code>",
            parse_mode="HTML",
            reply_markup=DIALOG_KEYBOARD
        )

    # --- Кнопка "Топ скины" ---
    elif text == "Топ скины":
        await update.message.reply_text(
            "Загрузка занимает 5-15 секунд — Steam отвечает не мгновенно.\n\n"
            "Выбери категорию:",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("Самые популярные", callback_data="topskins:popular")],
                [InlineKeyboardButton("Самые дорогие", callback_data="topskins:price")],
            ])
        )

    # --- Кнопка "Настройки" ---
    elif text == "Настройки":
        user_id = update.effective_user.id
        curr = get_user_currency(user_id)
        curr_name = CURRENCIES.get(curr, {}).get("name", "Рубли")
        await update.message.reply_text(
            f"Текущая валюта: <b>{curr_name}</b>\n\nВыбери валюту для отображения цен:",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("Рубли (руб.)", callback_data="setcurrency:5"),
                    InlineKeyboardButton("Доллары ($)", callback_data="setcurrency:1"),
                ]
            ])
        )

    # --- Кнопка "Написать нам" ---
    elif text == "Написать нам":
        context.user_data["state"] = "waiting_feedback"
        await update.message.reply_text(
            "Напиши своё сообщение — вопрос, ошибку или пожелание.\n\n"
            "Я передам его разработчику.",
            reply_markup=DIALOG_KEYBOARD
        )

    # --- Кнопка "Поделиться с другом" ---
    elif text == "Поделиться с другом":
        # Формируем ссылку для Telegram share — открывает диалог пересылки.
        # Пользователь выбирает другу и отправляет приглашение одним нажатием.
        share_text = "Отслеживай цены CS2 скинов на Steam прямо в Telegram!"
        share_url = f"https://t.me/share/url?url=https://t.me/cs2skinprice_bot&text={share_text}"
        await update.message.reply_text(
            "Отправь ссылку на бота другу:",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("Поделиться", url=share_url)
            ]])
        )

    # --- Кнопка "Помощь" ---
    elif text == "Помощь":
        await start(update, context)

    # --- Состояние: ждём сообщение от пользователя ---
    elif state == "waiting_feedback":
        context.user_data.pop("state", None)

        user = update.effective_user
        # Формируем сообщение для администратора с данными отправителя
        admin_message = (
            f"Сообщение от пользователя:\n\n"
            f"Имя: {user.full_name}\n"
            f"Username: @{user.username or 'нет'}\n"
            f"ID: {user.id}\n\n"
            f"Текст:\n{text}"
        )

        try:
            # Отправляем сообщение администратору (тебе)
            await context.bot.send_message(chat_id=ADMIN_CHAT_ID, text=admin_message)
            await update.message.reply_text(
                "Сообщение отправлено! Мы постараемся ответить как можно скорее.",
                reply_markup=MAIN_KEYBOARD
            )
        except Exception as e:
            print(f"[ошибка feedback] {e}")
            await update.message.reply_text(
                "Не удалось отправить сообщение. Попробуй позже.",
                reply_markup=MAIN_KEYBOARD
            )

    # --- Состояние: ждём название скина для /price ---
    elif state == "waiting_price":
        currency = get_user_currency(update.effective_user.id)
        success = await show_price_for_skin(update, text, currency=currency)
        if success:
            context.user_data.pop("state", None)
            await update.message.reply_text("Выбери действие:", reply_markup=MAIN_KEYBOARD)

    # --- Состояние: ждём название скина для /watch ---
    elif state == "watch_waiting_name":
        context.user_data["watch_skin"] = text
        context.user_data["state"] = "watch_waiting_price"
        currency = get_user_currency(update.effective_user.id)
        symbol = CURRENCIES.get(currency, {}).get("symbol", "руб.")
        example = "3000" if currency == 5 else "35"
        await update.message.reply_text(
            f"Скин: <b>{text}</b>\n\nТеперь введи цену-порог в <b>{symbol}</b>:\n\nНапример: <code>{example}</code>",
            parse_mode="HTML",
            reply_markup=DIALOG_KEYBOARD
        )

    # --- Состояние: ждём цену для /watch ---
    elif state == "watch_waiting_price":
        skin_input = context.user_data.get("watch_skin", "")

        try:
            threshold = float(text.replace(",", "."))
        except ValueError:
            await update.message.reply_text(
                f"<b>{text}</b> не похоже на число. Введи сумму цифрами, например: <code>3000</code>",
                parse_mode="HTML"
            )
            return

        context.user_data.pop("watch_skin", None)
        context.user_data.pop("state", None)

        if has_cyrillic(skin_input):
            await update.message.reply_text(f"Ищу скин: <b>{skin_input}</b>...", parse_mode="HTML")
            resolved = resolve_skin_name(skin_input)
            if resolved:
                skin_name = resolved
                await update.message.reply_text(f"Нашёл: <b>{resolved}</b>", parse_mode="HTML")
            else:
                await update.message.reply_text("Не удалось найти скин. Попробуй на английском.", reply_markup=MAIN_KEYBOARD)
                return
        else:
            skin_name = skin_input

        user_id = update.effective_user.id
        symbol = CURRENCIES.get(get_user_currency(user_id), {}).get("symbol", "руб.")
        await update.message.reply_text(
            f"Скин: <b>{skin_name}</b>\nПорог: <b>{fmt(threshold)} {symbol}</b>\n\nУведомить когда цена...",
            parse_mode="HTML",
            reply_markup=build_watch_keyboard(user_id, skin_name, threshold, symbol)
        )


# =============================================================
# ОБРАБОТЧИК ИНЛАЙН-КНОПОК
# =============================================================
async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    try:
        await query.answer()
    except Exception:
        pass

    data = query.data

    # --- "Обновить цену" ---
    if data.startswith("refresh:"):
        skin_name = data[len("refresh:"):]
        await query.edit_message_caption(caption=f"Обновляю цену для: <b>{skin_name}</b>...", parse_mode="HTML")
        result = get_skin_price(skin_name)
        if not result["success"]:
            await query.edit_message_caption(caption=f"Не удалось обновить цену.\n{result['error']}", parse_mode="HTML")
            return
        await query.edit_message_caption(
            caption=build_price_card(skin_name, result),
            parse_mode="HTML",
            reply_markup=build_price_keyboard(skin_name)
        )

    # --- "Отследить" из карточки цены ---
    elif data.startswith("watchmenu:"):
        skin_name = data[len("watchmenu:"):]
        user_id = query.from_user.id
        currency = get_user_currency(user_id)
        symbol = CURRENCIES.get(currency, {}).get("symbol", "руб.")
        example = "3000" if currency == 5 else "35"

        # Сохраняем название скина и переходим в режим ожидания цены.
        # Дальше пользователь вводит цифру — handle_text подхватит через state.
        context.user_data["watch_skin"] = skin_name
        context.user_data["state"] = "watch_waiting_price"

        await query.message.reply_text(
            f"Отслеживание скина:\n<b>{skin_name}</b>\n\n"
            f"Введи цену-порог в <b>{symbol}</b> — бот уведомит когда цена пересечёт её.\n\n"
            f"Например: <code>{example}</code>",
            parse_mode="HTML",
            reply_markup=DIALOG_KEYBOARD
        )

    # --- Выбор направления отслеживания ---
    elif data.startswith("sw_below:") or data.startswith("sw_above:"):
        user_id = query.from_user.id

        # Достаём сохранённые данные из временного хранилища
        pending = _pending_watches.pop(user_id, None)
        if not pending:
            await query.answer("Время действия кнопки истекло. Попробуй добавить скин заново.", show_alert=True)
            return

        skin_name = pending["skin_name"]
        threshold = pending["threshold"]
        symbol = CURRENCIES.get(get_user_currency(user_id), {}).get("symbol", "руб.")

        if data.startswith("sw_below:"):
            action = upsert_watch(user_id, skin_name, price_below=threshold)
            condition = f"упадёт ниже {fmt(threshold)} {symbol}"
        else:
            action = upsert_watch(user_id, skin_name, price_above=threshold)
            condition = f"вырастет выше {fmt(threshold)} {symbol}"

        # "updated" — уже было такое отслеживание, обновили порог
        # "created" — новая запись добавлена
        if action == "updated":
            header = "Порог обновлён!"
            note = "Старая запись обновлена — дублей нет."
        else:
            header = "Добавлено в отслеживание!"
            note = ""

        msg = f"{header}\n\nСкин: <b>{skin_name}</b>\nУведомлю когда цена {condition}"
        if note:
            msg += f"\n\n<i>{note}</i>"

        record_activity(user_id)
        await query.edit_message_text(msg, parse_mode="HTML")
        await query.message.reply_text("Что дальше?", reply_markup=MAIN_KEYBOARD)

    # --- Смена валюты ---
    elif data.startswith("setcurrency:"):
        currency = int(data[len("setcurrency:"):])
        user_id = query.from_user.id
        set_user_currency(user_id, currency)
        curr_name = CURRENCIES.get(currency, {}).get("name", "")
        symbol = CURRENCIES.get(currency, {}).get("symbol", "")
        await query.edit_message_text(
            f"Валюта изменена: <b>{curr_name} ({symbol})</b>\n\n"
            f"Все цены теперь будут отображаться в {symbol}",
            parse_mode="HTML"
        )
        await query.message.reply_text("Что дальше?", reply_markup=MAIN_KEYBOARD)

    # --- Топ скины ---
    elif data.startswith("topskins:"):
        sort_by = data[len("topskins:"):]
        label = "популярных" if sort_by == "popular" else "дорогих"
        await query.edit_message_text(
            f"Загружаю топ-5 {label} скинов CS2...\n\n"
            f"Это занимает 15-30 секунд: сначала список, потом цены для каждого скина.\n"
            f"Пожалуйста, подожди."
        )

        currency = get_user_currency(query.from_user.id)
        country = CURRENCIES.get(currency, {}).get("country", "RU")
        symbol = CURRENCIES.get(currency, {}).get("symbol", "руб.")
        skins = get_top_skins(sort_by=sort_by, count=5, currency=currency, country=country)

        if not skins:
            await query.edit_message_text("Не удалось загрузить список. Попробуй позже.")
            return

        other_label = "Самые дорогие" if sort_by == "popular" else "Самые популярные"
        other_sort = "price" if sort_by == "popular" else "popular"

        title = "Топ-5 самых популярных скинов CS2" if sort_by == "popular" else "Топ-5 самых дорогих скинов CS2"
        await context.bot.send_message(
            chat_id=query.message.chat_id,
            text=f"<b>{title}:</b>",
            parse_mode="HTML"
        )

        # Отправляем каждый скин отдельным фото.
        # Цену запрашиваем отдельно через priceoverview — он точно возвращает
        # нужную валюту. sell_price_text из поиска Steam ненадёжен (может дать EUR).
        sent_any = False
        for i, skin in enumerate(skins, 1):
            name = skin.get("name", "—")
            listings = skin.get("sell_listings", 0)

            icon_url = skin.get("asset_description", {}).get("icon_url")
            if not icon_url:
                continue

            img_url = (
                f"https://community.cloudflare.steamstatic.com"
                f"/economy/image/{icon_url}/128x128"
            )

            # Запрашиваем цену отдельно — так валюта точно будет правильной.
            # Если priceoverview вернул "нет данных" — берём sell_price_text
            # из оригинального поиска (он уже есть, дополнительный запрос не нужен).
            price_result = get_skin_price(name, currency=currency)
            price_text = None
            if price_result["success"]:
                p = price_result["lowest_price"]
                if p and p != "нет данных":
                    price_text = p
            if not price_text:
                # Запасной вариант — цена из поиска Steam (та по которой сортировали)
                price_text = skin.get("sell_price_text") or "—"

            if sort_by == "popular":
                caption = f"<b>{i}. {name}</b>\nЦена: <b>{price_text}</b>  |  Лотов: <b>{listings:,}</b>"
            else:
                caption = f"<b>{i}. {name}</b>\nЦена: <b>{price_text}</b>"

            await context.bot.send_photo(
                chat_id=query.message.chat_id,
                photo=img_url,
                caption=caption,
                parse_mode="HTML"
            )
            sent_any = True

        if not sent_any:
            await query.edit_message_text("Не удалось загрузить изображения. Попробуй позже.")
            return

        # Кнопка переключения между топами
        await context.bot.send_message(
            chat_id=query.message.chat_id,
            text="Переключить список:",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton(other_label, callback_data=f"topskins:{other_sort}")
            ]])
        )

    # --- Сравнение площадок ---
    elif data.startswith("cmp:"):
        user_id = query.from_user.id

        # Восстанавливаем имя скина
        if data == "cmp:__long__":
            skin_name = _pending_compares.get(user_id)
            if not skin_name:
                await query.answer("Данные устарели. Запроси цену скина заново.", show_alert=True)
                return
        else:
            skin_name = data[4:]

        # Проверяем лимит — Premium и администратор не ограничены
        user_premium = is_premium(user_id) or (user_id == ADMIN_CHAT_ID)
        compare_count = get_compare_count(user_id)

        if not user_premium and compare_count >= FREE_COMPARES_PER_WEEK:
            # Лимит исчерпан — предлагаем варианты
            remaining_text = (
                f"Ты использовал все {FREE_COMPARES_PER_WEEK} бесплатных сравнений на этой неделе.\n\n"
                f"Лимит обновится в понедельник.\n\n"
                f"Чтобы продолжать без ограничений:"
            )
            await query.message.reply_text(
                remaining_text,
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton(
                        "Купить Premium (Telegram Stars)",
                        callback_data="buy_premium"
                    )],
                ])
            )
            return

        # Показываем сообщение о загрузке — запросы идут к двум площадкам
        await query.message.reply_text(
            f"Сравниваю цены на <b>{skin_name}</b>...\n\n"
            f"Запрашиваю Steam, DMarket и Skinport — займёт 10-20 секунд.",
            parse_mode="HTML"
        )

        # Получаем цену Steam в долларах для расчёта экономии
        steam_usd = get_skin_price(skin_name, currency=1)  # currency=1 = USD
        steam_price_usd = None
        if steam_usd["success"]:
            # parse_price_value возвращает число из строки "$12.34" → 12.34
            steam_price_usd = parse_price_value(steam_usd["lowest_price"])

        # Запрашиваем все площадки
        comparison = get_price_comparison(skin_name, steam_price_usd=steam_price_usd)
        platforms = comparison["platforms"]

        if not platforms:
            await query.message.reply_text(
                "Не удалось получить цены ни с одной площадки. Попробуй позже."
            )
            return

        # Считаем использование (только после успешного ответа)
        if not user_premium:
            increment_compare_count(user_id)
            new_count = compare_count + 1
            remaining = FREE_COMPARES_PER_WEEK - new_count
        else:
            remaining = None  # Premium — безлимит

        # Формируем текст сравнения
        lines = [f"<b>Сравнение цен: {skin_name}</b>\n"]
        for p in platforms:
            # Звёздочка у самой дешёвой площадки
            marker = "* " if p["name"] == comparison["cheapest"] else "  "
            savings_text = ""
            if p["savings_pct"] > 0:
                savings_text = f"  (экономия {p['savings_pct']}%)"
            elif p["savings_pct"] < 0:
                savings_text = f"  (+{abs(p['savings_pct'])}% к Steam)"

            listings_text = f"  |  {p['listings']} лотов" if p["listings"] else ""
            lines.append(
                f"{marker}<b>{p['name']}</b>: {p['price_str']}{savings_text}{listings_text}"
            )

        lines.append("\n* — самая выгодная цена")
        lines.append("\nЦены в USD. Кликни на площадку чтобы открыть:")

        # Кнопки-ссылки на каждую площадку
        link_buttons = [
            [InlineKeyboardButton(p["name"], url=p["url"])]
            for p in platforms
        ]

        if remaining is not None:
            lines.append(f"\nОсталось бесплатных сравнений на этой неделе: <b>{remaining}</b>")

        await query.message.reply_text(
            "\n".join(lines),
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(link_buttons)
        )

        # Ошибки площадок показываем отдельно если есть.
        # "unavailable" — тихая ошибка (например Skinport блокирует серверные IP),
        # пользователю не показываем технические детали.
        visible_errors = [e for e in comparison["errors"] if "unavailable" not in e]
        if visible_errors:
            errs = "\n".join(visible_errors)
            await query.message.reply_text(
                f"Не удалось получить данные:\n{errs}",
                parse_mode="HTML"
            )

    # --- Покупка Premium через Telegram Stars ---
    elif data == "buy_premium":
        # send_invoice отправляет специальное сообщение с кнопкой оплаты.
        # currency="XTR" — это код Telegram Stars (не обычная валюта).
        # prices — список позиций: LabeledPrice("название", количество_stars).
        # provider_token="" — для Stars токен платёжного провайдера не нужен.
        await context.bot.send_invoice(
            chat_id=query.from_user.id,
            title="CS2 Tracker Premium",
            description=(
                f"Безлимитное сравнение цен на Steam, DMarket и Skinport навсегда. "
                f"Без подписки — платишь один раз."
            ),
            payload="premium_purchase",   # внутренний маркер платежа
            provider_token="",            # для Telegram Stars токен не нужен
            currency="XTR",               # XTR = Telegram Stars
            prices=[LabeledPrice("Premium навсегда", PREMIUM_PRICE_STARS)],
        )

    # --- Удаление из списка ---
    elif data.startswith("delwatch:"):
        watch_id = int(data[len("delwatch:"):])
        user_id = query.from_user.id
        remove_watch(watch_id, user_id)
        try:
            await query.edit_message_caption(caption="Удалено из отслеживания.", parse_mode="HTML", reply_markup=None)
        except Exception:
            try:
                await query.edit_message_text("Удалено из отслеживания.")
            except Exception:
                pass
        await query.answer("Удалено.", show_alert=False)


# =============================================================
# ФОНОВАЯ ЗАДАЧА — уведомление о восстановлении лимита (каждый понедельник)
# =============================================================
async def notify_limit_reset(context: ContextTypes.DEFAULT_TYPE):
    """
    Запускается каждый понедельник в 10:00 UTC (13:00 по Минску).
    Находит пользователей, которые исчерпали лимит на прошлой неделе,
    и сообщает им что бесплатные сравнения снова доступны.
    """
    users = get_users_who_hit_limit_last_week()
    if not users:
        return

    print(f"[лимит сброшен] Уведомляю {len(users)} пользователей...")

    for user_id in users:
        try:
            await context.bot.send_message(
                chat_id=user_id,
                text=(
                    f"Еженедельный лимит восстановлен!\n\n"
                    f"У тебя снова {DB_FREE_COMPARES} бесплатных сравнений цен "
                    f"на Steam, DMarket и Skinport.\n\n"
                    f"Нажми <b>Проверить цену</b> и сравни площадки."
                ),
                parse_mode="HTML",
                reply_markup=MAIN_KEYBOARD
            )
        except Exception as e:
            # Пользователь мог заблокировать бота — просто пропускаем
            print(f"[лимит уведомление] не удалось отправить {user_id}: {e}")


# =============================================================
# ОБРАБОТЧИКИ ОПЛАТЫ TELEGRAM STARS
# =============================================================

async def precheckout_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Telegram вызывает этот обработчик ПЕРЕД списанием Stars.
    Мы должны ответить в течение 10 секунд — подтвердить или отклонить платёж.
    Здесь просто всегда подтверждаем (ok=True).
    """
    query = update.pre_checkout_query
    await query.answer(ok=True)


async def successful_payment_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Вызывается когда пользователь успешно оплатил Stars.
    Здесь выдаём Premium и отправляем подтверждение.
    """
    user_id = update.effective_user.id
    payment = update.message.successful_payment

    # Проверяем что это именно наш платёж за Premium
    if payment.invoice_payload == "premium_purchase":
        set_premium(user_id, True)

        stars_paid = payment.total_amount  # количество Stars (= PREMIUM_PRICE_STARS)

        await update.message.reply_text(
            f"Оплата прошла успешно! Ты заплатил {stars_paid} Stars.\n\n"
            f"<b>Premium активирован навсегда.</b>\n\n"
            f"Теперь у тебя безлимитное сравнение цен на Steam, DMarket и Skinport.",
            parse_mode="HTML",
            reply_markup=MAIN_KEYBOARD
        )

        # Уведомляем администратора о новом покупателе
        try:
            user = update.effective_user
            await context.bot.send_message(
                chat_id=ADMIN_CHAT_ID,
                text=(
                    f"Новая покупка Premium!\n\n"
                    f"Пользователь: {user.full_name}\n"
                    f"Username: @{user.username or 'нет'}\n"
                    f"ID: {user_id}\n"
                    f"Оплачено: {stars_paid} Stars"
                )
            )
        except Exception:
            pass


# =============================================================
# ЗАПУСК БОТА
# =============================================================
if __name__ == "__main__":
    print("Запуск бота...")

    init_db()
    print("База данных готова.")

    # connect_timeout=60 — ждём соединения до 60 секунд (раньше было 30).
    # read_timeout=60    — ждём ответа от Telegram до 60 секунд.
    # pool_timeout=60    — ждём свободного соединения из пула.
    # Все три нужны чтобы бот не падал при медленном интернете или кратких сбоях.
    request = HTTPXRequest(connect_timeout=60, read_timeout=60, pool_timeout=60)
    app = Application.builder().token(BOT_TOKEN).request(request).build()

    # Регистрируем обработчики
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("price", price))
    app.add_handler(CommandHandler("watch", watch))
    app.add_handler(CommandHandler("list", list_watches))
    app.add_handler(CommandHandler("stats", stats))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(CallbackQueryHandler(button_callback))

    # Обработчики оплаты Telegram Stars.
    # PreCheckoutQueryHandler — подтверждаем платёж до списания.
    # SUCCESSFUL_PAYMENT — выдаём Premium после успешной оплаты.
    app.add_handler(PreCheckoutQueryHandler(precheckout_callback))
    app.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, successful_payment_callback))

    # Фоновая задача проверки цен
    # first=60 — первый запуск через 60 секунд после старта бота
    # interval=PRICE_CHECK_INTERVAL — затем каждые N секунд (3600 = час)
    if app.job_queue:
        app.job_queue.run_repeating(check_prices, interval=PRICE_CHECK_INTERVAL, first=60)
        print(f"Проверка цен запущена (каждые {PRICE_CHECK_INTERVAL // 60} мин).")

        # Уведомление о восстановлении лимита — каждый понедельник в 10:00 UTC.
        # days=(0,) — только понедельник (0 = понедельник в Python).
        # time — время запуска в UTC (10:00 = 13:00 по Минску).
        from datetime import time as dt_time
        app.job_queue.run_daily(
            notify_limit_reset,
            time=dt_time(hour=10, minute=0),
            days=(0,)
        )
        print("Уведомления о восстановлении лимита запущены (каждый понедельник в 10:00 UTC).")
    else:
        print("[внимание] JobQueue недоступен — фоновые уведомления не работают.")
        print("Убедись что установлено: pip install 'python-telegram-bot[job-queue]'")

    async def error_handler(update, context):
        if isinstance(context.error, (TimedOut, NetworkError)):
            print(f"[сеть] временный сбой: {context.error}")
        else:
            print(f"[ошибка] {context.error}")

    app.add_error_handler(error_handler)

    print("Бот запущен. Для остановки нажми Ctrl+C.")
    # bootstrap_retries=-1 — бесконечные попытки подключиться при старте.
    # Без этого бот падает если Telegram не ответил с первого раза.
    app.run_polling(bootstrap_retries=-1)

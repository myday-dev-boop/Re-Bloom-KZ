"""
Telegram-бот: доска объявлений цветов ReBloomKZ
С базой данных PostgreSQL — заявки и объявления сохраняются при перезапуске.
"""

import os
import json
import logging
from datetime import datetime
import psycopg2
from psycopg2.extras import RealDictCursor
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto
)
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ConversationHandler, filters, ContextTypes
)

logging.basicConfig(format="%(asctime)s | %(levelname)s | %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

TOKEN      = os.environ["TELEGRAM_TOKEN"]
ADMIN_ID   = int(os.environ.get("ADMIN_ID", "0"))
CHANNEL_ID = os.environ.get("CHANNEL_ID", "")
DB_URL     = os.environ.get("DATABASE_URL", "")

# ─────────────────────────────────────────────────────────────
# ОПЛАТА ПУБЛИКАЦИИ — включить/выключить одной строкой.
#   False — объявления бесплатные, сразу идут на модерацию (для старта).
#   True  — включается приём чека об оплате перед модерацией.
PAYMENT_ENABLED = False
# ─────────────────────────────────────────────────────────────

# реквизиты для оплаты публикации (используются, когда PAYMENT_ENABLED = True)
PAY_AMOUNT = "750"
PAY_PHONE  = "+7 702 261 62 15"
PAY_NAME   = "Meiramkul"

# состояния диалога:
#   PHOTOS   — приём фотографий
#   CARD     — открыта карточка-конструктор (продавец жмёт кнопки полей)
#   WAIT_VAL — ждём текстовый ввод значения выбранного поля
#   PAYMENT  — приём чека об оплате
PHOTOS, CARD, WAIT_VAL, PAYMENT = range(4)
MAX_PHOTOS = 5

# справочник размеров: код кнопки -> текст (как на скрине)
SIZES = {
    "size_huge":  "Огромный",
    "size_big":   "Большой",
    "size_mid":   "Средний",
    "size_small": "Маленький",
    "size_vol":   "Объёмный",
}

# состояние/свежесть букета: код кнопки -> текст (как на скрине)
CONDITIONS = {
    "fr_fresh":   "Свежайшие",
    "fr_good":    "Хорошее состояние",
    "fr_losing":  "Теряет свежесть",
    "fr_wilting": "Заметно вянут",
    "fr_slight":  "Немного увядшие",
    "fr_wilted":  "Увядшие",
}

# города Казахстана (областные центры и крупные города)
CITIES = [
    "Алматы", "Астана", "Шымкент", "Караганда", "Актобе", "Тараз",
    "Павлодар", "Усть-Каменогорск", "Семей", "Костанай", "Кызылорда",
    "Уральск", "Петропавловск", "Актау", "Атырау", "Талдыкорган",
    "Кокшетау", "Туркестан", "Темиртау", "Экибастуз", "Рудный",
]

# какие поля карточки обязательны для перехода к оплате
REQUIRED_FIELDS = ["title", "price", "city", "size", "fresh", "phone"]


# ════════════════════════════════════════════════════════════
#                    БАЗА ДАННЫХ
# ════════════════════════════════════════════════════════════
def db_connect():
    return psycopg2.connect(DB_URL, cursor_factory=RealDictCursor)

def db_init():
    """создаём таблицы при старте, если их нет"""
    if not DB_URL:
        logger.warning("⚠️ DATABASE_URL не задан — база не работает!")
        return
    try:
        conn = db_connect()
        cur = conn.cursor()
        # заявки на модерации
        cur.execute("""
            CREATE TABLE IF NOT EXISTS pending (
                admin_msg_id BIGINT PRIMARY KEY,
                data JSONB NOT NULL,
                created TIMESTAMP DEFAULT NOW()
            )
        """)
        # опубликованные объявления (для кнопки "Продано")
        cur.execute("""
            CREATE TABLE IF NOT EXISTS published (
                channel_msg_id BIGINT PRIMARY KEY,
                seller_id BIGINT NOT NULL,
                chat_id BIGINT NOT NULL,
                is_caption BOOLEAN NOT NULL,
                sold BOOLEAN DEFAULT FALSE,
                created TIMESTAMP DEFAULT NOW()
            )
        """)
        conn.commit()
        cur.close()
        conn.close()
        logger.info("✅ База данных готова")
    except Exception as e:
        logger.error(f"db_init err: {e}")

def db_save_pending(admin_msg_id, data):
    try:
        conn = db_connect(); cur = conn.cursor()
        cur.execute(
            "INSERT INTO pending (admin_msg_id, data) VALUES (%s, %s) "
            "ON CONFLICT (admin_msg_id) DO UPDATE SET data = EXCLUDED.data",
            (admin_msg_id, json.dumps(data)))
        conn.commit(); cur.close(); conn.close()
    except Exception as e:
        logger.error(f"save_pending err: {e}")

def db_get_pending(admin_msg_id):
    try:
        conn = db_connect(); cur = conn.cursor()
        cur.execute("SELECT data FROM pending WHERE admin_msg_id = %s", (admin_msg_id,))
        row = cur.fetchone(); cur.close(); conn.close()
        return row["data"] if row else None
    except Exception as e:
        logger.error(f"get_pending err: {e}")
        return None

def db_del_pending(admin_msg_id):
    try:
        conn = db_connect(); cur = conn.cursor()
        cur.execute("DELETE FROM pending WHERE admin_msg_id = %s", (admin_msg_id,))
        conn.commit(); cur.close(); conn.close()
    except Exception as e:
        logger.error(f"del_pending err: {e}")

def db_save_published(channel_msg_id, seller_id, chat_id, is_caption):
    try:
        conn = db_connect(); cur = conn.cursor()
        cur.execute(
            "INSERT INTO published (channel_msg_id, seller_id, chat_id, is_caption) "
            "VALUES (%s, %s, %s, %s) ON CONFLICT (channel_msg_id) DO NOTHING",
            (channel_msg_id, seller_id, chat_id, is_caption))
        conn.commit(); cur.close(); conn.close()
    except Exception as e:
        logger.error(f"save_published err: {e}")

def db_get_published(channel_msg_id):
    try:
        conn = db_connect(); cur = conn.cursor()
        cur.execute("SELECT * FROM published WHERE channel_msg_id = %s AND sold = FALSE",
                    (channel_msg_id,))
        row = cur.fetchone(); cur.close(); conn.close()
        return row
    except Exception as e:
        logger.error(f"get_published err: {e}")
        return None

def db_mark_sold(channel_msg_id):
    try:
        conn = db_connect(); cur = conn.cursor()
        cur.execute("UPDATE published SET sold = TRUE WHERE channel_msg_id = %s", (channel_msg_id,))
        conn.commit(); cur.close(); conn.close()
    except Exception as e:
        logger.error(f"mark_sold err: {e}")


# ════════════════════════════════════════════════════════════
#                    КОМАНДЫ
# ════════════════════════════════════════════════════════════
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    name = update.effective_user.first_name or "друг"
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("Подать объявление", callback_data="new_ad")],
        [InlineKeyboardButton("Как это работает", callback_data="how")],
    ])
    await update.message.reply_text(
        f"Здравствуйте, {name}.\n\n"
        "*ReBloomKZ* — площадка для продажи цветов и букетов.\n\n"
        "Подайте объявление — после проверки оно появится в нашем канале, "
        "где его увидят покупатели.",
        reply_markup=kb, parse_mode="Markdown")


async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    pay_line = (f"5.  Оплатите публикацию ({PAY_AMOUNT} ₸) и пришлите чек\n"
                "6.  Дождитесь проверки — объявление появится в канале\n"
                if PAYMENT_ENABLED else
                "5.  Дождитесь проверки — объявление появится в канале\n")
    await update.message.reply_text(
        "*Как подать объявление*\n"
        "─────────────\n"
        "1.  Сфотографируйте букет с подписью «ReBloomKZ» и датой\n"
        "2.  При желании добавьте ещё фото (до 5)\n"
        "3.  Заполните название, цену, город, размер, свежесть\n"
        "4.  Оставьте телефон\n"
        + pay_line +
        "─────────────\n"
        "Когда букет продан — нажмите «Продано» под своим объявлением.\n\n"
        "/start — начать\n/cancel — отменить",
        parse_mode="Markdown")


async def on_menu(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if q.data == "how":
        pay_line = (f"• Оплачиваете публикацию ({PAY_AMOUNT} ₸) и присылаете чек\n"
                    if PAYMENT_ENABLED else "")
        await q.message.reply_text(
            "*Как всё устроено*\n"
            "─────────────\n"
            "• Вы фотографируете букет с подписью на бумаге «ReBloomKZ» и датой "
            "(защита от обмана — подтверждает, что букет реальный и у вас на руках)\n"
            "• Заполняете анкету\n"
            + pay_line +
            "• Мы проверяем\n"
            "• Объявление публикуется в канале с вашим контактом\n"
            "• Покупатели пишут вам напрямую\n"
            "• Продали — нажали «Продано», объявление помечается\n"
            "─────────────\n"
            "Нажмите /start, чтобы начать.",
            parse_mode="Markdown")


async def cmd_id(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"Ваш Telegram ID: `{update.effective_user.id}`", parse_mode="Markdown")


# ════════════════════════════════════════════════════════════
#                    АНКЕТА
# ════════════════════════════════════════════════════════════
async def ad_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data.clear()
    ctx.user_data["photos"] = []
    today = datetime.now().strftime("%d.%m.%Y")
    text = (
        "*Фото букета*\n"
        "─────────────\n"
        "Для защиты от обмана положите рядом с букетом листок с надписью "
        f"*«ReBloomKZ»* и сегодняшней датой (_{today}_) и сфотографируйте букет "
        "вместе с этой подписью.\n\n"
        "Так покупатели будут уверены, что букет настоящий и у вас на руках.\n\n"
        "Отправьте фото (можно несколько, до 5). Кнопка «Готово» появится "
        "после первого фото."
    )
    if update.callback_query:
        await update.callback_query.answer()
        await ctx.bot.send_message(update.callback_query.message.chat_id, text,
                                   parse_mode="Markdown")
    else:
        await update.message.reply_text(text, parse_mode="Markdown")
    return PHOTOS


async def ad_photo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not update.message.photo:
        await update.message.reply_text("Пожалуйста, отправьте фото (или нажмите «Готово с фото»)")
        return PHOTOS
    photos = ctx.user_data.setdefault("photos", [])
    if len(photos) >= MAX_PHOTOS:
        await update.message.reply_text(f"Уже {MAX_PHOTOS} фото. Нажмите «Готово с фото».")
        return PHOTOS
    photos.append(update.message.photo[-1].file_id)
    left = MAX_PHOTOS - len(photos)

    # убираем предыдущую подсказку с кнопкой, чтобы не плодились дубли «Готово с фото»
    prev = ctx.user_data.get("photo_prompt_id")
    if prev:
        try:
            await ctx.bot.delete_message(update.effective_chat.id, prev)
        except Exception:
            pass

    kb = InlineKeyboardMarkup([[InlineKeyboardButton("Готово с фото", callback_data="photos_done")]])
    sent = await update.message.reply_text(
        f"Фото добавлено ({len(photos)}/{MAX_PHOTOS}).\n"
        + (f"Можно ещё {left}, или нажмите «Готово»." if left else "Это максимум. Нажмите «Готово»."),
        reply_markup=kb)
    ctx.user_data["photo_prompt_id"] = sent.message_id
    return PHOTOS


async def photos_done(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not ctx.user_data.get("photos"):
        await q.answer("Сначала отправьте хотя бы одно фото", show_alert=True)
        return PHOTOS
    # убираем подсказку с кнопкой
    try:
        await q.message.delete()
    except Exception:
        pass
    ctx.user_data.pop("photo_prompt_id", None)
    # сохраняем данные продавца сразу
    u = update.effective_user
    ctx.user_data["seller_id"] = u.id
    ctx.user_data["seller_name"] = u.first_name or "Продавец"
    ctx.user_data["seller_username"] = u.username or ""
    await send_card(q.message.chat_id, ctx)
    return CARD


# ────────────────────────────────────────────────────────────
#         КАРТОЧКА-КОНСТРУКТОР (как в узбекском боте)
# ────────────────────────────────────────────────────────────
def card_text(d: dict) -> str:
    """Текст-подпись под фото в карточке-конструкторе."""
    def v(key, default):
        return d.get(key) or default
    price = d.get("price")
    price_line = f"{price} ₸" if price else "—"
    return (
        "*Объявление*\n"
        "─────────────\n"
        f"*Название:*  {v('title', '—')}\n"
        f"*Цена:*  {price_line}\n"
        f"*Город:*  {v('city', '—')}\n"
        f"*Размер:*  {v('size', '—')}\n"
        f"*Свежесть:*  {v('fresh', '—')}\n"
        f"*Телефон:*  {v('phone', '—')}\n"
        "─────────────\n"
        "_Нажмите на поле ниже, чтобы заполнить его._"
    )


def card_keyboard(d: dict) -> InlineKeyboardMarkup:
    """Сетка кнопок карточки. ✓ помечает заполненные поля."""
    def mark(key, label):
        return f"✓  {label}" if d.get(key) else label

    ready = all(d.get(f) for f in REQUIRED_FIELDS)
    bottom = (
        InlineKeyboardButton("Опубликовать  ›", callback_data="card_done")
        if ready else
        InlineKeyboardButton("Заполните данные", callback_data="card_noop")
    )
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(mark("title", "Название"), callback_data="f:title"),
         InlineKeyboardButton(mark("price", "Цена"),    callback_data="f:price")],
        [InlineKeyboardButton(mark("city", "Город"),    callback_data="f:city"),
         InlineKeyboardButton(mark("size", "Размер"),   callback_data="f:size")],
        [InlineKeyboardButton(mark("fresh", "Свежесть"), callback_data="f:fresh"),
         InlineKeyboardButton(mark("phone", "Телефон"),  callback_data="f:phone")],
        [InlineKeyboardButton("Изменить фото", callback_data="f:photos")],
        [InlineKeyboardButton("Отменить", callback_data="cancel_ad"), bottom],
    ])


async def send_card(chat_id, ctx: ContextTypes.DEFAULT_TYPE):
    """
    Показывает карточку. Чтобы повторить эффект узбекского бота
    («старая карточка исчезает, появляется новая, уже заполненная»),
    перед отправкой новой карточки удаляем предыдущую.
    """
    d = ctx.user_data
    old_id = d.get("card_msg_id")
    if old_id:
        try:
            await ctx.bot.delete_message(chat_id, old_id)
        except Exception:
            pass
    sent = await ctx.bot.send_photo(
        chat_id=chat_id,
        photo=d["photos"][0],
        caption=card_text(d),
        parse_mode="Markdown",
        reply_markup=card_keyboard(d),
    )
    d["card_msg_id"] = sent.message_id


async def card_noop(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    missing = [name for f, name in [
        ("title", "название"), ("price", "цена"), ("city", "город"),
        ("size", "размер"), ("fresh", "свежесть"), ("phone", "телефон"),
    ] if not ctx.user_data.get(f)]
    await q.answer("Заполните: " + ", ".join(missing), show_alert=True)
    return CARD


# нажата кнопка поля в карточке
async def card_field(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    field = q.data.split(":", 1)[1]
    ctx.user_data["editing_field"] = field

    if field == "photos":
        # вернуться к добавлению фото заново
        ctx.user_data["photos"] = []
        await q.message.reply_text(
            "Пришлите новое фото (можно до 5). Когда закончите — нажмите «Готово с фото».",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("Готово с фото", callback_data="photos_done")]]))
        return PHOTOS

    # ── РАЗМЕР — список кнопок ──
    if field == "size":
        rows = [[InlineKeyboardButton(text, callback_data=code)]
                for code, text in SIZES.items()]
        rows.append([InlineKeyboardButton("↩  Назад", callback_data="back_card")])
        await q.message.reply_text("Выберите размер",
                                   reply_markup=InlineKeyboardMarkup(rows))
        return WAIT_VAL

    # ── СВЕЖЕСТЬ/СОСТОЯНИЕ — список кнопок ──
    if field == "fresh":
        rows = [[InlineKeyboardButton(text, callback_data=code)]
                for code, text in CONDITIONS.items()]
        rows.append([InlineKeyboardButton("↩  Назад", callback_data="back_card")])
        await q.message.reply_text("Выберите состояние",
                                   reply_markup=InlineKeyboardMarkup(rows))
        return WAIT_VAL

    # ── ГОРОД — список кнопок (по 2 в ряд, чтобы влезло) ──
    if field == "city":
        rows = []
        for i in range(0, len(CITIES), 2):
            row = [InlineKeyboardButton(CITIES[i], callback_data=f"city:{i}")]
            if i + 1 < len(CITIES):
                row.append(InlineKeyboardButton(CITIES[i + 1], callback_data=f"city:{i+1}"))
            rows.append(row)
        rows.append([InlineKeyboardButton("↩  Назад", callback_data="back_card")])
        await q.message.reply_text("Выберите город",
                                   reply_markup=InlineKeyboardMarkup(rows))
        return WAIT_VAL

    # ── остальные поля — ввод текстом ──
    prompts = {
        "title": "Напишите *название* — что продаёте:\n_Например: Букет из 25 роз_",
        "price": "Укажите *цену* в тенге:\n_Например: 8000_",
        "phone": "Оставьте *телефон* для связи:\n_Например: +7 707 123 45 67_",
    }
    await q.message.reply_text(prompts[field], parse_mode="Markdown")
    return WAIT_VAL


# выбран размер (кнопкой)
async def card_set_size(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    ctx.user_data["size"] = SIZES.get(q.data, "")
    try:
        await q.message.delete()
    except Exception:
        pass
    await send_card(q.message.chat_id, ctx)
    return CARD


# выбрана свежесть/состояние (кнопкой)
async def card_set_fresh(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    ctx.user_data["fresh"] = CONDITIONS.get(q.data, "")
    try:
        await q.message.delete()
    except Exception:
        pass
    await send_card(q.message.chat_id, ctx)
    return CARD


# выбран город (кнопкой)
async def card_set_city(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    idx = int(q.data.split(":", 1)[1])
    if 0 <= idx < len(CITIES):
        ctx.user_data["city"] = CITIES[idx]
    try:
        await q.message.delete()
    except Exception:
        pass
    await send_card(q.message.chat_id, ctx)
    return CARD


# нажато «↩️ Назад» в любом списке выбора — просто закрыть список, вернуть карточку
async def card_back(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    try:
        await q.message.delete()
    except Exception:
        pass
    await send_card(q.message.chat_id, ctx)
    return CARD


# получен текстовый ввод значения поля (название / цена / телефон)
async def card_value(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    field = ctx.user_data.get("editing_field")
    text = update.message.text.strip()
    limits = {"title": 80, "price": 20, "phone": 30}
    if field in limits:
        ctx.user_data[field] = text[:limits[field]]
    # удаляем сообщение пользователя, чтобы чат оставался чистым
    try:
        await update.message.delete()
    except Exception:
        pass
    await send_card(update.effective_chat.id, ctx)
    return CARD


# нажата «Опубликовать»
async def card_done(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    d = ctx.user_data
    if not all(d.get(f) for f in REQUIRED_FIELDS):
        await q.answer("Сначала заполните все поля", show_alert=True)
        return CARD

    # ── ОПЛАТА ВЫКЛЮЧЕНА: сразу на модерацию, без чека ──
    if not PAYMENT_ENABLED:
        await _submit_to_moderation(q.message.chat_id, ctx)
        await q.message.reply_text(
            "Объявление отправлено на проверку.\n"
            "После одобрения модератором оно появится в канале.")
        return ConversationHandler.END

    # ── ОПЛАТА ВКЛЮЧЕНА: показываем реквизиты и ждём чек ──
    await ctx.bot.send_message(
        q.message.chat_id,
        f"Так будет выглядеть объявление ({len(d['photos'])} фото).\n\n"
        "─────────────\n"
        "*Оплата публикации*\n\n"
        f"Стоимость размещения — *{PAY_AMOUNT} ₸*.\n\n"
        "*Шаг 1.*  Переведите сумму на Kaspi:\n"
        f"`{PAY_PHONE}`\n"
        f"Получатель: *{PAY_NAME}*\n\n"
        "*Шаг 2.*  Откройте чек перевода в Kaspi, нажмите «Поделиться» "
        "и сохраните его (PDF или фото).\n\n"
        "*Шаг 3.*  Пришлите чек сюда — файлом или фото.\n"
        "─────────────\n"
        "_Чек проверит модератор перед публикацией._",
        parse_mode="Markdown")
    return PAYMENT


async def ad_payment(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    receipt = None
    if msg.photo:
        receipt = ("photo", msg.photo[-1].file_id)
    elif msg.document:
        receipt = ("doc", msg.document.file_id)
    else:
        await msg.reply_text(
            "Пожалуйста, пришли *чек* об оплате — фото или файл (PDF) из Kaspi.\n"
            "Или нажми /cancel чтобы отменить.", parse_mode="Markdown")
        return PAYMENT
    ctx.user_data["receipt_type"] = receipt[0]
    ctx.user_data["receipt_id"] = receipt[1]
    await msg.reply_text(
        "Чек получен. Отправляем объявление на проверку?",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("Отправить на проверку", callback_data="send_mod")],
            [InlineKeyboardButton("Отменить", callback_data="cancel_ad")],
        ]))
    return ConversationHandler.END


def format_ad(d):
    price = d.get("price", "")
    try:
        price = f"{int(''.join(c for c in price if c.isdigit())):,}".replace(",", " ")
    except:
        pass
    txt = f"*{d['title']}*\n"
    txt += "─────────────\n"
    txt += f"*Цена:*  {price} ₸\n"
    txt += f"*Город:*  {d['city']}\n"
    if d.get("size"):
        txt += f"*Размер:*  {d['size']}\n"
    if d.get("fresh"):
        txt += f"*Свежесть:*  {d['fresh']}\n"
    txt += "─────────────\n"
    txt += f"*Связь:*  {d['phone']}"
    if d.get("seller_username"):
        txt += f"\n*Telegram:*  @{d['seller_username']}"
    return txt


async def _submit_to_moderation(chat_id, ctx: ContextTypes.DEFAULT_TYPE):
    """Отправляет объявление модератору. Чек прикладывается только если оплата включена."""
    d = dict(ctx.user_data)
    if not ADMIN_ID:
        await ctx.bot.send_message(chat_id, "Модератор не настроен.")
        return
    photos = d["photos"]
    if len(photos) > 1:
        try:
            await ctx.bot.send_media_group(chat_id=ADMIN_ID, media=[InputMediaPhoto(p) for p in photos])
        except Exception as e:
            logger.error(f"media_group err: {e}")
    caption = "🆕 *НА ПРОВЕРКУ*\n\n" + format_ad(d)
    sent = await ctx.bot.send_photo(
        chat_id=ADMIN_ID, photo=photos[0], caption=caption, parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("Одобрить", callback_data="approve"),
             InlineKeyboardButton("Отклонить", callback_data="reject")],
        ]))
    # сохраняем заявку в БАЗУ (а не в память!)
    db_save_pending(sent.message_id, d)
    # чек об оплате — только когда оплата включена
    if PAYMENT_ENABLED:
        try:
            if d.get("receipt_type") == "photo":
                await ctx.bot.send_photo(chat_id=ADMIN_ID, photo=d["receipt_id"],
                    caption=f"Чек об оплате ({PAY_AMOUNT} ₸ на {PAY_PHONE})")
            elif d.get("receipt_type") == "doc":
                await ctx.bot.send_document(chat_id=ADMIN_ID, document=d["receipt_id"],
                    caption=f"Чек об оплате ({PAY_AMOUNT} ₸ на {PAY_PHONE})")
        except Exception as e:
            logger.error(f"receipt err: {e}")


async def send_to_moderation(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    # используется в платном режиме (после получения чека)
    q = update.callback_query
    await q.answer()
    await _submit_to_moderation(q.message.chat_id, ctx)
    await q.message.reply_text(
        "Объявление и чек отправлены на проверку.\n"
        "После подтверждения оплаты оно появится в канале.")


async def cancel_ad_btn(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    # удаляем карточку, если она ещё висит
    card_id = ctx.user_data.get("card_msg_id")
    if card_id:
        try:
            await ctx.bot.delete_message(q.message.chat_id, card_id)
        except Exception:
            pass
    ctx.user_data.clear()
    await q.message.reply_text("Отменено. /start — начать заново.")
    return ConversationHandler.END


async def cmd_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data.clear()
    await update.message.reply_text("Отменено. /start — начать заново.")
    return ConversationHandler.END


# ════════════════════════════════════════════════════════════
#                    МОДЕРАЦИЯ
# ════════════════════════════════════════════════════════════
async def on_moderation(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    msg_id = q.message.message_id
    d = db_get_pending(msg_id)        # читаем из БАЗЫ
    if not d:
        await q.edit_message_caption(
            caption=(q.message.caption or "") + "\n\nДанные устарели.", parse_mode="Markdown")
        return

    if q.data == "approve":
        if not CHANNEL_ID:
            await q.message.reply_text("CHANNEL_ID не настроен!")
            return
        try:
            photos = d["photos"]
            if len(photos) > 1:
                await ctx.bot.send_media_group(chat_id=CHANNEL_ID, media=[InputMediaPhoto(p) for p in photos])
                sent = await ctx.bot.send_message(
                    chat_id=CHANNEL_ID, text=format_ad(d), parse_mode="Markdown",
                    reply_markup=channel_buttons(d))
                is_caption = False
            else:
                sent = await ctx.bot.send_photo(
                    chat_id=CHANNEL_ID, photo=photos[0], caption=format_ad(d),
                    parse_mode="Markdown", reply_markup=channel_buttons(d))
                is_caption = True
            # сохраняем опубликованное в БАЗУ
            db_save_published(sent.message_id, d["seller_id"], sent.chat_id, is_caption)
            await q.edit_message_caption(
                caption=(q.message.caption or "") + "\n\n*ОПУБЛИКОВАНО*", parse_mode="Markdown")
            try:
                await ctx.bot.send_message(d["seller_id"],
                    "Ваше объявление одобрено и опубликовано в канале.\n"
                    "Когда продадите — нажмите «Продано» под объявлением в канале.")
            except:
                pass
        except Exception as e:
            logger.error(f"publish err: {e}")
            await q.message.reply_text(f"Ошибка публикации: {e}")
    elif q.data == "reject":
        await q.edit_message_caption(
            caption=(q.message.caption or "") + "\n\n*ОТКЛОНЕНО*", parse_mode="Markdown")
        try:
            await ctx.bot.send_message(d["seller_id"],
                "К сожалению, объявление не прошло проверку.\n"
                "Возможно, нет подписи «ReBloomKZ» на фото, не хватает данных или оплаты. "
                "Попробуйте снова через /start")
        except:
            pass
    db_del_pending(msg_id)


def channel_buttons(d, sold=False):
    if sold:
        return None
    rows = []
    phone_digits = "".join(c for c in d["phone"] if c.isdigit())
    row = []
    if phone_digits:
        row.append(InlineKeyboardButton("WhatsApp", url=f"https://wa.me/{phone_digits}"))
    if d.get("seller_username"):
        row.append(InlineKeyboardButton("Telegram", url=f"https://t.me/{d['seller_username']}"))
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton("Продано (для продавца)", callback_data="mark_sold")])
    return InlineKeyboardMarkup(rows)


async def on_mark_sold(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    info = db_get_published(q.message.message_id)   # читаем из БАЗЫ
    if not info:
        await q.answer("Объявление не найдено или уже продано.", show_alert=True)
        return
    if q.from_user.id != info["seller_id"] and q.from_user.id != ADMIN_ID:
        await q.answer("Только продавец может отметить «Продано».", show_alert=True)
        return
    await q.answer("Отмечено как продано")
    try:
        if info["is_caption"]:
            await q.edit_message_caption(
                caption="*ПРОДАНО*\n\n" + (q.message.caption or ""),
                parse_mode="Markdown", reply_markup=None)
        else:
            await q.edit_message_text(
                text="*ПРОДАНО*\n\n" + (q.message.text or ""),
                parse_mode="Markdown", reply_markup=None)
    except Exception as e:
        logger.error(f"mark sold err: {e}")
    db_mark_sold(q.message.message_id)


# Страховка: если бот перезапустился (Railway сбросил состояние в памяти),
# старые кнопки объявления перестают ловиться диалогом. Этот глобальный
# обработчик ловит их и подсказывает начать заново, вместо «вечной загрузки».
async def orphan_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer("Сессия устарела. Нажмите /start, чтобы начать заново.", show_alert=True)
    try:
        await q.edit_message_reply_markup(reply_markup=None)
    except Exception:
        pass


# ════════════════════════════════════════════════════════════
def main():
    db_init()   # создаём таблицы при старте
    app = Application.builder().token(TOKEN).build()

    conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(ad_start, pattern="^new_ad$")],
        states={
            PHOTOS: [
                MessageHandler(filters.PHOTO, ad_photo),
                CallbackQueryHandler(photos_done, pattern="^photos_done$"),
            ],
            CARD: [
                CallbackQueryHandler(card_field, pattern="^f:"),
                CallbackQueryHandler(card_done, pattern="^card_done$"),
                CallbackQueryHandler(card_noop, pattern="^card_noop$"),
            ],
            WAIT_VAL: [
                CallbackQueryHandler(card_set_size, pattern="^size_"),
                CallbackQueryHandler(card_set_fresh, pattern="^fr_"),
                CallbackQueryHandler(card_set_city, pattern="^city:"),
                CallbackQueryHandler(card_back, pattern="^back_card$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, card_value),
            ],
            PAYMENT: [MessageHandler((filters.PHOTO | filters.Document.ALL) & ~filters.COMMAND, ad_payment)],
        },
        fallbacks=[
            CommandHandler("cancel", cmd_cancel),
            CallbackQueryHandler(cancel_ad_btn, pattern="^cancel_ad$"),
        ],
        per_message=False,
        allow_reentry=True,
    )

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("id", cmd_id))
    app.add_handler(conv)
    app.add_handler(CallbackQueryHandler(send_to_moderation, pattern="^send_mod$"))
    app.add_handler(CallbackQueryHandler(on_moderation, pattern="^(approve|reject)$"))
    app.add_handler(CallbackQueryHandler(on_mark_sold, pattern="^mark_sold$"))
    app.add_handler(CallbackQueryHandler(on_menu, pattern="^how$"))
    # САМЫМ ПОСЛЕДНИМ — ловит «осиротевшие» кнопки после перезапуска бота
    app.add_handler(CallbackQueryHandler(orphan_callback))

    logger.info("✅ ReBloomKZ бот запущен!")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()

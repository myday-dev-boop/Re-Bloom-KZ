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
    Update, InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto, InputMediaVideo
)
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ConversationHandler, filters, ContextTypes
)

# Firebase — для чтения заявок из веб-приложения ReBloomKZ
import firebase_admin
from firebase_admin import credentials, db as fbdb

logging.basicConfig(format="%(asctime)s | %(levelname)s | %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

TOKEN      = os.environ["TELEGRAM_TOKEN"]
ADMIN_ID   = int(os.environ.get("ADMIN_ID", "0"))
CHANNEL_ID = os.environ.get("CHANNEL_ID", "")
DB_URL     = os.environ.get("DATABASE_URL", "")

# ── Firebase: ключ из переменной окружения FIREBASE_CREDENTIALS (JSON-строка) ──
FIREBASE_DB_URL = "https://rebloomkz-97ca0-default-rtdb.europe-west1.firebasedatabase.app"
FIREBASE_OK = False
try:
    _cred_json = os.environ.get("FIREBASE_CREDENTIALS", "").strip()
    if _cred_json:
        _cred = credentials.Certificate(json.loads(_cred_json))
        firebase_admin.initialize_app(_cred, {"databaseURL": FIREBASE_DB_URL})
        FIREBASE_OK = True
        logger.info("✅ Firebase подключён")
    else:
        logger.warning("⚠️ FIREBASE_CREDENTIALS не задана — модерация из приложения отключена")
except Exception as e:
    logger.error(f"Firebase init error: {e}")

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
MAX_PHOTOS = 5   # общий лимит файлов (фото + видео)


def get_media(d: dict):
    """
    Возвращает список медиа в едином формате: [{"type": "photo"|"video", "id": file_id}, ...]
    Поддерживает старый формат, где d["photos"] — это просто список file_id строк.
    """
    media = d.get("media")
    if media:
        # уже новый формат
        return [m for m in media if isinstance(m, dict) and m.get("id")]
    # старый формат: photos = [file_id, file_id, ...] — считаем их фото
    return [{"type": "photo", "id": fid} for fid in d.get("photos", []) if fid]


def build_album(media, caption=None, parse_mode="Markdown"):
    """Собирает список InputMedia* для send_media_group. Подпись — на первый элемент."""
    out = []
    for i, m in enumerate(media):
        cap = caption if i == 0 else None
        if m["type"] == "video":
            out.append(InputMediaVideo(m["id"], caption=cap, parse_mode=parse_mode))
        else:
            out.append(InputMediaPhoto(m["id"], caption=cap, parse_mode=parse_mode))
    return out

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

# районы Алматы — показываются вторым шагом, если выбран город Алматы
ALMATY_DISTRICTS = [
    "Алмалинский район", "Ауэзовский район", "Бостандыкский район",
    "Жетысуский район", "Медеуский район", "Наурызбайский район",
    "Турксибский район", "Алатауский район",
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
        # опубликованные объявления (для отметки "Продано" в личке с ботом)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS published (
                channel_msg_id BIGINT PRIMARY KEY,
                seller_id BIGINT NOT NULL,
                chat_id BIGINT NOT NULL,
                is_caption BOOLEAN NOT NULL,
                title TEXT DEFAULT '',
                sold BOOLEAN DEFAULT FALSE,
                created TIMESTAMP DEFAULT NOW()
            )
        """)
        # на случай, если таблица уже была создана без колонки title — добавим
        cur.execute("ALTER TABLE published ADD COLUMN IF NOT EXISTS title TEXT DEFAULT ''")
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

def db_save_published(channel_msg_id, seller_id, chat_id, is_caption, title=""):
    try:
        conn = db_connect(); cur = conn.cursor()
        cur.execute(
            "INSERT INTO published (channel_msg_id, seller_id, chat_id, is_caption, title) "
            "VALUES (%s, %s, %s, %s, %s) ON CONFLICT (channel_msg_id) DO NOTHING",
            (channel_msg_id, seller_id, chat_id, is_caption, title))
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

def db_get_seller_ads(seller_id):
    """Непроданные объявления продавца — для отметки 'Продано' в личке."""
    try:
        conn = db_connect(); cur = conn.cursor()
        cur.execute(
            "SELECT channel_msg_id, title, created FROM published "
            "WHERE seller_id = %s AND sold = FALSE ORDER BY created DESC",
            (seller_id,))
        rows = cur.fetchall(); cur.close(); conn.close()
        return rows
    except Exception as e:
        logger.error(f"get_seller_ads err: {e}")
        return []

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
        "1.  Сфотографируйте или снимите букет с подписью «ReBloomKZ» и датой\n"
        f"2.  При желании добавьте ещё фото или видео (всего до {MAX_PHOTOS})\n"
        "3.  Заполните название, цену, город, размер, свежесть\n"
        "4.  Оставьте телефон\n"
        + pay_line +
        "─────────────\n"
        "Когда букет продан — отправьте команду /sold в этом чате с ботом, "
        "и отметьте его как проданный.\n\n"
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
            "• Вы фотографируете или снимаете букет с подписью на бумаге «ReBloomKZ» и датой "
            "(защита от обмана — подтверждает, что букет реальный и у вас на руках)\n"
            "• Заполняете анкету\n"
            + pay_line +
            "• Мы проверяем\n"
            "• Объявление публикуется в канале с вашим контактом\n"
            "• Покупатели пишут вам напрямую\n"
            "• Продали — отметили командой /sold, объявление помечается\n"
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
    ctx.user_data["media"] = []
    today = datetime.now().strftime("%d.%m.%Y")
    text = (
        "*Фото и видео букета*\n"
        "─────────────\n"
        "Для защиты от обмана положите рядом с букетом листок с надписью "
        f"*«ReBloomKZ»* и сегодняшней датой (_{today}_) и сфотографируйте букет "
        "вместе с этой подписью.\n\n"
        "Так покупатели будут уверены, что букет настоящий и у вас на руках.\n\n"
        f"Отправьте фото и/или видео (всего до {MAX_PHOTOS}). Кнопка «Готово» появится "
        "после первого файла."
    )
    if update.callback_query:
        await update.callback_query.answer()
        await ctx.bot.send_message(update.callback_query.message.chat_id, text,
                                   parse_mode="Markdown")
    else:
        await update.message.reply_text(text, parse_mode="Markdown")
    return PHOTOS


async def ad_photo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    # определяем тип присланного файла
    new_item = None
    if msg.photo:
        new_item = {"type": "photo", "id": msg.photo[-1].file_id}
    elif msg.video:
        new_item = {"type": "video", "id": msg.video.file_id}
    elif msg.document and (msg.document.mime_type or "").startswith("video"):
        # видео, присланное файлом
        new_item = {"type": "video", "id": msg.document.file_id}
    else:
        await msg.reply_text("Пожалуйста, отправьте фото или видео (или нажмите «Готово»)")
        return PHOTOS

    media = ctx.user_data.setdefault("media", [])
    if len(media) >= MAX_PHOTOS:
        if not ctx.user_data.get("_warned_max"):
            ctx.user_data["_warned_max"] = True
            await msg.reply_text(
                f"Можно максимум {MAX_PHOTOS} файлов — лишние не добавлены. Нажмите «Готово».")
        return PHOTOS

    media.append(new_item)
    count = len(media)
    left = MAX_PHOTOS - count
    chat_id = update.effective_chat.id

    n_photo = sum(1 for m in media if m["type"] == "photo")
    n_video = sum(1 for m in media if m["type"] == "video")
    parts = []
    if n_photo: parts.append(f"фото: {n_photo}")
    if n_video: parts.append(f"видео: {n_video}")
    summary = ", ".join(parts)

    text = (f"Добавлено ({count}/{MAX_PHOTOS}) — {summary}.\n"
            + (f"Можно ещё {left}, или нажмите «Готово»." if left else "Это максимум. Нажмите «Готово»."))
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("Готово", callback_data="photos_done")]])

    # Если подсказка уже есть — просто обновляем счётчик в ней (важно для альбомов:
    # несколько файлов из одного альбома обновят одно сообщение, а не создадут дубли кнопки).
    prompt_id = ctx.user_data.get("photo_prompt_id")
    if prompt_id:
        try:
            await ctx.bot.edit_message_text(chat_id=chat_id, message_id=prompt_id,
                                             text=text, reply_markup=kb)
            return PHOTOS
        except Exception:
            # сообщение не удалось отредактировать (например, текст совпал) — создадим новое
            pass

    sent = await ctx.bot.send_message(chat_id, text, reply_markup=kb)
    ctx.user_data["photo_prompt_id"] = sent.message_id
    return PHOTOS


async def photos_done(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not ctx.user_data.get("media"):
        await q.answer("Сначала отправьте хотя бы одно фото или видео", show_alert=True)
        return PHOTOS
    # ВРЕМЕННАЯ ДИАГНОСТИКА
    n = len(ctx.user_data.get("media", []))
    logger.info(f"PHOTOS_DONE: collected = {n}")
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
    media = get_media(d)
    n_photo = sum(1 for m in media if m["type"] == "photo")
    n_video = sum(1 for m in media if m["type"] == "video")
    parts = []
    if n_photo: parts.append(f"фото {n_photo}")
    if n_video: parts.append(f"видео {n_video}")
    photo_line = f"*Медиа:*  {', '.join(parts)}" if parts else "*Медиа:*  —"
    return (
        "*Объявление*\n"
        "─────────────\n"
        f"{photo_line}\n"
        f"*Название:*  {v('title', '—')}\n"
        f"*Цена:*  {price_line}\n"
        f"*Город:*  {v('city', '—')}\n"
        f"*Размер:*  {v('size', '—')}\n"
        f"*Свежесть:*  {v('fresh', '—')}\n"
        f"*Телефон:*  {v('phone', '—')}\n"
        "─────────────\n"
        "_В объявлении покажутся все фото и видео. Нажмите на поле ниже, чтобы заполнить его._"
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
        [InlineKeyboardButton("Изменить фото/видео", callback_data="f:photos")],
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
    media = get_media(d)
    first = media[0]
    if first["type"] == "video":
        sent = await ctx.bot.send_video(
            chat_id=chat_id,
            video=first["id"],
            caption=card_text(d),
            parse_mode="Markdown",
            reply_markup=card_keyboard(d),
        )
    else:
        sent = await ctx.bot.send_photo(
            chat_id=chat_id,
            photo=first["id"],
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
        # вернуться к добавлению медиа заново — сбрасываем счётчик и флаги
        ctx.user_data["media"] = []
        ctx.user_data.pop("photo_prompt_id", None)
        ctx.user_data.pop("_warned_max", None)
        await q.message.reply_text(
            f"Пришлите новые фото и/или видео (всего до {MAX_PHOTOS}). "
            "Когда закончите — нажмите «Готово».",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("Готово", callback_data="photos_done")]]))
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
    sent = await q.message.reply_text(prompts[field], parse_mode="Markdown")
    ctx.user_data["field_prompt_id"] = sent.message_id
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
    if not (0 <= idx < len(CITIES)):
        return CARD
    city = CITIES[idx]

    # Алматы → второй шаг: выбор района
    if city == "Алматы":
        rows = []
        for i in range(0, len(ALMATY_DISTRICTS), 2):
            row = [InlineKeyboardButton(ALMATY_DISTRICTS[i], callback_data=f"distr:{i}")]
            if i + 1 < len(ALMATY_DISTRICTS):
                row.append(InlineKeyboardButton(ALMATY_DISTRICTS[i + 1], callback_data=f"distr:{i+1}"))
            rows.append(row)
        rows.append([InlineKeyboardButton("↩  Назад", callback_data="back_card")])
        try:
            await q.message.edit_text("Выберите район Алматы",
                                      reply_markup=InlineKeyboardMarkup(rows))
        except Exception:
            await q.message.reply_text("Выберите район Алматы",
                                       reply_markup=InlineKeyboardMarkup(rows))
        return WAIT_VAL

    # остальные города — записываем сразу
    ctx.user_data["city"] = city
    try:
        await q.message.delete()
    except Exception:
        pass
    await send_card(q.message.chat_id, ctx)
    return CARD


# выбран район Алматы (кнопкой)
async def card_set_district(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    idx = int(q.data.split(":", 1)[1])
    if 0 <= idx < len(ALMATY_DISTRICTS):
        ctx.user_data["city"] = f"Алматы, {ALMATY_DISTRICTS[idx]}"
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
    # удаляем подсказку-вопрос ("Напишите название…" и т.п.)
    prompt_id = ctx.user_data.pop("field_prompt_id", None)
    if prompt_id:
        try:
            await ctx.bot.delete_message(update.effective_chat.id, prompt_id)
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
        f"Так будет выглядеть объявление ({len(get_media(d))} файлов).\n\n"
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
    # контакты ссылками (вместо кнопок — чтобы влезли в подпись альбома)
    contacts = []
    phone_digits = "".join(c for c in d["phone"] if c.isdigit())
    if phone_digits:
        contacts.append(f"[WhatsApp](https://wa.me/{phone_digits})")
    if d.get("seller_username"):
        contacts.append(f"[Telegram](https://t.me/{d['seller_username']})")
    if contacts:
        txt += "\n*Написать:*  " + "  ·  ".join(contacts)
    return txt


async def _submit_to_moderation(chat_id, ctx: ContextTypes.DEFAULT_TYPE):
    """Отправляет объявление модератору. Чек прикладывается только если оплата включена."""
    d = dict(ctx.user_data)
    if not ADMIN_ID:
        await ctx.bot.send_message(chat_id, "Модератор не настроен.")
        return
    media_items = get_media(d)
    caption = "🆕 *НА ПРОВЕРКУ*\n\n" + format_ad(d)
    mod_kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("Одобрить", callback_data="approve"),
         InlineKeyboardButton("Отклонить", callback_data="reject")],
    ])
    if len(media_items) > 1:
        # все фото/видео + текст одним альбомом (подпись на первом файле).
        # Кнопки к альбому прикрепить нельзя — шлём их отдельным коротким
        # сообщением, и именно его message_id сохраняем как ключ заявки
        # (на нём же висят кнопки Одобрить/Отклонить).
        album = build_album(media_items, caption=caption)
        try:
            await ctx.bot.send_media_group(chat_id=ADMIN_ID, media=album)
        except Exception as e:
            logger.error(f"media_group err: {e}")
        sent = await ctx.bot.send_message(
            chat_id=ADMIN_ID, text="Решение по объявлению выше:",
            reply_markup=mod_kb)
    else:
        first = media_items[0]
        if first["type"] == "video":
            sent = await ctx.bot.send_video(
                chat_id=ADMIN_ID, video=first["id"], caption=caption,
                parse_mode="Markdown", reply_markup=mod_kb)
        else:
            sent = await ctx.bot.send_photo(
                chat_id=ADMIN_ID, photo=first["id"], caption=caption,
                parse_mode="Markdown", reply_markup=mod_kb)
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
async def _edit_mod_status(q, suffix):
    """Дописывает статус к карточке заявки. Работает и для фото-с-подписью
    (одно фото), и для текстового сообщения с кнопками (альбом)."""
    base = q.message.caption if q.message.caption is not None else (q.message.text or "")
    new_text = base + suffix
    try:
        await q.edit_message_caption(caption=new_text, parse_mode="Markdown")
    except Exception:
        try:
            await q.edit_message_text(text=new_text, parse_mode="Markdown")
        except Exception:
            pass


async def on_moderation(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    msg_id = q.message.message_id
    d = db_get_pending(msg_id)        # читаем из БАЗЫ
    if not d:
        await _edit_mod_status(q, "\n\nДанные устарели.")
        return

    if q.data == "approve":
        if not CHANNEL_ID:
            await q.message.reply_text("CHANNEL_ID не настроен!")
            return
        try:
            media_items = get_media(d)
            # ВРЕМЕННАЯ ДИАГНОСТИКА
            logger.info(f"PUBLISH: media count = {len(media_items)} | {media_items}")
            await q.message.reply_text(f"Публикую: {len(media_items)} файлов.")
            caption = format_ad(d)
            if len(media_items) > 1:
                # всё одним альбомом: подпись на первом файле, контакты — ссылками в тексте
                album = build_album(media_items, caption=caption)
                sent_group = await ctx.bot.send_media_group(chat_id=CHANNEL_ID, media=album)
                sent = sent_group[0]
            else:
                first = media_items[0]
                if first["type"] == "video":
                    sent = await ctx.bot.send_video(
                        chat_id=CHANNEL_ID, video=first["id"], caption=caption,
                        parse_mode="Markdown")
                else:
                    sent = await ctx.bot.send_photo(
                        chat_id=CHANNEL_ID, photo=first["id"], caption=caption,
                        parse_mode="Markdown")
            is_caption = True
            # сохраняем опубликованное в БАЗУ (с названием, чтобы показать в /sold)
            db_save_published(sent.message_id, d["seller_id"], sent.chat_id, is_caption,
                              d.get("title", ""))
            await _edit_mod_status(q, "\n\n*ОПУБЛИКОВАНО*")
            try:
                await ctx.bot.send_message(
                    d["seller_id"],
                    "Ваше объявление одобрено и опубликовано в канале! 🎉\n\n"
                    "Когда продадите букет — отметьте это командой /sold здесь, в чате с ботом. "
                    "Я покажу ваши объявления, и вы отметите проданное одним нажатием.",
                    reply_markup=InlineKeyboardMarkup(
                        [[InlineKeyboardButton("Мои объявления", callback_data="my_ads")]]))
            except:
                pass
        except Exception as e:
            logger.error(f"publish err: {e}")
            await q.message.reply_text(f"Ошибка публикации: {e}")
    elif q.data == "reject":
        await _edit_mod_status(q, "\n\n*ОТКЛОНЕНО*")
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
    # Кнопка "Продано" в канале НЕ показывается — продавец отмечает продажу
    # в личном чате с ботом командой /sold (так её не видят подписчики канала).
    return InlineKeyboardMarkup(rows) if rows else None


async def cmd_sold(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Личная команда продавца: показать свои объявления и отметить проданные."""
    seller_id = update.effective_user.id
    ads = db_get_seller_ads(seller_id)
    if not ads:
        await update.message.reply_text(
            "У вас нет активных объявлений.\n\n"
            "Когда ваше объявление одобрят и опубликуют, оно появится здесь — "
            "и вы сможете отметить его как проданное.")
        return
    await update.message.reply_text(
        "*Ваши активные объявления*\n"
        "Нажмите на проданный букет, чтобы отметить его как «Продано»:",
        parse_mode="Markdown",
        reply_markup=_seller_ads_keyboard(ads))


def _seller_ads_keyboard(ads):
    rows = []
    for a in ads:
        title = (a.get("title") or "Объявление").strip()
        if len(title) > 40:
            title = title[:38] + "…"
        date = ""
        try:
            date = a["created"].strftime("%d.%m")
        except Exception:
            pass
        label = f"{title}" + (f"  ({date})" if date else "")
        rows.append([InlineKeyboardButton(
            label, callback_data=f"sold:{a['channel_msg_id']}")])
    return InlineKeyboardMarkup(rows)


async def on_my_ads(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Кнопка 'Мои объявления' — то же, что /sold, но из-под уведомления."""
    q = update.callback_query
    await q.answer()
    ads = db_get_seller_ads(q.from_user.id)
    if not ads:
        await q.message.reply_text(
            "У вас пока нет активных объявлений для отметки.")
        return
    await q.message.reply_text(
        "*Ваши активные объявления*\n"
        "Нажмите на проданный букет, чтобы отметить «Продано»:",
        parse_mode="Markdown",
        reply_markup=_seller_ads_keyboard(ads))


async def on_sold_pick(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Продавец выбрал объявление в личке → помечаем проданным + убираем пост из канала."""
    q = update.callback_query
    msg_id = int(q.data.split(":", 1)[1])
    info = db_get_published(msg_id)
    if not info:
        await q.answer("Это объявление уже отмечено или не найдено.", show_alert=True)
        # обновим список (вдруг устарел)
        ads = db_get_seller_ads(q.from_user.id)
        try:
            if ads:
                await q.edit_message_reply_markup(reply_markup=_seller_ads_keyboard(ads))
            else:
                await q.edit_message_text("Активных объявлений больше нет. Все отмечены как проданные.")
        except Exception:
            pass
        return
    # проверка прав: только владелец (или админ)
    if q.from_user.id != info["seller_id"] and q.from_user.id != ADMIN_ID:
        await q.answer("Это не ваше объявление.", show_alert=True)
        return

    await q.answer("Отмечено как продано ✅")
    db_mark_sold(msg_id)

    # помечаем пост в канале как ПРОДАНО и убираем кнопки контактов
    if CHANNEL_ID:
        try:
            if info["is_caption"]:
                # пост был фото с подписью — дописываем ПРОДАНО в подпись
                await ctx.bot.edit_message_caption(
                    chat_id=CHANNEL_ID, message_id=msg_id,
                    caption="✅ *ПРОДАНО*", parse_mode="Markdown", reply_markup=None)
            else:
                await ctx.bot.edit_message_text(
                    chat_id=CHANNEL_ID, message_id=msg_id,
                    text="✅ *ПРОДАНО*", parse_mode="Markdown", reply_markup=None)
        except Exception as e:
            logger.error(f"channel mark sold err: {e}")

    # обновляем список объявлений в личке
    ads = db_get_seller_ads(q.from_user.id)
    try:
        if ads:
            await q.edit_message_text(
                "Отмечено как проданное ✅\n\n"
                "*Остальные ваши объявления:*\nНажмите, если что-то ещё продано:",
                parse_mode="Markdown", reply_markup=_seller_ads_keyboard(ads))
        else:
            await q.edit_message_text(
                "Готово! Отмечено как проданное ✅\n\n"
                "Активных объявлений больше нет. Спасибо, что продаёте на ReBloomKZ!")
    except Exception as e:
        logger.error(f"update seller list err: {e}")


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
#   МОДЕРАЦИЯ ЗАЯВОК ИЗ ВЕБ-ПРИЛОЖЕНИЯ (Firebase)
# ════════════════════════════════════════════════════════════
def fb_format_ad(p: dict) -> str:
    """Текст карточки заявки из приложения."""
    price = p.get("price", 0)
    try:
        price = f"{int(price):,}".replace(",", " ")
    except Exception:
        pass
    lines = [
        f"*{p.get('name','Без названия')}*",
        "─────────────",
        f"*Цена:*  {price} ₸",
        f"*Город:*  {p.get('city','—')}",
        f"*Размер:*  {p.get('size','—')}",
        f"*Свежесть:*  {p.get('fresh','—')}",
    ]
    if p.get("count"):
        lines.append(f"*Цветов:*  {p['count']} шт.")
    if p.get("occasion"):
        lines.append(f"*Повод:*  {p['occasion']}")
    if p.get("desc"):
        lines.append(f"\n{p['desc']}")
    lines.append("─────────────")
    contacts = []
    if p.get("whatsapp"): contacts.append(f"WhatsApp: {p['whatsapp']}")
    if p.get("telegram"): contacts.append(f"Telegram: {p['telegram']}")
    if contacts:
        lines.append("*Контакты:*  " + " · ".join(contacts))
    return "\n".join(lines)


def fb_format_ad_channel(p: dict) -> str:
    """Текст карточки заявки из приложения для канала — с кликабельными контактами."""
    price = p.get("price", 0)
    try:
        price = f"{int(price):,}".replace(",", " ")
    except Exception:
        pass
    lines = [
        f"*{p.get('name','Без названия')}*",
        "─────────────",
        f"*Цена:*  {price} ₸",
        f"*Город:*  {p.get('city','—')}",
        f"*Размер:*  {p.get('size','—')}",
        f"*Свежесть:*  {p.get('fresh','—')}",
    ]
    if p.get("count"):
        lines.append(f"*Цветов:*  {p['count']} шт.")
    if p.get("occasion"):
        lines.append(f"*Повод:*  {p['occasion']}")
    if p.get("desc"):
        lines.append(f"\n{p['desc']}")
    lines.append("─────────────")
    contacts = []
    wa = "".join(c for c in (p.get("whatsapp") or "") if c.isdigit())
    if wa:
        contacts.append(f"[WhatsApp](https://wa.me/{wa})")
    tg = (p.get("telegram") or "").strip().lstrip("@")
    if tg and not tg.replace("+", "").isdigit():
        contacts.append(f"[Telegram](https://t.me/{tg})")
    if contacts:
        lines.append("*Написать:*  " + "  ·  ".join(contacts))
    return "\n".join(lines)


def _data_url_to_bytes(data_url: str):
    """Превращает 'data:image/jpeg;base64,...' в bytes для отправки фото."""
    import base64
    try:
        if data_url and data_url.startswith("data:"):
            b64 = data_url.split(",", 1)[1]
            return base64.b64decode(b64)
    except Exception as e:
        logger.error(f"image decode err: {e}")
    return None


async def fb_check_pending(context: ContextTypes.DEFAULT_TYPE):
    """Периодически проверяет Firebase на новые заявки (status=pending) и шлёт админу."""
    if not FIREBASE_OK or not ADMIN_ID:
        return
    try:
        snap = fbdb.reference("products").get()
    except Exception as e:
        logger.error(f"fb read err: {e}")
        return
    if not snap:
        return
    for pid, p in snap.items():
        if not isinstance(p, dict):
            continue
        # шлём только новые заявки на модерации, которые ещё не отправляли
        if p.get("status") != "pending" or p.get("notified"):
            continue

        caption = "🆕 *НОВАЯ ЗАЯВКА ИЗ ПРИЛОЖЕНИЯ*\n\n" + fb_format_ad(p)
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("Одобрить", callback_data=f"fbok:{pid}"),
            InlineKeyboardButton("Отклонить", callback_data=f"fbno:{pid}"),
        ]])
        img = _data_url_to_bytes(p.get("image", ""))
        try:
            if img:
                await context.bot.send_photo(ADMIN_ID, photo=img, caption=caption,
                                             parse_mode="Markdown", reply_markup=kb)
            elif p.get("image", "").startswith("http"):
                await context.bot.send_photo(ADMIN_ID, photo=p["image"], caption=caption,
                                             parse_mode="Markdown", reply_markup=kb)
            else:
                await context.bot.send_message(ADMIN_ID, caption,
                                               parse_mode="Markdown", reply_markup=kb)
            # помечаем как отправленную, чтобы не слать повторно
            fbdb.reference(f"products/{pid}/notified").set(True)
        except Exception as e:
            logger.error(f"fb notify err: {e}")


async def fb_approve(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Одобрение заявки из приложения: статус→approved + постинг в канал."""
    q = update.callback_query
    await q.answer()
    pid = q.data.split(":", 1)[1]
    try:
        p = fbdb.reference(f"products/{pid}").get()
    except Exception as e:
        await q.answer("Ошибка доступа к базе", show_alert=True)
        logger.error(f"fb approve read: {e}")
        return
    if not p:
        await q.edit_message_caption("Заявка не найдена (возможно, уже обработана).")
        return

    # 1) одобряем в Firebase — товар появится в каталоге приложения
    try:
        fbdb.reference(f"products/{pid}/status").set("approved")
    except Exception as e:
        await q.answer("Не удалось одобрить", show_alert=True)
        logger.error(f"fb approve write: {e}")
        return

    # 2) постим в канал — контакты ссылками прямо в тексте (без кнопок)
    posted = ""
    if CHANNEL_ID:
        caption = fb_format_ad_channel(p)
        try:
            img = _data_url_to_bytes(p.get("image", ""))
            if img:
                await ctx.bot.send_photo(CHANNEL_ID, photo=img, caption=caption,
                                         parse_mode="Markdown")
            elif p.get("image", "").startswith("http"):
                await ctx.bot.send_photo(CHANNEL_ID, photo=p["image"], caption=caption,
                                         parse_mode="Markdown")
            else:
                await ctx.bot.send_message(CHANNEL_ID, caption,
                                           parse_mode="Markdown")
            posted = "\n\n✅ Опубликовано в канале"
        except Exception as e:
            posted = f"\n\n⚠️ Одобрено, но не удалось опубликовать: {e}"
            logger.error(f"fb channel post err: {e}")

    try:
        await q.edit_message_caption("✅ *ОДОБРЕНО*" + posted, parse_mode="Markdown")
    except Exception:
        try: await q.edit_message_text("✅ ОДОБРЕНО" + posted)
        except Exception: pass


async def fb_reject(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Отклонение заявки из приложения: удаляем товар из Firebase."""
    q = update.callback_query
    await q.answer()
    pid = q.data.split(":", 1)[1]
    try:
        fbdb.reference(f"products/{pid}").delete()
    except Exception as e:
        await q.answer("Не удалось отклонить", show_alert=True)
        logger.error(f"fb reject err: {e}")
        return
    try:
        await q.edit_message_caption("❌ *ОТКЛОНЕНО*", parse_mode="Markdown")
    except Exception:
        try: await q.edit_message_text("❌ ОТКЛОНЕНО")
        except Exception: pass


# ════════════════════════════════════════════════════════════
def main():
    db_init()   # создаём таблицы при старте
    app = Application.builder().token(TOKEN).build()

    conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(ad_start, pattern="^new_ad$")],
        states={
            PHOTOS: [
                MessageHandler(filters.PHOTO | filters.VIDEO | filters.Document.VIDEO, ad_photo),
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
                CallbackQueryHandler(card_set_district, pattern="^distr:"),
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
    app.add_handler(CommandHandler("sold", cmd_sold))
    app.add_handler(conv)
    app.add_handler(CallbackQueryHandler(send_to_moderation, pattern="^send_mod$"))
    app.add_handler(CallbackQueryHandler(on_moderation, pattern="^(approve|reject)$"))
    # отметка "Продано" в личке с ботом (вместо кнопки в канале)
    app.add_handler(CallbackQueryHandler(on_my_ads, pattern="^my_ads$"))
    app.add_handler(CallbackQueryHandler(on_sold_pick, pattern="^sold:"))
    app.add_handler(CallbackQueryHandler(on_menu, pattern="^how$"))
    # модерация заявок из веб-приложения (Firebase) — отдельные callback'и
    app.add_handler(CallbackQueryHandler(fb_approve, pattern="^fbok:"))
    app.add_handler(CallbackQueryHandler(fb_reject, pattern="^fbno:"))
    # САМЫМ ПОСЛЕДНИМ — ловит «осиротевшие» кнопки после перезапуска бота
    app.add_handler(CallbackQueryHandler(orphan_callback))

    # периодическая проверка новых заявок из приложения (каждые 30 сек)
    if FIREBASE_OK and app.job_queue:
        app.job_queue.run_repeating(fb_check_pending, interval=30, first=10)
        logger.info("✅ Проверка заявок из приложения включена")

    logger.info("✅ ReBloomKZ бот запущен!")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()

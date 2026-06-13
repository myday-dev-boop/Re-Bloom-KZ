"""
Telegram-бот: доска объявлений цветов ReBloomKZ
Анкета (фото с подписью, размер, свежесть, контакт) → модерация → канал → кнопка "Продано"
"""

import os
import logging
from datetime import datetime
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

# шаги анкеты
PHOTOS, TITLE, PRICE, CITY, SIZE, FRESH, PHONE = range(7)

MAX_PHOTOS = 5

# заявки на модерации: admin_msg_id → данные (+ список message_id карточек у админа)
pending = {}
# опубликованные: channel_msg_id → {seller_id, ...} чтобы продавец мог отметить "Продано"
published = {}

# варианты размера
SIZES = {
    "size_low":  "Низкий (до 30 см)",
    "size_mid":  "Средний (30–50 см)",
    "size_high": "Высокий (50–80 см)",
    "size_xl":   "Экстра-высокий (более 80 см)",
}


# ── /start ────────────────────────────────────────────────────
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    name = update.effective_user.first_name or "друг"
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🌷 Подать объявление", callback_data="new_ad")],
        [InlineKeyboardButton("ℹ️ Как это работает", callback_data="how")],
    ])
    await update.message.reply_text(
        f"👋 Привет, {name}!\n\n"
        "Это *ReBloomKZ* — площадка для продажи цветов и букетов 🌸\n\n"
        "Подай объявление — после проверки оно появится в нашем канале, "
        "где его увидят покупатели.\n\n"
        "Нажми кнопку, чтобы начать:",
        reply_markup=kb, parse_mode="Markdown"
    )


async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🌷 *Как подать объявление:*\n\n"
        "1️⃣ Сфоткай букет с подписью «ReBloomKZ» и датой\n"
        "2️⃣ Можно добавить несколько фото (до 5)\n"
        "3️⃣ Заполни название, цену, город, размер, свежесть\n"
        "4️⃣ Оставь телефон\n"
        "5️⃣ Жди проверки — и объявление в канале!\n\n"
        "Когда букет продан — нажми «✅ Продано» под своим объявлением.\n\n"
        "*/start* — начать\n*/cancel* — отменить",
        parse_mode="Markdown"
    )


async def on_menu(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if q.data == "how":
        await q.message.reply_text(
            "🌷 Как всё устроено:\n\n"
            "• Ты фотографируешь букет с подписью на бумаге «ReBloomKZ» и датой "
            "(это защита от обмана — подтверждает, что букет реальный и у тебя на руках)\n"
            "• Заполняешь анкету\n"
            "• Мы проверяем\n"
            "• Объявление публикуется в канале с твоим контактом\n"
            "• Покупатели пишут тебе напрямую\n"
            "• Продал — нажал «Продано», и объявление помечается\n\n"
            "Нажми /start чтобы начать!"
        )


# ── НАЧАЛО АНКЕТЫ ─────────────────────────────────────────────
async def ad_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data.clear()
    ctx.user_data["photos"] = []
    today = datetime.now().strftime("%d.%m.%Y")
    text = (
        "📷 *Шаг 1 из 7 — Фото*\n\n"
        "⚠️ *Важно для защиты от обмана:*\n"
        "Положи рядом с букетом листок с надписью *«ReBloomKZ»* и сегодняшней датой "
        f"(_{today}_), и сфотографируй букет вместе с этой подписью.\n\n"
        "Так покупатели будут уверены, что букет настоящий и у тебя на руках.\n\n"
        "📸 Отправь фото (можно несколько, до 5).\n"
        "Когда закончишь — нажми кнопку «Готово»."
    )
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("✅ Готово с фото", callback_data="photos_done")]])
    if update.callback_query:
        await update.callback_query.answer()
        await ctx.bot.send_message(update.callback_query.message.chat_id, text,
                                   parse_mode="Markdown", reply_markup=kb)
    else:
        await update.message.reply_text(text, parse_mode="Markdown", reply_markup=kb)
    return PHOTOS


async def ad_photo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not update.message.photo:
        await update.message.reply_text("Пожалуйста, отправь фото 📷 (или нажми «Готово с фото»)")
        return PHOTOS
    photos = ctx.user_data.setdefault("photos", [])
    if len(photos) >= MAX_PHOTOS:
        await update.message.reply_text(f"Уже {MAX_PHOTOS} фото — достаточно. Нажми «Готово с фото».")
        return PHOTOS
    photos.append(update.message.photo[-1].file_id)
    left = MAX_PHOTOS - len(photos)
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("✅ Готово с фото", callback_data="photos_done")]])
    await update.message.reply_text(
        f"✅ Фото добавлено ({len(photos)}/{MAX_PHOTOS}).\n"
        + (f"Можно ещё {left}, или нажми «Готово»." if left else "Это максимум. Нажми «Готово»."),
        reply_markup=kb)
    return PHOTOS


async def photos_done(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not ctx.user_data.get("photos"):
        await q.message.reply_text("Сначала отправь хотя бы одно фото 📷")
        return PHOTOS
    await q.message.reply_text(
        "🌸 *Шаг 2 из 7*\n\nНапиши *название* (что продаёшь):\n_Напр. Букет из 25 роз_",
        parse_mode="Markdown")
    return TITLE


async def ad_title(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data["title"] = update.message.text.strip()[:80]
    await update.message.reply_text(
        "💰 *Шаг 3 из 7*\n\nУкажи *цену* в тенге:\n_Напр. 8000_", parse_mode="Markdown")
    return PRICE


async def ad_price(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data["price"] = update.message.text.strip()[:20]
    await update.message.reply_text(
        "📍 *Шаг 4 из 7*\n\nВ каком *городе*?\n_Напр. Алматы_", parse_mode="Markdown")
    return CITY


async def ad_city(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data["city"] = update.message.text.strip()[:40]
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("Низкий · до 30 см", callback_data="size_low")],
        [InlineKeyboardButton("Средний · 30–50 см", callback_data="size_mid")],
        [InlineKeyboardButton("Высокий · 50–80 см", callback_data="size_high")],
        [InlineKeyboardButton("Экстра · более 80 см", callback_data="size_xl")],
    ])
    await update.message.reply_text(
        "📏 *Шаг 5 из 7 — Размер*\n\nВыбери высоту букета:",
        parse_mode="Markdown", reply_markup=kb)
    return SIZE


async def ad_size(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    ctx.user_data["size"] = SIZES.get(q.data, "")
    await q.edit_message_text(f"📏 Размер: {ctx.user_data['size']} ✅")
    await ctx.bot.send_message(
        q.message.chat_id,
        "🌿 *Шаг 6 из 7 — Свежесть*\n\nНасколько свежий букет?\n"
        "_Напр. собран сегодня, подарили вчера, стоит 2 дня_",
        parse_mode="Markdown")
    return FRESH


async def ad_fresh(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data["fresh"] = update.message.text.strip()[:120]
    await update.message.reply_text(
        "📞 *Шаг 7 из 7*\n\nОставь *телефон* для связи:\n_Напр. +7 707 123 45 67_",
        parse_mode="Markdown")
    return PHONE


async def ad_phone(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data["phone"] = update.message.text.strip()[:30]
    u = update.effective_user
    ctx.user_data["seller_id"] = u.id
    ctx.user_data["seller_name"] = u.first_name or "Продавец"
    ctx.user_data["seller_username"] = u.username or ""

    d = ctx.user_data
    # предпросмотр (первое фото + текст)
    await update.message.reply_photo(
        photo=d["photos"][0], caption=format_ad(d), parse_mode="Markdown")
    await update.message.reply_text(
        f"👆 Так будет выглядеть объявление ({len(d['photos'])} фото).\n\nОтправляем на проверку?",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Отправить", callback_data="send_mod")],
            [InlineKeyboardButton("❌ Отменить", callback_data="cancel_ad")],
        ]))
    return ConversationHandler.END


# ── формат карточки ──────────────────────────────────────────
def format_ad(d):
    price = d.get("price", "")
    try:
        price = f"{int(''.join(c for c in price if c.isdigit())):,}".replace(",", " ")
    except:
        pass
    txt = f"🌸 *{d['title']}*\n\n"
    txt += f"💰 Цена: *{price} ₸*\n"
    txt += f"📍 Город: {d['city']}\n"
    if d.get("size"):
        txt += f"📏 Размер: {d['size']}\n"
    if d.get("fresh"):
        txt += f"🌿 Свежесть: {d['fresh']}\n"
    txt += f"\n📞 Связь: {d['phone']}"
    if d.get("seller_username"):
        txt += f"\n💬 Telegram: @{d['seller_username']}"
    return txt


# ── отправка на модерацию ────────────────────────────────────
async def send_to_moderation(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    d = dict(ctx.user_data)
    if not ADMIN_ID:
        await q.message.reply_text("⚠️ Модератор не настроен.")
        return

    # отправляем альбом фото админу (если несколько)
    photos = d["photos"]
    if len(photos) > 1:
        media = [InputMediaPhoto(p) for p in photos]
        try:
            await ctx.bot.send_media_group(chat_id=ADMIN_ID, media=media)
        except Exception as e:
            logger.error(f"media_group err: {e}")

    caption = "🆕 *НА ПРОВЕРКУ*\n\n" + format_ad(d)
    sent = await ctx.bot.send_photo(
        chat_id=ADMIN_ID, photo=photos[0], caption=caption, parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Одобрить", callback_data="approve"),
             InlineKeyboardButton("❌ Отклонить", callback_data="reject")],
        ]))
    pending[sent.message_id] = d
    await q.message.reply_text(
        "✅ Объявление отправлено на проверку!\n"
        "Как только модератор одобрит — оно появится в канале. 🌷")


async def cancel_ad_btn(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    ctx.user_data.clear()
    await q.message.reply_text("❌ Отменено. /start — начать заново.")


async def cmd_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data.clear()
    await update.message.reply_text("❌ Отменено. /start — начать заново.")
    return ConversationHandler.END


# ── РЕШЕНИЕ МОДЕРАТОРА ────────────────────────────────────────
async def on_moderation(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    msg_id = q.message.message_id
    d = pending.get(msg_id)
    if not d:
        await q.edit_message_caption(caption=(q.message.caption or "") + "\n\n⚠️ Данные устарели.", parse_mode="Markdown")
        return

    if q.data == "approve":
        if not CHANNEL_ID:
            await q.message.reply_text("⚠️ CHANNEL_ID не настроен!")
            return
        try:
            photos = d["photos"]
            # если несколько фото — публикуем альбом, текст отдельным сообщением с кнопками
            if len(photos) > 1:
                media = [InputMediaPhoto(p) for p in photos]
                await ctx.bot.send_media_group(chat_id=CHANNEL_ID, media=media)
                sent = await ctx.bot.send_message(
                    chat_id=CHANNEL_ID, text=format_ad(d), parse_mode="Markdown",
                    reply_markup=channel_buttons(d))
            else:
                sent = await ctx.bot.send_photo(
                    chat_id=CHANNEL_ID, photo=photos[0], caption=format_ad(d),
                    parse_mode="Markdown", reply_markup=channel_buttons(d))

            published[sent.message_id] = {
                "seller_id": d["seller_id"],
                "chat_id": sent.chat_id,
                "is_caption": len(photos) == 1,
            }
            await q.edit_message_caption(caption=(q.message.caption or "") + "\n\n✅ *ОПУБЛИКОВАНО*", parse_mode="Markdown")
            try:
                await ctx.bot.send_message(d["seller_id"],
                    "🎉 Твоё объявление одобрено и опубликовано в канале!\n"
                    "Когда продашь — нажми «✅ Продано» под объявлением в канале. Удачи! 🌷")
            except:
                pass
        except Exception as e:
            logger.error(f"publish err: {e}")
            await q.message.reply_text(f"❌ Ошибка публикации: {e}")

    elif q.data == "reject":
        await q.edit_message_caption(caption=(q.message.caption or "") + "\n\n❌ *ОТКЛОНЕНО*", parse_mode="Markdown")
        try:
            await ctx.bot.send_message(d["seller_id"],
                "К сожалению, объявление не прошло проверку. 😔\n"
                "Возможно, нет подписи «ReBloomKZ» на фото или не хватает данных. "
                "Попробуй снова через /start")
        except:
            pass
    pending.pop(msg_id, None)


def channel_buttons(d, sold=False):
    """кнопки под объявлением в канале"""
    if sold:
        return None
    rows = []
    phone_digits = "".join(c for c in d["phone"] if c.isdigit())
    row = []
    if phone_digits:
        row.append(InlineKeyboardButton("💬 WhatsApp", url=f"https://wa.me/{phone_digits}"))
    if d.get("seller_username"):
        row.append(InlineKeyboardButton("✈️ Написать", url=f"https://t.me/{d['seller_username']}"))
    if row:
        rows.append(row)
    # кнопка "Продано" — нажимает продавец
    rows.append([InlineKeyboardButton("✅ Продано (для продавца)", callback_data="mark_sold")])
    return InlineKeyboardMarkup(rows)


# ── КНОПКА "ПРОДАНО" ──────────────────────────────────────────
async def on_mark_sold(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    info = published.get(q.message.message_id)
    if not info:
        await q.answer("Не удалось найти объявление.", show_alert=True)
        return
    # отметить может только продавец или админ
    if q.from_user.id != info["seller_id"] and q.from_user.id != ADMIN_ID:
        await q.answer("Только продавец может отметить «Продано».", show_alert=True)
        return
    await q.answer("Отмечено как продано ✅")
    try:
        if info["is_caption"]:
            new_cap = "❌ *ПРОДАНО*\n\n" + (q.message.caption or "")
            await q.edit_message_caption(caption=new_cap, parse_mode="Markdown", reply_markup=None)
        else:
            new_txt = "❌ *ПРОДАНО*\n\n" + (q.message.text or "")
            await q.edit_message_text(text=new_txt, parse_mode="Markdown", reply_markup=None)
    except Exception as e:
        logger.error(f"mark sold err: {e}")
    published.pop(q.message.message_id, None)


async def cmd_id(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"🆔 Твой Telegram ID: `{update.effective_user.id}`", parse_mode="Markdown")


# ── запуск ────────────────────────────────────────────────────
def main():
    app = Application.builder().token(TOKEN).build()

    conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(ad_start, pattern="^new_ad$")],
        states={
            PHOTOS: [
                MessageHandler(filters.PHOTO, ad_photo),
                CallbackQueryHandler(photos_done, pattern="^photos_done$"),
            ],
            TITLE: [MessageHandler(filters.TEXT & ~filters.COMMAND, ad_title)],
            PRICE: [MessageHandler(filters.TEXT & ~filters.COMMAND, ad_price)],
            CITY:  [MessageHandler(filters.TEXT & ~filters.COMMAND, ad_city)],
            SIZE:  [CallbackQueryHandler(ad_size, pattern="^size_")],
            FRESH: [MessageHandler(filters.TEXT & ~filters.COMMAND, ad_fresh)],
            PHONE: [MessageHandler(filters.TEXT & ~filters.COMMAND, ad_phone)],
        },
        fallbacks=[CommandHandler("cancel", cmd_cancel)],
    )

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("id", cmd_id))
    app.add_handler(conv)
    app.add_handler(CallbackQueryHandler(send_to_moderation, pattern="^send_mod$"))
    app.add_handler(CallbackQueryHandler(cancel_ad_btn, pattern="^cancel_ad$"))
    app.add_handler(CallbackQueryHandler(on_moderation, pattern="^(approve|reject)$"))
    app.add_handler(CallbackQueryHandler(on_mark_sold, pattern="^mark_sold$"))
    app.add_handler(CallbackQueryHandler(on_menu, pattern="^how$"))

    logger.info("✅ ReBloomKZ бот запущен!")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()

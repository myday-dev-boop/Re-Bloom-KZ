"""
Telegram-бот: доска объявлений цветов с модерацией
Продавец заполняет анкету → админ модерирует → публикация в канал
"""

import os
import logging
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
    ReplyKeyboardMarkup, ReplyKeyboardRemove
)
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ConversationHandler, filters, ContextTypes
)

# ── логирование ──────────────────────────────────────────────
logging.basicConfig(format="%(asctime)s | %(levelname)s | %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

# ── настройки из переменных окружения ────────────────────────
TOKEN      = os.environ["TELEGRAM_TOKEN"]
ADMIN_ID   = int(os.environ.get("ADMIN_ID", "0"))      # твой Telegram ID (куда падает модерация)
CHANNEL_ID = os.environ.get("CHANNEL_ID", "")          # ID канала для публикации (напр. @flowers_almaty или -100...)

# ── шаги анкеты ──────────────────────────────────────────────
PHOTO, TITLE, PRICE, CITY, DESC, PHONE = range(6)

# временное хранилище заявок на модерации: msg_id → данные
pending = {}


# ── /start ────────────────────────────────────────────────────
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    name = update.effective_user.first_name or "друг"
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🌷 Подать объявление", callback_data="new_ad")],
        [InlineKeyboardButton("ℹ️ Как это работает", callback_data="how")],
    ])
    await update.message.reply_text(
        f"👋 Привет, {name}!\n\n"
        "Это бот для продажи цветов, букетов и растений 🌸\n\n"
        "Здесь ты можешь подать объявление — после проверки "
        "оно появится в нашем канале, где его увидят покупатели.\n\n"
        "Нажми кнопку, чтобы начать:",
        reply_markup=kb
    )


async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🌷 *Как подать объявление:*\n\n"
        "1️⃣ Нажми «Подать объявление»\n"
        "2️⃣ Отправь фото цветов\n"
        "3️⃣ Напиши название, цену, город\n"
        "4️⃣ Добавь описание и телефон\n"
        "5️⃣ Жди проверки — и объявление в канале!\n\n"
        "*/start* — начать\n"
        "*/cancel* — отменить заполнение",
        parse_mode="Markdown"
    )


# ── кнопки стартового меню ───────────────────────────────────
async def on_menu(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if q.data == "how":
        await q.message.reply_text(
            "🌷 Всё просто:\n\n"
            "• Ты заполняешь короткую анкету про свой букет\n"
            "• Мы проверяем (чтобы не было спама)\n"
            "• Объявление публикуется в канале с твоим контактом\n"
            "• Покупатели пишут тебе напрямую\n\n"
            "Нажми /start чтобы подать объявление!"
        )


# ── НАЧАЛО АНКЕТЫ ─────────────────────────────────────────────
async def ad_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    # может прийти и от кнопки, и от команды
    if update.callback_query:
        await update.callback_query.answer()
        chat = update.callback_query.message.chat_id
        await ctx.bot.send_message(chat,
            "📷 Шаг 1 из 6\n\nОтправь *фото* твоих цветов/букета:",
            parse_mode="Markdown")
    else:
        await update.message.reply_text(
            "📷 Шаг 1 из 6\n\nОтправь *фото* твоих цветов/букета:",
            parse_mode="Markdown")
    ctx.user_data.clear()
    return PHOTO


async def ad_photo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not update.message.photo:
        await update.message.reply_text("Пожалуйста, отправь именно фото 📷")
        return PHOTO
    # берём самое большое фото
    ctx.user_data["photo"] = update.message.photo[-1].file_id
    await update.message.reply_text(
        "✅ Фото есть!\n\n🌸 Шаг 2 из 6\n\nНапиши *название* (что продаёшь):\n"
        "_Напр. Букет из 25 роз_",
        parse_mode="Markdown")
    return TITLE


async def ad_title(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data["title"] = update.message.text.strip()[:80]
    await update.message.reply_text(
        "💰 Шаг 3 из 6\n\nУкажи *цену* в тенге:\n_Напр. 8000_",
        parse_mode="Markdown")
    return PRICE


async def ad_price(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    price = update.message.text.strip()
    ctx.user_data["price"] = price[:20]
    await update.message.reply_text(
        "📍 Шаг 4 из 6\n\nВ каком *городе*?\n_Напр. Алматы_",
        parse_mode="Markdown")
    return CITY


async def ad_city(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data["city"] = update.message.text.strip()[:40]
    await update.message.reply_text(
        "📝 Шаг 5 из 6\n\nДобавь *описание* (состав, доставка и т.д.):\n"
        "_Или напиши «-» чтобы пропустить_",
        parse_mode="Markdown")
    return DESC


async def ad_desc(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    desc = update.message.text.strip()
    ctx.user_data["desc"] = "" if desc == "-" else desc[:500]
    await update.message.reply_text(
        "📞 Шаг 6 из 6\n\nОставь *телефон* для связи:\n_Напр. +7 707 123 45 67_",
        parse_mode="Markdown")
    return PHONE


async def ad_phone(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    phone = update.message.text.strip()[:30]
    ctx.user_data["phone"] = phone
    u = update.effective_user
    ctx.user_data["seller_id"] = u.id
    ctx.user_data["seller_name"] = u.first_name or "Продавец"
    ctx.user_data["seller_username"] = u.username or ""

    d = ctx.user_data
    # предпросмотр продавцу
    caption = format_ad(d, preview=True)
    await update.message.reply_photo(
        photo=d["photo"], caption=caption, parse_mode="Markdown")
    await update.message.reply_text(
        "👆 Так будет выглядеть объявление.\n\n"
        "Отправляем на проверку?",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Отправить", callback_data="send_mod")],
            [InlineKeyboardButton("❌ Отменить", callback_data="cancel_ad")],
        ]))
    return ConversationHandler.END


# ── формат карточки ──────────────────────────────────────────
def format_ad(d, preview=False):
    price = d.get("price", "")
    # если цена — число, красиво форматируем
    try:
        price = f"{int(''.join(c for c in price if c.isdigit())):,}".replace(",", " ")
    except:
        pass
    txt = f"🌸 *{d['title']}*\n\n"
    txt += f"💰 Цена: *{price} ₸*\n"
    txt += f"📍 Город: {d['city']}\n"
    if d.get("desc"):
        txt += f"\n{d['desc']}\n"
    txt += f"\n📞 Связь: {d['phone']}"
    uname = d.get("seller_username")
    if uname:
        txt += f"\n💬 Telegram: @{uname}"
    return txt


# ── отправка на модерацию ────────────────────────────────────
async def send_to_moderation(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    d = ctx.user_data

    if not ADMIN_ID:
        await q.message.reply_text(
            "⚠️ Модератор не настроен. Сообщи администратору.")
        return

    # отправляем админу карточку с кнопками одобрить/отклонить
    caption = "🆕 *НОВОЕ ОБЪЯВЛЕНИЕ НА ПРОВЕРКУ*\n\n" + format_ad(d)
    sent = await ctx.bot.send_photo(
        chat_id=ADMIN_ID,
        photo=d["photo"],
        caption=caption,
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Одобрить", callback_data=f"approve_{q.from_user.id}"),
             InlineKeyboardButton("❌ Отклонить", callback_data=f"reject_{q.from_user.id}")],
        ]))
    # сохраняем данные заявки по id сообщения у админа
    pending[sent.message_id] = dict(d)

    await q.message.reply_text(
        "✅ Объявление отправлено на проверку!\n"
        "Как только модератор одобрит — оно появится в канале. "
        "Обычно это занимает немного времени. 🌷")


# ── отмена ───────────────────────────────────────────────────
async def cancel_ad_btn(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    ctx.user_data.clear()
    await q.message.reply_text("❌ Отменено. Нажми /start чтобы начать заново.")


async def cmd_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data.clear()
    await update.message.reply_text(
        "❌ Заполнение отменено. /start — начать заново.",
        reply_markup=ReplyKeyboardRemove())
    return ConversationHandler.END


# ── РЕШЕНИЕ МОДЕРАТОРА ────────────────────────────────────────
async def on_moderation(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = q.data
    msg_id = q.message.message_id
    d = pending.get(msg_id)

    if not d:
        await q.edit_message_caption(
            caption=q.message.caption + "\n\n⚠️ Данные устарели.",
            parse_mode="Markdown")
        return

    if data.startswith("approve_"):
        # публикуем в канал
        if not CHANNEL_ID:
            await q.message.reply_text("⚠️ CHANNEL_ID не настроен!")
            return
        try:
            # кнопка связи с продавцом в канале
            buttons = []
            phone_digits = "".join(c for c in d["phone"] if c.isdigit())
            row = []
            if phone_digits:
                row.append(InlineKeyboardButton("💬 WhatsApp", url=f"https://wa.me/{phone_digits}"))
            if d.get("seller_username"):
                row.append(InlineKeyboardButton("✈️ Написать", url=f"https://t.me/{d['seller_username']}"))
            if row:
                buttons.append(row)

            await ctx.bot.send_photo(
                chat_id=CHANNEL_ID,
                photo=d["photo"],
                caption=format_ad(d),
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup(buttons) if buttons else None)

            # помечаем у админа
            await q.edit_message_caption(
                caption=q.message.caption + "\n\n✅ *ОПУБЛИКОВАНО*",
                parse_mode="Markdown")
            # уведомляем продавца
            try:
                await ctx.bot.send_message(
                    d["seller_id"],
                    "🎉 Твоё объявление одобрено и опубликовано в канале!\n"
                    "Покупатели уже могут его видеть. Удачных продаж! 🌷")
            except:
                pass
        except Exception as e:
            logger.error(f"Ошибка публикации: {e}")
            await q.message.reply_text(f"❌ Ошибка публикации: {e}")

    elif data.startswith("reject_"):
        await q.edit_message_caption(
            caption=q.message.caption + "\n\n❌ *ОТКЛОНЕНО*",
            parse_mode="Markdown")
        try:
            await ctx.bot.send_message(
                d["seller_id"],
                "К сожалению, твоё объявление не прошло проверку. 😔\n"
                "Возможно, не хватает фото или информации. "
                "Попробуй ещё раз через /start")
        except:
            pass

    pending.pop(msg_id, None)


# ── /id (узнать свой Telegram ID) ────────────────────────────
async def cmd_id(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"🆔 Твой Telegram ID: `{update.effective_user.id}`\n\n"
        "Используй его как ADMIN_ID в настройках бота.",
        parse_mode="Markdown")


# ── запуск ────────────────────────────────────────────────────
def main():
    app = Application.builder().token(TOKEN).build()

    # анкета (ConversationHandler)
    conv = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(ad_start, pattern="^new_ad$"),
        ],
        states={
            PHOTO: [MessageHandler(filters.PHOTO | (filters.TEXT & ~filters.COMMAND), ad_photo)],
            TITLE: [MessageHandler(filters.TEXT & ~filters.COMMAND, ad_title)],
            PRICE: [MessageHandler(filters.TEXT & ~filters.COMMAND, ad_price)],
            CITY:  [MessageHandler(filters.TEXT & ~filters.COMMAND, ad_city)],
            DESC:  [MessageHandler(filters.TEXT & ~filters.COMMAND, ad_desc)],
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
    app.add_handler(CallbackQueryHandler(on_moderation, pattern="^(approve_|reject_)"))
    app.add_handler(CallbackQueryHandler(on_menu, pattern="^(how)$"))

    logger.info("✅ Бот-цветы запущен!")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()

"""
بوت متابعة الأسعار - Telegram Price Tracker Bot (v2 - واجهة محسّنة)
======================================================================

الجديد في النسخة دي:
- قايمة أزرار تفاعلية (Inline Keyboard) بدل النصوص العادية
- إيموجيز في كل الرسايل
- قايمة أوامر (Menu) تظهر جنب حقل الكتابة في تليجرام
- رسايل أوضح وأجمل شكل

المتطلبات قبل التشغيل:
1. pip install "python-telegram-bot[job-queue]" --upgrade
2. اعمل بوت من BotFather وخد الـ Token
3. حط الـ Token في متغير بيئة TELEGRAM_BOT_TOKEN
4. فعّل دالة fetch_price() بمنطق سحب السعر الحقيقي (لسه TODO)
"""

import os
import sqlite3
import logging
from datetime import datetime, timedelta

from telegram import (
    Update,
    LabeledPrice,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    BotCommand,
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    PreCheckoutQueryHandler,
    ContextTypes,
    filters,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ------------------------------------------------------------------
# الإعدادات
# ------------------------------------------------------------------
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "ضع_التوكن_هنا")
DB_PATH = "price_tracker.db"

FREE_TIER_LIMIT = 2
PRO_TIER_LIMIT = 20
PRO_PRICE_STARS = 150
CHECK_INTERVAL_SECONDS = 3600


# ------------------------------------------------------------------
# قاعدة البيانات
# ------------------------------------------------------------------
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            telegram_id INTEGER PRIMARY KEY,
            tier TEXT DEFAULT 'free',
            tier_expires_at TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS tracked_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            url TEXT,
            product_name TEXT,
            last_price REAL,
            created_at TEXT
        )
    """)
    conn.commit()
    conn.close()


def get_or_create_user(telegram_id: int):
    conn = get_db()
    user = conn.execute(
        "SELECT * FROM users WHERE telegram_id = ?", (telegram_id,)
    ).fetchone()
    if not user:
        conn.execute(
            "INSERT INTO users (telegram_id, tier) VALUES (?, 'free')",
            (telegram_id,),
        )
        conn.commit()
        user = conn.execute(
            "SELECT * FROM users WHERE telegram_id = ?", (telegram_id,)
        ).fetchone()
    conn.close()
    return user


def is_pro(user_row) -> bool:
    if user_row["tier"] != "pro":
        return False
    if not user_row["tier_expires_at"]:
        return False
    return datetime.fromisoformat(user_row["tier_expires_at"]) > datetime.now()


def user_item_count(telegram_id: int) -> int:
    conn = get_db()
    count = conn.execute(
        "SELECT COUNT(*) as c FROM tracked_items WHERE user_id = ?",
        (telegram_id,),
    ).fetchone()["c"]
    conn.close()
    return count


# ------------------------------------------------------------------
# دالة سحب السعر (لسه محتاجة تتفعّل)
# ------------------------------------------------------------------
def fetch_price(url: str):
    """
    TODO: نفّذ منطق سحب السعر هنا حسب كل موقع.
    ترجع (product_name: str, price: float) أو تعمل raise لو فشلت.
    """
    raise NotImplementedError("ضيف منطق سحب السعر الخاص بالموقع هنا")


# ------------------------------------------------------------------
# القوايم التفاعلية (Inline Keyboards)
# ------------------------------------------------------------------
def main_menu_keyboard():
    # ملاحظة: "style" خاصية جديدة في Bot API 9.4 (فبراير 2026) بتلوّن الزرار
    # فعلياً من جوه البوت (مش من ثيم تليجرام). المكتبة لسه ما بتدعمهاش رسمي
    # في الكود، فبنبعتها يدوي عن طريق api_kwargs عشان تليجرام يفهمها.
    # القيم المتاحة: "primary" (أزرق), "success" (أخضر), "danger" (أحمر)
    keyboard = [
        [InlineKeyboardButton(
            "📦 منتجاتي", callback_data="menu_items",
            api_kwargs={"style": "primary"},
        )],
        [InlineKeyboardButton(
            "⭐ ترقية لخطة Pro", callback_data="menu_upgrade",
            api_kwargs={"style": "success"},
        )],
        [InlineKeyboardButton(
            "ℹ️ إزاي أستخدم البوت", callback_data="menu_help",
        )],
    ]
    return InlineKeyboardMarkup(keyboard)


def back_to_menu_keyboard():
    keyboard = [[InlineKeyboardButton(
        "⬅️ رجوع للقايمة الرئيسية", callback_data="menu_main",
    )]]
    return InlineKeyboardMarkup(keyboard)


WELCOME_TEXT = (
    "👋 *أهلاً بيك في بوت متابعة الأسعار!*\n\n"
    "🔗 ابعتلي لينك أي منتج، وأنا هتابعلك سعره وأبعتلك تنبيه فوري 🔻 لما ينزل.\n\n"
    "📊 اختار من القايمة تحت 👇"
)

HELP_TEXT = (
    "ℹ️ *إزاي تستخدم البوت:*\n\n"
    "1️⃣ ابعت لينك أي منتج من أي موقع تسوق\n"
    "2️⃣ البوت هيحفظه ويراقب السعر تلقائي\n"
    "3️⃣ هتوصلك رسالة 🔻 فوراً لما السعر ينزل\n\n"
    f"🆓 الخطة المجانية: حتى {FREE_TIER_LIMIT} منتجات\n"
    f"⭐ خطة Pro: حتى {PRO_TIER_LIMIT} منتج مقابل {PRO_PRICE_STARS} نجمة/شهر"
)


# ------------------------------------------------------------------
# أوامر البوت
# ------------------------------------------------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    get_or_create_user(update.effective_user.id)
    await update.message.reply_text(
        WELCOME_TEXT,
        parse_mode="Markdown",
        reply_markup=main_menu_keyboard(),
    )


async def menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """يتعامل مع ضغطات أزرار القايمة."""
    query = update.callback_query
    await query.answer()
    telegram_id = query.from_user.id

    if query.data == "menu_main":
        await query.edit_message_text(
            WELCOME_TEXT, parse_mode="Markdown", reply_markup=main_menu_keyboard()
        )

    elif query.data == "menu_help":
        await query.edit_message_text(
            HELP_TEXT, parse_mode="Markdown", reply_markup=back_to_menu_keyboard()
        )

    elif query.data == "menu_items":
        conn = get_db()
        items = conn.execute(
            "SELECT product_name, last_price, url FROM tracked_items WHERE user_id = ?",
            (telegram_id,),
        ).fetchall()
        conn.close()

        if not items:
            text = "📭 مفيش منتجات بتتابعها دلوقتي.\n\n🔗 ابعتلي لينك منتج عشان تبدأ."
        else:
            text = "📦 *المنتجات اللي بتتابعها:*\n\n"
            for item in items:
                text += f"• {item['product_name']} — 💰 {item['last_price']}\n{item['url']}\n\n"
        await query.edit_message_text(
            text, parse_mode="Markdown", reply_markup=back_to_menu_keyboard()
        )

    elif query.data == "menu_upgrade":
        prices = [LabeledPrice("اشتراك Pro لمدة شهر", PRO_PRICE_STARS)]
        await context.bot.send_invoice(
            chat_id=query.message.chat_id,
            title="⭐ اشتراك Pro - متابعة الأسعار",
            description=f"تابع حتى {PRO_TIER_LIMIT} منتج لمدة شهر كامل",
            payload="pro_subscription_1_month",
            provider_token="",
            currency="XTR",
            prices=prices,
        )


async def track(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """يستقبل أي رسالة فيها لينك ويبدأ يتابعه."""
    url = update.message.text.strip()
    if not url.startswith("http"):
        await update.message.reply_text(
            "🔗 ابعتلي لينك صحيح يبدأ بـ http أو https 🙂",
            reply_markup=main_menu_keyboard(),
        )
        return

    telegram_id = update.effective_user.id
    user = get_or_create_user(telegram_id)
    limit = PRO_TIER_LIMIT if is_pro(user) else FREE_TIER_LIMIT

    if user_item_count(telegram_id) >= limit:
        await update.message.reply_text(
            f"⚠️ وصلت للحد الأقصى ({limit} منتج) في خطتك الحالية.\n"
            "⭐ استخدم زرار الترقية عشان تزود العدد.",
            reply_markup=main_menu_keyboard(),
        )
        return

    try:
        product_name, price = fetch_price(url)
    except NotImplementedError:
        await update.message.reply_text(
            "⚠️ لسه دالة سحب السعر مش متفعّلة لهذا الموقع.",
            reply_markup=main_menu_keyboard(),
        )
        return
    except Exception as e:
        logger.error(f"fetch_price failed: {e}")
        await update.message.reply_text(
            "❌ معرفتش أجيب سعر المنتج ده، جرب لينك تاني.",
            reply_markup=main_menu_keyboard(),
        )
        return

    conn = get_db()
    conn.execute(
        "INSERT INTO tracked_items (user_id, url, product_name, last_price, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (telegram_id, url, product_name, price, datetime.now().isoformat()),
    )
    conn.commit()
    conn.close()

    await update.message.reply_text(
        f"✅ *بدأت أتابع:* {product_name}\n💰 السعر الحالي: {price}\n\n"
        "🔔 هبعتلك تنبيه فوراً لو نزل.",
        parse_mode="Markdown",
        reply_markup=main_menu_keyboard(),
    )


async def my_items(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """أمر /items مباشرة (بديل للزرار)."""
    telegram_id = update.effective_user.id
    conn = get_db()
    items = conn.execute(
        "SELECT product_name, last_price, url FROM tracked_items WHERE user_id = ?",
        (telegram_id,),
    ).fetchall()
    conn.close()

    if not items:
        await update.message.reply_text(
            "📭 مفيش منتجات بتتابعها دلوقتي.", reply_markup=main_menu_keyboard()
        )
        return

    text = "📦 *المنتجات اللي بتتابعها:*\n\n"
    for item in items:
        text += f"• {item['product_name']} — 💰 {item['last_price']}\n{item['url']}\n\n"
    await update.message.reply_text(
        text, parse_mode="Markdown", reply_markup=main_menu_keyboard()
    )


async def upgrade(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """أمر /upgrade مباشرة (بديل للزرار)."""
    prices = [LabeledPrice("اشتراك Pro لمدة شهر", PRO_PRICE_STARS)]
    await context.bot.send_invoice(
        chat_id=update.effective_chat.id,
        title="⭐ اشتراك Pro - متابعة الأسعار",
        description=f"تابع حتى {PRO_TIER_LIMIT} منتج لمدة شهر كامل",
        payload="pro_subscription_1_month",
        provider_token="",
        currency="XTR",
        prices=prices,
    )


async def precheckout(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.pre_checkout_query
    await query.answer(ok=True)


async def successful_payment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    telegram_id = update.effective_user.id
    expires = datetime.now() + timedelta(days=30)
    conn = get_db()
    conn.execute(
        "UPDATE users SET tier = 'pro', tier_expires_at = ? WHERE telegram_id = ?",
        (expires.isoformat(), telegram_id),
    )
    conn.commit()
    conn.close()
    await update.message.reply_text(
        f"🎉 *تم تفعيل اشتراك Pro بنجاح!*\n\nتقدر دلوقتي تتابع حتى {PRO_TIER_LIMIT} منتج 🚀",
        parse_mode="Markdown",
        reply_markup=main_menu_keyboard(),
    )


# ------------------------------------------------------------------
# فحص الأسعار الدوري
# ------------------------------------------------------------------
async def check_prices_job(context: ContextTypes.DEFAULT_TYPE):
    conn = get_db()
    items = conn.execute("SELECT * FROM tracked_items").fetchall()
    conn.close()

    for item in items:
        try:
            _, new_price = fetch_price(item["url"])
        except Exception:
            continue

        if new_price < item["last_price"]:
            await context.bot.send_message(
                chat_id=item["user_id"],
                text=(
                    f"🔻 *السعر نزل!*\n\n"
                    f"📦 {item['product_name']}\n"
                    f"💰 من {item['last_price']} ➡️ {new_price}\n{item['url']}"
                ),
                parse_mode="Markdown",
            )
            conn = get_db()
            conn.execute(
                "UPDATE tracked_items SET last_price = ? WHERE id = ?",
                (new_price, item["id"]),
            )
            conn.commit()
            conn.close()


# ------------------------------------------------------------------
# قايمة الأوامر (بتظهر جنب حقل الكتابة في تليجرام)
# ------------------------------------------------------------------
async def post_init(application: Application):
    await application.bot.set_my_commands([
        BotCommand("start", "🏠 القايمة الرئيسية"),
        BotCommand("items", "📦 منتجاتي المتابعة"),
        BotCommand("upgrade", "⭐ ترقية لخطة Pro"),
    ])


# ------------------------------------------------------------------
# التشغيل
# ------------------------------------------------------------------
def main():
    init_db()
    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("items", my_items))
    app.add_handler(CommandHandler("upgrade", upgrade))
    app.add_handler(CallbackQueryHandler(menu_callback, pattern="^menu_"))
    app.add_handler(PreCheckoutQueryHandler(precheckout))
    app.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, successful_payment))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, track))

    app.job_queue.run_repeating(check_prices_job, interval=CHECK_INTERVAL_SECONDS)

    logger.info("Bot is running...")
    app.run_polling()


if __name__ == "__main__":
    main()

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
import re
import json
import sqlite3
import logging
from datetime import datetime, timedelta

import requests
from bs4 import BeautifulSoup

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
# دالة سحب السعر
# ------------------------------------------------------------------

# هيدرز بتقلد متصفح حقيقي، عشان نقلل احتمال الحجب المباشر
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ar,en;q=0.9",
}


def fetch_price_noon(url: str):
    """
    يسحب اسم المنتج وسعره من صفحة منتج على نون (noon.com).

    ⚠️ ملاحظة مهمة: نون بيستخدم نظام حماية (Akamai) بيحاول يمنع
    أدوات السكرابينج. الكود ده بيشتغل بمحاولة مباشرة، وممكن يتحجب
    بعد استخدام كتير أو مكثف. لو حصل حجب متكرر، الحل الأعملي هو
    استخدام خدمة scraping API جاهزة (زي ScraperAPI أو ScrapingBee)
    بدل الطلب المباشر.
    """
    response = requests.get(url, headers=HEADERS, timeout=15)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, "html.parser")

    # نون موقع مبني بـ Next.js، وغالباً البيانات بتتخزن في
    # <script id="__NEXT_DATA__"> كـ JSON. بنحاول نقرأها من هناك الأول
    # لأنها أدق من محاولة قراءة الشكل المرئي للصفحة.
    next_data_tag = soup.find("script", id="__NEXT_DATA__")
    if next_data_tag and next_data_tag.string:
        try:
            data = json.loads(next_data_tag.string)
            # المسار جوه الـ JSON ممكن يتغير مع تحديثات نون، فبندور
            # عن أول مفتاح اسمه sellingPrice أو price جوه الشجرة كلها
            price = _search_json_for_price(data)
            name = _search_json_for_name(data)
            if price is not None and name is not None:
                return name, price
        except (json.JSONDecodeError, KeyError):
            pass

    # لو فشلت طريقة الـ JSON، نجرب نلاقي السعر من الصفحة المرئية
    # مباشرة (Fallback) عن طريق meta tags أو نصوص فيها رقم + "EGP"/"ج.م"
    price_meta = soup.find("meta", {"property": "product:price:amount"})
    name_meta = soup.find("meta", {"property": "og:title"})
    if price_meta and name_meta:
        try:
            return name_meta["content"], float(price_meta["content"])
        except (ValueError, KeyError):
            pass

    raise ValueError("معرفتش أستخرج السعر من صفحة نون دي")


def _search_json_for_price(data):
    """بيدور جوه أي JSON متداخل عن حقل سعر معروف."""
    price_keys = ("sellingPrice", "salePrice", "price")
    if isinstance(data, dict):
        for key in price_keys:
            if key in data and isinstance(data[key], (int, float)):
                return float(data[key])
        for value in data.values():
            result = _search_json_for_price(value)
            if result is not None:
                return result
    elif isinstance(data, list):
        for item in data:
            result = _search_json_for_price(item)
            if result is not None:
                return result
    return None


def _search_json_for_name(data):
    """بيدور جوه أي JSON متداخل عن حقل اسم منتج معروف."""
    name_keys = ("title", "name", "productTitle")
    if isinstance(data, dict):
        for key in name_keys:
            if key in data and isinstance(data[key], str) and len(data[key]) > 3:
                return data[key]
        for value in data.values():
            result = _search_json_for_name(value)
            if result is not None:
                return result
    elif isinstance(data, list):
        for item in data:
            result = _search_json_for_name(item)
            if result is not None:
                return result
    return None


def fetch_price(url: str):
    """
    نقطة الدخول الرئيسية: بتوجّه الطلب لدالة الموقع المناسبة حسب اسم
    الدومين في اللينك. لسه بس نون مفعّلة، الباقي (جوميا/أمازون) TODO.
    """
    if "noon.com" in url:
        return fetch_price_noon(url)

    raise NotImplementedError(
        "الموقع ده لسه مش مدعوم. حالياً بس نون (noon.com) شغالة."
    )


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

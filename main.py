"""
بوت متابعة الأسعار - Telegram Price Tracker Bot
=================================================

الفكرة:
- المستخدم يبعت لينك منتج (أمازون / نون / جوميا / أي موقع)
- البوت يحفظ اللينك والسعر الحالي
- Job دوري (كل ساعة مثلاً) يفحص السعر تاني ويبعت تنبيه لو نزل
- المستخدم بيدفع اشتراك بنجوم تليجرام (Telegram Stars) عشان يقدر يتابع
  عدد منتجات أكبر من الحد المجاني

المتطلبات قبل التشغيل:
1. pip install python-telegram-bot --upgrade
2. اعمل بوت من BotFather على تليجرام وخد الـ Token
3. حط الـ Token في المتغير BOT_TOKEN تحت (أو في متغير بيئة TELEGRAM_BOT_TOKEN)
4. لسحب الأسعار الفعلي من المواقع، محتاج تضيف دالة scraping مناسبة
   لكل موقع (موجودة أماكنها محددة بـ TODO تحت) لأن كل موقع بنية HTML
   مختلفة، وبعض المواقع (زي أمازون) بيحتاج مكتبة زي requests + headers
   مناسبة أو خدمة scraping API جاهزة عشان ميحصلش حجب للـ IP.

هيكل قاعدة البيانات (SQLite بسيط عشان تبدأ بسرعة، تقدر تستبدلها بـ Postgres لاحقاً):
- users: telegram_id, tier (free/pro), tier_expires_at
- tracked_items: id, user_id, url, product_name, last_price, created_at
"""

import os
import sqlite3
import logging
from datetime import datetime, timedelta

from telegram import Update, LabeledPrice
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
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

FREE_TIER_LIMIT = 2          # عدد المنتجات المسموح بيها مجاناً
PRO_TIER_LIMIT = 20          # عدد المنتجات في الاشتراك المدفوع
PRO_PRICE_STARS = 150        # سعر الاشتراك الشهري بالنجوم (XTR)
CHECK_INTERVAL_SECONDS = 3600  # كل قد ايه يفحص الأسعار (ساعة هنا)


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
# دالة سحب السعر (المكان اللي محتاج تخصصه لكل موقع)
# ------------------------------------------------------------------
def fetch_price(url: str):
    """
    TODO: نفّذ منطق سحب السعر هنا.

    اقتراحات عملية:
    - لو هتعمل scraping مباشر: استخدم requests + BeautifulSoup،
      وحدد الـ CSS selector بتاع السعر لكل موقع على حدة (أمازون
      مختلف عن نون مختلف عن جوميا).
    - أفضل من كده لتفادي الحجب: استخدم خدمة scraping API جاهزة
      (ScraperAPI, ScrapingBee, Bright Data) بتتعامل مع الـ proxies
      والـ CAPTCHA نيابة عنك.
    - أمازون بالذات عنده Product Advertising API رسمي لو هتشتغل
      بشكل تجاري كبير.

    الدالة المفروض ترجع (product_name: str, price: float) أو None
    لو فشلت.
    """
    raise NotImplementedError("ضيف منطق سحب السعر الخاص بالموقع هنا")


# ------------------------------------------------------------------
# أوامر البوت
# ------------------------------------------------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    get_or_create_user(update.effective_user.id)
    await update.message.reply_text(
        "أهلاً بيك في بوت متابعة الأسعار!\n\n"
        "ابعتلي لينك أي منتج وهتابعلك سعره، وأبعتلك تنبيه فوراً لما ينزل.\n\n"
        f"الخطة المجانية: حتى {FREE_TIER_LIMIT} منتجات.\n"
        f"للاشتراك في خطة Pro ({PRO_TIER_LIMIT} منتج) استخدم /upgrade"
    )


async def track(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """يستقبل أي رسالة فيها لينك ويبدأ يتابعه."""
    url = update.message.text.strip()
    if not url.startswith("http"):
        await update.message.reply_text("ابعتلي لينك صحيح يبدأ بـ http أو https 🙂")
        return

    telegram_id = update.effective_user.id
    user = get_or_create_user(telegram_id)
    limit = PRO_TIER_LIMIT if is_pro(user) else FREE_TIER_LIMIT

    if user_item_count(telegram_id) >= limit:
        await update.message.reply_text(
            f"وصلت للحد الأقصى ({limit} منتج) في خطتك الحالية.\n"
            "استخدم /upgrade عشان تزود العدد."
        )
        return

    try:
        product_name, price = fetch_price(url)
    except NotImplementedError:
        await update.message.reply_text(
            "⚠️ لسه دالة سحب السعر مش متفعّلة لهذا الموقع. "
            "(ده مكان الكود اللي محتاج تضيفه بنفسك حسب كل موقع)"
        )
        return
    except Exception as e:
        logger.error(f"fetch_price failed: {e}")
        await update.message.reply_text("معرفتش أجيب سعر المنتج ده، جرب لينك تاني.")
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
        f"✅ بدأت أتابع: {product_name}\nالسعر الحالي: {price}\n"
        "هبعتلك تنبيه لو نزل."
    )


async def my_items(update: Update, context: ContextTypes.DEFAULT_TYPE):
    telegram_id = update.effective_user.id
    conn = get_db()
    items = conn.execute(
        "SELECT product_name, last_price, url FROM tracked_items WHERE user_id = ?",
        (telegram_id,),
    ).fetchall()
    conn.close()

    if not items:
        await update.message.reply_text("مفيش منتجات بتتابعها دلوقتي.")
        return

    text = "📦 المنتجات اللي بتتابعها:\n\n"
    for item in items:
        text += f"• {item['product_name']} — {item['last_price']}\n{item['url']}\n\n"
    await update.message.reply_text(text)


# ------------------------------------------------------------------
# الدفع بنجوم تليجرام (Telegram Stars)
# ------------------------------------------------------------------
async def upgrade(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """يبعت فاتورة دفع بالنجوم للترقية لخطة Pro."""
    prices = [LabeledPrice("اشتراك Pro لمدة شهر", PRO_PRICE_STARS)]
    await context.bot.send_invoice(
        chat_id=update.effective_chat.id,
        title="اشتراك Pro - متابعة الأسعار",
        description=f"تابع حتى {PRO_TIER_LIMIT} منتج لمدة شهر كامل",
        payload="pro_subscription_1_month",
        provider_token="",  # نجوم تليجرام ماتحتاجش provider token
        currency="XTR",     # XTR = عملة Telegram Stars
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
        "🎉 تم تفعيل اشتراك Pro! تقدر دلوقتي تتابع حتى "
        f"{PRO_TIER_LIMIT} منتج."
    )


# ------------------------------------------------------------------
# فحص الأسعار الدوري (Job)
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
                    f"🔻 السعر نزل!\n{item['product_name']}\n"
                    f"من {item['last_price']} إلى {new_price}\n{item['url']}"
                ),
            )
            conn = get_db()
            conn.execute(
                "UPDATE tracked_items SET last_price = ? WHERE id = ?",
                (new_price, item["id"]),
            )
            conn.commit()
            conn.close()


# ------------------------------------------------------------------
# التشغيل
# ------------------------------------------------------------------
def main():
    init_db()
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("items", my_items))
    app.add_handler(CommandHandler("upgrade", upgrade))
    app.add_handler(PreCheckoutQueryHandler(precheckout))
    app.add_handler(
        MessageHandler(filters.SUCCESSFUL_PAYMENT, successful_payment)
    )
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, track))

    app.job_queue.run_repeating(check_prices_job, interval=CHECK_INTERVAL_SECONDS)

    logger.info("Bot is running...")
    app.run_polling()


if __name__ == "__main__":
    main()

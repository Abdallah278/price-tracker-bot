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
import random
import asyncio
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
# لو عملت Volume على Railway بمسار /data، البيانات هتفضل محفوظة حتى
# بعد أي رفع كود جديد. لو مفيش Volume، هيشتغل عادي بس البيانات هتتمسح
# مع كل Redeploy زي ما كان بيحصل قبل كده.
DB_PATH = "/data/price_tracker.db" if os.path.isdir("/data") else "price_tracker.db"

FREE_TIER_LIMIT = 2
PRO_TIER_LIMIT = 20
PRO_PRICE_STARS = 150
CHECK_INTERVAL_SECONDS = 900  # 15 دقيقة (كانت ساعة)

# كل مرة نفحص فيها الأسعار، بنوزّع الطلبات على مدى عشوائي بين الرقمين
# دول (بالثواني) بدل ما نبعتهم كلهم مرة واحدة، عشان نقلل احتمال الحجب
SPREAD_MIN_SECONDS = 300   # 5 دقايق
SPREAD_MAX_SECONDS = 600   # 10 دقايق

# لو لينك معين فشل الفحص بيه العدد ده من المرات على التوالي، نعطّله
# تلقائياً عشان منستهلكش طلبات على حاجة واضح إنها باظت أو اتحجبت
MAX_FAIL_COUNT = 6

# صاحب البوت: معفي تلقائي من حد المنتجات وميحتاجش يدفع نجوم
OWNER_TELEGRAM_ID = 2057835002


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
            created_at TEXT,
            fail_count INTEGER DEFAULT 0,
            disabled INTEGER DEFAULT 0
        )
    """)
    # Migration: لو الجدول كان موجود من قبل (على الـ Volume القديم) من
    # غير الأعمدة الجديدة، نضيفها هنا بأمان (بنتجاهل الخطأ لو موجودة أصلاً)
    for column_def in ("fail_count INTEGER DEFAULT 0", "disabled INTEGER DEFAULT 0"):
        try:
            conn.execute(f"ALTER TABLE tracked_items ADD COLUMN {column_def}")
        except sqlite3.OperationalError:
            pass  # العمود موجود بالفعل
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
    # المالك دايماً معامل كـ Pro، من غير ما يحتاج يدفع أو يشترك
    if user_row["telegram_id"] == OWNER_TELEGRAM_ID:
        return True
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

    بيجرب 4 طرق بالترتيب (الأقوى للأضعف):
    1. JSON-LD (بيانات Schema.org اللي المواقع بتحطها لمحركات البحث)
    2. __NEXT_DATA__ (بيانات داخلية لو الموقع مبني بـ Next.js)
    3. Meta tags (og:price, product:price:amount)
    4. Regex مباشر على نص الصفحة الخام
    """
    response = requests.get(url, headers=HEADERS, timeout=15)
    logger.info(f"[noon] status={response.status_code} len={len(response.text)}")
    response.raise_for_status()
    html = response.text

    # تشخيص: نطبع أول 300 حرف من الصفحة عشان نتأكد هل رجعتلنا صفحة
    # المنتج الحقيقية ولا صفحة تحقق أمني (Challenge/CAPTCHA) من نون
    snippet = re.sub(r"\s+", " ", html[:300])
    logger.info(f"[noon] html_snippet={snippet}")

    soup = BeautifulSoup(html, "html.parser")

    # --- 1) JSON-LD ---
    for script in soup.find_all("script", type="application/ld+json"):
        if not script.string:
            continue
        try:
            data = json.loads(script.string)
        except json.JSONDecodeError:
            continue
        for node in data if isinstance(data, list) else [data]:
            if not isinstance(node, dict):
                continue
            if node.get("@type") == "Product":
                name = node.get("name")
                offers = node.get("offers")
                price = None
                if isinstance(offers, dict):
                    price = offers.get("price") or offers.get("lowPrice")
                elif isinstance(offers, list) and offers:
                    price = offers[0].get("price")
                if name and price is not None:
                    logger.info("[noon] matched via JSON-LD")
                    return name, float(price)

    # --- 2) __NEXT_DATA__ ---
    next_data_tag = soup.find("script", id="__NEXT_DATA__")
    if next_data_tag and next_data_tag.string:
        try:
            data = json.loads(next_data_tag.string)
            price = _search_json_for_price(data)
            name = _search_json_for_name(data)
            if price is not None and name is not None:
                logger.info("[noon] matched via __NEXT_DATA__")
                return name, price
        except (json.JSONDecodeError, KeyError):
            pass

    # --- 3) Meta tags ---
    price_meta = soup.find("meta", {"property": "product:price:amount"})
    name_meta = soup.find("meta", {"property": "og:title"})
    if price_meta and name_meta:
        try:
            logger.info("[noon] matched via meta tags")
            return name_meta["content"], float(price_meta["content"])
        except (ValueError, KeyError):
            pass

    # --- 4) Regex مباشر على النص الخام (آخر حل) ---
    price_match = re.search(r'"sellingPrice"\s*:\s*([\d.]+)', html)
    title_match = re.search(r'"title"\s*:\s*"([^"]{5,150})"', html)
    if price_match and title_match:
        logger.info("[noon] matched via regex fallback")
        return title_match.group(1), float(price_match.group(1))

    logger.warning(f"[noon] all methods failed for url={url}")
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


def fetch_price_amazon(url: str):
    """
    يسحب اسم المنتج وسعره من صفحة منتج على أمازون.

    ⚠️ ملاحظة مهمة: أمازون بيستخدم حماية قوية (زي DataDome/Akamai) وغالباً
    أشد من نون. الطلب المباشر هنا ممكن يتحجب أسرع، خصوصاً مع الاستخدام
    المتكرر. لو حصل حجب متكرر، برضو الحل الأعملي هو scraping API.
    """
    response = requests.get(url, headers=HEADERS, timeout=15)
    logger.info(f"[amazon] status={response.status_code} len={len(response.text)}")
    response.raise_for_status()
    html = response.text
    soup = BeautifulSoup(html, "html.parser")

    # --- 1) العناصر القياسية في صفحة منتج أمازون ---
    title_el = soup.select_one("#productTitle")
    price_el = soup.select_one(".a-price .a-offscreen")
    if title_el and price_el:
        try:
            name = title_el.get_text(strip=True)
            price_text = price_el.get_text(strip=True)
            price = float(re.sub(r"[^\d.]", "", price_text))
            logger.info("[amazon] matched via CSS selectors")
            return name, price
        except ValueError:
            pass

    # --- 2) JSON-LD (لو موجودة) ---
    for script in soup.find_all("script", type="application/ld+json"):
        if not script.string:
            continue
        try:
            data = json.loads(script.string)
        except json.JSONDecodeError:
            continue
        for node in data if isinstance(data, list) else [data]:
            if not isinstance(node, dict):
                continue
            if node.get("@type") == "Product":
                name = node.get("name")
                offers = node.get("offers")
                price = None
                if isinstance(offers, dict):
                    price = offers.get("price") or offers.get("lowPrice")
                elif isinstance(offers, list) and offers:
                    price = offers[0].get("price")
                if name and price is not None:
                    logger.info("[amazon] matched via JSON-LD")
                    return name, float(price)

    # --- 3) Meta tags ---
    price_meta = soup.find("meta", {"property": "product:price:amount"})
    name_meta = soup.find("meta", {"property": "og:title"})
    if price_meta and name_meta:
        try:
            logger.info("[amazon] matched via meta tags")
            return name_meta["content"], float(price_meta["content"])
        except (ValueError, KeyError):
            pass

    snippet = re.sub(r"\s+", " ", html[:300])
    logger.info(f"[amazon] html_snippet={snippet}")
    logger.warning(f"[amazon] all methods failed for url={url}")
    raise ValueError("معرفتش أستخرج السعر من صفحة أمازون دي")


def fetch_price(url: str):
    """
    نقطة الدخول الرئيسية: بتوجّه الطلب لدالة الموقع المناسبة حسب اسم
    الدومين في اللينك. حالياً نون وأمازون مفعّلين، جوميا لسه TODO.

    ملاحظة: أمازون بيستخدم كذا دومين مختصر (amzn.eu, amzn.to, a.co)
    غير amazon.com الأساسي، فبندور على أي واحد فيهم.
    """
    if "noon.com" in url:
        return fetch_price_noon(url)
    if any(domain in url for domain in ("amazon.", "amzn.", "a.co/")):
        return fetch_price_amazon(url)

    raise NotImplementedError(
        "الموقع ده لسه مش مدعوم. حالياً نون وأمازون شغالين بس."
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
            "➕ ضيف منتج", callback_data="menu_add",
            api_kwargs={"style": "success"},
        )],
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
            api_kwargs={"style": "primary"},
        )],
    ]
    return InlineKeyboardMarkup(keyboard)


def back_to_menu_keyboard():
    keyboard = [[InlineKeyboardButton(
        "⬅️ رجوع للقايمة الرئيسية", callback_data="menu_main",
        api_kwargs={"style": "primary"},
    )]]
    return InlineKeyboardMarkup(keyboard)


def platform_choice_keyboard():
    """قايمة اختيار المنصة قبل إدخال اللينك."""
    keyboard = [
        [InlineKeyboardButton(
            "🟠 أمازون", callback_data="platform_amazon",
            api_kwargs={"style": "success"},
        )],
        [InlineKeyboardButton(
            "🟡 نون", callback_data="platform_noon",
            api_kwargs={"style": "primary"},
        )],
        [InlineKeyboardButton(
            "🔀 الاتنين مع بعض", callback_data="platform_both",
            api_kwargs={"style": "primary"},
        )],
        [InlineKeyboardButton(
            "⬅️ رجوع", callback_data="menu_main",
        )],
    ]
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
            "SELECT product_name, last_price, url, disabled FROM tracked_items WHERE user_id = ?",
            (telegram_id,),
        ).fetchall()
        conn.close()

        if not items:
            text = "📭 مفيش منتجات بتتابعها دلوقتي.\n\n🔗 ابعتلي لينك منتج عشان تبدأ."
        else:
            text = "📦 *المنتجات اللي بتتابعها:*\n\n"
            for item in items:
                status = "⏸️ (متوقف مؤقتاً)" if item["disabled"] else ""
                text += f"• {item['product_name']} — 💰 {item['last_price']} {status}\n{item['url']}\n\n"
        await query.edit_message_text(
            text, parse_mode="Markdown", reply_markup=back_to_menu_keyboard()
        )

    elif query.data == "menu_add":
        await query.edit_message_text(
            "➕ *ضيف منتج جديد*\n\nاختار المنصة اللي عايز تتابع منتج منها:",
            parse_mode="Markdown",
            reply_markup=platform_choice_keyboard(),
        )

    elif query.data in ("platform_amazon", "platform_noon", "platform_both"):
        platform_names = {
            "platform_amazon": "أمازون",
            "platform_noon": "نون",
            "platform_both": "أمازون أو نون",
        }
        # بنسجل اختيار المستخدم مؤقتاً عشان نتأكد إن اللينك اللي هيبعته
        # فعلاً بتاع المنصة اللي اختارها
        context.user_data["awaiting_platform"] = query.data.replace("platform_", "")
        await query.edit_message_text(
            f"🔗 تمام، دلوقتي ابعتلي لينك المنتج من *{platform_names[query.data]}*.",
            parse_mode="Markdown",
            reply_markup=back_to_menu_keyboard(),
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

    # لو المستخدم كان اختار منصة معينة قبل كده (من زرار "ضيف منتج")،
    # نتأكد إن اللينك اللي بعته فعلاً بتاع نفس المنصة
    awaiting = context.user_data.pop("awaiting_platform", None)
    is_amazon_link = any(d in url for d in ("amazon.", "amzn.", "a.co/"))
    is_noon_link = "noon.com" in url
    if awaiting == "amazon" and not is_amazon_link:
        await update.message.reply_text(
            "⚠️ اللينك ده مش من أمازون. ابعت لينك أمازون صحيح 🙂",
            reply_markup=main_menu_keyboard(),
        )
        return
    if awaiting == "noon" and not is_noon_link:
        await update.message.reply_text(
            "⚠️ اللينك ده مش من نون. ابعت لينك نون صحيح 🙂",
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
        "SELECT product_name, last_price, url, disabled FROM tracked_items WHERE user_id = ?",
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
        status = "⏸️ (متوقف مؤقتاً)" if item["disabled"] else ""
        text += f"• {item['product_name']} — 💰 {item['last_price']} {status}\n{item['url']}\n\n"
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
async def check_prices_job(context: ContextTypes.DEFAULT_TYPE, spread: bool = True):
    conn = get_db()
    items = conn.execute(
        "SELECT * FROM tracked_items WHERE disabled = 0"
    ).fetchall()
    conn.close()

    if not items:
        return 0

    # نجمع اللينكات الفريدة بس، عشان لو أكتر من مستخدم بيتابع نفس
    # اللينك بالظبط، نفحصه مرة واحدة بس ونستخدم النتيجة للكل
    unique_urls = list({item["url"] for item in items})

    # بنوزّع الطلبات على مدى عشوائي بين 5 و10 دقايق بدل ما نبعتهم
    # كلهم مرة واحدة، عشان نقلل احتمال إن نون يحس إننا بوت.
    # لو spread=False (زي أمر /checknow اليدوي)، بنفحص فوراً من غير تأخير.
    if spread and len(unique_urls) > 1:
        total_spread = random.uniform(SPREAD_MIN_SECONDS, SPREAD_MAX_SECONDS)
        delay_per_url = total_spread / len(unique_urls)
    else:
        delay_per_url = 0

    price_cache = {}  # url -> (name, price) أو None لو فشل
    for i, url in enumerate(unique_urls):
        try:
            price_cache[url] = fetch_price(url)
        except Exception as e:
            logger.warning(f"[check_job] failed for {url}: {e}")
            price_cache[url] = None
        if i < len(unique_urls) - 1 and delay_per_url:
            await asyncio.sleep(delay_per_url)

    changed_count = 0
    conn = get_db()
    for item in items:
        result = price_cache.get(item["url"])

        if result is None:
            # فشل الفحص: نزود عداد الفشل، ولو وصل للحد الأقصى نعطّل
            # اللينك ده ونبلغ المستخدم مرة واحدة بس
            new_fail_count = item["fail_count"] + 1
            if new_fail_count >= MAX_FAIL_COUNT:
                conn.execute(
                    "UPDATE tracked_items SET fail_count = ?, disabled = 1 WHERE id = ?",
                    (new_fail_count, item["id"]),
                )
                conn.commit()
                await context.bot.send_message(
                    chat_id=item["user_id"],
                    text=(
                        f"⚠️ *وقفنا متابعة المنتج ده مؤقتاً:*\n📦 {item['product_name']}\n\n"
                        f"فشل الفحص {MAX_FAIL_COUNT} مرات على التوالي "
                        "(غالباً الموقع بيحجب الطلبات أو الرابط اتغير)."
                    ),
                    parse_mode="Markdown",
                )
            else:
                conn.execute(
                    "UPDATE tracked_items SET fail_count = ? WHERE id = ?",
                    (new_fail_count, item["id"]),
                )
                conn.commit()
            continue

        _, new_price = result

        if new_price < item["last_price"]:
            changed_count += 1
            await context.bot.send_message(
                chat_id=item["user_id"],
                text=(
                    f"🔻 *السعر نزل!*\n\n"
                    f"📦 {item['product_name']}\n"
                    f"💰 من {item['last_price']} ➡️ {new_price}\n{item['url']}"
                ),
                parse_mode="Markdown",
            )

        # في كل الحالات (نزل أو لأ) بنصفّر عداد الفشل لأن الفحص نجح
        conn.execute(
            "UPDATE tracked_items SET last_price = ?, fail_count = 0 WHERE id = ?",
            (new_price, item["id"]),
        )
        conn.commit()

    conn.close()
    return changed_count


# ------------------------------------------------------------------
# أوامر اختبار (المالك بس)
# ------------------------------------------------------------------
async def checknow(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """🔧 أمر خاص بالمالك: يجبر البوت يفحص كل الأسعار فوراً بدل ما يستنى
    الـ 15 دقيقة، عشان تقدر تختبر آلية التنبيهات بسرعة."""
    if update.effective_user.id != OWNER_TELEGRAM_ID:
        return
    await update.message.reply_text("⏳ بفحص كل الأسعار دلوقتي...")
    changed = await check_prices_job(context, spread=False)
    await update.message.reply_text(
        f"✅ خلصت الفحص. عدد الأسعار اللي نزلت: {changed}"
    )


async def simulate_drop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """🔧 أمر خاص بالمالك: يرفع السعر المحفوظ لمنتجاتك بنسبة 10% صناعياً،
    عشان لما نعمل /checknow بعده، البوت يكتشف "نزول" وهمي في السعر
    ويبعتلك التنبيه — كده تتأكد إن آلية التنبيهات شغالة من غير ما
    تستنى السعر الحقيقي يتغير فعلاً."""
    if update.effective_user.id != OWNER_TELEGRAM_ID:
        return
    conn = get_db()
    items = conn.execute(
        "SELECT id, last_price FROM tracked_items WHERE user_id = ?",
        (OWNER_TELEGRAM_ID,),
    ).fetchall()
    for item in items:
        fake_price = item["last_price"] * 1.10
        conn.execute(
            "UPDATE tracked_items SET last_price = ? WHERE id = ?",
            (fake_price, item["id"]),
        )
    conn.commit()
    conn.close()
    await update.message.reply_text(
        f"🧪 اترفع السعر المحفوظ صناعياً لـ {len(items)} منتج.\n"
        "دلوقتي استخدم /checknow عشان تشوف التنبيه الوهمي."
    )


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
    logger.info(f"Database path: {DB_PATH} (persistent={'/data' in DB_PATH})")
    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("items", my_items))
    app.add_handler(CommandHandler("upgrade", upgrade))
    app.add_handler(CommandHandler("checknow", checknow))
    app.add_handler(CommandHandler("simulate", simulate_drop))
    app.add_handler(CallbackQueryHandler(menu_callback, pattern="^(menu_|platform_)"))
    app.add_handler(PreCheckoutQueryHandler(precheckout))
    app.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, successful_payment))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, track))

    app.job_queue.run_repeating(check_prices_job, interval=CHECK_INTERVAL_SECONDS)

    logger.info("Bot is running...")
    app.run_polling()


if __name__ == "__main__":
    main()

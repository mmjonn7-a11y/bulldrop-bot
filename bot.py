import datetime
import functools
import logging
import time

import telebot
from telebot import types
from flask import Flask, request, abort
from bson import ObjectId
from bson.errors import InvalidId
from pymongo import MongoClient, DESCENDING
from pymongo.errors import ConnectionFailure, ServerSelectionTimeoutError

from config import (
    BOT_TOKEN, MONGO_URI, MONGO_DB_NAME, WEBHOOK_HOST, PORT, WEBHOOK_SECRET,
    ADMINS, ADMIN_USERNAME, CHANNELS, REFERRAL_REWARD,
)

# ============================== LOGGING ==============================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("bulldrop_bot")

# ============================== BOT & FLASK ==============================
bot = telebot.TeleBot(BOT_TOKEN, threaded=False, parse_mode="HTML")
app = Flask(__name__)

user_states = {}

try:
    BOT_USERNAME = bot.get_me().username
    log.info("Bot username aniqlandi: @%s", BOT_USERNAME)
except Exception as e:
    BOT_USERNAME = ""
    log.error("Bot username aniqlanmadi: %s", e)


# ============================== MONGODB ==============================
def connect_mongo(retries=5, delay=3):
    for attempt in range(1, retries + 1):
        try:
            client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000)
            client.admin.command("ping")
            log.info("MongoDB ulanish muvaffaqiyatli (urinish %d).", attempt)
            return client
        except (ConnectionFailure, ServerSelectionTimeoutError) as e:
            log.error("MongoDB ulanishda xato (urinish %d/%d): %s", attempt, retries, e)
            if attempt < retries:
                time.sleep(delay)
    log.critical("MongoDB ga ulanib bo'lmadi! MONGO_URI va Network Access (0.0.0.0/0) sozlamasini tekshiring.")
    raise SystemExit(1)


mongo_client = connect_mongo()
db = mongo_client[MONGO_DB_NAME]
users_col = db["users"]
promos_col = db["promo_codes"]
shop_col = db["shop_items"]


# ============================== XATOLARDAN HIMOYA ==============================
def safe_handler(func):
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except Exception as e:
            log.exception("Handler '%s' ichida xato: %s", func.__name__, e)
            try:
                target = args[0]
                chat_id = None
                if hasattr(target, "chat"):
                    chat_id = target.chat.id
                elif hasattr(target, "message"):
                    chat_id = target.message.chat.id
                if chat_id:
                    bot.send_message(chat_id, "⚠️ Kutilmagan xatolik yuz berdi. Iltimos, qayta urinib ko'ring.")
            except Exception:
                pass
    return wrapper


# ============================== FOYDALANUVCHI DB ==============================
def get_user(user_id):
    try:
        return users_col.find_one({"_id": user_id})
    except Exception as e:
        log.error("get_user xato: %s", e)
        return None


def register_user(user_id, username, ref_payload):
    """Yangi foydalanuvchini ro'yxatdan o'tkazadi, agar referal orqali kelgan bo'lsa bonus beradi."""
    try:
        existing = users_col.find_one({"_id": user_id})
        if existing:
            return existing, False  # allaqachon mavjud

        referred_by = None
        if ref_payload and ref_payload.isdigit():
            ref_id = int(ref_payload)
            if ref_id != user_id and users_col.find_one({"_id": ref_id}):
                referred_by = ref_id

        new_user = {
            "_id": user_id,
            "username": username or "",
            "balance": 0,
            "referred_by": referred_by,
            "referral_count": 0,
            "joined_date": datetime.datetime.utcnow(),
        }
        users_col.insert_one(new_user)

        if referred_by:
            users_col.update_one(
                {"_id": referred_by},
                {"$inc": {"balance": REFERRAL_REWARD, "referral_count": 1}},
            )
            try:
                bot.send_message(
                    referred_by,
                    f"🎉 Sizning taklifingiz bilan yangi foydalanuvchi qo'shildi!\n"
                    f"💰 +{REFERRAL_REWARD} token hisobingizga qo'shildi.",
                )
                send_fun_animation(referred_by, "🎯")
            except Exception:
                pass

        return new_user, True
    except Exception as e:
        log.error("register_user xato: %s", e)
        return None, False


def get_balance(user_id):
    user = get_user(user_id)
    return user.get("balance", 0) if user else 0


def change_balance(user_id, amount):
    try:
        users_col.update_one({"_id": user_id}, {"$inc": {"balance": amount}}, upsert=True)
        return True
    except Exception as e:
        log.error("change_balance xato: %s", e)
        return False


def get_users_count():
    try:
        return users_col.count_documents({})
    except Exception:
        return 0


def get_top_referrers(limit=10):
    try:
        return list(users_col.find({"referral_count": {"$gt": 0}}).sort("referral_count", DESCENDING).limit(limit))
    except Exception as e:
        log.error("get_top_referrers xato: %s", e)
        return []


def get_all_user_ids():
    try:
        return [u["_id"] for u in users_col.find({}, {"_id": 1})]
    except Exception as e:
        log.error("get_all_user_ids xato: %s", e)
        return []


# ============================== PROMOKODLAR (BEPUL) ==============================
def add_promo(code, description=""):
    try:
        promos_col.insert_one({
            "code": code,
            "description": description,
            "added_date": datetime.datetime.utcnow(),
        })
        return True
    except Exception as e:
        log.error("add_promo xato: %s", e)
        return False


def get_all_promos(limit=30):
    try:
        return list(promos_col.find().sort("added_date", DESCENDING).limit(limit))
    except Exception as e:
        log.error("get_all_promos xato: %s", e)
        return []


def delete_promo(promo_id):
    try:
        result = promos_col.delete_one({"_id": ObjectId(promo_id)})
        return result.deleted_count > 0
    except (InvalidId, Exception) as e:
        log.error("delete_promo xato: %s", e)
        return False


# ============================== DO'KON / SAVDO (TOKENGA) ==============================
def add_shop_item(name, price, content, stock):
    try:
        shop_col.insert_one({
            "name": name,
            "price": price,
            "content": content,
            "stock": stock,  # -1 = cheksiz
            "added_date": datetime.datetime.utcnow(),
        })
        return True
    except Exception as e:
        log.error("add_shop_item xato: %s", e)
        return False


def get_shop_items(limit=30):
    try:
        return list(shop_col.find({"$or": [{"stock": -1}, {"stock": {"$gt": 0}}]}).sort("added_date", DESCENDING).limit(limit))
    except Exception as e:
        log.error("get_shop_items xato: %s", e)
        return []


def get_all_shop_items_admin(limit=30):
    try:
        return list(shop_col.find().sort("added_date", DESCENDING).limit(limit))
    except Exception as e:
        log.error("get_all_shop_items_admin xato: %s", e)
        return []


def get_shop_item(item_id):
    try:
        return shop_col.find_one({"_id": ObjectId(item_id)})
    except Exception:
        return None


def delete_shop_item(item_id):
    try:
        result = shop_col.delete_one({"_id": ObjectId(item_id)})
        return result.deleted_count > 0
    except Exception as e:
        log.error("delete_shop_item xato: %s", e)
        return False


def buy_shop_item(user_id, item_id):
    """Xarid qilishga urinish. Qaytaradi: (muvaffaqiyat, xabar, item)"""
    item = get_shop_item(item_id)
    if not item:
        return False, "❌ Mahsulot topilmadi yoki o'chirilgan.", None

    balance = get_balance(user_id)
    if balance < item["price"]:
        return False, f"❌ Tokeningiz yetarli emas. Kerak: {item['price']} 🪙, sizda: {balance} 🪙", None

    if item["stock"] == 0:
        return False, "❌ Bu mahsulot tugagan.", None

    try:
        query = {"_id": item["_id"]}
        if item["stock"] > 0:
            query["stock"] = {"$gt": 0}
        update = {"$inc": {"stock": -1}} if item["stock"] > 0 else {}
        if update:
            result = shop_col.update_one(query, update)
            if result.modified_count == 0:
                return False, "❌ Mahsulot hozirgina tugadi. Boshqasini tanlang.", None

        change_balance(user_id, -item["price"])
        return True, "✅ Xarid muvaffaqiyatli amalga oshirildi!", item
    except Exception as e:
        log.error("buy_shop_item xato: %s", e)
        return False, "⚠️ Xatolik yuz berdi, qayta urinib ko'ring.", None


# ============================== YORDAMCHI ==============================
def is_admin(user_id):
    return user_id in ADMINS


def check_subscription(user_id):
    if is_admin(user_id):
        return True
    if not CHANNELS:
        return True
    for channel in CHANNELS:
        try:
            member = bot.get_chat_member(channel["id"], user_id)
            if member.status not in ["member", "administrator", "creator"]:
                return False
        except Exception as e:
            log.warning("Obunani tekshirishda xato (%s): %s — bot kanalda ADMIN ekanini tekshiring!", channel["id"], e)
            return False
    return True


def subscription_keyboard():
    markup = types.InlineKeyboardMarkup()
    for channel in CHANNELS:
        markup.add(types.InlineKeyboardButton(text=f"📢 {channel['name']}", url=channel["url"]))
    markup.add(types.InlineKeyboardButton(text="✅ Obuna bo'ldim", callback_data="check_sub"))
    return markup


def send_subscription_message(chat_id):
    try:
        bot.send_message(
            chat_id,
            "🔒 <b>Botdan foydalanish uchun quyidagi kanal(lar)ga obuna bo'ling!</b>\n\n"
            "Obuna bo'lgach, pastdagi \"✅ Obuna bo'ldim\" tugmasini bosing.",
            reply_markup=subscription_keyboard(),
        )
    except Exception as e:
        log.error("send_subscription_message xato: %s", e)


def main_menu_keyboard(user_id):
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    markup.add(
        types.KeyboardButton("🎁 Promokodlar"),
        types.KeyboardButton("🛒 Do'kon"),
    )
    markup.add(
        types.KeyboardButton("👤 Profilim"),
        types.KeyboardButton("🤝 Referal"),
    )
    markup.add(
        types.KeyboardButton("🏆 Reyting"),
        types.KeyboardButton("ℹ️ Yordam"),
    )
    if is_admin(user_id):
        markup.add(types.KeyboardButton("🛠 Admin panel"))
    return markup


def send_fun_animation(chat_id, emoji="🎰"):
    """Telegram'ning o'ziga xos animatsion emoji (dice) — stiker o'rnini bosadi.
    Haqiqiy stiker file_id har bir botga xos bo'lgani uchun oldindan bera olmaymiz,
    shuning uchun universal ishlaydigan animatsiyadan foydalanamiz."""
    try:
        bot.send_dice(chat_id, emoji=emoji)
    except Exception as e:
        log.warning("send_fun_animation xato: %s", e)


def referral_link(user_id):
    if BOT_USERNAME:
        return f"https://t.me/{BOT_USERNAME}?start={user_id}"
    return f"ID: {user_id}"


# ============================== /start ==============================
@bot.message_handler(commands=["start"])
@safe_handler
def cmd_start(message):
    user_id = message.from_user.id
    parts = message.text.split(maxsplit=1)
    ref_payload = parts[1].strip() if len(parts) > 1 else None

    user, is_new = register_user(user_id, message.from_user.username, ref_payload)

    if not check_subscription(user_id):
        send_subscription_message(message.chat.id)
        return

    send_fun_animation(message.chat.id, "🎰")
    welcome = "🎉 <b>Xush kelibsiz!</b>\n\n" if is_new else "👋 <b>Yana xush kelibsiz!</b>\n\n"
    bot.send_message(
        message.chat.id,
        f"{welcome}🐂 <b>BullDrop Promokod Bot</b>\n\n"
        "Bu yerda bepul promokodlarni olishingiz, referal orqali token "
        "to'plashingiz va do'konda ularni almashtirishingiz mumkin.\n\n"
        "🎁 Promokodlar — bepul kodlar\n"
        "🛒 Do'kon — tokenga mahsulot\n"
        "🤝 Referal — do'st taklif qil, token yig'\n"
        "🏆 Reyting — eng faollar\n\n"
        "Pastdagi tugmalardan birini tanlang 👇",
        reply_markup=main_menu_keyboard(user_id),
    )


@bot.callback_query_handler(func=lambda call: call.data == "check_sub")
@safe_handler
def callback_check_sub(call):
    user_id = call.from_user.id
    if check_subscription(user_id):
        try:
            bot.delete_message(call.message.chat.id, call.message.message_id)
        except Exception:
            pass
        bot.send_message(
            call.message.chat.id,
            "✅ Obuna tasdiqlandi! Xush kelibsiz 🎉",
            reply_markup=main_menu_keyboard(user_id),
        )
    else:
        bot.answer_callback_query(call.id, "❌ Siz hali barcha kanallarga obuna bo'lmadingiz!", show_alert=True)


# ============================== MENYU: PROMOKODLAR ==============================
@bot.message_handler(func=lambda m: m.text == "🎁 Promokodlar")
@safe_handler
def menu_promos(message):
    if not check_subscription(message.from_user.id):
        send_subscription_message(message.chat.id)
        return
    promos = get_all_promos()
    if not promos:
        bot.send_message(message.chat.id, "😔 Hozircha promokodlar mavjud emas. Keyinroq qayta tekshiring!")
        return
    lines = ["🎁 <b>Faol promokodlar:</b>\n"]
    for p in promos:
        desc = f" — {p['description']}" if p.get("description") else ""
        lines.append(f"🔑 <code>{p['code']}</code>{desc}")
    lines.append("\n💡 Kodni nusxalab, BullDrop saytida faollashtiring.")
    bot.send_message(message.chat.id, "\n".join(lines)[:4000])


# ============================== MENYU: DO'KON ==============================
@bot.message_handler(func=lambda m: m.text == "🛒 Do'kon")
@safe_handler
def menu_shop(message):
    if not check_subscription(message.from_user.id):
        send_subscription_message(message.chat.id)
        return
    items = get_shop_items()
    if not items:
        bot.send_message(message.chat.id, "😔 Hozircha do'konda mahsulot yo'q.")
        return
    balance = get_balance(message.from_user.id)
    markup = types.InlineKeyboardMarkup()
    for item in items:
        stock_text = "♾" if item["stock"] == -1 else str(item["stock"])
        label = f"{item['name']} — {item['price']}🪙 (qoldi: {stock_text})"
        markup.add(types.InlineKeyboardButton(text=label[:64], callback_data=f"buy:{item['_id']}"))
    bot.send_message(
        message.chat.id,
        f"🛒 <b>Do'kon</b>\n\n💰 Sizning balansingiz: <b>{balance} 🪙</b>\n\nXarid qilish uchun mahsulotni tanlang:",
        reply_markup=markup,
    )


@bot.callback_query_handler(func=lambda call: call.data.startswith("buy:"))
@safe_handler
def callback_buy_item(call):
    item_id = call.data.split(":", 1)[1]
    success, msg, item = buy_shop_item(call.from_user.id, item_id)
    bot.answer_callback_query(call.id, msg, show_alert=True)
    if success and item:
        send_fun_animation(call.message.chat.id, "🎰")
        bot.send_message(
            call.message.chat.id,
            f"🎉 <b>{item['name']}</b> xaridingiz tayyor!\n\n📦 Mazmuni:\n<code>{item['content']}</code>",
        )


# ============================== MENYU: PROFIL ==============================
@bot.message_handler(func=lambda m: m.text == "👤 Profilim")
@safe_handler
def menu_profile(message):
    user_id = message.from_user.id
    user = get_user(user_id) or {}
    balance = user.get("balance", 0)
    ref_count = user.get("referral_count", 0)
    joined = user.get("joined_date")
    joined_str = joined.strftime("%Y-%m-%d") if joined else "—"
    bot.send_message(
        message.chat.id,
        f"👤 <b>Profilingiz</b>\n\n"
        f"🆔 ID: <code>{user_id}</code>\n"
        f"💰 Balans: <b>{balance} 🪙</b>\n"
        f"🤝 Takliflar: <b>{ref_count} kishi</b>\n"
        f"📅 Ro'yxatdan o'tgan: {joined_str}",
    )


# ============================== MENYU: REFERAL ==============================
@bot.message_handler(func=lambda m: m.text == "🤝 Referal")
@safe_handler
def menu_referral(message):
    user_id = message.from_user.id
    user = get_user(user_id) or {}
    ref_count = user.get("referral_count", 0)
    link = referral_link(user_id)
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton(
        text="📤 Do'stlarga ulashish",
        url=f"https://t.me/share/url?url={link}&text=🐂 BullDrop botga qo'shil va bepul promokodlar ol!"
    ))
    bot.send_message(
        message.chat.id,
        f"🤝 <b>Referal tizimi</b>\n\n"
        f"Har bir taklif qilingan do'stingiz uchun <b>{REFERRAL_REWARD} 🪙 token</b> olasiz!\n\n"
        f"🔗 Sizning havolangiz:\n<code>{link}</code>\n\n"
        f"👥 Hozirgacha taklif qilganlaringiz: <b>{ref_count} kishi</b>\n"
        f"💰 Jami ishlangan: <b>{ref_count * REFERRAL_REWARD} 🪙</b>",
        reply_markup=markup,
    )


# ============================== MENYU: REYTING ==============================
@bot.message_handler(func=lambda m: m.text == "🏆 Reyting")
@safe_handler
def menu_top(message):
    top_users = get_top_referrers()
    if not top_users:
        bot.send_message(message.chat.id, "😔 Hozircha reyting bo'sh.")
        return
    medals = ["🥇", "🥈", "🥉"]
    lines = ["🏆 <b>Eng faol takliflar reytingi:</b>\n"]
    for i, u in enumerate(top_users):
        medal = medals[i] if i < 3 else f"{i+1}."
        name = f"@{u['username']}" if u.get("username") else f"ID {u['_id']}"
        lines.append(f"{medal} {name} — {u.get('referral_count', 0)} ta taklif")
    bot.send_message(message.chat.id, "\n".join(lines))


# ============================== MENYU: YORDAM ==============================
@bot.message_handler(func=lambda m: m.text == "ℹ️ Yordam")
@safe_handler
def menu_help(message):
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton(text="✉️ Admin bilan bog'lanish", url=f"https://t.me/{ADMIN_USERNAME}"))
    bot.send_message(
        message.chat.id,
        "ℹ️ <b>Yordam</b>\n\n"
        "🎁 Promokodlar — bepul BullDrop kodlari\n"
        "🛒 Do'kon — tokenga mahsulot/kod sotib olish\n"
        "🤝 Referal — do'st taklif qilib token yig'ish\n"
        "🏆 Reyting — eng faol foydalanuvchilar\n\n"
        "Savollar bo'lsa, admin bilan bog'laning.",
        reply_markup=markup,
    )


# ============================== TUGMALI ADMIN PANEL ==============================
def admin_menu_keyboard():
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(
        types.InlineKeyboardButton("➕ Promokod qo'shish", callback_data="adm:addpromo"),
        types.InlineKeyboardButton("🗑 Promokod o'chirish", callback_data="adm:delpromo"),
    )
    markup.add(
        types.InlineKeyboardButton("🛒 Mahsulot qo'shish", callback_data="adm:additem"),
        types.InlineKeyboardButton("🗑 Mahsulot o'chirish", callback_data="adm:delitem"),
    )
    markup.add(
        types.InlineKeyboardButton("💰 Token qo'shish/ayirish", callback_data="adm:token"),
        types.InlineKeyboardButton("📊 Statistika", callback_data="adm:stats"),
    )
    markup.add(
        types.InlineKeyboardButton("📢 Xabar yuborish", callback_data="adm:broadcast"),
    )
    return markup


def back_to_menu_keyboard():
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("⬅️ Orqaga", callback_data="adm:menu"))
    return markup


@bot.message_handler(commands=["admin"])
@safe_handler
def cmd_admin(message):
    if not is_admin(message.from_user.id):
        bot.send_message(message.chat.id, "⛔ Sizda admin huquqi yo'q.")
        return
    user_states.pop(message.from_user.id, None)
    bot.send_message(message.chat.id, "🛠 <b>Admin panel</b>\n\nKerakli bo'limni tanlang:", reply_markup=admin_menu_keyboard())


@bot.message_handler(func=lambda m: m.text == "🛠 Admin panel")
@safe_handler
def menu_admin_button(message):
    cmd_admin(message)


@bot.message_handler(commands=["cancel"])
@safe_handler
def cmd_cancel(message):
    if user_states.pop(message.from_user.id, None) is not None:
        bot.send_message(message.chat.id, "🚫 Amal bekor qilindi.")


@bot.callback_query_handler(func=lambda call: call.data.startswith("adm:"))
@safe_handler
def callback_admin_menu(call):
    user_id = call.from_user.id
    if not is_admin(user_id):
        bot.answer_callback_query(call.id, "⛔ Sizda admin huquqi yo'q.", show_alert=True)
        return

    action = call.data.split(":", 1)[1]
    chat_id = call.message.chat.id
    msg_id = call.message.message_id

    if action == "menu":
        user_states.pop(user_id, None)
        bot.edit_message_text("🛠 <b>Admin panel</b>\n\nKerakli bo'limni tanlang:", chat_id, msg_id,
                               reply_markup=admin_menu_keyboard())

    elif action == "addpromo":
        user_states[user_id] = {"step": "waiting_promo_code", "data": {}}
        bot.edit_message_text("🔑 Yangi promokodni kiriting (masalan: BULL2026):\n\n/cancel — bekor qilish",
                               chat_id, msg_id, reply_markup=back_to_menu_keyboard())

    elif action == "delpromo":
        promos = get_all_promos(15)
        if not promos:
            bot.edit_message_text("📃 Promokodlar mavjud emas.", chat_id, msg_id, reply_markup=back_to_menu_keyboard())
        else:
            markup = types.InlineKeyboardMarkup()
            for p in promos:
                markup.add(types.InlineKeyboardButton(f"🗑 {p['code']}", callback_data=f"delpromo:{p['_id']}"))
            markup.add(types.InlineKeyboardButton("⬅️ Orqaga", callback_data="adm:menu"))
            bot.edit_message_text("🗑 O'chirmoqchi bo'lgan promokodni tanlang:", chat_id, msg_id, reply_markup=markup)

    elif action == "additem":
        user_states[user_id] = {"step": "waiting_item_name", "data": {}}
        bot.edit_message_text("🛒 Mahsulot nomini kiriting:\n\n/cancel — bekor qilish",
                               chat_id, msg_id, reply_markup=back_to_menu_keyboard())

    elif action == "delitem":
        items = get_all_shop_items_admin(15)
        if not items:
            bot.edit_message_text("📃 Mahsulotlar mavjud emas.", chat_id, msg_id, reply_markup=back_to_menu_keyboard())
        else:
            markup = types.InlineKeyboardMarkup()
            for it in items:
                markup.add(types.InlineKeyboardButton(f"🗑 {it['name']} ({it['price']}🪙)", callback_data=f"delitem:{it['_id']}"))
            markup.add(types.InlineKeyboardButton("⬅️ Orqaga", callback_data="adm:menu"))
            bot.edit_message_text("🗑 O'chirmoqchi bo'lgan mahsulotni tanlang:", chat_id, msg_id, reply_markup=markup)

    elif action == "token":
        user_states[user_id] = {"step": "waiting_token_userid", "data": {}}
        bot.edit_message_text("💰 Foydalanuvchi ID sini kiriting:\n\n/cancel — bekor qilish",
                               chat_id, msg_id, reply_markup=back_to_menu_keyboard())

    elif action == "stats":
        text = (
            f"📊 <b>Statistika</b>\n\n"
            f"👥 Foydalanuvchilar: {get_users_count()}\n"
            f"🎁 Promokodlar: {promos_col.count_documents({})}\n"
            f"🛒 Mahsulotlar: {shop_col.count_documents({})}\n"
        )
        bot.edit_message_text(text, chat_id, msg_id, reply_markup=back_to_menu_keyboard())

    elif action == "broadcast":
        user_states[user_id] = {"step": "waiting_broadcast_text", "data": {}}
        bot.edit_message_text("📢 Barcha foydalanuvchilarga yuboriladigan xabar matnini kiriting:\n\n/cancel — bekor qilish",
                               chat_id, msg_id, reply_markup=back_to_menu_keyboard())

    bot.answer_callback_query(call.id)


@bot.callback_query_handler(func=lambda call: call.data.startswith("delpromo:"))
@safe_handler
def callback_delete_promo(call):
    if not is_admin(call.from_user.id):
        bot.answer_callback_query(call.id, "⛔ Ruxsat yo'q.", show_alert=True)
        return
    promo_id = call.data.split(":", 1)[1]
    ok = delete_promo(promo_id)
    bot.answer_callback_query(call.id, "✅ O'chirildi!" if ok else "❌ Topilmadi.")
    # ro'yxatni yangilash uchun qayta chizamiz
    promos = get_all_promos(15)
    markup = types.InlineKeyboardMarkup()
    for p in promos:
        markup.add(types.InlineKeyboardButton(f"🗑 {p['code']}", callback_data=f"delpromo:{p['_id']}"))
    markup.add(types.InlineKeyboardButton("⬅️ Orqaga", callback_data="adm:menu"))
    try:
        bot.edit_message_text(
            "🗑 O'chirmoqchi bo'lgan promokodni tanlang:" if promos else "📃 Promokodlar qolmadi.",
            call.message.chat.id, call.message.message_id, reply_markup=markup,
        )
    except Exception:
        pass


@bot.callback_query_handler(func=lambda call: call.data.startswith("delitem:"))
@safe_handler
def callback_delete_item(call):
    if not is_admin(call.from_user.id):
        bot.answer_callback_query(call.id, "⛔ Ruxsat yo'q.", show_alert=True)
        return
    item_id = call.data.split(":", 1)[1]
    ok = delete_shop_item(item_id)
    bot.answer_callback_query(call.id, "✅ O'chirildi!" if ok else "❌ Topilmadi.")
    items = get_all_shop_items_admin(15)
    markup = types.InlineKeyboardMarkup()
    for it in items:
        markup.add(types.InlineKeyboardButton(f"🗑 {it['name']} ({it['price']}🪙)", callback_data=f"delitem:{it['_id']}"))
    markup.add(types.InlineKeyboardButton("⬅️ Orqaga", callback_data="adm:menu"))
    try:
        bot.edit_message_text(
            "🗑 O'chirmoqchi bo'lgan mahsulotni tanlang:" if items else "📃 Mahsulotlar qolmadi.",
            call.message.chat.id, call.message.message_id, reply_markup=markup,
        )
    except Exception:
        pass


@bot.callback_query_handler(func=lambda call: call.data.startswith("bcast:"))
@safe_handler
def callback_broadcast_confirm(call):
    user_id = call.from_user.id
    state = user_states.get(user_id)
    if not is_admin(user_id) or not state or state.get("step") != "waiting_broadcast_confirm":
        bot.answer_callback_query(call.id, "⚠️ Bu amal muddati o'tgan.", show_alert=True)
        return

    decision = call.data.split(":", 1)[1]
    if decision == "no":
        user_states.pop(user_id, None)
        bot.edit_message_text("🚫 Xabar yuborish bekor qilindi.", call.message.chat.id, call.message.message_id,
                               reply_markup=back_to_menu_keyboard())
        bot.answer_callback_query(call.id)
        return

    text = state["data"]["text"]
    bot.edit_message_text("⏳ Xabar yuborilmoqda...", call.message.chat.id, call.message.message_id)
    success, fail = 0, 0
    for uid in get_all_user_ids():
        try:
            bot.send_message(uid, text)
            success += 1
        except Exception:
            fail += 1
    bot.edit_message_text(f"✅ Yuborildi: {success}\n❌ Yuborilmadi: {fail}",
                           call.message.chat.id, call.message.message_id, reply_markup=back_to_menu_keyboard())
    user_states.pop(user_id, None)
    bot.answer_callback_query(call.id)


# ============================== ADMIN MATN BOSQICHLARI ==============================
@bot.message_handler(content_types=["text"])
@safe_handler
def handle_text(message):
    user_id = message.from_user.id
    state = user_states.get(user_id)
    step = state.get("step") if state else None
    text = message.text.strip() if message.text else ""

    if not step or not is_admin(user_id):
        return  # oddiy menyu tugmalari yuqorida alohida ushlanadi, boshqasiga javob bermaymiz

    if text.startswith("/"):
        return

    # ---- PROMOKOD QO'SHISH ----
    if step == "waiting_promo_code":
        if add_promo(text):
            bot.send_message(message.chat.id, f"✅ Promokod qo'shildi: <code>{text}</code>", reply_markup=back_to_menu_keyboard())
        else:
            bot.send_message(message.chat.id, "❌ Xatolik yuz berdi.", reply_markup=back_to_menu_keyboard())
        user_states.pop(user_id, None)
        return

    # ---- MAHSULOT QO'SHISH: NOM ----
    if step == "waiting_item_name":
        state["data"]["name"] = text
        state["step"] = "waiting_item_price"
        bot.send_message(message.chat.id, "💰 Narxini kiriting (token miqdorida, masalan: 10):")
        return

    # ---- MAHSULOT QO'SHISH: NARX ----
    if step == "waiting_item_price":
        if not text.isdigit():
            bot.send_message(message.chat.id, "❗ Narx musbat butun son bo'lishi kerak. Qayta kiriting:")
            return
        state["data"]["price"] = int(text)
        state["step"] = "waiting_item_content"
        bot.send_message(message.chat.id, "📦 Xaridordan keyin yuboriladigan mazmunni kiriting (kod/matn):")
        return

    # ---- MAHSULOT QO'SHISH: MAZMUN ----
    if step == "waiting_item_content":
        state["data"]["content"] = text
        state["step"] = "waiting_item_stock"
        bot.send_message(message.chat.id, "📦 Miqdorini kiriting (cheksiz bo'lsa: -1, masalan: 5 yoki -1):")
        return

    # ---- MAHSULOT QO'SHISH: MIQDOR ----
    if step == "waiting_item_stock":
        try:
            stock = int(text)
        except ValueError:
            bot.send_message(message.chat.id, "❗ Miqdor butun son bo'lishi kerak (masalan: 5 yoki -1). Qayta kiriting:")
            return
        data = state["data"]
        ok = add_shop_item(data["name"], data["price"], data["content"], stock)
        if ok:
            bot.send_message(
                message.chat.id,
                f"✅ Mahsulot qo'shildi!\n\n🛒 {data['name']}\n💰 {data['price']} 🪙\n📦 Miqdor: {'♾' if stock == -1 else stock}",
                reply_markup=back_to_menu_keyboard(),
            )
        else:
            bot.send_message(message.chat.id, "❌ Xatolik yuz berdi.", reply_markup=back_to_menu_keyboard())
        user_states.pop(user_id, None)
        return

    # ---- TOKEN BOSHQARUVI: ID ----
    if step == "waiting_token_userid":
        if not text.isdigit():
            bot.send_message(message.chat.id, "❗ Foydalanuvchi ID raqam bo'lishi kerak. Qayta kiriting:")
            return
        state["data"]["target_id"] = int(text)
        state["step"] = "waiting_token_amount"
        bot.send_message(message.chat.id, "💰 Miqdorni kiriting (qo'shish uchun musbat, ayirish uchun manfiy, masalan: 10 yoki -5):")
        return

    # ---- TOKEN BOSHQARUVI: MIQDOR ----
    if step == "waiting_token_amount":
        try:
            amount = int(text)
        except ValueError:
            bot.send_message(message.chat.id, "❗ Butun son kiriting (masalan: 10 yoki -5). Qayta kiriting:")
            return
        target_id = state["data"]["target_id"]
        change_balance(target_id, amount)
        new_balance = get_balance(target_id)
        bot.send_message(
            message.chat.id,
            f"✅ <code>{target_id}</code> balansi yangilandi: {amount:+d} 🪙\n💰 Yangi balans: {new_balance} 🪙",
            reply_markup=back_to_menu_keyboard(),
        )
        try:
            bot.send_message(target_id, f"💰 Balansingizga o'zgarish kiritildi: {amount:+d} 🪙\nYangi balans: {new_balance} 🪙")
        except Exception:
            pass
        user_states.pop(user_id, None)
        return

    # ---- BROADCAST MATNI ----
    if step == "waiting_broadcast_text":
        state["data"]["text"] = text
        state["step"] = "waiting_broadcast_confirm"
        markup = types.InlineKeyboardMarkup()
        markup.add(
            types.InlineKeyboardButton("✅ Ha, yuborish", callback_data="bcast:yes"),
            types.InlineKeyboardButton("🚫 Bekor qilish", callback_data="bcast:no"),
        )
        bot.send_message(message.chat.id, f"📢 <b>Ushbu xabar barchaga yuborilsinmi?</b>\n\n{text}", reply_markup=markup)
        return


# ============================== FLASK WEBHOOK (RENDER UCHUN) ==============================
WEBHOOK_PATH = f"/webhook/{WEBHOOK_SECRET}"


@app.route(WEBHOOK_PATH, methods=["POST"])
def telegram_webhook():
    try:
        content_type = request.headers.get("content-type", "")
        if not content_type.startswith("application/json"):
            abort(403)
        json_str = request.get_data().decode("utf-8")
        update = telebot.types.Update.de_json(json_str)
        bot.process_new_updates([update])
        return "OK", 200
    except Exception as e:
        log.exception("Webhook so'rovni qayta ishlashda xato: %s", e)
        return "OK", 200


@app.route("/")
def index():
    return "🐂 BullDrop bot ishlayapti ✅", 200


@app.route("/health")
def health():
    try:
        mongo_client.admin.command("ping")
        mongo_status = "ok"
    except Exception as e:
        mongo_status = f"xato: {e}"
    return {"status": "running", "mongo": mongo_status, "admins": ADMINS}, 200


@app.errorhandler(404)
def not_found(e):
    return "Not found", 404


@app.errorhandler(500)
def server_error(e):
    log.exception("Flask ichki server xatosi: %s", e)
    return "Internal error", 500


def setup_webhook():
    if not WEBHOOK_HOST:
        log.warning("WEBHOOK_HOST aniqlanmadi (RENDER_EXTERNAL_URL yo'q). Render Web Service sifatida deploy qilinganiga ishonch hosil qiling.")
        return
    full_url = f"{WEBHOOK_HOST}{WEBHOOK_PATH}"
    for attempt in range(1, 4):
        try:
            bot.remove_webhook()
            time.sleep(1)
            bot.set_webhook(url=full_url)
            log.info("Webhook muvaffaqiyatli o'rnatildi: %s", full_url)
            return
        except Exception as e:
            log.error("Webhook o'rnatishda xato (urinish %d/3): %s", attempt, e)
            time.sleep(2)
    log.critical("Webhookni 3 marta urinishdan keyin ham o'rnatib bo'lmadi!")


setup_webhook()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT)

import os
import sys
import logging

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("config")

# ============================================================
#  SOZLAMALAR FAYLI — BullDrop Promokod Bot
#  Maxfiy narsalar (BOT_TOKEN, MONGO_URI) Render Environment
#  Variables orqali beriladi.
# ============================================================

def _get_env(name, default=None, required=False):
    value = os.getenv(name, default)
    if value is not None:
        value = str(value).strip()
    if required and not value:
        log.error("MUHIM XATO: '%s' environment variable topilmadi! Render → Environment bo'limida qo'shing.", name)
        sys.exit(1)
    return value


# ---- Majburiy qiymatlar ----
BOT_TOKEN = _get_env("BOT_TOKEN", required=True)
MONGO_URI = _get_env("MONGO_URI", required=True)
MONGO_DB_NAME = _get_env("MONGO_DB_NAME", default="bulldrop_bot")

WEBHOOK_HOST = _get_env("RENDER_EXTERNAL_URL") or _get_env("WEBHOOK_URL")
if WEBHOOK_HOST:
    WEBHOOK_HOST = WEBHOOK_HOST.rstrip("/")

PORT = int(_get_env("PORT", default="10000"))
WEBHOOK_SECRET = _get_env("WEBHOOK_SECRET", default="bulldrop_secret")

# ---- ADMIN(LAR) ----
DEFAULT_ADMINS = "8866852203"
_admins_raw = _get_env("ADMINS", default=DEFAULT_ADMINS)
ADMINS = []
for part in _admins_raw.split(","):
    part = part.strip()
    if part.isdigit():
        ADMINS.append(int(part))
if not ADMINS:
    log.warning("OGOHLANTIRISH: ADMINS bo'sh! Hech kim admin buyruqlaridan foydalana olmaydi.")

ADMIN_USERNAME = _get_env("ADMIN_USERNAME", default="admin")

# ---- MAJBURIY OBUNA KANALLARI ----
# Render'da CHANNELS="kanal1,kanal2" deb to'ldiring (@ belgisiz ham bo'ladi)
DEFAULT_CHANNELS = "kanal_username1,kanal_username2"
_channels_raw = _get_env("CHANNELS", default=DEFAULT_CHANNELS)
CHANNELS = []
for part in _channels_raw.split(","):
    part = part.strip()
    if part:
        username = part if part.startswith("@") else f"@{part}"
        CHANNELS.append({
            "id": username,
            "url": f"https://t.me/{username.lstrip('@')}",
            "name": username,
        })

# ---- REFERAL TIZIMI ----
REFERRAL_REWARD = int(_get_env("REFERRAL_REWARD", default="3"))  # har referalga necha token

log.info("Sozlamalar yuklandi: adminlar=%s | kanallar=%s | referal_bonus=%s",
         ADMINS, [c["id"] for c in CHANNELS], REFERRAL_REWARD)

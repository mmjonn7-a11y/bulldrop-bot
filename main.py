import asyncio
import os
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
import edge_tts
from aiohttp import web

# Token va Kanal yuzername'sini Render Environment Variables'dan olamiz
API_TOKEN = os.environ.get("BOT_TOKEN")
# Kanalingiz yuzername'si Render'da masalan @mening_kanalim ko'rinishida yoziladi
CHANNEL_ID = os.environ.get("https://t.me/veko_bulldrop") 

if not API_TOKEN:
    raise ValueError("Xatolik: BOT_TOKEN muhit o'zgaruvchisi topilmadi!")

bot = Bot(token=API_TOKEN)
dp = Dispatcher()

# 1. Kanalga a'zolikni tekshirish
async def is_subscribed(user_id: int) -> bool:
    if not CHANNEL_ID:
        return True # Agar kanal o'zgaruvchisi berilmagan bo'lsa, tekshirmasdan o'tkazadi
    try:
        member = await bot.get_chat_member(chat_id=CHANNEL_ID, user_id=user_id)
        return member.status in ["member", "administrator", "creator"]
    except Exception as e:
        print(f"A'zolikni tekshirishda xatolik: {e}")
        return False

# 2. /start buyrug'i
@dp.message(Command("start"))
async def start_cmd(message: types.Message):
    welcome = (
        f"👋 Salom, {message.from_user.first_name}!\n\n"
        f"Men har qanday matnni tabiiy o'zbekcha ovozga aylantirib beraman.\n"
        f"Menga biror matn yozib yuboring!"
    )
    if CHANNEL_ID:
        welcome += f"\n\n⚠️ Botdan foydalanish uchun kanalimizga a'zo bo'ling: {CHANNEL_ID}"
    await message.answer(welcome)

# 3. Matnni ovozga aylantirish qismi
@dp.message()
async def handle_text(message: types.Message):
    user_id = message.from_user.id
    text = message.text

    if len(text) > 500:
        await message.answer("❌ Matn juda uzun! Maksimal 500 ta harf yuboring.")
        return

    # Kanalni tekshirish
    if not await is_subscribed(user_id):
        await message.answer(f"❌ Bot bloklangan!\n\nFoydalanish uchun kanalimizga obuna bo'ling:\n{CHANNEL_ID}")
        return

    status_msg = await message.answer("⏳ Ovoz tayyorlanmoqda, iltimos kuting...")
    output_file = f"voice_{user_id}.mp3"
    
    try:
        # Edge-TTS orqali o'zbekcha ovoz yaratish
        communicate = edge_tts.Communicate(text, "uz-UZ-SardorNeural") 
        await communicate.save(output_file)
        
        voice_file = types.FSInputFile(output_file)
        await status_msg.delete()
        await bot.send_voice(chat_id=message.chat.id, voice=voice_file, reply_to_message_id=message.message_id)
        
    except Exception as e:
        await status_msg.edit_text("❌ Ovozlashtirishda xatolik yuz berdi.")
        print(f"Xatolik: {e}")
    finally:
        if os.path.exists(output_file):
            os.remove(output_file)

# 4. Render uchun soxta Web Server (o'chib qolmasligi uchun)
async def handle_web(request):
    return web.Response(text="Bot muvaffaqiyatli ishlayapti!")

async def main():
    # Telegram botni fonda ishga tushiramiz
    asyncio.create_task(dp.start_polling(bot))
    
    # Render talab qiladigan Portni eshitamiz
    app = web.Application()
    app.router.add_get('/', handle_web)
    
    port = int(os.environ.get("PORT", 8080))
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', port)
    await site.start()
    
    print("Bot va Server muvaffaqiyatli yoqildi...")
    while True:
        await asyncio.sleep(3600)

if __name__ == "__main__":
    asyncio.run(main())

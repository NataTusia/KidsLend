import os
import asyncio
import logging
import datetime
import requests
import psycopg2
import google.generativeai as genai
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import CommandStart
from aiogram.utils.keyboard import InlineKeyboardBuilder
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from aiohttp import web

# --- Налаштування ---
BOT_TOKEN = os.environ.get("BOT_TOKEN")
DATABASE_URL = os.environ.get("DATABASE_URL")
CHANNEL_ID = os.environ.get("CHANNEL_ID")
ADMIN_ID = int(os.environ.get("ADMIN_ID", 0))
UNSPLASH_KEY = os.environ.get("UNSPLASH_KEY")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")

# Налаштування AI
genai.configure(api_key=GEMINI_API_KEY)
# Використовуємо модель, яка точно працює у тебе
model = genai.GenerativeModel('gemini-flash-latest')

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# --- Допоміжна функція очистки тексту ---
def clean_text(text):
    """Прибирає зайві символи Markdown"""
    text = text.replace("**", "")  # Прибираємо жирні зірочки
    text = text.replace("### ", "") # Прибираємо заголовки
    text = text.replace("## ", "")
    return text

# --- 1. Функція генерації тексту (AI) ---
async def generate_ai_post(topic, context):
    """Просить AI написати пост."""
    prompt = (
        f"Ти SMM-менеджер. Напиши пост для Telegram українською мовою."
        f"\nТема: {topic}."
        f"\nКонтекст: {context}."
        f"\nВимоги:"
        f"\n1. ОБОВ'ЯЗКОВО: Довжина тексту ДО 950 символів (щоб вмістився в підпис фото)."
        f"\n2. Не використовуй символи ** або ##. Для жирного шрифту використовуй тільки тег <b>Текст</b>."
        f"\n3. Додай емодзі."
        f"\n4. Без вступу 'Ось пост', одразу текст."
    )
    try:
        response = model.generate_content(prompt)
        return clean_text(response.text)
    except Exception as e:
        logging.error(f"Помилка AI: {e}")
        return f"<b>{topic}</b>\n\n{context}"

# --- 2. Функція пошуку фото ---
async def get_random_photo(keywords):
    url = f"https://api.unsplash.com/photos/random?query={keywords}&client_id={UNSPLASH_KEY}&orientation=landscape"
    try:
        response = requests.get(url, timeout=10)
        if response.status_code == 200:
            return response.json()['urls']['regular']
    except Exception as e:
        logging.error(f"Помилка Unsplash: {e}")
    return "https://via.placeholder.com/800x600?text=No+Photo"

# --- 3. Основна логіка ---
async def prepare_draft(manual_day=None):
    day_now = manual_day if manual_day else datetime.datetime.now().day
    
    try:
        conn = psycopg2.connect(DATABASE_URL)
        cursor = conn.cursor()
        
        cursor.execute(
            "SELECT topic, content, photo_keywords FROM monthly_plan WHERE day_number = %s", 
            (day_now,)
        )
        result = cursor.fetchone()
        
        if result:
            topic, short_context, keywords = result
            
            # Генеруємо контент
            photo_url = await get_random_photo(keywords)
            full_post_text = await generate_ai_post(topic, short_context)
            
            # Формуємо заголовок чернетки
            caption = f"<b>📅 ЧЕРНЕТКА (День {day_now})</b>\n\n{full_post_text}"
            
            # Жорстка обрізка без зайвих слів, тільки якщо AI не послухався і написав дуже багато
            if len(caption) > 1020:
                caption = caption[:1015] + "..."
            
            builder = InlineKeyboardBuilder()
            builder.row(types.InlineKeyboardButton(text="✅ Опублікувати", callback_data="confirm_publish"))
            builder.row(types.InlineKeyboardButton(text="🔄 Інше фото", callback_data=f"regen_photo_{day_now}"))
            
            await bot.send_photo(
                chat_id=ADMIN_ID,
                photo=photo_url,
                caption=caption,
                reply_markup=builder.as_markup(),
                parse_mode="HTML"
            )
        else:
            await bot.send_message(ADMIN_ID, f"⚠️ У базі немає теми на день {day_now}.")
            
        cursor.close()
        conn.close()
    except Exception as e:
        logging.error(f"Помилка: {e}")

# --- Обробка команд ---
@dp.message(CommandStart())
async def cmd_start(message: types.Message):
    if message.from_user.id == ADMIN_ID:
        await message.answer("🤖 Генерую новий варіант...")
        await prepare_draft()

# Кнопка публікації
@dp.callback_query(F.data == "confirm_publish")
async def publish_to_channel(callback: types.CallbackQuery):
    caption = callback.message.html_text
    # Прибираємо слово "ЧЕРНЕТКА"
    clean_caption = caption.split("\n\n", 1)[1] if "ЧЕРНЕТКА" in caption else caption
    
    await bot.send_photo(
        chat_id=CHANNEL_ID,
        photo=callback.message.photo[-1].file_id,
        caption=clean_caption,
        parse_mode="HTML"
    )
    await callback.message.edit_caption(caption=f"✅ <b>ОПУБЛІКОВАНО</b>\n\n{clean_caption}", parse_mode="HTML")

# Кнопка "Інше фото" (Нова фішка, щоб ти могла поміняти картинку, якщо не сподобалась)
@dp.callback_query(F.data.startswith("regen_photo_"))
async def regen_photo(callback: types.CallbackQuery):
    day = int(callback.data.split("_")[2])
    await callback.message.answer("🔄 Шукаю інше фото...")
    await prepare_draft(manual_day=day)

# --- Сервер ---
async def handle(request): return web.Response(text="Bot Running")

async def main():
    logging.basicConfig(level=logging.INFO)
    app = web.Application()
    app.router.add_get("/", handle)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", int(os.environ.get("PORT", 10000))).start()
    
    scheduler = AsyncIOScheduler(timezone="Europe/Kyiv")
    scheduler.add_job(prepare_draft, 'cron', hour=9, minute=0)
    scheduler.start()
    
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
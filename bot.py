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

# Налаштування AI (Gemini)
genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel('gemini-flash-latest')

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# --- 1. Функція генерації тексту (AI) ---
async def generate_ai_post(topic, context):
    """Просить AI написати повноцінний пост на основі теми."""
    prompt = (
        f"Ти професійний SMM-менеджер для дитячого центру розвитку. "
        f"Напиши цікавий, корисний та емоційний пост для Instagram та Telegram українською мовою. "
        f"Тема посту: {topic}. "
        f"Ключова думка (контекст): {context}. "
        f"Вимоги: "
        f"1. Використовуй смайлики. "
        f"2. Структуруй текст (заголовок, основна частина, висновок). "
        f"3. Додай заклик до дії в кінці. "
        f"4. Додай 5-7 тематичних хештегів. "
        f"Текст має бути готовим до публікації, без зайвих слів на кшталт 'Ось ваш пост'."
    )
    try:
        response = model.generate_content(prompt)
        return response.text
    except Exception as e:
        logging.error(f"Помилка AI: {e}")
        return f"<b>{topic}</b>\n\n{context}\n\n(AI не зміг розширити текст, це базова версія)"

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

# --- 3. Основна логіка підготовки чернетки ---
async def prepare_draft(manual_day=None):
    day_now = manual_day if manual_day else datetime.datetime.now().day
    
    try:
        conn = psycopg2.connect(DATABASE_URL)
        cursor = conn.cursor()
        
        # Беремо "зерно" (тему) з бази
        cursor.execute(
            "SELECT topic, content, photo_keywords FROM monthly_plan WHERE day_number = %s", 
            (day_now,)
        )
        result = cursor.fetchone()
        
        if result:
            topic, short_context, keywords = result
            
            # 1. Шукаємо фото
            photo_url = await get_random_photo(keywords)
            
            # 2. Генеруємо довгий текст через AI
            full_post_text = await generate_ai_post(topic, short_context)
            
            # Формуємо повідомлення
            caption = f"<b>📅 ЧЕРНЕТКА (День {day_now})</b>\n\n{full_post_text}"
            
            # Якщо текст задовгий для підпису фото (ліміт Телеграм 1024), обрізаємо
            if len(caption) > 1000:
                caption = caption[:950] + "... (текст скорочено для прев'ю)"
            
            builder = InlineKeyboardBuilder()
            builder.row(types.InlineKeyboardButton(text="✅ Опублікувати", callback_data="confirm_publish"))
            
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
        await message.answer("🤖 Вмикаю режим копірайтера... Генерую пост...")
        await prepare_draft()

@dp.callback_query(F.data == "confirm_publish")
async def publish_to_channel(callback: types.CallbackQuery):
    caption = callback.message.html_text
    clean_caption = caption.split("\n\n", 1)[1] if "ЧЕРНЕТКА" in caption else caption
    
    await bot.send_photo(
        chat_id=CHANNEL_ID,
        photo=callback.message.photo[-1].file_id,
        caption=clean_caption,
        parse_mode="HTML"
    )
    await callback.message.edit_caption(caption=f"✅ <b>ОПУБЛІКОВАНО</b>\n\n{clean_caption}", parse_mode="HTML")

# --- Сервер ---
async def handle(request): return web.Response(text="AI Bot Running")

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
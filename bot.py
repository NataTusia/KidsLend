import os
import asyncio
import logging
import datetime
import time
import requests
import psycopg2
import google.generativeai as genai
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.types import InputMediaPhoto
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
model = genai.GenerativeModel('gemini-flash-latest')

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# --- Допоміжна функція очистки тексту ---
def clean_text(text):
    text = text.replace("**", "").replace("### ", "").replace("## ", "")
    return text

# --- 1. Функція генерації тексту (AI) ---
async def generate_ai_post(topic, context):
    prompt = (
        f"Ти SMM-менеджер. Напиши пост для Telegram українською мовою."
        f"\nТема: {topic}.\nКонтекст: {context}."
        f"\nВимоги: До 950 символів, використовуй <b>жирний</b>, додай емодзі."
        f"\nНапиши новий унікальний варіант."
    )
    try:
        response = model.generate_content(prompt)
        return clean_text(response.text)
    except Exception as e:
        return f"ERROR_AI: {str(e)}"

# --- 2. Функція пошуку фото ---
async def get_random_photo(keywords):
    # time.time() гарантує, що фото не береться з кешу
    url = f"https://api.unsplash.com/photos/random?query={keywords}&client_id={UNSPLASH_KEY}&orientation=landscape&count=1&t={int(time.time())}"
    try:
        response = requests.get(url, timeout=10)
        if response.status_code == 200:
            data = response.json()
            if isinstance(data, list):
                return data[0]['urls']['regular']
            return data['urls']['regular']
    except Exception as e:
        logging.error(f"Unsplash Error: {e}")
    return "https://via.placeholder.com/800x600?text=No+Photo"

# --- 3. Функція підключення до БД ---
def connect_to_db_with_retry():
    for i in range(3):
        try:
            conn = psycopg2.connect(DATABASE_URL)
            return conn
        except Exception as e:
            logging.warning(f"Спроба {i+1} невдала: {e}")
            if i < 2:
                time.sleep(5)
            else:
                raise e

# --- 4. Основна логіка ---
async def prepare_draft(manual_day=None, from_command=False):
    day_now = manual_day if manual_day else datetime.datetime.now().day
    
    try:
        conn = connect_to_db_with_retry()
        cursor = conn.cursor()
        
        cursor.execute("SELECT topic, content, photo_keywords FROM monthly_plan WHERE day_number = %s", (day_now,))
        result = cursor.fetchone()
        
        if result:
            topic, short_context, keywords = result
            
            # Повідомляємо тільки якщо це запуск по команді або розкладу
            if from_command:
                await bot.send_message(ADMIN_ID, f"🎨 Генерую пост на День {day_now}...")
            elif not manual_day:
                await bot.send_message(ADMIN_ID, "⏰ 9:00! Починаю роботу...")

            photo_url = await get_random_photo(keywords)
            full_post_text = await generate_ai_post(topic, short_context)
            
            if "ERROR_AI" in full_post_text:
                await bot.send_message(ADMIN_ID, f"🆘 <b>Збій AI:</b>\n{full_post_text}", parse_mode="HTML")
                return

            caption = f"<b>📅 ЧЕРНЕТКА (День {day_now})</b>\n\n{full_post_text}"
            if len(caption) > 1020: caption = caption[:1015] + "..."
            
            builder = InlineKeyboardBuilder()
            builder.row(types.InlineKeyboardButton(text="✅ Опублікувати", callback_data="confirm_publish"))
            builder.row(
                types.InlineKeyboardButton(text="🖼 Змінити фото", callback_data=f"regen_photo_{day_now}"),
                types.InlineKeyboardButton(text="📝 Переписати текст", callback_data=f"regen_text_{day_now}")
            )
            
            await bot.send_photo(chat_id=ADMIN_ID, photo=photo_url, caption=caption, reply_markup=builder.as_markup(), parse_mode="HTML")
        else:
            await bot.send_message(ADMIN_ID, f"⚠️ На сьогодні (День {day_now}) немає теми в базі!")
            
        cursor.close()
        conn.close()
    except Exception as e:
        logging.error(f"CRITICAL ERROR: {e}")
        await bot.send_message(ADMIN_ID, f"🆘 <b>Помилка:</b>\n{e}", parse_mode="HTML")

# --- Обробка команд ---

@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    if message.from_user.id == ADMIN_ID:
        await message.answer(
            "👋 <b>Привіт! Бот працює.</b>\n\n"
            "Доступні команди:\n"
            "/generate — 🎲 Створити пост на сьогодні вручну\n"
            "/start — 🔄 Перезапустити бота (цей текст)",
            parse_mode="HTML"
        )

@dp.message(Command("generate"))
async def cmd_generate(message: types.Message):
    if message.from_user.id == ADMIN_ID:
        # Запускаємо генерацію вручну
        await prepare_draft(from_command=True)

# --- Кнопки (Callback) ---

@dp.callback_query(F.data.startswith("regen_photo_"))
async def regen_photo_only(callback: types.CallbackQuery):
    day = int(callback.data.split("_")[2])
    await callback.answer("🔄 Нове фото...")
    try:
        conn = connect_to_db_with_retry()
        cursor = conn.cursor()
        cursor.execute("SELECT photo_keywords FROM monthly_plan WHERE day_number = %s", (day,))
        result = cursor.fetchone()
        cursor.close()
        conn.close()
        if result:
            keywords = result[0]
            new_photo_url = await get_random_photo(keywords)
            old_caption = callback.message.caption
            old_entities = callback.message.caption_entities
            media = InputMediaPhoto(media=new_photo_url, caption=old_caption, caption_entities=old_entities)
            await callback.message.edit_media(media=media, reply_markup=callback.message.reply_markup)
    except Exception as e:
        await callback.message.answer(f"Помилка: {e}")

@dp.callback_query(F.data.startswith("regen_text_"))
async def regen_text_only(callback: types.CallbackQuery):
    day = int(callback.data.split("_")[2])
    await callback.answer("📝 Новий текст...")
    try:
        conn = connect_to_db_with_retry()
        cursor = conn.cursor()
        cursor.execute("SELECT topic, content FROM monthly_plan WHERE day_number = %s", (day,))
        result = cursor.fetchone()
        cursor.close()
        conn.close()
        if result:
            topic, context = result
            new_text = await generate_ai_post(topic, context)
            new_caption = f"<b>📅 ЧЕРНЕТКА (День {day})</b>\n\n{new_text}"
            if len(new_caption) > 1020: new_caption = new_caption[:1015] + "..."
            await callback.message.edit_caption(caption=new_caption, parse_mode="HTML", reply_markup=callback.message.reply_markup)
    except Exception as e:
        await callback.message.answer(f"Помилка: {e}")

@dp.callback_query(F.data == "confirm_publish")
async def publish_to_channel(callback: types.CallbackQuery):
    caption = callback.message.html_text if callback.message.html_text else callback.message.caption
    clean_caption = caption
    if "ЧЕРНЕТКА" in caption:
         parts = caption.split("\n\n", 1)
         if len(parts) > 1: clean_caption = parts[1]
    
    await bot.send_photo(
        chat_id=CHANNEL_ID, 
        photo=callback.message.photo[-1].file_id, 
        caption=clean_caption, 
        caption_entities=callback.message.caption_entities
    )
    await callback.message.edit_caption(caption=f"✅ <b>ОПУБЛІКОВАНО</b>\n\n{clean_caption}", parse_mode="HTML")

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
    scheduler.add_job(prepare_draft, 'cron', hour=14, minute=0, misfire_grace_time=3600)
    scheduler.start()
    
    try:
        await bot.send_message(ADMIN_ID, "🟢 Бот оновлено! Додано команду /generate")
    except:
        pass

    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
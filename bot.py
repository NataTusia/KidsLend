import os
import asyncio
import logging
import datetime
import time
import requests
import psycopg2
import re
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

genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel('gemini-flash-latest')

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# --- Допоміжні функції ---
def clean_text(text):
    """Видаляє будь-які спроби форматування"""
    # Видаляємо Markdown
    text = text.replace("**", "").replace("### ", "").replace("## ", "")
    # Видаляємо HTML теги (на всяк випадок)
    clean = re.compile('<.*?>')
    text = re.sub(clean, '', text)
    return text.strip()

def connect_to_db_with_retry():
    for i in range(3):
        try:
            return psycopg2.connect(DATABASE_URL)
        except Exception as e:
            time.sleep(5)
            if i == 2: raise e

# --- 1. Логіка AI (ПРОСТИЙ ТЕКСТ) ---
async def generate_ai_post(topic, context, platform):
    if platform == "tg":
        role_desc = "Ти автор блогу в Telegram."
        # Прибрали вимогу про жирний шрифт
        requirements = "Стиль корисний, спокійний, експертний. Пиши звичайним текстом без виділень."
    else: # inst
        role_desc = "Ти Instagram-блогера."
        requirements = "Стиль емоційний. Структура: Хук -> Історія -> Користь -> Питання. Додай хештеги."

    prompt = (
        f"{role_desc} Напиши пост українською мовою.\n"
        f"Тема: {topic}.\nКонтекст: {context}.\n"
        f"Вимоги: {requirements}\n"
        f"ВАЖЛИВО: Не використовуй жодного форматування (ніяких ** або <b>). Просто чистий текст."
        f"Довжина тексту ДО 900 символів."
    )
    
    try:
        response = model.generate_content(prompt)
        return clean_text(response.text)
    except Exception as e:
        return f"ERROR_AI: {str(e)}"

# --- 2. Пошук фото ---
async def get_random_photo(keywords):
    url = f"https://api.unsplash.com/photos/random?query={keywords}&client_id={UNSPLASH_KEY}&orientation=landscape&count=1&t={int(time.time())}"
    try:
        response = requests.get(url, timeout=10)
        if response.status_code == 200:
            data = response.json()
            return data[0]['urls']['regular'] if isinstance(data, list) else data['urls']['regular']
    except Exception as e:
        logging.error(f"Unsplash Error: {e}")
    return "https://via.placeholder.com/800x600?text=No+Photo"

# --- 3. Основна функція ---
async def prepare_draft(platform, manual_day=None, from_command=False):
    day_now = manual_day if manual_day else datetime.datetime.now().day
    table_name = "telegram_posts" if platform == "tg" else "instagram_posts"
    platform_name = "Telegram" if platform == "tg" else "Instagram"
    
    try:
        conn = connect_to_db_with_retry()
        cursor = conn.cursor()
        cursor.execute(f"SELECT topic, content, photo_keywords FROM {table_name} WHERE day_number = %s", (day_now,))
        result = cursor.fetchone()
        
        if result:
            topic, short_context, keywords = result
            
            if from_command:
                await bot.send_message(ADMIN_ID, f"🎨 Генерую для {platform_name} (День {day_now})...")
            elif not manual_day:
                await bot.send_message(ADMIN_ID, f"⏰ Час посту для {platform_name}!")

            photo_url = await get_random_photo(keywords)
            full_post_text = await generate_ai_post(topic, short_context, platform)
            
            # Прибрали теги <b> з заголовка
            caption = f"📸 {platform_name.upper()} (День {day_now})\n\n{full_post_text}"
            
            if len(caption) > 1020: caption = caption[:1015] + "..."
            
            builder = InlineKeyboardBuilder()
            if platform == "tg":
                builder.row(types.InlineKeyboardButton(text="✅ Опублікувати в канал", callback_data="confirm_publish"))
            
            builder.row(
                types.InlineKeyboardButton(text="🖼 Інше фото", callback_data=f"photo_{platform}_{day_now}"),
                types.InlineKeyboardButton(text="📝 Інший текст", callback_data=f"text_{platform}_{day_now}")
            )
            
            # ВІДПРАВЛЯЄМО БЕЗ parse_mode (Це гарантує відсутність помилок тегів)
            await bot.send_photo(chat_id=ADMIN_ID, photo=photo_url, caption=caption, reply_markup=builder.as_markup())
            
        else:
            await bot.send_message(ADMIN_ID, f"⚠️ У таблиці {table_name} немає теми на день {day_now}!")
            
        cursor.close()
        conn.close()
    except Exception as e:
        await bot.send_message(ADMIN_ID, f"🆘 Помилка ({platform}): {e}")

# --- Обробка команд ---
@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    if message.from_user.id == ADMIN_ID:
        await message.answer("👋 KidsLand Bot (Текст без форматування)")

@dp.message(Command("generate_tg"))
async def cmd_gen_tg(message: types.Message):
    if message.from_user.id == ADMIN_ID:
        await prepare_draft(platform="tg", from_command=True)

@dp.message(Command("generate_inst"))
async def cmd_gen_inst(message: types.Message):
    if message.from_user.id == ADMIN_ID:
        await prepare_draft(platform="inst", from_command=True)

# --- Callbacks ---
@dp.callback_query(F.data.startswith("photo_"))
async def regen_photo(callback: types.CallbackQuery):
    _, platform, day = callback.data.split("_")
    day = int(day)
    table_name = "telegram_posts" if platform == "tg" else "instagram_posts"

    await callback.answer("🔄 Шукаю нове фото...")
    try:
        conn = connect_to_db_with_retry()
        cursor = conn.cursor()
        cursor.execute(f"SELECT photo_keywords FROM {table_name} WHERE day_number = %s", (day,))
        result = cursor.fetchone()
        cursor.close()
        conn.close()

        if result:
            new_photo_url = await get_random_photo(result[0])
            # Зберігаємо підпис
            media = InputMediaPhoto(media=new_photo_url, caption=callback.message.caption)
            await callback.message.edit_media(media=media, reply_markup=callback.message.reply_markup)
    except Exception as e:
        await callback.message.answer(f"Помилка: {e}")

@dp.callback_query(F.data.startswith("text_"))
async def regen_text(callback: types.CallbackQuery):
    _, platform, day = callback.data.split("_")
    day = int(day)
    table_name = "telegram_posts" if platform == "tg" else "instagram_posts"
    platform_name = "TELEGRAM" if platform == "tg" else "INSTAGRAM"

    await callback.answer("📝 Переписую текст...")
    try:
        conn = connect_to_db_with_retry()
        cursor = conn.cursor()
        cursor.execute(f"SELECT topic, content FROM {table_name} WHERE day_number = %s", (day,))
        result = cursor.fetchone()
        cursor.close()
        conn.close()

        if result:
            new_text = await generate_ai_post(result[0], result[1], platform)
            new_caption = f"📸 {platform_name} (День {day})\n\n{new_text}"
            
            if len(new_caption) > 1020: new_caption = new_caption[:1015] + "..."
            
            # Редагуємо БЕЗ parse_mode
            await callback.message.edit_caption(caption=new_caption, reply_markup=callback.message.reply_markup)
    except Exception as e:
        await callback.message.answer(f"Помилка: {e}")

@dp.callback_query(F.data == "confirm_publish")
async def publish_to_channel(callback: types.CallbackQuery):
    caption = callback.message.caption
    clean_caption = caption
    if "TELEGRAM" in caption:
         parts = caption.split("\n\n", 1)
         if len(parts) > 1: clean_caption = parts[1]
    
    # Публікуємо БЕЗ parse_mode
    await bot.send_photo(chat_id=CHANNEL_ID, photo=callback.message.photo[-1].file_id, caption=clean_caption)
    
    # Редагуємо повідомлення адміна (тут можна залишити HTML для галочки)
    await callback.message.edit_caption(caption=f"✅ <b>ОПУБЛІКОВАНО</b>\n\n{clean_caption}", parse_mode="HTML")

# --- Сервер ---
async def handle(request): return web.Response(text="KidsLand Bot Running")

async def main():
    logging.basicConfig(level=logging.INFO)
    app = web.Application()
    app.router.add_get("/", handle)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", int(os.environ.get("PORT", 10000))).start()
    
    scheduler = AsyncIOScheduler(timezone="Europe/Kyiv")
    scheduler.add_job(prepare_draft, 'cron', hour=9, minute=0, args=['tg'], misfire_grace_time=3600)
    scheduler.add_job(prepare_draft, 'cron', hour=9, minute=10, args=['inst'], misfire_grace_time=3600)
    scheduler.start()
    
    try:
        await bot.send_message(ADMIN_ID, "🟢 KidsLand: Готовий до роботи")
    except:
        pass

    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
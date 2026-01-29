import os
import asyncio
import logging
import datetime
import requests
import psycopg2
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import CommandStart
from aiogram.utils.keyboard import InlineKeyboardBuilder
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from aiohttp import web

# --- Налаштування середовища ---
BOT_TOKEN = os.environ.get("BOT_TOKEN")
DATABASE_URL = os.environ.get("DATABASE_URL")
CHANNEL_ID = os.environ.get("CHANNEL_ID")
ADMIN_ID = int(os.environ.get("ADMIN_ID", 0))
UNSPLASH_KEY = os.environ.get("UNSPLASH_KEY")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# --- Логіка роботи з контентом ---

async def get_random_photo(keywords):
    """Пошук професійного фото на Unsplash за ключовими словами."""
    url = f"https://api.unsplash.com/photos/random?query={keywords}&client_id={UNSPLASH_KEY}"
    try:
        response = requests.get(url, timeout=10)
        if response.status_code == 200:
            return response.json()['urls']['regular']
    except Exception as e:
        logging.error(f"Помилка Unsplash: {e}")
    # Резервне фото, якщо API не відповіло
    return "https://images.unsplash.com/photo-1503676260728-1c00da094a0b?q=80&w=1000"

async def prepare_draft():
    """Формування чернетки для адміна на основі поточного дня місяця."""
    # Отримуємо сьогоднішнє число
    day_now = datetime.datetime.now().day
    
    try:
        conn = psycopg2.connect(DATABASE_URL)
        cursor = conn.cursor()
        
        # Шукаємо контент у таблиці monthly_plan
        cursor.execute(
            "SELECT topic, content, photo_keywords FROM monthly_plan WHERE day_number = %s", 
            (day_now,)
        )
        result = cursor.fetchone()
        
        if result:
            topic, content, keywords = result
            photo_url = await get_random_photo(keywords)
            
            caption = f"<b>📅 ЧЕРНЕТКА (День {day_now})</b>\n\n" \
                      f"<b>{topic}</b>\n\n" \
                      f"{content}"
            
            # Кнопка для підтвердження публікації
            builder = InlineKeyboardBuilder()
            builder.row(types.InlineKeyboardButton(
                text="✅ Опублікувати в канал", 
                callback_data="confirm_publish"
            ))
            
            await bot.send_photo(
                chat_id=ADMIN_ID,
                photo=photo_url,
                caption=caption,
                reply_markup=builder.as_markup(),
                parse_mode="HTML"
            )
            logging.info(f"Чернетка на день {day_now} надіслана адміну.")
        else:
            await bot.send_message(ADMIN_ID, f"⚠️ Пост на {day_now}-е число не знайдено в базі.")
            
        cursor.close()
        conn.close()
    except Exception as e:
        logging.error(f"Помилка бази даних: {e}")

# --- Обробка взаємодії ---

@dp.message(CommandStart())
async def cmd_start(message: types.Message):
    """При натисканні /start адмін отримує чернетку на сьогодні (для тесту)."""
    if message.from_user.id == ADMIN_ID:
        await message.answer("Привіт! Генерую чернетку згідно з планом на сьогодні...")
        await prepare_draft()
    else:
        await message.answer("Вітаю! Бот працює в автоматичному режимі.")

@dp.callback_query(F.data == "confirm_publish")
async def publish_to_channel(callback: types.CallbackQuery):
    """Пересилка чернетки в основний канал."""
    try:
        # Отримуємо дані з повідомлення з кнопкою
        caption = callback.message.html_text
        # Видаляємо рядок "ЧЕРНЕТКА" для фінального посту
        clean_caption = caption.split("\n\n", 1)[1] if "ЧЕРНЕТКА" in caption else caption
        
        # Відправка фото + тексту в канал
        await bot.send_photo(
            chat_id=CHANNEL_ID,
            photo=callback.message.photo[-1].file_id,
            caption=clean_caption,
            parse_mode="HTML"
        )
        
        # Оновлюємо статус у адміна
        await callback.message.edit_caption(
            caption=f"✅ <b>ОПУБЛІКОВАНО В КАНАЛ</b>\n\n{clean_caption}",
            parse_mode="HTML"
        )
        await callback.answer("Пост успішно опубліковано!")
    except Exception as e:
        logging.error(f"Помилка публікації: {e}")
        await callback.answer("Помилка при відправці в канал.", show_alert=True)

# --- Технічна частина (Сервер та Планувальник) ---

async def handle(request):
    return web.Response(text="Bot is alive!")

async def main():
    logging.basicConfig(level=logging.INFO)
    
    # Веб-сервер для Render (порт 10000)
    app = web.Application()
    app.router.add_get("/", handle)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", int(os.environ.get("PORT", 10000)))
    await site.start()
    
    # Планувальник (надсилати чернетку адміну щодня о 09:00)
    scheduler = AsyncIOScheduler(timezone="Europe/Kyiv")
    scheduler.add_job(prepare_draft, 'cron', hour=9, minute=0)
    scheduler.start()
    
    logging.info("🚀 Бот запущений та чекає на команду /start або настання 09:00")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
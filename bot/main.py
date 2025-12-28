"""
Телеграм бот для размещения лимитных ордеров на Opinion.trade.

Алгоритм работы:
1. Команда /start - регистрация (кошелек, приватный ключ, API ключ)
2. Данные шифруются и сохраняются в SQLite
3. Команда /make_market - размещение ордера (логика из simple_flow.py)
"""

import asyncio

from aiogram import Bot, Dispatcher, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Message, BufferedInputFile
from aiogram_dialog import DialogManager, StartMode, setup_dialogs
from dotenv import load_dotenv

# Импортируем локальные модули
from config import settings
from database import (
    init_database,
    get_user,
    export_all_tables_to_zip
)
from spam_protection import AntiSpamMiddleware
from orders_dialog import orders_dialog, OrdersSG
from client_factory import setup_proxy
from sync_orders import async_sync_all_orders
from logger_config import setup_logger

# Загружаем переменные окружения
load_dotenv()

# Настройка логирования
logger = setup_logger("bot", "bot.log")

# Инициализация бота и диспетчера
bot = Bot(
    token=settings.bot_token,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML)
)
dp = Dispatcher(storage=MemoryStorage())
router = Router()

# ============================================================================
# Импорт routers
# ============================================================================

from start_router import start_router
from market_router import market_router


# ============================================================================
# Обработчики команд
# ============================================================================


@router.message(Command("get_db"))
async def cmd_get_db(message: Message):
    """Обработчик команды /get_db - экспорт всех таблиц базы данных в ZIP архив (только для администратора)."""
    # Проверяем права администратора
    if message.from_user.id != settings.admin_telegram_id:
        return
    
    try:
        # Экспортируем все таблицы в ZIP архив
        zip_content = await export_all_tables_to_zip()
        
        # Создаем файл для отправки
        zip_file = BufferedInputFile(
            zip_content,
            filename="database_export.zip"
        )
        
        await message.answer_document(
            document=zip_file,
            caption="📊 Database export (all tables)"
        )
        logger.info(f"Администратор {message.from_user.id} экспортировал базу данных")
    except Exception as e:
        logger.error(f"Ошибка экспорта базы данных: {e}")
        await message.answer(f"""❌ Error exporting database: {e}""")


@router.message(Command("orders"))
async def cmd_orders(message: Message, dialog_manager: DialogManager):
    """Обработчик команды /orders - просмотр ордеров пользователя."""
    # Проверяем, зарегистрирован ли пользователь
    user = await get_user(message.from_user.id)
    if not user:
        await message.answer(
            """❌ You are not registered. Use /start to register first."""
        )
        return
    
    # Сохраняем telegram_id в start_data для использования в диалоге
    telegram_id = message.from_user.id
    
    # Запускаем диалог с передачей telegram_id
    # Пагинация будет сброшена автоматически при запуске диалога
    await dialog_manager.start(OrdersSG.orders_list, data={"telegram_id": telegram_id}, mode=StartMode.RESET_STACK)


# ============================================================================
# Общий обработчик для всех сообщений (заглушка)
# ============================================================================

@router.message()
async def handle_unknown_message(message: Message):
    """
    Обработчик для всех сообщений, которые не попали в другие хендлеры.
    Отвечает стандартным сообщением с инструкцией.
    """
    await message.answer(
        """Use the /make_market command to start a new farm.
Use the /orders command to manage your orders."""
    )


# ============================================================================
# Главная функция
# ============================================================================

async def background_sync_task():
    """Фоновая задача для периодической синхронизации ордеров."""
    # Ждем 30 секунд после старта бота перед первой синхронизацией
    await asyncio.sleep(30)
    
    # Интервал синхронизации: 60 секунд (1 минута)
    SYNC_INTERVAL = 60
    
    while True:
        try:
            await async_sync_all_orders(bot)
        except Exception as e:
            logger.error(f"Error in background sync task: {e}")
        
        # Ждем перед следующей синхронизацией
        await asyncio.sleep(SYNC_INTERVAL)


async def main():
    """Главная функция запуска бота."""
    # Настраиваем прокси для всех API запросов (если указан в настройках)
    setup_proxy()
    
    # Инициализируем базу данных
    await init_database()
    
    # Регистрируем middleware для антиспама (глобально)
    dp.message.middleware(AntiSpamMiddleware(bot=bot))
    dp.callback_query.middleware(AntiSpamMiddleware(bot=bot))
    
    # Регистрируем диалоги
    dp.include_router(orders_dialog)
    
    # Настраиваем диалоги
    setup_dialogs(dp)
    
    # Регистрируем роутеры
    dp.include_router(start_router)  # User registration router
    dp.include_router(market_router)  # Market order placement router
    dp.include_router(router)  # Main router (orders, get_db, etc.)
    
    # Запускаем фоновую задачу синхронизации ордеров
    asyncio.create_task(background_sync_task())
    logger.info("Background sync task started")
    
    # Отправляем сообщение админу при старте (если указан)
    if settings.admin_telegram_id and settings.admin_telegram_id != 0:
        try:
            await bot.send_message(
                chat_id=settings.admin_telegram_id,
                text="✅ Bot started successfully"
            )
            logger.info(f"Startup notification sent to admin {settings.admin_telegram_id}")
        except Exception as e:
            logger.warning(f"Failed to send startup notification to admin: {e}")
    
    logger.info("Бот запущен")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())

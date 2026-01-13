"""
Телеграм бот для размещения лимитных ордеров на Opinion.trade.

Алгоритм работы:
1. Команда /start - регистрация (кошелек, приватный ключ, API ключ)
2. Данные шифруются и сохраняются в SQLite
3. Команда /make_market - размещение ордера (логика из simple_flow.py)
"""

import asyncio
import logging
from typing import Optional

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram_dialog import setup_dialogs
from dotenv import load_dotenv
from middlewares.spam_protection import AntiSpamMiddleware
from middlewares.typing_middleware import TypingMiddleware
from opinion.sync_orders import async_sync_all_orders
from opinion.websocket_test import WebSocketTestMonitor

# from opinion.websocket_sync import WebSocketOrderSync, set_websocket_sync
from routers.account import account_router
from routers.admin import admin_router
from routers.make_market import market_router
from routers.orders import orders_manage_router
from routers.orders_dialog import orders_dialog
from routers.plug import plug_router
from routers.start import start_router
from routers.users import user_router
from service.config import settings
from service.database import init_database
from service.logger_config import setup_root_logger
from service.proxy_checker import async_check_all_proxies

# Загружаем переменные окружения
load_dotenv()

# Настройка логирования
# Настраиваем корневой логгер - все модули будут логировать в logs/bot.log
setup_root_logger("bot.log")
logger = logging.getLogger(__name__)

# Инициализация бота и диспетчера
bot = Bot(
    token=settings.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML)
)
dp = Dispatcher(storage=MemoryStorage())


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


async def background_proxy_check_task():
    """Фоновая задача для периодической проверки прокси."""
    PROXY_CHECK_INTERVAL = 600  # 10 минут

    while True:
        try:
            await async_check_all_proxies(bot)
        except Exception as e:
            logger.error(f"Error in background proxy check task: {e}")

        # Ждем перед следующей проверкой
        await asyncio.sleep(PROXY_CHECK_INTERVAL)


async def main():
    """Главная функция запуска бота."""
    # Инициализируем базу данных
    await init_database()

    # Регистрируем middleware для антиспама (глобально)
    dp.message.middleware(AntiSpamMiddleware(bot=bot))
    dp.callback_query.middleware(AntiSpamMiddleware(bot=bot))

    # Регистрируем middleware для действия печатания (глобально)
    dp.message.middleware(TypingMiddleware(bot=bot))
    dp.callback_query.middleware(TypingMiddleware(bot=bot))

    # Регистрируем диалоги
    dp.include_router(orders_dialog)

    # Настраиваем диалоги
    setup_dialogs(dp)

    # Регистрируем роутеры
    dp.include_router(start_router)  # User registration router
    dp.include_router(account_router)  # Account management router
    dp.include_router(market_router)  # Market order placement router
    dp.include_router(orders_manage_router)  # Orders management router
    dp.include_router(
        user_router
    )  # User commands router (help, support, check_account)
    dp.include_router(admin_router)  # Admin commands router
    dp.include_router(plug_router)  # Fallback router (unknown message handler)

    # Запускаем фоновую задачу синхронизации ордеров
    asyncio.create_task(background_sync_task())
    logger.info("Background sync task started")

    # Запускаем фоновую задачу проверки прокси
    asyncio.create_task(background_proxy_check_task())
    logger.info("Background proxy check task started")

    # Запускаем тестовый монитор WebSocket (для отладки)
    websocket_test: Optional[WebSocketTestMonitor] = None
    try:
        websocket_test = WebSocketTestMonitor(bot)
        await websocket_test.start()
        logger.info("WebSocket test monitor started")
    except Exception as e:
        logger.error(f"Ошибка при запуске тестового монитора WebSocket: {e}")

    # Запускаем WebSocket менеджер синхронизации ордеров (закомментировано)
    # websocket_sync = WebSocketOrderSync(bot)
    # set_websocket_sync(websocket_sync)  # Устанавливаем глобальный экземпляр
    # await websocket_sync.start()
    # logger.info("WebSocket sync manager started")

    # Отправляем сообщение админу при старте (если указан)
    if settings.admin_telegram_id and settings.admin_telegram_id != 0:
        try:
            await bot.send_message(
                chat_id=settings.admin_telegram_id, text="✅ Bot started successfully"
            )
            logger.info(
                f"Startup notification sent to admin {settings.admin_telegram_id}"
            )
        except Exception as e:
            logger.warning(f"Failed to send startup notification to admin: {e}")

    logger.info("Бот запущен")
    try:
        await dp.start_polling(bot)
    finally:
        # Останавливаем тестовый монитор WebSocket при завершении работы
        if websocket_test:
            await websocket_test.stop()
            logger.info("WebSocket test monitor stopped")

        # Останавливаем WebSocket менеджер при завершении работы (закомментировано)
        # if websocket_sync:
        #     await websocket_sync.stop()
        #     logger.info("WebSocket sync manager stopped")


if __name__ == "__main__":
    asyncio.run(main())

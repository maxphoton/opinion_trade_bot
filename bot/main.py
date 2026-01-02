"""
Телеграм бот для размещения лимитных ордеров на Opinion.trade.

Алгоритм работы:
1. Команда /start - регистрация (кошелек, приватный ключ, API ключ)
2. Данные шифруются и сохраняются в SQLite
3. Команда /make_market - размещение ордера (логика из simple_flow.py)
"""

import asyncio
import logging

from aiogram import Bot, Dispatcher, Router, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Message, BufferedInputFile, CallbackQuery
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram_dialog import DialogManager, StartMode, setup_dialogs
from dotenv import load_dotenv

# Импортируем локальные модули
from config import settings
from database import (
    init_database,
    get_user,
    delete_user,
    export_all_tables_to_zip
)
from invites import get_unused_invites, get_invites_statistics
from spam_protection import AntiSpamMiddleware
from orders_dialog import orders_dialog, OrdersSG
from client_factory import setup_proxy
from sync_orders import async_sync_all_orders
from logger_config import setup_root_logger
from start_router import start_router
from market_router import market_router
from help_text import HELP_TEXT, HELP_TEXT_ENG, HELP_TEXT_CN

# Загружаем переменные окружения
load_dotenv()

# Настройка логирования
# Настраиваем корневой логгер - все модули будут логировать в logs/bot.log
setup_root_logger("bot.log")
logger = logging.getLogger(__name__)

# Инициализация бота и диспетчера
bot = Bot(
    token=settings.bot_token,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML)
)
dp = Dispatcher(storage=MemoryStorage())
router = Router()


# ============================================================================
# States for support command
# ============================================================================

class SupportStates(StatesGroup):
    """States for support message."""
    waiting_support_message = State()


class DeleteUserStates(StatesGroup):
    """States for delete user command."""
    waiting_telegram_id = State()


# ============================================================================
# Обработчики команд
# ============================================================================


@router.message(Command("get_db"))
async def cmd_get_db(message: Message):
    """Обработчик команды /get_db - экспорт всех таблиц базы данных в ZIP архив (только для администратора)."""
    logger.info(f"Команда /get_db от пользователя {message.from_user.id}")
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


@router.message(Command("get_invites"))
async def cmd_get_invites(message: Message):
    """Обработчик команды /get_invites - получение 10 неиспользованных инвайтов (только для администратора)."""
    logger.info(f"Команда /get_invites от пользователя {message.from_user.id}")
    # Проверяем права администратора
    if message.from_user.id != settings.admin_telegram_id:
        return
    
    try:
        # Получаем статистику
        stats = await get_invites_statistics()
        
        # Получаем 10 неиспользованных инвайтов (создаст новые если нужно)
        invites = await get_unused_invites(10)
        
        # Формируем сообщение со статистикой и инвайтами
        stats_text = f"""📊 <b>Invites Statistics:</b>
• Total: {stats['total']}
• Used: {stats['used']}
• Unused: {stats['unused']}

📋 <b>10 Unused Invites (ID - Code):</b>
"""
        
        invites_list = []
        for invite in invites:
            invites_list.append(f"{invite['id']} - <code>{invite['invite']}</code>")
        
        invites_text = "\n".join(invites_list)
        
        full_message = stats_text + invites_text
        
        await message.answer(full_message)
        logger.info(f"Администратор {message.from_user.id} получил список инвайтов")
    except Exception as e:
        logger.error(f"Ошибка получения инвайтов: {e}")
        await message.answer(f"""❌ Error getting invites: {e}""")


@router.message(Command("delete_user"))
async def cmd_delete_user(message: Message, state: FSMContext):
    """Обработчик команды /delete_user - удаление пользователя из БД (только для администратора)."""
    logger.info(f"Команда /delete_user от пользователя {message.from_user.id}")
    # Проверяем права администратора
    if message.from_user.id != settings.admin_telegram_id:
        return
    
    await message.answer(
        """🗑️ <b>Delete User</b>
Please enter the Telegram ID of the user you want to delete.
The user and all their orders will be removed from the database, allowing them to register again."""
    )
    await state.set_state(DeleteUserStates.waiting_telegram_id)


@router.message(DeleteUserStates.waiting_telegram_id)
async def process_delete_user_telegram_id(message: Message, state: FSMContext):
    """Обработчик ввода Telegram ID для удаления пользователя."""
    # Проверяем права администратора
    if message.from_user.id != settings.admin_telegram_id:
        await state.clear()
        return
    
    try:
        # Пытаемся преобразовать введенный текст в число
        telegram_id = int(message.text.strip())
    except ValueError:
        await message.answer(
            """❌ Invalid Telegram ID format. Please enter a numeric ID.
Example: 123456789

Please try again:"""
        )
        return
    
    # Проверяем, существует ли пользователь
    user = await get_user(telegram_id)
    if not user:
        await message.answer(
            f"""❌ User with Telegram ID <code>{telegram_id}</code> not found in database.
Please check the ID and try again:"""
        )
        await state.clear()
        return
    
    # Удаляем пользователя
    try:
        deleted = await delete_user(telegram_id)
        if deleted:
            username = user.get('username', 'N/A')
            await message.answer(
                f"""✅ User deleted successfully!

📋 <b>Deleted User Info:</b>
• Telegram ID: <code>{telegram_id}</code>
• Username: @{username if username != 'N/A' else 'N/A'}

The user and all their orders have been removed from the database.
They can now register again using /start."""
            )
            logger.info(f"Администратор {message.from_user.id} удалил пользователя {telegram_id}")
        else:
            await message.answer(
                f"""❌ Failed to delete user with Telegram ID <code>{telegram_id}</code>.
Please try again:"""
            )
    except Exception as e:
        logger.error(f"Ошибка при удалении пользователя {telegram_id}: {e}")
        await message.answer(
            f"""❌ Error deleting user: {e}
Please try again:"""
        )
    finally:
        await state.clear()


@router.message(Command("orders"))
async def cmd_orders(message: Message, dialog_manager: DialogManager):
    """Обработчик команды /orders - просмотр ордеров пользователя."""
    logger.info(f"Команда /orders от пользователя {message.from_user.id}")
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


@router.message(Command("help"))
async def cmd_help(message: Message):
    """Обработчик команды /help - инструкция по работе с ботом."""
    logger.info(f"Команда /help от пользователя {message.from_user.id}")
    
    # Создаем клавиатуру с кнопками выбора языка
    builder = InlineKeyboardBuilder()
    builder.button(text="🇷🇺 Русский", callback_data="help_lang_ru")
    builder.button(text="🇬🇧 English", callback_data="help_lang_eng")
    builder.button(text="🇨🇳 中文", callback_data="help_lang_cn")
    builder.adjust(3)
    
    await message.answer(HELP_TEXT_ENG, parse_mode="HTML", reply_markup=builder.as_markup())


@router.callback_query(F.data.startswith("help_lang_"))
async def process_help_lang(callback: CallbackQuery):
    """Обработчик переключения языка в инструкции."""
    lang = callback.data.split("_")[-1]
    
    # Выбираем текст в зависимости от языка
    if lang == "ru":
        text = HELP_TEXT
    elif lang == "eng":
        text = HELP_TEXT_ENG
    elif lang == "cn":
        text = HELP_TEXT_CN
    else:
        text = HELP_TEXT
    
    # Создаем клавиатуру с кнопками выбора языка
    builder = InlineKeyboardBuilder()
    builder.button(text="🇷🇺 Русский", callback_data="help_lang_ru")
    builder.button(text="🇬🇧 English", callback_data="help_lang_eng")
    builder.button(text="🇨🇳 中文", callback_data="help_lang_cn")
    builder.adjust(3)
    
    try:
        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=builder.as_markup())
    except Exception as e:
        logger.error(f"Ошибка при обновлении текста инструкции: {e}")
        await callback.answer("❌ Error updating message")
        return
    
    await callback.answer()


@router.message(Command("support"))
async def cmd_support(message: Message, state: FSMContext):
    """Обработчик команды /support - отправка сообщения в поддержку."""
    logger.info(f"Команда /support от пользователя {message.from_user.id}")
    await message.answer(
        """💬 <b>Support</b>

Please describe your question or issue. You can send text or a photo with a caption.

Your message will be forwarded to the administrator."""
    )
    await state.set_state(SupportStates.waiting_support_message)


@router.message(SupportStates.waiting_support_message)
async def process_support_message(message: Message, state: FSMContext):
    """Обработчик сообщения поддержки - пересылает админу."""
    # Проверяем, что админ указан
    if not settings.admin_telegram_id or settings.admin_telegram_id == 0:
        await message.answer(
            """❌ Support is not available. Administrator is not configured."""
        )
        await state.clear()
        return
    
    try:
        # Формируем информацию о пользователе
        user_info = f"<b>Support message from:</b>\n"
        user_info += f"• User ID: <code>{message.from_user.id}</code>\n"
        if message.from_user.username:
            user_info += f"• Username: @{message.from_user.username}\n"
        
        # Если есть фото
        if message.photo:
            # Отправляем фото с подписью админу
            caption = f"{user_info}\n{message.caption or ''}" if message.caption else user_info
            await bot.send_photo(
                chat_id=settings.admin_telegram_id,
                photo=message.photo[-1].file_id,  # Берем фото наибольшего размера
                caption=caption,
                parse_mode=ParseMode.HTML
            )
        else:
            # Отправляем текстовое сообщение админу
            full_message = f"{user_info}\n\n<b>Message:</b>\n{message.text}"
            await bot.send_message(
                chat_id=settings.admin_telegram_id,
                text=full_message,
                parse_mode=ParseMode.HTML
            )
        
        # Подтверждаем пользователю
        await message.answer(
            """✅ Your message has been sent to support. We will get back to you soon!"""
        )
        
        logger.info(f"Support message from user {message.from_user.id} forwarded to admin")
        
    except Exception as e:
        logger.error(f"Ошибка при отправке сообщения поддержки: {e}")
        await message.answer(
            """❌ Failed to send your message. Please try again later."""
        )
    finally:
        await state.clear()


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
Use the /orders command to manage your orders.
Use the /help command to view instructions.
Use the /support command to contact administrator."""
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

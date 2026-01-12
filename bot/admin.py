"""
Админские команды для телеграм бота.
"""

import logging
from pathlib import Path

from aiogram import Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import BufferedInputFile, Message
from config import settings
from database import (
    delete_user,
    export_all_tables_to_zip,
    get_database_statistics,
    get_user,
)
from invites import get_invites_statistics, get_unused_invites

logger = logging.getLogger(__name__)

# Создаем роутер для админских команд
admin_router = Router()


# ============================================================================
# States for admin commands
# ============================================================================


class DeleteUserStates(StatesGroup):
    """States for delete user command."""

    waiting_telegram_id = State()


# ============================================================================
# Admin handlers
# ============================================================================


@admin_router.message(Command("get_db"))
async def cmd_get_db(message: Message):
    """Обработчик команды /get_db - экспорт всех таблиц базы данных в ZIP архив и логов (только для администратора)."""
    logger.info(f"Команда /get_db от пользователя {message.from_user.id}")
    # Проверяем права администратора
    if message.from_user.id != settings.admin_telegram_id:
        return

    try:
        # Экспортируем все таблицы в ZIP архив
        zip_content = await export_all_tables_to_zip()

        # Создаем файл для отправки
        zip_file = BufferedInputFile(zip_content, filename="database_export.zip")

        await message.answer_document(
            document=zip_file, caption="📊 Database export (all tables)"
        )
        logger.info(f"Администратор {message.from_user.id} экспортировал базу данных")

        # Отправляем файлы логов отдельными файлами
        logs_dir = Path(__file__).parent.parent / "logs"
        log_files = [
            ("bot.log", "📝 Bot logs"),
            ("sync_orders.log", "🔄 Sync orders logs"),
        ]

        for log_filename, caption in log_files:
            log_path = logs_dir / log_filename
            if log_path.exists():
                try:
                    log_content = log_path.read_bytes()
                    log_file = BufferedInputFile(log_content, filename=log_filename)
                    await message.answer_document(document=log_file, caption=caption)
                    logger.info(f"Отправлен файл лога: {log_filename}")
                except Exception as e:
                    logger.error(f"Ошибка при отправке файла лога {log_filename}: {e}")
                    await message.answer(
                        f"❌ Error sending log file {log_filename}: {e}"
                    )
            else:
                logger.warning(f"Файл лога не найден: {log_path}")
                await message.answer(f"⚠️ Log file not found: {log_filename}")

    except Exception as e:
        logger.error(f"Ошибка экспорта базы данных: {e}")
        await message.answer(f"""❌ Error exporting database: {e}""")


@admin_router.message(Command("get_invites"))
async def cmd_get_invites(message: Message):
    """Обработчик команды /get_invites - получение неиспользованных инвайтов (только для администратора).

    Использование: /get_invites [количество]
    Если количество не указано, генерируется 10 инвайтов по умолчанию.
    """
    logger.info(f"Команда /get_invites от пользователя {message.from_user.id}")
    # Проверяем права администратора
    if message.from_user.id != settings.admin_telegram_id:
        return

    try:
        # Извлекаем количество инвайтов из аргументов команды
        command_parts = message.text.split()
        invites_count = 10  # Значение по умолчанию

        if len(command_parts) > 1:
            try:
                invites_count = int(command_parts[1])
                if invites_count <= 0:
                    await message.answer(
                        "❌ Invalid number. Please enter a positive number.\n"
                        "Example: /get_invites 5"
                    )
                    return
                if invites_count > 100:
                    await message.answer(
                        "❌ Too many invites. Maximum is 100.\n"
                        "Example: /get_invites 100"
                    )
                    return
            except ValueError:
                await message.answer(
                    "❌ Invalid format. Please enter a number.\nExample: /get_invites 5"
                )
                return

        # Получаем статистику
        stats = await get_invites_statistics()

        # Получаем указанное количество неиспользованных инвайтов (создаст новые если нужно)
        invites = await get_unused_invites(invites_count)

        # Формируем сообщение со статистикой и инвайтами
        stats_text = f"""📊 <b>Invites Statistics:</b>
• Total: {stats["total"]}
• Used: {stats["used"]}
• Unused: {stats["unused"]}

📋 <b>{invites_count} Unused Invites (ID - Code):</b>
"""

        invites_list = []
        for invite in invites:
            invites_list.append(f"{invite['id']} - <code>{invite['invite']}</code>")

        invites_text = "\n".join(invites_list)

        full_message = stats_text + invites_text

        await message.answer(full_message)
        logger.info(
            f"Администратор {message.from_user.id} получил список из {invites_count} инвайтов"
        )
    except Exception as e:
        logger.error(f"Ошибка получения инвайтов: {e}")
        await message.answer(f"""❌ Error getting invites: {e}""")


@admin_router.message(Command("delete_user"))
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


@admin_router.message(DeleteUserStates.waiting_telegram_id)
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
            username = user.get("username", "N/A")
            await message.answer(
                f"""✅ User deleted successfully!

📋 <b>Deleted User Info:</b>
• Telegram ID: <code>{telegram_id}</code>
• Username: @{username if username != "N/A" else "N/A"}

The user and all their orders have been removed from the database.
They can now register again using /start."""
            )
            logger.info(
                f"Администратор {message.from_user.id} удалил пользователя {telegram_id}"
            )
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


@admin_router.message(Command("stats"))
async def cmd_stats(message: Message):
    """Обработчик команды /stats - статистика по базе данных (только для администратора)."""
    logger.info(f"Команда /stats от пользователя {message.from_user.id}")
    # Проверяем права администратора
    if message.from_user.id != settings.admin_telegram_id:
        return

    try:
        # Получаем статистику по базе данных
        db_stats = await get_database_statistics()
        invites_stats = await get_invites_statistics()

        # Формируем сообщение
        stats_text = """📊 <b>Database Statistics</b>

👥 <b>Users:</b>
• Total users: {total_users}
• Users with orders: {users_with_orders}
• Users with active orders: {users_with_active_orders}

📋 <b>Orders:</b>
• Total orders: {total_orders}
• Unique markets: {unique_markets}

📈 <b>Orders by Status:</b>
{orders_by_status}

💰 <b>Total Amount:</b> {total_amount:.2f} USDT
📊 <b>Average Order Amount:</b> {average_amount:.2f} USDT

🎫 <b>Invites:</b>
• Total: {invites_total}
• Used: {invites_used}
• Unused: {invites_unused}"""

        # Формируем строку со статусами ордеров (только те, которые реально есть в БД)
        status_emojis = {
            "FILLED": "✅",
            "OPEN": "🟢",
            "CANCELLED": "❌",
        }

        orders_by_status_lines = []
        # Выводим только те статусы, которые реально используются в коде и БД
        for status in ["FILLED", "OPEN", "CANCELLED"]:
            status_data = db_stats["orders"]["by_status"][status]
            emoji = status_emojis.get(status, "•")
            orders_by_status_lines.append(
                f"{emoji} {status}: {status_data['count']} ({status_data['amount']:.2f} USDT)"
            )

        orders_by_status_text = "\n".join(orders_by_status_lines)

        # Форматируем финальное сообщение
        full_message = stats_text.format(
            total_users=db_stats["users"]["total"],
            users_with_orders=db_stats["users"]["with_orders"],
            users_with_active_orders=db_stats["users"]["with_active_orders"],
            total_orders=db_stats["orders"]["total"],
            unique_markets=db_stats["orders"]["unique_markets"],
            orders_by_status=orders_by_status_text,
            total_amount=db_stats["orders"]["total_amount"],
            average_amount=db_stats["orders"]["average_amount"],
            invites_total=invites_stats["total"],
            invites_used=invites_stats["used"],
            invites_unused=invites_stats["unused"],
        )

        await message.answer(full_message)
        logger.info(
            f"Администратор {message.from_user.id} получил статистику базы данных"
        )
    except Exception as e:
        logger.error(f"Ошибка получения статистики: {e}")
        await message.answer(f"""❌ Error getting statistics: {e}""")

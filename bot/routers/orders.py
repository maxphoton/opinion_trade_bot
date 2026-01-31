"""
Router for orders management commands.
Handles viewing and managing user orders.
"""

import logging

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram_dialog import DialogManager, StartMode
from service.database import get_user, get_user_accounts

from routers.orders_dialog import OrdersSG

logger = logging.getLogger(__name__)

# ============================================================================
# Router and handlers
# ============================================================================

orders_manage_router = Router()


@orders_manage_router.message(Command("orders"))
async def cmd_orders(message: Message, dialog_manager: DialogManager):
    """Обработчик команды /orders - просмотр ордеров пользователя."""
    logger.info(f"Команда /orders от пользователя {message.from_user.id}")
    telegram_id = message.from_user.id

    # Проверяем, зарегистрирован ли пользователь
    user = await get_user(telegram_id)
    if not user:
        await message.answer(
            """❌ You are not registered. Use /start to register first."""
        )
        return

    # Получаем все аккаунты пользователя
    accounts = await get_user_accounts(telegram_id)
    if not accounts:
        await message.answer(
            """❌ You don't have any Opinion profiles yet.

Use /add_profile to add your first Opinion profile."""
        )
        return

    # Если аккаунт один, используем его автоматически
    if len(accounts) == 1:
        account_id = accounts[0]["account_id"]
        # Запускаем диалог с передачей account_id
        await dialog_manager.start(
            OrdersSG.orders_list,
            data={"account_id": account_id},
            mode=StartMode.RESET_STACK,
        )
        return

    # Если аккаунтов несколько, показываем выбор
    builder = InlineKeyboardBuilder()
    for account in accounts:
        wallet = account["wallet_address"]
        account_id = account["account_id"]
        builder.button(
            text=f"Account {account_id} ({wallet[:8]}...)",
            callback_data=f"orders_account_{account_id}",
        )
    builder.button(text="✖️ Cancel", callback_data="cancel_orders")
    builder.adjust(1)

    await message.answer(
        """📋 View Orders

Select an account to view orders:""",
        reply_markup=builder.as_markup(),
    )


@orders_manage_router.callback_query(F.data.startswith("orders_account_"))
async def process_orders_account_selection(
    callback: CallbackQuery, dialog_manager: DialogManager
):
    """Handles account selection for orders dialog."""
    account_id_str = callback.data.replace("orders_account_", "")
    try:
        account_id = int(account_id_str)
    except ValueError:
        await callback.answer("Invalid account ID", show_alert=True)
        return

    # Запускаем диалог с передачей account_id
    await callback.message.delete()
    await dialog_manager.start(
        OrdersSG.orders_list,
        data={"account_id": account_id},
        mode=StartMode.RESET_STACK,
    )
    await callback.answer()


@orders_manage_router.callback_query(F.data == "cancel_orders")
async def cancel_orders_selection(callback: CallbackQuery):
    """Handles canceling orders account selection."""
    await callback.message.edit_text("❌ Order viewing cancelled.")
    await callback.answer()

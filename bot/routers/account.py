"""
Router for account management commands.
Handles adding, listing, and removing Opinion accounts.
"""

import logging
import re
from pathlib import Path

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, FSInputFile, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from opinion.client_factory import create_client
from opinion.opinion_api_wrapper import get_usdt_balance
from service.database import (
    check_api_key_exists,
    check_private_key_exists,
    check_proxy_exists,
    check_wallet_address_exists,
    delete_opinion_account,
    get_user,
    get_user_accounts,
    save_opinion_account,
)
from service.proxy_checker import check_proxy_health, validate_proxy_format

logger = logging.getLogger(__name__)

# ============================================================================
# States for account management
# ============================================================================


class AddAccountStates(StatesGroup):
    """States for adding a new account."""

    waiting_wallet = State()
    waiting_private_key = State()
    waiting_api_key = State()
    waiting_proxy = State()


# ============================================================================
# Router and handlers
# ============================================================================

account_router = Router()


@account_router.message(Command("add_account"))
async def cmd_add_account(message: Message, state: FSMContext):
    """Handler for /add_account command - start of account addition process."""
    logger.info(f"Команда /add_account от пользователя {message.from_user.id}")
    telegram_id = message.from_user.id

    user = await get_user(telegram_id)
    if not user:
        await message.answer(
            """❌ You are not registered. Use the /start command to register first."""
        )
        return

    # Create keyboard with "Cancel" button
    builder = InlineKeyboardBuilder()
    builder.button(text="✖️ Cancel", callback_data="cancel_add_account")

    # Путь к изображению (файл находится в корне проекта)
    image_path = Path(__file__).parent.parent.parent / "files" / "spot_addr.png"
    photo = FSInputFile(image_path)

    await message.answer_photo(
        photo=photo,
        caption="""🔐 Add Opinion Account

Please enter your wallet address:""",
        reply_markup=builder.as_markup(),
    )
    await state.set_state(AddAccountStates.waiting_wallet)


@account_router.message(AddAccountStates.waiting_wallet)
async def process_wallet(message: Message, state: FSMContext):
    """Handles wallet address input."""
    wallet_address = message.text.strip()

    # Проверяем формат (должен начинаться с 0x и быть длиной 42 символа)
    if not re.match(r"^0x[a-fA-F0-9]{40}$", wallet_address):
        await message.answer(
            """❌ Invalid wallet address format. 
            
Please enter a valid Ethereum wallet address (0x...):"""
        )
        return

    telegram_id = message.from_user.id

    # Проверяем, не используется ли уже этот кошелек
    if await check_wallet_address_exists(wallet_address, telegram_id):
        await message.answer(
            """❌ This wallet address is already registered.
            
Please enter a different wallet address:"""
        )
        return

    # Сохраняем wallet_address в state
    await state.update_data(wallet_address=wallet_address)

    # Удаляем сообщение с кошельком из диалога
    try:
        await message.delete()
    except Exception:
        pass

    # Create keyboard with "Cancel" button
    builder = InlineKeyboardBuilder()
    builder.button(text="✖️ Cancel", callback_data="cancel_add_account")

    await message.answer(
        """✅ Wallet address saved.

Please enter your private key:""",
        reply_markup=builder.as_markup(),
    )
    await state.set_state(AddAccountStates.waiting_private_key)


@account_router.message(AddAccountStates.waiting_private_key)
async def process_private_key(message: Message, state: FSMContext):
    """Handles private key input."""
    private_key = message.text.strip()

    telegram_id = message.from_user.id

    # Проверяем, не используется ли уже этот приватный ключ
    if await check_private_key_exists(private_key, telegram_id):
        await message.answer(
            """❌ This private key is already registered.
            
Please enter a different private key:"""
        )
        return

    # Сохраняем private_key в state
    await state.update_data(private_key=private_key)

    # Удаляем сообщение с приватным ключом из диалога
    try:
        await message.delete()
    except Exception:
        pass

    # Create keyboard with "Cancel" button
    builder = InlineKeyboardBuilder()
    builder.button(text="✖️ Cancel", callback_data="cancel_add_account")

    await message.answer(
        """✅ Private key saved.

Please enter your API key:""",
        reply_markup=builder.as_markup(),
    )
    await state.set_state(AddAccountStates.waiting_api_key)


@account_router.message(AddAccountStates.waiting_api_key)
async def process_api_key(message: Message, state: FSMContext):
    """Handles API key input."""
    api_key = message.text.strip()

    # Проверяем формат (должен быть непустой строкой)
    if not api_key or len(api_key) < 10:
        await message.answer(
            """❌ Invalid API key format. 
            
Please enter a valid API key:"""
        )
        return

    telegram_id = message.from_user.id

    # Проверяем, не используется ли уже этот API ключ
    if await check_api_key_exists(api_key, telegram_id):
        await message.answer(
            """❌ This API key is already registered.
            
Please enter a different API key:"""
        )
        return

    # Сохраняем api_key в state
    await state.update_data(api_key=api_key)

    # Удаляем сообщение с API ключом из диалога
    try:
        await message.delete()
    except Exception:
        pass

    # Create keyboard with "Cancel" button
    builder = InlineKeyboardBuilder()
    builder.button(text="✖️ Cancel", callback_data="cancel_add_account")

    await message.answer(
        """✅ API key saved.

Please enter proxy in format ip:port:login:password:

Example: 91.216.126.156:8000:h28djN:3sndjj8u""",
        reply_markup=builder.as_markup(),
    )
    await state.set_state(AddAccountStates.waiting_proxy)


@account_router.message(AddAccountStates.waiting_proxy)
async def process_proxy(message: Message, state: FSMContext):
    """Handles proxy input."""
    proxy_str = message.text.strip()

    # Валидируем формат прокси
    is_valid, error_message = validate_proxy_format(proxy_str)
    if not is_valid:
        await message.answer(
            f"""❌ {error_message}

Please enter proxy in format ip:port:login:password:

Example: 91.216.126.156:8000:h28djN:3sndjj8u"""
        )
        return

    # Проверяем, не используется ли уже этот прокси
    if await check_proxy_exists(proxy_str):
        await message.answer(
            """❌ This proxy is already registered to another account.

Please enter a different proxy."""
        )
        return

    # Удаляем сообщение с прокси из диалога
    try:
        await message.delete()
    except Exception:
        pass

    # Проверяем работоспособность прокси
    await message.answer("⏳ Checking proxy...")

    proxy_status = await check_proxy_health(proxy_str)
    if proxy_status != "working":
        await message.answer(
            """❌ Proxy is not working. Please check your proxy settings and try again.

Please enter a working proxy in format ip:port:login:password:"""
        )
        return

    # Сохраняем прокси и создаем аккаунт
    data = await state.get_data()
    await save_and_notify_account(message, state, data, proxy_str, proxy_status)


@account_router.callback_query(F.data == "cancel_add_account")
async def cancel_add_account(callback: CallbackQuery, state: FSMContext):
    """Handles canceling account addition."""
    await state.clear()
    await callback.message.answer("❌ Account addition cancelled.")
    await callback.answer()


async def save_and_notify_account(
    message: Message,
    state: FSMContext,
    data: dict,
    proxy_str: str,
    proxy_status: str,
):
    """Saves account and sends notification."""
    telegram_id = message.from_user.id
    wallet_address = data.get("wallet_address")
    private_key = data.get("private_key")
    api_key = data.get("api_key")

    try:
        # Проверяем, что данные валидны, создавая клиент
        test_account_data = {
            "wallet_address": wallet_address,
            "private_key": private_key,
            "api_key": api_key,
            "proxy_str": proxy_str,
        }
        client = create_client(test_account_data)

        # Пробуем получить баланс для проверки валидности аккаунта
        # Если клиент создался успешно и баланс получен (даже если 0.0), значит аккаунт валиден
        balance = await get_usdt_balance(client)
        # get_usdt_balance всегда возвращает float (0.0 в случае ошибки)
        # Если дошли сюда без исключения, значит аккаунт валиден

        # Сохраняем аккаунт
        account_id = await save_opinion_account(
            telegram_id=telegram_id,
            wallet_address=wallet_address,
            private_key=private_key,
            api_key=api_key,
            proxy_str=proxy_str,
            proxy_status=proxy_status,
        )

        await state.clear()

        # Формируем информацию о прокси
        proxy_parts = proxy_str.split(":")
        proxy_host_port = f"{proxy_parts[0]}:{proxy_parts[1]}"
        # Определяем эмодзи и текст статуса
        status_emoji = {"working": "✅", "failed": "❌", "unknown": "❓"}.get(
            proxy_status, "❓"
        )
        proxy_info = f"\n\n🔐 Proxy: {proxy_host_port} {status_emoji} ({proxy_status})"

        await message.answer(
            f"""✅ <b>Account added successfully!</b>

🆔 Account ID: <code>{account_id}</code>
💼 Wallet: <code>{wallet_address[:10]}...{wallet_address[-6:]}</code>
💰 Balance: <b>{balance:.6f} USDT</b>{proxy_info}

Use /list_accounts to view all your accounts.
Use /make_market to place an order.""",
            parse_mode="HTML",
        )

    except Exception as e:
        logger.error(f"Ошибка при добавлении аккаунта: {e}")
        await message.answer(
            f"""❌ Failed to add account: {str(e)}

Please check your credentials and try again."""
        )
        await state.clear()


@account_router.message(Command("list_accounts"))
async def cmd_list_accounts(message: Message):
    """Handler for /list_accounts command - shows all user accounts."""
    logger.info(f"Команда /list_accounts от пользователя {message.from_user.id}")
    telegram_id = message.from_user.id

    user = await get_user(telegram_id)
    if not user:
        await message.answer(
            """❌ You are not registered. Use the /start command to register first."""
        )
        return

    accounts = await get_user_accounts(telegram_id)
    if not accounts:
        await message.answer(
            """📋 You don't have any accounts yet.

Use /add_account to add your first Opinion account."""
        )
        return

    # Формируем список аккаунтов
    accounts_list = []
    for i, account in enumerate(accounts, 1):
        wallet = account["wallet_address"]
        account_id = account["account_id"]
        proxy_status = account.get("proxy_status", "unknown")

        proxy_info = ""
        if account.get("proxy_str"):
            proxy_parts = account["proxy_str"].split(":")
            proxy_info = (
                f"\n\n🔐 Proxy: {proxy_parts[0]}:{proxy_parts[1]} ({proxy_status})"
            )
        else:
            proxy_info = "\n\n🔐 Proxy: Not configured"

        accounts_list.append(
            f"{i}. Account ID: {account_id}\n"
            f"   Wallet: {wallet[:10]}...{wallet[-6:]}{proxy_info}"
        )

    message_text = f"""📋 Your Opinion Accounts

{chr(10).join(accounts_list)}

Total accounts: {len(accounts)}"""
    await message.answer(message_text)


@account_router.message(Command("remove_account"))
async def cmd_remove_account(message: Message):
    """Handler for /remove_account command - shows account selection for removal."""
    logger.info(f"Команда /remove_account от пользователя {message.from_user.id}")
    telegram_id = message.from_user.id

    user = await get_user(telegram_id)
    if not user:
        await message.answer(
            """❌ You are not registered. Use the /start command to register first."""
        )
        return

    accounts = await get_user_accounts(telegram_id)
    if not accounts:
        await message.answer(
            """📋 You don't have any accounts to remove.

Use /add_account to add an Opinion account."""
        )
        return

    # Создаем клавиатуру с выбором аккаунта
    builder = InlineKeyboardBuilder()
    for account in accounts:
        wallet = account["wallet_address"]
        account_id = account["account_id"]
        builder.button(
            text=f"Account {account_id} ({wallet[:8]}...)",
            callback_data=f"remove_account_{account_id}",
        )
    builder.button(text="✖️ Cancel", callback_data="cancel_remove_account")
    builder.adjust(1)

    await message.answer(
        """🗑️ Remove Account

Select an account to remove:
⚠️ Note: Account can only be removed if it has no active orders.""",
        reply_markup=builder.as_markup(),
    )


@account_router.callback_query(F.data.startswith("remove_account_"))
async def process_remove_account(callback: CallbackQuery):
    """Handles account removal."""
    account_id_str = callback.data.replace("remove_account_", "")
    try:
        account_id = int(account_id_str)
    except ValueError:
        await callback.answer("Invalid account ID", show_alert=True)
        return

    success = await delete_opinion_account(account_id)
    if success:
        await callback.message.edit_text(
            f"✅ Account {account_id} has been removed successfully."
        )
    else:
        await callback.message.edit_text(
            f"""❌ Failed to remove account {account_id}.

Possible reasons:
• Account has active orders
• Account not found"""
        )
    await callback.answer()

"""
Router for user registration flow (/start command).
Handles the complete registration process from wallet address to API key.
"""

import hashlib
import logging
import re
from datetime import datetime
from pathlib import Path

from aiogram import Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import FSInputFile, Message
from client_factory import create_client
from database import (
    check_api_key_exists,
    check_private_key_exists,
    check_wallet_address_exists,
    get_user,
    save_user,
)
from invites import is_invite_valid, use_invite
from opinion_api_wrapper import get_my_orders

logger = logging.getLogger(__name__)

# ============================================================================
# States for user registration
# ============================================================================


class RegistrationStates(StatesGroup):
    """States for the registration process."""

    waiting_invite = State()
    waiting_wallet = State()
    waiting_private_key = State()
    waiting_api_key = State()


# ============================================================================
# Router and handlers
# ============================================================================

start_router = Router()


@start_router.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext):
    """Handler for /start command - start of registration process."""
    logger.info(f"Команда /start от пользователя {message.from_user.id}")
    user = await get_user(message.from_user.id)

    if user:
        await message.answer(
            """✅ You are already registered!

Use the /make_market command to place an order.
Use the /orders command to manage your orders.
Use the /check_account command to view account statistics.
Use the /help command to view instructions.
Use the /support command to contact administrator."""
        )
        return

    # Запрашиваем инвайт
    await message.answer(
        """🔐 Bot Registration

To register, you need an invite code.

Please enter your invite code:"""
    )
    await state.set_state(RegistrationStates.waiting_invite)


@start_router.message(RegistrationStates.waiting_invite)
async def process_invite(message: Message, state: FSMContext):
    """Handles invite code input."""
    invite_code = message.text.strip()

    # Проверяем формат (латиница и цифры)
    if not re.match(r"^[A-Za-z0-9]{10}$", invite_code):
        await message.answer(
            """❌ Invalid invite code format. 
            
Please try again:"""
        )
        return

    # Проверяем валидность инвайта (но не используем его пока)
    if not await is_invite_valid(invite_code):
        await message.answer(
            """❌ Invalid or already used invite code.

Please enter a valid invite code:"""
        )
        return

    # Сохраняем инвайт в state (будем использовать в конце регистрации)
    await state.update_data(invite_code=invite_code)

    # Удаляем сообщение пользователя с инвайт-кодом
    try:
        await message.delete()
    except Exception:
        pass

    # Переходим к следующему шагу
    # Send image with caption in one message
    photo_path = Path(__file__).parent.parent / "files" / "spot_addr.png"

    photo = FSInputFile(str(photo_path))
    await message.answer_photo(
        photo,
        caption="""🔐 Bot Registration
    
⚠️ Attention: All data (wallet address, private key, API key) is encrypted using a private encryption key and stored in an encrypted form.
The data is never used in its raw form and is not shared with third parties.

Please enter your Balance spot address found <a href="https://app.opinion.trade?code=BJea79">in your profile</a>:

⚠️ Important: You must specify the spot address for which you received the API key.""",
    )
    await state.set_state(RegistrationStates.waiting_wallet)


@start_router.message(RegistrationStates.waiting_wallet)
async def process_wallet(message: Message, state: FSMContext):
    """Handles wallet address input."""
    wallet_address = message.text.strip()

    if not wallet_address or len(wallet_address) < 10:
        await message.answer("""❌ Invalid wallet address format. Please try again:""")
        return

    # Проверяем уникальность wallet_address
    if await check_wallet_address_exists(wallet_address):
        await message.answer(
            """❌ This wallet address is already registered.
            
Please enter a different wallet address:"""
        )
        return

    await state.update_data(wallet_address=wallet_address)

    # Удаляем сообщение пользователя с адресом кошелька
    try:
        await message.delete()
    except Exception:
        pass

    await message.answer("""Please enter your private key:

⚠️ Important: You must specify the private key of the wallet you registered with (the same wallet address you entered above).""")
    await state.set_state(RegistrationStates.waiting_private_key)


@start_router.message(RegistrationStates.waiting_private_key)
async def process_private_key(message: Message, state: FSMContext):
    """Handles private key input."""
    private_key = message.text.strip()

    if not private_key or len(private_key) < 20:
        await message.answer("""❌ Invalid private key format. Please try again:""")
        return

    # Проверяем уникальность private_key
    if await check_private_key_exists(private_key):
        await message.answer(
            """❌ This private key is already registered.
            
Please enter a different private key:"""
        )
        return

    await state.update_data(private_key=private_key)

    # Удаляем сообщение пользователя с приватным ключом
    try:
        await message.delete()
    except Exception:
        pass

    await message.answer("""Please enter your Opinion Labs API key, which you can obtain by completing <a href="https://docs.google.com/forms/d/1h7gp8UffZeXzYQ-lv4jcou9PoRNOqMAQhyW4IwZDnII/viewform?edit_requested=true">the form</a>:

⚠️ Important: You must enter the API key that was obtained for the wallet address from step 1.""")
    await state.set_state(RegistrationStates.waiting_api_key)


@start_router.message(RegistrationStates.waiting_api_key)
async def process_api_key(message: Message, state: FSMContext):
    """Handles API key input and completes registration."""
    api_key = message.text.strip()

    if not api_key:
        await message.answer("""❌ Invalid API key format. Please try again:""")
        return

    # Проверяем уникальность api_key
    if await check_api_key_exists(api_key):
        await message.answer(
            """❌ This API key is already registered.
            
Please enter a different API key:"""
        )
        return

    data = await state.get_data()
    telegram_id = message.from_user.id

    # Подготавливаем данные для проверки подключения
    wallet_address = data["wallet_address"].strip()
    private_key = data["private_key"].strip()
    api_key_clean = api_key.strip()

    # Проверяем подключение к API перед сохранением в БД
    await message.answer("""🔍 Verifying connection to API...""")

    try:
        # Создаем временный словарь с данными пользователя для проверки
        test_user_data = {
            "wallet_address": wallet_address,
            "private_key": private_key,
            "api_key": api_key_clean,
        }

        # Пытаемся создать клиент
        test_client = create_client(test_user_data)

        # Пытаемся получить ордера пользователя для проверки подключения
        orders = await get_my_orders(
            test_client, market_id=0, status="", limit=1, page=1
        )

        # Если дошли сюда без исключений, значит подключение успешно
        logger.info(f"Успешная проверка подключения для пользователя {telegram_id}")

    except Exception as e:
        # Генерируем код ошибки для сопоставления с логами
        error_str = str(e)
        error_hash = hashlib.md5(error_str.encode()).hexdigest()[:8].upper()
        error_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        await message.answer(
            f"""❌ Registration failed: Could not connect to API.

Error code: <code>{error_hash}</code>
Time: {error_time}

Please check the correctness of the entered data and try again with /start command.

If the problem persists, contact administrator via /support and provide the error code above."""
        )
        await state.clear()
        logger.error(
            f"Ошибка проверки подключения для пользователя {telegram_id} [CODE: {error_hash}] [TIME: {error_time}]: {e}"
        )
        return

    # Если проверка прошла успешно, используем инвайт и сохраняем пользователя в БД
    invite_code = data.get("invite_code")
    if invite_code:
        # Используем инвайт (атомарно, с проверкой валидности внутри)
        if not await use_invite(invite_code, telegram_id):
            await state.clear()
            await message.answer(
                """❌ Registration failed: The invite code could not be used.

Please start registration again with /start using a valid invite code."""
            )
            return

    # Сохраняем пользователя в БД
    await save_user(
        telegram_id=telegram_id,
        username=message.from_user.username.strip()
        if message.from_user.username
        else None,
        wallet_address=wallet_address,
        private_key=private_key,
        api_key=api_key_clean,
    )

    # Удаляем сообщение пользователя с API ключом
    try:
        await message.delete()
    except Exception:
        pass

    await state.clear()
    await message.answer(
        """✅ Registration Completed!

Your data has been encrypted and verified.

Use the /make_market command to start a new farm.
Use the /orders command to manage your orders.
Use the /check_account command to view account statistics.
Use the /help command to view instructions.
Use the /support command to contact administrator."""
    )

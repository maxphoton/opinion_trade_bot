"""
Router for user registration flow (/start command).
Handles the registration process - only invite code.
"""

import logging
import re

from aiogram import Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message
from service.database import get_user, save_user

from routers.invites import is_invite_valid, use_invite

logger = logging.getLogger(__name__)

# ============================================================================
# States for user registration
# ============================================================================


class RegistrationStates(StatesGroup):
    """States for the registration process."""

    waiting_invite = State()


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

Use the /floating_order to place floating order.
Use the /market to place a market order.
Use the /limit to place a limit order.
Use the /limit_first command for keeps your limit orders always first in the order book.
Use the /orders to manage your orders.
Use the /follow &lt;address&gt; &lt;label&gt; to follow a wallet.
Use the /unfollow &lt;address&gt; to stop monitoring a wallet.
Use the /check_profile to view profile statistics.
Use the /profile_list to view all your profiles.
Use the /help to view instructions.
Use the /support to contact administrator.

🚀 Subscribe for best strategies, updates and VIP access @cmchn_public

📚 Docs: https://bidask-bot.gitbook.io/docs/""",
            disable_web_page_preview=True,
        )
        return

    # Запрашиваем инвайт
    await message.answer(
        """ Welcome!
🚀 Subscribe for best strategies, updates and VIP access @cmchn_public
📚 Docs: https://bidask-bot.gitbook.io/docs/
        
🔐 Step 1: Bot Registration

To register, you need an invite code.

Please enter your invite code:""",
        disable_web_page_preview=True,
    )
    await state.set_state(RegistrationStates.waiting_invite)


@start_router.message(RegistrationStates.waiting_invite)
async def process_invite(message: Message, state: FSMContext):
    """Handles invite code input and completes registration."""
    invite_code = message.text.strip()

    # Проверяем формат (латиница и цифры)
    if not re.match(r"^[A-Za-z0-9]{10}$", invite_code):
        await message.answer(
            """❌ Invalid invite code format. 
            
Please try again:"""
        )
        return

    # Проверяем валидность инвайта
    if not await is_invite_valid(invite_code):
        await message.answer(
            """❌ Invalid or already used invite code.

Please enter a valid invite code:"""
        )
        return

    telegram_id = message.from_user.id

    # Используем инвайт (атомарно, с проверкой валидности внутри)
    if not await use_invite(invite_code, telegram_id):
        await state.clear()
        await message.answer(
            """❌ Registration failed: The invite code could not be used.

Please start registration again with /start using a valid invite code."""
        )
        return

    # Сохраняем пользователя в БД (только telegram_id и username)
    await save_user(
        telegram_id=telegram_id,
        username=message.from_user.username.strip()
        if message.from_user.username
        else None,
    )

    # Удаляем сообщение пользователя с инвайт-кодом
    try:
        await message.delete()
    except Exception:
        pass

    await state.clear()
    await message.answer(
        """✅ Registration Completed!

Now you need to add your Opinion profile.

Step 2: Use the /add_profile to add your first Opinion profile with wallet address, private key, and API key.

After adding an account, you can:
• Use /floating_order to place floating order.
• Use /market to place a market order.
• Use /limit to place a limit order.
• Use /limit_first command for keeps your limit orders always first in the order book.
• Use /orders to manage your orders.
• Use /follow &lt;address&gt; &lt;label&gt; to follow a wallet.
• Use /unfollow &lt;address&gt; to stop monitoring a wallet.
• Use /check_profile to view profile statistics.
• Use /profile_list to view all your profiles.
• Use /help to view instructions.
• Use /support to contact administrator.

📚 Docs: https://bidask-bot.gitbook.io/docs/""",
        disable_web_page_preview=True,
    )

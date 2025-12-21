"""
Router for user registration flow (/start command).
Handles the complete registration process from wallet address to API key.
"""

import logging
from pathlib import Path

from aiogram import Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message, FSInputFile

from database import get_user, save_user

logger = logging.getLogger(__name__)

# ============================================================================
# States for user registration
# ============================================================================

class RegistrationStates(StatesGroup):
    """States for the registration process."""
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
    user = await get_user(message.from_user.id)
    
    if user:
        await message.answer(
            """✅ You are already registered!

Use the /make_market command to place an order."""
        )
        return
    
    # Send image with caption in one message
    photo_path = Path(__file__).parent.parent / "files" / "spot_addr.png"
    
    photo = FSInputFile(str(photo_path))
    await message.answer_photo(
        photo,
        caption="""🔐 Bot Registration
    
⚠️ Attention: All data (wallet address, private key, API key) is encrypted using a private encryption key and stored in an encrypted form.
The data is never used in its raw form and is not shared with third parties.

Please enter your Balance spot address found <a href="https://app.opinion.trade/profile">in your profile</a>:"""
    )
    await state.set_state(RegistrationStates.waiting_wallet)


@start_router.message(RegistrationStates.waiting_wallet)
async def process_wallet(message: Message, state: FSMContext):
    """Handles wallet address input."""
    wallet_address = message.text.strip()
    
    if not wallet_address or len(wallet_address) < 10:
        await message.answer("""❌ Invalid wallet address format. Please try again:""")
        return
    
    await state.update_data(wallet_address=wallet_address)
    await message.answer("Please enter your private key:")
    await state.set_state(RegistrationStates.waiting_private_key)


@start_router.message(RegistrationStates.waiting_private_key)
async def process_private_key(message: Message, state: FSMContext):
    """Handles private key input."""
    private_key = message.text.strip()
    
    if not private_key or len(private_key) < 20:
        await message.answer("""❌ Invalid private key format. Please try again:""")
        return
    
    await state.update_data(private_key=private_key)
    await message.answer("""Please enter your Opinion Labs API key, which you can obtain by completing <a href="https://docs.google.com/forms/d/1h7gp8UffZeXzYQ-lv4jcou9PoRNOqMAQhyW4IwZDnII/viewform?edit_requested=true">the form</a>:""")
    await state.set_state(RegistrationStates.waiting_api_key)


@start_router.message(RegistrationStates.waiting_api_key)
async def process_api_key(message: Message, state: FSMContext):
    """Handles API key input and completes registration."""
    api_key = message.text.strip()
    
    if not api_key:
        await message.answer("""❌ Invalid API key format. Please try again:""")
        return
    
    data = await state.get_data()
    
    # Save user to database
    await save_user(
        telegram_id=message.from_user.id,
        username=message.from_user.username,
        wallet_address=data['wallet_address'],
        private_key=data['private_key'],
        api_key=api_key
    )
    
    await state.clear()
    await message.answer(
        """✅ Registration Completed!

Your data has been encrypted.

Use the /make_market command to start a new farm.""")

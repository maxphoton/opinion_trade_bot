"""
Router for handling unknown messages (fallback handler).
Responds with standard instruction message for messages that don't match any other handlers.
"""

from aiogram import Router
from aiogram.types import Message

plug_router = Router()


@plug_router.message()
async def handle_unknown_message(message: Message):
    """
    Handler for all messages that don't match any other handlers.
    Responds with a standard instruction message.
    """
    await message.answer(
        """Use the /make_market command to start a new farm.
Use the /orders command to manage your orders.
Use the /check_account command to view account statistics.
Use the /help command to view instructions.
Use the /support command to contact administrator."""
    )

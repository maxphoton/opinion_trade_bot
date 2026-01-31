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
        """Use the /floating_order command to start a new farm.
Use the /market command to place a market order.
Use the /limit command to place a limit order.
Use the /limit_first command for a fixed offset limit order.
Use the /orders command to manage your orders.
Use the /check_profile command to view profile statistics.
Use the /profile_list command to view all your accounts.
Use the /help command to view instructions.
Use the /support command to contact administrator."""
    )

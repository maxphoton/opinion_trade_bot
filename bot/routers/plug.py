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
        """Use the /floating_order to place floating order.
Use the /market to place a market order.
Use the /limit to place a limit order.
Use the /limit_first command for keeps your limit orders always first in the order book.
Use the /orders to manage your orders.
Use the /check_profile to view profile statistics.
Use the /profile_list to view all your profiles.
Use the /help to view instructions.
Use the /support to contact administrator.

Docs: https://bidask-bot.gitbook.io/docs/""",
        disable_web_page_preview=True,
    )
